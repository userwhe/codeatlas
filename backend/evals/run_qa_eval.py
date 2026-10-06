"""Answer-quality evaluation for repository Q&A (SC-003, SC-004, SC-006, and the SC-005 sheet).

Run from `backend/` (see `evals/README.md`):

    uv run python -m evals.run_qa_eval --set evals/qa_v1.jsonl --split heldout \\
        [--fork-owner LOGIN] [--retrieval-only] [--limit N]
    uv run python -m evals.run_qa_eval --fixtures --set FILE --split all    # fake mode

Each pinned repository commit is connected and indexed through the normal domain functions and
the job worker, as a dedicated evaluation user and workspace. Then, for every selected question:

- retrieval: `assemble_evidence` runs directly, and Recall@5 is computed against the labeled
  line ranges;
- answers (unless `--retrieval-only`): `submit` queues the question and the worker answers it,
  exactly as for a user, and every displayed citation is checked against the stored file lines.

A Markdown report and a CSV audit sheet are written under `evals/out/`. The metric math is in
small pure functions at the top of this module, tested in `tests/unit/test_eval_metrics.py`.
"""

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.api.errors import ApiError
from codeatlas.auth.crypto import encrypt
from codeatlas.config import Settings, get_settings
from codeatlas.db import new_session, session_scope
from codeatlas.github.gateway import GitHubError, GitHubGateway, GitHubNotFound, get_gateway
from codeatlas.ingestion.chunking import index_version
from codeatlas.jobs.worker import run_once
from codeatlas.logging import configure_logging
from codeatlas.models import (
    AnalysisRun,
    File,
    GitHubCredential,
    Job,
    Membership,
    Repository,
    Snapshot,
    User,
    Workspace,
)
from codeatlas.providers.answer_model import FAKE_MODEL
from codeatlas.qa.prompt import PROMPT_VERSION
from codeatlas.qa.runs import citations, submit
from codeatlas.qa.schema import AnswerOutput
from codeatlas.retrieval.evidence import assemble_evidence, file_lines
from codeatlas.workspace.repositories import connect, reindex

SPLITS = ("tuning", "heldout")
RECALL_K = 5
RECALL_TARGET = 0.80
CITATION_VALIDITY_TARGET = 1.0
ABSTENTION_TARGET = 0.80
_SHA = re.compile(r"[0-9a-f]{40}")
_RECORD_KEYS = {
    "id",
    "repository",
    "commit_sha",
    "question",
    "answerable",
    "split",
    "relevant_evidence",
}


class EvalError(Exception):
    """A problem that stops the run; the message says how to fix it."""


# --- The question set ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LineRange:
    path: str
    start_line: int
    end_line: int

    def __str__(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class Question:
    id: str
    repository: str
    commit_sha: str
    question: str
    answerable: bool
    split: str
    relevant_evidence: tuple[LineRange, ...]


def _line_range(value: object, where: str) -> LineRange:
    if not isinstance(value, dict) or set(value) != {"path", "start_line", "end_line"}:
        raise ValueError(f"{where}: evidence needs exactly path, start_line, and end_line")
    path, start, end = value["path"], value["start_line"], value["end_line"]
    if not isinstance(path, str) or not path or path.startswith("/"):
        raise ValueError(f"{where}: evidence path must be a relative path")
    if type(start) is not int or type(end) is not int or not 1 <= start <= end:
        raise ValueError(f"{where}: evidence lines must be integers with 1 <= start <= end")
    return LineRange(path, start, end)


def parse_question(record: object, where: str) -> Question:
    """Validate one JSON Lines record. `where` names it in error messages."""
    if not isinstance(record, dict):
        raise ValueError(f"{where}: a record must be a JSON object")
    if set(record) != _RECORD_KEYS:
        raise ValueError(f"{where}: a record needs exactly the keys {sorted(_RECORD_KEYS)}")
    qid, repository, sha = record["id"], record["repository"], record["commit_sha"]
    text, answerable, split = record["question"], record["answerable"], record["split"]
    if not isinstance(qid, str) or not qid:
        raise ValueError(f"{where}: id must be a non-empty string")
    where = f"{where} ({qid})"
    if not isinstance(repository, str) or repository.count("/") != 1:
        raise ValueError(f"{where}: repository must be an owner/name full name")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ValueError(f"{where}: commit_sha must be a 40-character lowercase hex SHA")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{where}: question must be a non-empty string")
    if not isinstance(answerable, bool):
        raise ValueError(f"{where}: answerable must be true or false")
    if split not in SPLITS:
        raise ValueError(f"{where}: split must be one of {', '.join(SPLITS)}")
    evidence = record["relevant_evidence"]
    if not isinstance(evidence, list):
        raise ValueError(f"{where}: relevant_evidence must be a list")
    ranges = tuple(_line_range(item, where) for item in evidence)
    if answerable and not ranges:
        raise ValueError(f"{where}: an answerable question needs labeled evidence")
    if not answerable and ranges:
        raise ValueError(f"{where}: an unanswerable question must have no labeled evidence")
    return Question(qid, repository, sha, text, answerable, split, ranges)


def load_questions(path: Path) -> list[Question]:
    """Read and validate a JSON Lines question set; blank lines are ignored."""
    questions: list[Question] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} line {number}: invalid JSON ({exc.msg})") from exc
        questions.append(parse_question(record, f"{path.name} line {number}"))
    seen: set[str] = set()
    for question in questions:
        if question.id in seen:
            raise ValueError(f"{path.name}: duplicate id {question.id}")
        seen.add(question.id)
    return questions


def select_questions(
    questions: Sequence[Question], split: str, limit: int | None
) -> list[Question]:
    """Questions of `split` (`tuning`, `heldout`, or `all`) in file order, at most `limit`."""
    selected = [q for q in questions if split == "all" or q.split == split]
    return selected if limit is None else selected[:limit]


def eval_branch(commit_sha: str) -> str:
    """The branch name, in each fork, that points at the pinned commit."""
    return f"codeatlas-eval-{commit_sha[:12]}"


# --- Metric math --------------------------------------------------------------------------------


def ranges_overlap(a: LineRange, b: LineRange) -> bool:
    """Whether two ranges share at least one line of the same file."""
    return a.path == b.path and a.start_line <= b.end_line and b.start_line <= a.end_line


def recall_at_k(
    labeled: Sequence[LineRange], retrieved: Sequence[LineRange], k: int = RECALL_K
) -> float:
    """Share of labeled ranges overlapped by any of the first `k` retrieved items (SC-004)."""
    if not labeled:
        raise ValueError("recall is defined only for questions with labeled evidence")
    top = retrieved[:k]
    hits = sum(1 for wanted in labeled if any(ranges_overlap(wanted, item) for item in top))
    return hits / len(labeled)


def citation_problem(
    *,
    content: str | None,
    commit_sha: str,
    expected_commit_sha: str,
    start_line: int,
    end_line: int,
    excerpt: str,
) -> str | None:
    """Why a displayed citation does not resolve to the stored file lines, or None (SC-003).

    `content` is the cited file's stored content in the run's snapshot (None if missing). Lines
    are numbered as the indexer numbers them.
    """
    if commit_sha != expected_commit_sha:
        return f"cites commit {commit_sha[:12]}, not the answer's {expected_commit_sha[:12]}"
    if content is None:
        return "the cited file is not in the indexed version"
    lines = file_lines(content)
    if not 1 <= start_line <= end_line <= len(lines):
        return f"lines {start_line}-{end_line} are outside the file's {len(lines)} lines"
    if "\n".join(lines[start_line - 1 : end_line]) != excerpt:
        return "the excerpt differs from the stored file lines"
    return None


@dataclass(frozen=True)
class Rate:
    """A metric with its denominator; for a mean, `numerator` is the sum of the values."""

    numerator: float
    denominator: int

    @property
    def value(self) -> float | None:
        return self.numerator / self.denominator if self.denominator else None


def meets(rate: Rate, target: float) -> bool | None:
    """Whether the rate reaches `target`; None when there is nothing to measure."""
    return None if rate.value is None else rate.value >= target


# --- Per-question results -----------------------------------------------------------------------


@dataclass(frozen=True)
class CitationCheck:
    label: str
    path: str
    start_line: int
    end_line: int
    excerpt: str
    problem: str | None


@dataclass(frozen=True)
class ClaimRecord:
    text: str
    kind: str
    labels: tuple[str, ...]


@dataclass
class QuestionResult:
    question: Question
    retrieved: list[LineRange] = field(default_factory=list)
    recall: float | None = None
    docs_degraded: bool = False
    answer_status: str | None = None
    """The answer job's final status (`succeeded`, `failed`, ...); None if never submitted."""
    quality_state: str | None = None
    summary: str = ""
    gaps: list[str] = field(default_factory=list)
    claims: list[ClaimRecord] = field(default_factory=list)
    citations: list[CitationCheck] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    """Execution failures, kept apart from answer quality."""

    @property
    def answered(self) -> bool:
        """Whether a published answer exists (`answered` or `insufficient_evidence`)."""
        return self.answer_status == "succeeded" and self.quality_state is not None


@dataclass(frozen=True)
class Summary:
    questions: int
    recall: Rate
    """Mean Recall@5 over answerable questions whose retrieval ran."""
    citation_validity: Rate
    """Displayed citations that match the stored lines, over all displayed citations."""
    abstention: Rate
    """`insufficient_evidence` results over unanswerable questions with a published answer."""
    answered_when_answerable: Rate
    """`answered` results over answerable questions with a published answer (false-abstention
    guard: abstaining on everything would otherwise score 100% on SC-006)."""
    execution_failures: int
    """Questions with at least one execution failure."""


def summarize(results: Sequence[QuestionResult]) -> Summary:
    recalls = [r.recall for r in results if r.question.answerable and r.recall is not None]
    checks = [c for r in results if r.answered for c in r.citations]
    unanswerable = [r for r in results if not r.question.answerable and r.answered]
    answerable = [r for r in results if r.question.answerable and r.answered]
    return Summary(
        questions=len(results),
        recall=Rate(sum(recalls), len(recalls)),
        citation_validity=Rate(sum(1 for c in checks if c.problem is None), len(checks)),
        abstention=Rate(
            sum(1 for r in unanswerable if r.quality_state == "insufficient_evidence"),
            len(unanswerable),
        ),
        answered_when_answerable=Rate(
            sum(1 for r in answerable if r.quality_state == "answered"), len(answerable)
        ),
        execution_failures=sum(1 for r in results if r.failures),
    )


# --- Report and audit sheet ---------------------------------------------------------------------

AUDIT_COLUMNS = (
    "question_id",
    "repository",
    "commit_sha",
    "split",
    "answerable",
    "question",
    "quality_state",
    "answer_summary",
    "gaps",
    "claim_number",
    "claim_kind",
    "claim_text",
    "cited_evidence",
    "cited_excerpts",
    "supported",
    "reviewer_notes",
)
MAX_CELL_CHARS = 30_000  # spreadsheet cells hold at most 32,767 characters


def audit_rows(results: Iterable[QuestionResult]) -> list[dict[str, str]]:
    """One row per claim of every published answer, with empty reviewer columns (SC-005)."""
    rows: list[dict[str, str]] = []
    for result in results:
        if not result.answered or not result.claims:
            continue
        by_label = {check.label: check for check in result.citations}
        for number, claim in enumerate(result.claims, start=1):
            cited = [by_label[label] for label in claim.labels if label in by_label]
            excerpts = "\n\n".join(
                f"[{c.label}] {c.path}:{c.start_line}-{c.end_line}\n{c.excerpt}" for c in cited
            )
            if len(excerpts) > MAX_CELL_CHARS:
                excerpts = excerpts[:MAX_CELL_CHARS] + "\n[truncated; open the answer in the app]"
            q = result.question
            rows.append(
                {
                    "question_id": q.id,
                    "repository": q.repository,
                    "commit_sha": q.commit_sha,
                    "split": q.split,
                    "answerable": "yes" if q.answerable else "no",
                    "question": q.question,
                    "quality_state": result.quality_state or "",
                    "answer_summary": result.summary,
                    "gaps": "\n".join(result.gaps),
                    "claim_number": str(number),
                    "claim_kind": claim.kind,
                    "claim_text": claim.text,
                    "cited_evidence": "; ".join(
                        f"{c.label} {c.path}:{c.start_line}-{c.end_line}" for c in cited
                    ),
                    "cited_excerpts": excerpts,
                    "supported": "",
                    "reviewer_notes": "",
                }
            )
    return rows


@dataclass(frozen=True)
class IndexedTarget:
    """One pinned repository commit, indexed and ready to be asked about."""

    repository: str
    github_full_name: str
    commit_sha: str
    repository_id: uuid.UUID
    snapshot_id: uuid.UUID
    coverage: dict[str, Any]


@dataclass(frozen=True)
class RunInfo:
    started_at: datetime
    set_path: str
    set_sha256: str
    split: str
    limit: int | None
    mode: str
    retrieval_only: bool
    answer_model: str
    thinking_level: str
    embedding_model: str
    index_version: str


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _rate_text(rate: Rate, *, percent: bool) -> str:
    if rate.value is None:
        return "n/a"
    if percent:
        return f"{rate.value:.1%} ({rate.numerator:g}/{rate.denominator})"
    return f"{rate.value:.3f} (mean of {rate.denominator})"


def _status(rate: Rate, target: float, applies: bool = True) -> str:
    if not applies:
        return "not applicable"
    verdict = meets(rate, target)
    return "n/a" if verdict is None else ("PASS" if verdict else "FAIL")


def render_report(
    info: RunInfo,
    targets: Sequence[IndexedTarget],
    results: Sequence[QuestionResult],
    summary: Summary,
) -> str:
    answerable = sum(1 for r in results if r.question.answerable)
    lines = [
        "# Answer-quality evaluation",
        "",
        f"- Started: {info.started_at.isoformat(timespec='seconds')}",
        f"- Set: `{info.set_path}` (SHA-256 `{info.set_sha256[:12]}`), split `{info.split}`"
        + (f", limit {info.limit}" if info.limit is not None else ""),
        f"- Questions: {len(results)} ({answerable} answerable, "
        f"{len(results) - answerable} unanswerable)",
        f"- Mode: {info.mode}" + (", retrieval only" if info.retrieval_only else ""),
        f"- Answer model: `{info.answer_model}` (thinking `{info.thinking_level}`), "
        f"prompt `{PROMPT_VERSION}`",
        f"- Embedding model: `{info.embedding_model}`, index version `{info.index_version}`",
        "",
        "## Indexed versions",
        "",
        "| Repository | Indexed from | Commit | Eligible files | Source lines |",
        "| --- | --- | --- | --- | --- |",
    ]
    for target in targets:
        lines.append(
            f"| {target.repository} | {target.github_full_name} | `{target.commit_sha[:12]}` "
            f"| {target.coverage.get('eligible_files', '?')} "
            f"| {target.coverage.get('source_lines', '?')} |"
        )
    heldout = info.split == "heldout"
    lines += [
        "",
        "## Metrics",
        "",
        "Execution failures are excluded from every quality denominator and listed below.",
        "",
        "| Metric | Value | Target | Status |",
        "| --- | --- | --- | --- |",
        f"| Recall@{RECALL_K} of labeled evidence, answerable questions (SC-004) "
        f"| {_rate_text(summary.recall, percent=False)} | >= {RECALL_TARGET:.2f} "
        f"| {_status(summary.recall, RECALL_TARGET)} |",
        f"| Citation validity: displayed citations equal to stored lines (SC-003) "
        f"| {_rate_text(summary.citation_validity, percent=True)} "
        f"| {CITATION_VALIDITY_TARGET:.0%} "
        f"| {_status(summary.citation_validity, CITATION_VALIDITY_TARGET)} |",
        f"| Abstention on unanswerable questions, split `{info.split}` (SC-006 on held-out) "
        f"| {_rate_text(summary.abstention, percent=True)} | >= {ABSTENTION_TARGET:.0%} "
        f"| {_status(summary.abstention, ABSTENTION_TARGET, heldout)} |",
        f"| Answered when answerable (false-abstention guard) "
        f"| {_rate_text(summary.answered_when_answerable, percent=True)} | report only | |",
        f"| Questions with execution failures | {summary.execution_failures} "
        f"of {summary.questions} | 0 | |",
        "",
    ]
    degraded = sum(1 for r in results if r.docs_degraded)
    if degraded:
        lines += [
            f"Documentation search ran in reduced (keyword-only) mode for {degraded} questions.",
            "",
        ]
    lines += [
        "## Per-question results",
        "",
        "| ID | Split | Answerable | Recall@5 | Result | Valid citations | Execution failure |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        valid = sum(1 for c in r.citations if c.problem is None)
        lines.append(
            f"| {r.question.id} | {r.question.split} | {'yes' if r.question.answerable else 'no'} "
            f"| {'' if r.recall is None else f'{r.recall:.2f}'} | {r.quality_state or ''} "
            f"| {f'{valid}/{len(r.citations)}' if r.answered else ''} "
            f"| {_cell('; '.join(r.failures))} |"
        )
    lines += ["", "## Failures", "", *_failure_sections(results)]
    return "\n".join(lines).rstrip("\n") + "\n"


def _section(title: str, items: Sequence[str]) -> list[str]:
    return [f"### {title}", "", *(items or ["None."]), ""]


def _failure_sections(results: Sequence[QuestionResult]) -> list[str]:
    misses: list[str] = []
    for r in results:
        if r.recall is None or r.recall == 1:
            continue
        top = r.retrieved[:RECALL_K]
        missing = [
            str(want)
            for want in r.question.relevant_evidence
            if not any(ranges_overlap(want, got) for got in top)
        ]
        shown = ", ".join(str(item) for item in top) or "nothing"
        misses.append(f"- {r.question.id} ({r.recall:.2f}): missed {', '.join(missing)}; "
                      f"top {RECALL_K}: {shown}")  # fmt: skip
    invalid = [
        f"- {r.question.id} {c.label} {c.path}:{c.start_line}-{c.end_line}: {c.problem}"
        for r in results
        for c in r.citations
        if c.problem
    ]
    not_abstained = [
        f"- {r.question.id}: {_cell(r.summary)}"
        for r in results
        if not r.question.answerable and r.quality_state == "answered"
    ]
    false_abstentions = [
        f"- {r.question.id}: {_cell('; '.join(r.gaps))}"
        for r in results
        if r.question.answerable and r.quality_state == "insufficient_evidence"
    ]
    errored = [f"- {r.question.id}: {_cell('; '.join(r.failures))}" for r in results if r.failures]
    return [
        *_section(f"Retrieval misses (Recall@{RECALL_K} below 1)", misses),
        *_section("Invalid citations", invalid),
        *_section("Unanswerable questions that were answered", not_abstained),
        *_section("Answerable questions with an insufficient-evidence result", false_abstentions),
        *_section("Execution failures", errored),
    ]


# --- Running against the system -----------------------------------------------------------------

EVAL_GITHUB_USER_ID = -1  # synthetic; GitHub account IDs are positive
EVAL_LOGIN = "codeatlas-eval"
FIXTURE_LOGIN = "octocat"
EVAL_DAILY_QUESTION_LIMIT = 1_000_000
TERMINAL_JOB_STATUSES = ("succeeded", "failed", "canceled")
JOB_ATTEMPTS = 3  # the queue's default `max_attempts`
MAX_BACKOFF_SECONDS = 60.0  # the queue's retry backoff cap
POLL_SECONDS = 1.0
GITHUB_API = "https://api.github.com"


class EvalSettings(BaseSettings):
    """Evaluation-only settings, read like the application's from the environment or `.env`."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    eval_github_token: str = ""


@dataclass(frozen=True)
class JobOutcome:
    status: str
    error_code: str | None
    error_message: str | None


def _check_settings(settings: Settings, *, fixtures: bool, retrieval_only: bool) -> None:
    if fixtures:
        if not settings.fake_externals:
            raise EvalError(
                "--fixtures runs against the fake GitHub and fake providers: set "
                "CODEATLAS_ENV=development and CODEATLAS_FAKE_EXTERNALS=1."
            )
    elif settings.fake_externals:
        raise EvalError(
            "CODEATLAS_FAKE_EXTERNALS is on, so real providers are not used. Pass --fixtures for "
            "a smoke test, or set CODEATLAS_FAKE_EXTERNALS=0 for a real run."
        )
    required = {"TOKEN_ENCRYPTION_KEY": settings.token_encryption_key}
    if not fixtures:
        required |= {
            "GITHUB_APP_ID": settings.github_app_id,
            "GITHUB_APP_PRIVATE_KEY_PATH": settings.github_app_private_key_path,
            "VOYAGE_API_KEY": settings.voyage_api_key,
            "EVAL_GITHUB_TOKEN": EvalSettings().eval_github_token,
        }
        if not retrieval_only:
            required["GEMINI_API_KEY"] = settings.gemini_api_key
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise EvalError(f"Set {', '.join(missing)} in the environment or .env (evals/README.md).")


def _user_token(gateway: GitHubGateway, *, fixtures: bool) -> str:
    if fixtures:
        return gateway.exchange_code(f"fake:{FIXTURE_LOGIN}").access_token
    return EvalSettings().eval_github_token


def _ensure_identity(token: str) -> None:
    """Create or refresh the dedicated evaluation user, its workspace, and its stored token.

    The token is stored without an expiry, so it is used as is and never refreshed.
    """
    with session_scope() as db:
        user = db.scalar(select(User).where(User.github_user_id == EVAL_GITHUB_USER_ID))
        if user is None:
            user = User(
                github_user_id=EVAL_GITHUB_USER_ID, github_login=EVAL_LOGIN, name="Evaluation"
            )
            db.add(user)
            db.flush()
        has_workspace = db.scalar(
            select(Membership.workspace_id).where(
                Membership.user_id == user.id, Membership.role == "owner"
            )
        )
        if has_workspace is None:
            workspace = Workspace(name=EVAL_LOGIN)
            db.add(workspace)
            db.flush()
            db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        credential = db.get(GitHubCredential, user.id)
        if credential is None:
            credential = GitHubCredential(user_id=user.id)
            db.add(credential)
        credential.access_token_enc = encrypt(token)
        credential.access_token_expires_at = None
        credential.refresh_token_enc = None
        credential.refresh_token_expires_at = None


def _identity(db: Session) -> tuple[User, Workspace]:
    user = db.scalar(select(User).where(User.github_user_id == EVAL_GITHUB_USER_ID))
    if user is None:
        raise EvalError("The evaluation user is missing.")
    workspace = db.scalar(
        select(Workspace)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(Membership.user_id == user.id, Membership.role == "owner")
    )
    if workspace is None:
        raise EvalError("The evaluation workspace is missing.")
    return user, workspace


def _github_repository_id(
    gateway: GitHubGateway, token: str, full_name: str, *, fixtures: bool
) -> int:
    """The GitHub repository ID, after checking that the App is installed on the repository."""
    install_hint = (
        f"The GitHub App cannot see {full_name}. Fork the upstream repository, install the App "
        "on the fork, and pass --fork-owner (evals/README.md)."
    )
    if fixtures:
        for repository in gateway.list_accessible_repositories(token):
            if repository.full_name.lower() == full_name.lower():
                return repository.id
        raise EvalError(
            f"{full_name} is not a fixture repository visible to the fake user "
            f"{FIXTURE_LOGIN} (for example, use octo-org/sample-app)."
        )
    try:
        gateway.get_installation_id(full_name)
    except GitHubNotFound as exc:
        raise EvalError(install_hint) from exc
    except (GitHubError, OSError) as exc:
        raise EvalError(f"Checking the App installation on {full_name} failed: {exc}") from exc
    try:
        response = httpx.get(
            f"{GITHUB_API}/repos/{full_name}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "codeatlas-eval",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise EvalError(f"Looking up {full_name} on GitHub failed: {exc}") from exc
    if response.status_code != 200:
        raise EvalError(f"EVAL_GITHUB_TOKEN cannot read {full_name} (HTTP {response.status_code}).")
    return int(response.json()["id"])


def _drive_job(job_id: uuid.UUID, timeout_seconds: float) -> JobOutcome:
    """Run worker iterations in this process until the job ends or the timeout passes."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        with new_session() as db:
            job = db.get(Job, job_id)
            if job is None:
                raise EvalError(f"Job {job_id} disappeared.")
            outcome = JobOutcome(job.status, job.error_code, job.error_message)
        if outcome.status in TERMINAL_JOB_STATUSES:
            return outcome
        if time.monotonic() > deadline:
            return JobOutcome("timed_out", "eval_timeout", f"still {outcome.status}")
        if not run_once():
            time.sleep(POLL_SECONDS)  # retry backoff, or another worker holds the job


def _index_target(
    repository: str,
    github_full_name: str,
    github_id: int,
    commit_sha: str,
    *,
    fixtures: bool,
    timeout_seconds: float,
) -> IndexedTarget:
    """Connect (or re-index) the repository at its pinned commit and wait for a ready version."""
    branch = None if fixtures else eval_branch(commit_sha)
    with session_scope() as db:
        user, workspace = _identity(db)
        existing = db.scalar(
            select(Repository).where(
                Repository.workspace_id == workspace.id,
                Repository.github_repository_id == github_id,
                Repository.deleted_at.is_(None),
            )
        )
        try:
            if existing is None:
                connected, job = connect(
                    db,
                    user=user,
                    workspace=workspace,
                    github_repository_id=github_id,
                    branch=branch,
                    accept_external_processing=True,
                    request_id=None,
                )
                repository_id = connected.id
            else:
                job = reindex(
                    db,
                    user=user,
                    workspace=workspace,
                    repository_id=existing.id,
                    branch=branch,
                    request_id=None,
                )
                repository_id = existing.id
        except ApiError as exc:
            raise EvalError(
                f"Connecting {github_full_name} failed with {exc.code}: {exc.message}"
            ) from exc
        job_id = job.id

    outcome = _drive_job(job_id, timeout_seconds)
    if outcome.status != "succeeded":
        hint = ""
        if outcome.error_code == "branch_not_found":
            hint = (
                f" Create the branch at the pinned commit: gh api -X POST "
                f"repos/{github_full_name}/git/refs -f ref=refs/heads/{branch} -f sha={commit_sha}"
            )
        raise EvalError(
            f"Indexing {github_full_name} ended {outcome.status} with {outcome.error_code}: "
            f"{outcome.error_message}.{hint}"
        )

    version = index_version(get_settings())
    with session_scope() as db:
        snapshot = db.scalar(
            select(Snapshot)
            .where(
                Snapshot.repository_id == repository_id,
                Snapshot.commit_sha == commit_sha,
                Snapshot.index_version == version,
                Snapshot.status == "ready",
            )
            .order_by(Snapshot.ready_at.desc())
            .limit(1)
        )
        if snapshot is None:
            indexed = db.get(Job, job_id)
            seen = (indexed.payload.get("commit_sha") if indexed else None) or "unknown"
            where = f"branch {branch}" if branch else "the default branch"
            raise EvalError(
                f"{github_full_name} was indexed at {seen}, not at the pinned {commit_sha}. "
                f"Point {where} at the pinned commit."
            )
        return IndexedTarget(
            repository, github_full_name, commit_sha, repository_id, snapshot.id, snapshot.coverage
        )


def _retrieve(target: IndexedTarget, result: QuestionResult) -> None:
    question = result.question
    try:
        with session_scope() as db:
            snapshot = db.get(Snapshot, target.snapshot_id)
            if snapshot is None:
                raise EvalError(f"Snapshot {target.snapshot_id} disappeared.")
            evidence, result.docs_degraded = assemble_evidence(db, snapshot, question.question)
    except Exception as exc:  # recorded as an execution failure; the run continues
        result.failures.append(f"retrieval: {type(exc).__name__}: {exc}")
        return
    result.retrieved = [LineRange(e.path, e.start_line, e.end_line) for e in evidence]
    if question.answerable:
        result.recall = recall_at_k(question.relevant_evidence, result.retrieved)


def _answer(target: IndexedTarget, result: QuestionResult, timeout_seconds: float) -> None:
    try:
        with session_scope() as db:
            user, workspace = _identity(db)
            run, job = submit(
                db,
                user=user,
                workspace=workspace,
                repository_id=target.repository_id,
                snapshot_id=target.snapshot_id,
                question=result.question.question,
                request_id=None,
            )
            run_id, job_id = run.id, job.id
    except ApiError as exc:
        result.failures.append(f"submit: {exc.code}: {exc.message}")
        return

    outcome = _drive_job(job_id, timeout_seconds)
    result.answer_status = outcome.status
    if outcome.status != "succeeded":
        result.failures.append(
            f"answer {outcome.status}: {outcome.error_code}: {outcome.error_message}"
        )
        return

    with new_session() as db:
        stored = db.get(AnalysisRun, run_id)
        if stored is None or stored.result is None:
            result.failures.append("answer: the run has no published result")
            return
        output = AnswerOutput.model_validate(stored.result)
        result.quality_state = stored.quality_state
        result.summary = output.summary
        result.gaps = list(output.gaps)
        result.claims = [
            ClaimRecord(claim.text, claim.kind, tuple(claim.evidence_ids))
            for claim in output.claims
        ]
        for shown in citations(db, stored):
            content = db.scalar(
                select(File.content).where(
                    File.snapshot_id == stored.snapshot_id, File.path == shown["path"]
                )
            )
            result.citations.append(
                CitationCheck(
                    label=shown["label"],
                    path=shown["path"],
                    start_line=shown["start_line"],
                    end_line=shown["end_line"],
                    excerpt=shown["excerpt"],
                    problem=citation_problem(
                        content=content,
                        commit_sha=shown["commit_sha"],
                        expected_commit_sha=stored.commit_sha,
                        start_line=shown["start_line"],
                        end_line=shown["end_line"],
                        excerpt=shown["excerpt"],
                    ),
                )
            )


def _progress(index: int, total: int, result: QuestionResult) -> str:
    parts = [f"[{index}/{total}] {result.question.id}"]
    if result.recall is not None:
        parts.append(f"recall@{RECALL_K}={result.recall:.2f}")
    if result.quality_state:
        valid = sum(1 for c in result.citations if c.problem is None)
        parts.append(f"{result.quality_state}, citations {valid}/{len(result.citations)} valid")
    if result.failures:
        parts.append("FAILED: " + "; ".join(result.failures))
    return "  ".join(parts)


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return path.name


def _write_outputs(
    out_dir: Path,
    info: RunInfo,
    targets: Sequence[IndexedTarget],
    results: Sequence[QuestionResult],
    summary: Summary,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = info.started_at.strftime("%Y%m%dT%H%M%SZ")
    report = out_dir / f"qa-eval-{stamp}.md"
    sheet = out_dir / f"qa-eval-{stamp}-audit.csv"
    report.write_text(render_report(info, targets, results, summary), encoding="utf-8")
    with sheet.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_COLUMNS)
        writer.writeheader()
        writer.writerows(audit_rows(results))
    return report, sheet


def run(args: argparse.Namespace) -> int:
    started_at = datetime.now(UTC)
    set_path = Path(args.set)
    questions = select_questions(load_questions(set_path), args.split, args.limit)
    if not questions:
        raise EvalError(f"No questions in {set_path.name} for split {args.split}.")

    settings = get_settings()
    _check_settings(settings, fixtures=args.fixtures, retrieval_only=args.retrieval_only)
    # A run asks more questions than a user's daily allowance; lift it for this process only.
    settings.daily_question_limit = EVAL_DAILY_QUESTION_LIMIT
    index_timeout = (
        settings.indexing_deadline.total_seconds() + MAX_BACKOFF_SECONDS
    ) * JOB_ATTEMPTS
    answer_timeout = (
        settings.question_deadline.total_seconds() + MAX_BACKOFF_SECONDS
    ) * JOB_ATTEMPTS

    gateway = get_gateway(settings)
    token = _user_token(gateway, fixtures=args.fixtures)
    _ensure_identity(token)

    targets: dict[tuple[str, str], IndexedTarget] = {}
    for repository, commit_sha in dict.fromkeys((q.repository, q.commit_sha) for q in questions):
        name = repository.split("/", 1)[1]
        full_name = f"{args.fork_owner}/{name}" if args.fork_owner else repository
        github_id = _github_repository_id(gateway, token, full_name, fixtures=args.fixtures)
        print(f"Indexing {full_name} at {commit_sha[:12]} ...", flush=True)
        target = _index_target(
            repository,
            full_name,
            github_id,
            commit_sha,
            fixtures=args.fixtures,
            timeout_seconds=index_timeout,
        )
        print(
            f"  ready: {target.coverage.get('eligible_files', '?')} files, "
            f"{target.coverage.get('source_lines', '?')} source lines",
            flush=True,
        )
        targets[(repository, commit_sha)] = target

    results: list[QuestionResult] = []
    for index, question in enumerate(questions, start=1):
        result = QuestionResult(question)
        target = targets[(question.repository, question.commit_sha)]
        try:
            _retrieve(target, result)
            if not args.retrieval_only:
                _answer(target, result, answer_timeout)
        except EvalError:
            raise
        except Exception as exc:  # one broken question must not end the run
            result.failures.append(f"{type(exc).__name__}: {exc}")
        results.append(result)
        print(_progress(index, len(questions), result), flush=True)

    summary = summarize(results)
    info = RunInfo(
        started_at=started_at,
        set_path=args.set,
        set_sha256=hashlib.sha256(set_path.read_bytes()).hexdigest(),
        split=args.split,
        limit=args.limit,
        mode="fixtures (fake GitHub and providers)" if args.fixtures else "real providers",
        retrieval_only=args.retrieval_only,
        answer_model=FAKE_MODEL if args.fixtures else settings.answer_model,
        thinking_level=settings.answer_thinking_level,
        embedding_model="fake embedder" if args.fixtures else settings.embedding_model,
        index_version=index_version(settings),
    )
    report, sheet = _write_outputs(Path(args.out), info, list(targets.values()), results, summary)
    print()
    print(f"Recall@{RECALL_K}: {_rate_text(summary.recall, percent=False)}")
    print(f"Citation validity: {_rate_text(summary.citation_validity, percent=True)}")
    print(f"Abstention ({args.split}): {_rate_text(summary.abstention, percent=True)}")
    print(f"Answered when answerable: {_rate_text(summary.answered_when_answerable, percent=True)}")
    print(f"Questions with execution failures: {summary.execution_failures}/{summary.questions}")
    print(f"Report: {_display(report)}")
    print(f"Audit sheet: {_display(sheet)}")
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run_qa_eval", description="Run the answer-quality evaluation."
    )
    parser.add_argument("--set", required=True, help="question set (JSON Lines)")
    parser.add_argument(
        "--split", choices=[*SPLITS, "all"], default="heldout", help="default: heldout"
    )
    parser.add_argument("--limit", type=int, help="ask at most N questions")
    parser.add_argument(
        "--retrieval-only", action="store_true", help="measure retrieval only; no model calls"
    )
    parser.add_argument(
        "--fork-owner", help="read OWNER/NAME instead of each upstream repository (your forks)"
    )
    parser.add_argument(
        "--fixtures", action="store_true", help="smoke test against the fake GitHub fixtures"
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "out"),
        help="output directory (default: evals/out)",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(logging.WARNING)
    try:
        return run(args)
    except (EvalError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
