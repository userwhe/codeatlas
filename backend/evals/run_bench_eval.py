"""Code Review Bench evaluation (004 FR-027, research R16).

Run from `backend/` (see `evals/README.md`, "Code Review Bench"):

    uv run python -m evals.run_bench_eval --bench-commit <sha>                # review and judge
    uv run python -m evals.run_bench_eval --bench-commit <sha> --skip-judge   # review only
    uv run python -m evals.run_bench_eval --bench-commit <sha> --limit 2 --exclude-flagged

The runner downloads the benchmark (`withmartian/code-review-benchmark`) at the given commit and
reads the golden comments of its Sentry (Python) and Cal.com (TypeScript) pull requests. For each
pull request it resolves the base, head, and merge base with GitHub's REST API, downloads both
archives into the review runner's cache, reads them with the review job's `read_tree` under raised
repository limits, and calls the job's `analyze` with the real model, as `run_review_eval.py`
does. Each risk becomes one candidate for the benchmark's judge, written for the tool `codeatlas`
into the benchmark's own `results/` files and keyed by the golden comment's URL. Unless
`--skip-judge`, the benchmark's deduplication (step 2.5) and judge (step 3) then run on those
candidates. The report compares CodeAtlas's precision and recall with the published results of
every other tool on the same pull requests, by the same judge, with and without the items that
the benchmark flags with a data warning (`az_comment`).

Parsing, the export, and the comparison are small pure functions tested in
`tests/unit/test_bench_eval.py`.
"""

import argparse
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from codeatlas.config import GIB, Settings, get_settings
from codeatlas.github.client import API_HEADERS, GITHUB_API
from codeatlas.ingestion.extract import LimitExceeded
from codeatlas.jobs.queue import JobFailure
from codeatlas.logging import configure_logging
from codeatlas.providers.answer_model import AnswerModel, GeminiAnswerModel
from codeatlas.review.diff import read_tree
from codeatlas.review.prompt import PROMPT_VERSION
from codeatlas.review.review import Analysis, analyze
from evals.run_qa_eval import Rate
from evals.run_review_eval import (
    EXIT_SETUP,
    EvalError,
    EvidenceRef,
    RiskRecord,
    download_archive,
)

EVALS_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = EVALS_DIR / "out" / "bench"
BENCH_REPOSITORY = "withmartian/code-review-benchmark"
GOLDEN_FILES = ("sentry.json", "cal_dot_com.json")
"""The Python and TypeScript repositories of the benchmark's offline set, read in this order."""
TOOL = "codeatlas"
"""The tool name under which CodeAtlas's reviews are written into the benchmark's files."""
CANDIDATE_SOURCE = "risk"
JUDGE_STEPS = ("step2_5_dedup_candidates", "step3_judge_comments")
DEDUP_MIN_CANDIDATES = 2
"""Step 2.5 calls its model only for a review with at least this many candidates."""

EXIT_DONE = 0

RAISED_LIMITS: Mapping[str, int] = {
    "max_files_per_snapshot": 50_000,
    "max_source_lines_per_snapshot": 10_000_000,
    "max_expanded_bytes": 1 * GIB,
    "max_archive_members": 200_000,
    "max_archive_bytes": 2 * GIB,
}
"""001 FR-009's repository limits and the archive caps, raised for this process only: both
repositories exceed the defaults (a 2025 Sentry head has about 17,000 eligible files, 2.8 million
lines, and 105 MB)."""
KEPT_LIMITS = (
    "max_file_bytes",
    "review_max_files",
    "review_max_changed_lines",
    "review_max_hunks",
    "review_max_diff_tokens",
    "review_max_input_tokens",
)
"""Limits that keep the application's values, so a pull request is reviewed as in the pilot."""

PRICES: tuple[tuple[str, float, float], ...] = (
    ("2027-01-01", 1.50, 7.50),
    ("0001-01-01", 0.75, 3.75),
)
"""(first day, input, output) in USD per million tokens, newest first: the paid-tier prices of the
default review model from 001 research R11. Thinking tokens are billed as output."""

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_PULL_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)"
)
_ITEM_KEYS = frozenset({"pr_title", "url", "comments"})
_COMMENT_KEYS = frozenset({"comment", "severity", "category"})


# --- Golden comments ----------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldenComment:
    comment: str
    severity: str
    category: str


@dataclass(frozen=True)
class BenchItem:
    """One pull request of the benchmark with its golden comments, keyed by `url`."""

    url: str
    owner: str
    repository: str
    number: int
    pr_title: str
    comments: tuple[GoldenComment, ...]
    source_file: str
    original_url: str | None = None
    az_comment: str | None = None
    """The benchmark's data warning for this item, if any."""

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repository}"

    @property
    def flagged(self) -> bool:
        """Whether the benchmark notes a data warning for this item, so results are reported
        with and without it."""
        return bool(self.az_comment)


def parse_pr_url(url: object) -> tuple[str, str, int]:
    """The owner, repository, and number of a `https://github.com/<owner>/<repo>/pull/<n>` URL."""
    match = _PULL_URL.fullmatch(url) if isinstance(url, str) else None
    if match is None:
        raise ValueError(f"{url!r} is not a GitHub pull request URL")
    return match[1], match[2], int(match[3])


def _optional_text(record: Mapping[str, Any], key: str, where: str) -> str | None:
    value = record.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{where}: {key} must be a string when present")
    return value


def _golden_comment(value: object, where: str) -> GoldenComment:
    if not isinstance(value, dict) or not _COMMENT_KEYS <= value.keys():
        raise ValueError(f"{where}: a golden comment needs the keys {sorted(_COMMENT_KEYS)}")
    comment, severity, category = value["comment"], value["severity"], value["category"]
    if not isinstance(comment, str) or not comment.strip():
        raise ValueError(f"{where}: comment must be a non-empty string")
    if not isinstance(severity, str) or not isinstance(category, str):
        raise ValueError(f"{where}: severity and category must be strings")
    return GoldenComment(comment, severity, category)


def parse_golden(data: object, source_file: str) -> dict[str, BenchItem]:
    """Every item of one golden-comments file, keyed by its URL, in file order.

    An item is `{pr_title, url, comments: [{comment, severity, category}]}`, optionally with
    `original_url` and `az_comment`; other keys are ignored. Raises ValueError naming the item.
    """
    if not isinstance(data, list):
        raise ValueError(f"{source_file}: expected a list of items")
    items: dict[str, BenchItem] = {}
    for number, record in enumerate(data, start=1):
        where = f"{source_file} item {number}"
        if not isinstance(record, dict) or not _ITEM_KEYS <= record.keys():
            raise ValueError(f"{where}: an item needs the keys {sorted(_ITEM_KEYS)}")
        url, title, comments = record["url"], record["pr_title"], record["comments"]
        try:
            owner, repository, pr_number = parse_pr_url(url)
        except ValueError as exc:
            raise ValueError(f"{where}: {exc}") from exc
        if url in items:
            raise ValueError(f"{where}: duplicate url {url}")
        if not isinstance(title, str):
            raise ValueError(f"{where}: pr_title must be a string")
        if not isinstance(comments, list):
            raise ValueError(f"{where}: comments must be a list")
        items[url] = BenchItem(
            url=url,
            owner=owner,
            repository=repository,
            number=pr_number,
            pr_title=title,
            comments=tuple(
                _golden_comment(comment, f"{where} comment {n}")
                for n, comment in enumerate(comments, start=1)
            ),
            source_file=source_file,
            original_url=_optional_text(record, "original_url", where),
            az_comment=_optional_text(record, "az_comment", where),
        )
    return items


def load_golden(golden_dir: Path) -> dict[str, BenchItem]:
    """The items of `sentry.json`, then `cal_dot_com.json`, keyed by URL."""
    items: dict[str, BenchItem] = {}
    for name in GOLDEN_FILES:
        parsed = parse_golden(json.loads((golden_dir / name).read_text(encoding="utf-8")), name)
        repeated = sorted(items.keys() & parsed.keys())
        if repeated:
            raise ValueError(f"{name}: {repeated[0]} is also in another golden-comments file")
        items |= parsed
    return items


def select_items(
    items: Iterable[BenchItem], *, exclude_flagged: bool, limit: int | None
) -> list[BenchItem]:
    """The items to review, in file order."""
    selected = [item for item in items if not (exclude_flagged and item.flagged)]
    return selected if limit is None else selected[:limit]


# --- The benchmark and GitHub -------------------------------------------------------------------


def download_benchmark(commit: str, out_dir: Path) -> Path:
    """The benchmark's `offline/` directory at `commit`: the archive is downloaded once into
    `out_dir` and extracted next to it."""
    root = out_dir / f"code-review-benchmark-{commit}"
    offline = root / "offline"
    if offline.is_dir():
        return offline
    print(f"Downloading {BENCH_REPOSITORY} at {commit[:12]} ...", flush=True)
    data = download_archive(BENCH_REPOSITORY, commit, cache_dir=out_dir)
    partial = out_dir / f".{root.name}.part"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            # The `data` filter refuses absolute paths, links out of the tree, and special files.
            tar.extractall(partial, filter="data")  # noqa: S202
    except (tarfile.TarError, OSError) as exc:
        raise EvalError(f"The benchmark archive is unreadable: {exc}") from exc
    tops = list(partial.iterdir())
    if len(tops) != 1 or not (tops[0] / "offline" / "golden_comments").is_dir():
        raise EvalError("The benchmark archive has no offline/golden_comments directory.")
    tops[0].replace(root)
    shutil.rmtree(partial)
    return offline


@dataclass(frozen=True)
class PullRequest:
    """A golden URL resolved on GitHub. `full_name` is the base repository after redirects."""

    full_name: str
    title: str
    body: str
    base_sha: str
    head_sha: str
    merge_base_sha: str
    renamed: Mapping[str, str]
    """New path to previous path, for files that GitHub's comparison reports as renamed."""


@contextmanager
def github_client(
    token: str, transport: httpx.BaseTransport | None = None
) -> Iterator[httpx.Client]:
    """A REST client that follows redirects (`calcom/cal.com` is now `calcom/cal.diy`); a token,
    when given, only raises the rate limit."""
    headers = {**API_HEADERS, "User-Agent": "codeatlas-bench-eval"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(
        base_url=GITHUB_API,
        headers=headers,
        follow_redirects=True,
        timeout=30.0,
        transport=transport,
    ) as client:
        yield client


def _get(client: httpx.Client, path: str, what: str, **params: Any) -> dict[str, Any]:
    try:
        response = client.get(path, params=params)
    except httpx.HTTPError as exc:
        raise EvalError(f"Reading {what} from GitHub failed: {exc}") from exc
    if response.status_code != 200:
        hint = (
            " Set GITHUB_TOKEN to raise the rate limit."
            if response.status_code in (403, 429)
            else ""
        )
        raise EvalError(
            f"Reading {what} from GitHub failed with HTTP {response.status_code}.{hint}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise EvalError(f"GitHub's answer for {what} is not JSON.") from exc
    if not isinstance(data, dict):
        raise EvalError(f"GitHub's answer for {what} is not an object.")
    return data


def _sha(value: object, what: str) -> str:
    if not isinstance(value, str) or not _FULL_SHA.fullmatch(value):
        raise EvalError(f"GitHub's answer has no commit SHA for {what}.")
    return value


def resolve_pull_request(client: httpx.Client, item: BenchItem) -> PullRequest:
    """The pull request's base and head commits, and the merge base its review compares with
    the head, as GitHub shows the change (base...head)."""
    pull = _get(client, f"/repos/{item.full_name}/pulls/{item.number}", item.url)
    base, head = pull.get("base") or {}, pull.get("head") or {}
    full_name = (base.get("repo") or {}).get("full_name")
    if not isinstance(full_name, str) or "/" not in full_name:
        raise EvalError(f"GitHub's answer for {item.url} has no base repository.")
    base_sha = _sha(base.get("sha"), f"the base of {item.url}")
    head_sha = _sha(head.get("sha"), f"the head of {item.url}")
    comparison = _get(
        client,
        f"/repos/{full_name}/compare/{base_sha}...{head_sha}",
        f"the comparison of {item.url}",
        per_page=1,  # the commit list is paginated; the changed files come on the first page
    )
    merge_base = _sha(
        (comparison.get("merge_base_commit") or {}).get("sha"), f"the merge base of {item.url}"
    )
    renamed = {
        str(entry["filename"]): str(entry["previous_filename"])
        for entry in comparison.get("files") or ()
        if entry.get("status") == "renamed" and entry.get("previous_filename")
    }
    return PullRequest(
        full_name,
        str(pull.get("title") or ""),
        str(pull.get("body") or ""),
        base_sha,
        head_sha,
        merge_base,
        renamed,
    )


# --- Reviews and candidates ---------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One risk as a candidate issue for the judge, which reads only `text`."""

    text: str
    path: str | None
    line: int | None

    def as_json(self) -> dict[str, Any]:
        """The entry of `candidates.json`, shaped like the output of the benchmark's step 2."""
        return {"text": self.text, "path": self.path, "line": self.line, "source": CANDIDATE_SOURCE}


def _place(ref: EvidenceRef) -> str:
    lines = (
        f"line {ref.start_line}"
        if ref.start_line == ref.end_line
        else f"lines {ref.start_line}-{ref.end_line}"
    )
    side = ", before the change" if ref.side == "before" else ""
    return f"{ref.path} {lines}{side}"


def candidate(risk: RiskRecord, refs: Mapping[str, EvidenceRef]) -> Candidate:
    """The risk's title, the file and line range of each cited evidence item (or a rule risk's
    file), and its explanation."""
    cited = [refs[label] for label in risk.evidence_ids if label in refs]
    places = list(dict.fromkeys(_place(ref) for ref in cited))
    if not places and risk.path:
        places = [risk.path]
    where = f" ({'; '.join(places)})" if places else ""
    first = cited[0] if cited else None
    return Candidate(
        f"{risk.title.rstrip('.')}{where}: {risk.explanation}",
        first.path if first is not None else risk.path,
        first.start_line if first is not None else None,
    )


@dataclass
class ItemResult:
    item: BenchItem
    pull_request: PullRequest | None = None
    quality_state: str | None = None
    changed_files: int = 0
    reviewed_files: int = 0
    partial: bool = False
    risks: list[RiskRecord] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    failures: list[str] = field(default_factory=list)
    """Review failures, kept apart from review quality."""
    evaluation: Mapping[str, Any] | None = None
    """The judge's result for `codeatlas` on this item."""
    judge_failure: str | None = None

    @property
    def reviewed(self) -> bool:
        return self.quality_state is not None and not self.failures


def raised_settings(settings: Settings) -> Settings:
    """A copy of `settings` with `RAISED_LIMITS`; the application's settings are not changed."""
    return settings.model_copy(update=dict(RAISED_LIMITS))


def _fill(result: ItemResult, analysis: Analysis) -> None:
    review = analysis.result
    refs = {
        item.label: EvidenceRef(item.label, item.side, item.path, item.start_line, item.end_line)
        for item in analysis.evidence
    }
    coverage = review.get("coverage") or {}
    result.quality_state = analysis.quality_state
    result.changed_files = int(coverage.get("changed_files") or 0)
    result.reviewed_files = int(coverage.get("reviewed_files") or 0)
    result.partial = bool((review.get("overall_risk") or {}).get("partial"))
    result.usage = dict(analysis.usage.as_dict())
    result.risks = [RiskRecord.from_result(risk) for risk in review.get("risks") or ()]
    result.candidates = [candidate(risk, refs) for risk in result.risks]


def review_archives(
    item: BenchItem,
    pull: PullRequest,
    head_bytes: bytes,
    base_bytes: bytes,
    settings: Settings,
    model: AnswerModel,
) -> ItemResult:
    """Read both archives as the review job does and run its `analyze`; failures are recorded,
    not raised, so one broken review does not end the run."""
    result = ItemResult(item, pull)
    started = time.monotonic()
    try:
        head = read_tree(io.BytesIO(head_bytes), settings)
        base = read_tree(
            io.BytesIO(base_bytes),
            settings,
            skip=lambda path, sha256: head.hashes.get(path) == sha256,
        )
        analysis = analyze(
            {"title": pull.title, "body": pull.body},
            head,
            base,
            pull.renamed,
            head_sha=pull.head_sha,
            merge_base_sha=pull.merge_base_sha,
            settings=settings,
            model=model,
        )
    except LimitExceeded as exc:
        result.failures.append(f"limit_exceeded: {exc.limit_name} ({exc.limit_value})")
    except JobFailure as exc:
        result.failures.append(f"{exc.code}: {exc.message}")
    except Exception as exc:  # one broken review must not end the run
        result.failures.append(f"{type(exc).__name__}: {exc}")
    else:
        _fill(result, analysis)
    result.seconds = time.monotonic() - started
    return result


def review_cost(usage: Mapping[str, Any], day: str) -> float:
    """The estimated model cost of one review in USD at the prices of `day` (YYYY-MM-DD)."""
    _, input_price, output_price = next(price for price in PRICES if day >= price[0])
    output = int(usage.get("output_tokens", 0)) + int(usage.get("thinking_tokens", 0))
    return (int(usage.get("input_tokens", 0)) * input_price + output * output_price) / 1_000_000


# --- The export ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Export:
    """CodeAtlas's review of one golden URL, as the benchmark's files hold it."""

    url: str
    repo_name: str
    candidates: tuple[Candidate, ...]


def model_dir_name(model: str) -> str:
    """The `results/` directory that the benchmark's steps use for a judge model (their
    `sanitize_model_name`)."""
    return model.strip().replace("/", "_")


def check_entry(data: Mapping[str, Any], item: BenchItem) -> None:
    """Raise ValueError unless `results/benchmark_data.json` has the item with the same golden
    comments, which are the ones step 3 judges against."""
    entry = data.get(item.url)
    if not isinstance(entry, dict):
        raise ValueError(f"{item.url} is not in results/benchmark_data.json")
    stored = [comment.get("comment") for comment in entry.get("golden_comments") or ()]
    if stored != [comment.comment for comment in item.comments]:
        raise ValueError(
            f"{item.url}: the golden comments in results/benchmark_data.json differ from "
            f"golden_comments/{item.source_file}"
        )


def _write_json(path: Path, data: object) -> None:
    """Replace `path` in one step, indented as the benchmark writes its results."""
    partial = path.with_name(f"{path.name}.part")
    partial.write_text(json.dumps(data, indent=2), encoding="utf-8")
    partial.replace(path)


def write_exports(
    offline: Path,
    model_dir: str,
    exports: Sequence[Export],
    golden: Mapping[str, BenchItem],
    created_at: str,
) -> None:
    """Write one `codeatlas` review per exported golden URL into `results/benchmark_data.json`,
    and one candidate per risk into `results/<model_dir>/candidates.json`.

    Earlier `codeatlas` entries are removed first, so the judge sees exactly this run; every
    other tool's entries are kept. Nothing is written when an exported URL is missing or its
    golden comments differ (ValueError).
    """
    results = offline / "results"
    data_file = results / "benchmark_data.json"
    data = json.loads(data_file.read_text(encoding="utf-8"))
    for export in exports:
        check_entry(data, golden[export.url])
    for entry in data.values():
        entry["reviews"] = [r for r in entry.get("reviews") or () if r.get("tool") != TOOL]
    for export in exports:
        data[export.url]["reviews"].append(
            {
                "tool": TOOL,
                "repo_name": export.repo_name,
                "pr_url": export.url,
                "review_comments": [
                    {"path": c.path, "line": c.line, "body": c.text, "created_at": created_at}
                    for c in export.candidates
                ],
            }
        )

    candidates_file = results / model_dir / "candidates.json"
    candidates: dict[str, dict[str, Any]] = (
        json.loads(candidates_file.read_text(encoding="utf-8")) if candidates_file.is_file() else {}
    )
    for url in list(candidates):
        if TOOL in candidates[url]:
            del candidates[url][TOOL]
            if not candidates[url]:
                del candidates[url]
    for export in exports:
        candidates.setdefault(export.url, {})[TOOL] = [c.as_json() for c in export.candidates]
    candidates_file.parent.mkdir(parents=True, exist_ok=True)
    _write_json(data_file, data)
    _write_json(candidates_file, candidates)


# --- The judge ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Judge:
    """A judge model behind the benchmark's published results, reached through an
    OpenAI-compatible endpoint."""

    model: str
    base_url: str
    key_name: str
    """The environment variable that holds the provider's key."""
    published_dir: str
    """The `results/` directory of the published evaluations by this judge."""


JUDGES: Mapping[str, Judge] = {
    judge.model: judge
    for judge in (
        Judge(
            "claude-opus-4-5-20251101",
            "https://api.anthropic.com/v1/",
            "ANTHROPIC_API_KEY",
            "anthropic_claude-opus-4-5-20251101",
        ),
        Judge("gpt-5.2", "https://api.openai.com/v1", "OPENAI_API_KEY", "openai_gpt-5.2"),
    )
}
DEFAULT_JUDGE = "claude-opus-4-5-20251101"
"""The default judge of the benchmark's dashboard; GPT-5.2 is the fallback (research R16)."""

_PASSED_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_PASSED_PREFIXES = ("UV_", "XDG_")


def judge_environment(judge: Judge, api_key: str, base: Mapping[str, str]) -> dict[str, str]:
    """The environment of the benchmark's steps: what `uv` and the network need, plus the
    judge's `MARTIAN_*` variables, but none of this runner's other credentials."""
    env = {
        key: value
        for key, value in base.items()
        if key in _PASSED_ENV or key.startswith(_PASSED_PREFIXES)
    }
    return env | {
        "MARTIAN_API_KEY": api_key,
        "MARTIAN_BASE_URL": judge.base_url,
        "MARTIAN_MODEL": judge.model,
    }


def step_command(uv: str, offline: Path, step: str) -> list[str]:
    """One benchmark step for `codeatlas` only. `--directory` makes `offline/` the working
    directory, where the steps find `results/` and their `.env` (`offline/` has no build system,
    so `--project` would fail). `--force` judges this run's candidates again instead of keeping
    an earlier run's results. Step 3 finds `dedup_groups.json` itself."""
    return [
        uv,
        "run",
        "--directory",
        str(offline),
        "python",
        "-m",
        f"code_review_benchmark.{step}",
        "--tool",
        TOOL,
        "--force",
    ]


def run_judge(offline: Path, judge: Judge, api_key: str, uv: str) -> None:
    env = judge_environment(judge, api_key, os.environ)
    for step in JUDGE_STEPS:
        print(f"Running the benchmark's {step} with {judge.model} ...", flush=True)
        command = step_command(uv, offline.resolve(), step)
        try:
            completed = subprocess.run(command, env=env, check=False)  # noqa: S603
        except OSError as exc:
            raise EvalError(f"Starting the benchmark's {step} failed: {exc}") from exc
        if completed.returncode != 0:
            raise EvalError(f"The benchmark's {step} exited with code {completed.returncode}.")


def judge_problem(evaluation: Mapping[str, Any] | None) -> str | None:
    """Why the judge's result for an item cannot be compared, or None."""
    if evaluation is None:
        return "the judge left no result"
    if evaluation.get("skipped"):
        return f"the judge skipped it ({evaluation.get('reason')})"
    errors = int(evaluation.get("errors_count") or 0)
    if errors:
        return f"{errors} judge calls failed"
    return None


# --- Comparison ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Totals:
    """One tool's judged counts summed over pull requests, as the benchmark's step 3 sums them."""

    tool: str
    prs: int
    tp: int
    fp: int
    fn: int
    older: int = 0
    """Of `prs`, the pull requests whose evaluation was made against other golden comments than
    the current ones (an older version of the benchmark's labels)."""

    @property
    def precision(self) -> Rate:
        return Rate(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> Rate:
        return Rate(self.tp, self.tp + self.fn)


def judged_against_other_golden(result: Mapping[str, Any], item: BenchItem) -> bool:
    """Whether an evaluation's golden comments (its true positives and false negatives) differ
    from the item's current ones. An evaluation without those lists is not checked."""
    if "true_positives" not in result and "false_negatives" not in result:
        return False
    judged = [
        str(entry.get("golden_comment"))
        for key in ("true_positives", "false_negatives")
        for entry in result.get(key) or ()
    ]
    return sorted(judged) != sorted(comment.comment for comment in item.comments)


def tool_totals(
    evaluations: Mapping[str, Mapping[str, Any]],
    urls: Iterable[str],
    golden: Mapping[str, BenchItem] | None = None,
) -> dict[str, Totals]:
    """Each tool's true positives, false positives, and false negatives in an `evaluations.json`,
    summed over `urls`; skipped evaluations are left out. With `golden`, evaluations made against
    other golden comments are counted in `older`."""
    sums: dict[str, list[int]] = {}
    for url in dict.fromkeys(urls):
        item = golden.get(url) if golden is not None else None
        for tool, result in (evaluations.get(url) or {}).items():
            if not isinstance(result, Mapping) or result.get("skipped"):
                continue
            counts = sums.setdefault(tool, [0, 0, 0, 0, 0])
            counts[0] += 1
            counts[1] += int(result.get("tp") or 0)
            counts[2] += int(result.get("fp") or 0)
            counts[3] += int(result.get("fn") or 0)
            counts[4] += int(item is not None and judged_against_other_golden(result, item))
    return {tool: Totals(tool, *counts) for tool, counts in sums.items()}


def comparisons(
    items: Sequence[BenchItem],
    published: Mapping[str, Mapping[str, Any]],
    ours: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, list[Totals]]:
    """Every tool's totals on the same pull requests: `all` of `items`, and `without_flagged`.
    CodeAtlas (from `ours`, when judged) comes first, then the published tools by name."""
    golden = {item.url: item for item in items}
    selections = {
        "all": [item.url for item in items],
        "without_flagged": [item.url for item in items if not item.flagged],
    }
    tables: dict[str, list[Totals]] = {}
    for name, urls in selections.items():
        rows: list[Totals] = []
        if ours is not None:
            rows.append(tool_totals(ours, urls, golden).get(TOOL, Totals(TOOL, 0, 0, 0, 0)))
        others = tool_totals(published, urls, golden)
        others.pop(TOOL, None)
        rows += sorted(others.values(), key=lambda totals: totals.tool)
        tables[name] = rows
    return tables


# --- Report -------------------------------------------------------------------------------------

TABLE_TITLES = {"all": "All compared items", "without_flagged": "Without the flagged items"}

CAVEATS = (
    "The pull requests come from well-known public repositories, so the models may have seen "
    "them, and their fixes, in training.",
    "The judge is a model: it decides whether a candidate and a golden comment describe the same "
    "issue, and another judge may decide differently.",
    "CodeAtlas reports risks only, so golden comments about style or documentation that it does "
    "not aim to find count against its recall.",
    "The benchmark's step 2 (a model splitting a tool's comments into issues) was skipped for "
    "CodeAtlas, whose risks are already one issue each; the published tools' candidates went "
    "through it.",
    "Flagged items carry the benchmark's own data warnings (`az_comment`), so results are shown "
    "with and without them.",
    "Some published tools were judged against an older version of the golden comments on some "
    "pull requests; they are listed apart, and CodeAtlas is compared with the others.",
    "The repository limits were raised for this run (see above); the pilot keeps the defaults "
    "and would refuse these repositories.",
)


@dataclass(frozen=True)
class RunInfo:
    started_at: str
    seconds: float
    bench_commit: str
    judge: Judge
    limit: int | None
    exclude_flagged: bool
    model: str
    thinking_level: str
    limits: Settings
    judged: bool = True
    """False with `--skip-judge`: only the published tools are compared."""
    judge_error: str | None = None


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _rate_text(rate: Rate) -> str:
    if rate.value is None:
        return "n/a"
    return f"{rate.value:.1%} ({rate.numerator:g}/{rate.denominator})"


def _tokens(result: ItemResult, key: str) -> int:
    return int(result.usage.get(key, 0))


def _judge_calls(results: Sequence[ItemResult]) -> tuple[int, int]:
    """Judge comparisons (golden comments times candidates) and deduplication calls."""
    reviewed = [r for r in results if r.reviewed]
    pairs = sum(len(r.item.comments) * len(r.candidates) for r in reviewed)
    dedup = sum(1 for r in reviewed if len(r.candidates) >= DEDUP_MIN_CANDIDATES)
    return pairs, dedup


def _limit_lines(settings: Settings) -> list[str]:
    defaults = Settings.model_fields
    lines = ["| Setting | Default | This run |", "| --- | --- | --- |"]
    for name in (*RAISED_LIMITS, *KEPT_LIMITS):
        lines.append(f"| `{name}` | {defaults[name].default:,} | {getattr(settings, name):,} |")
    return lines


def _judge_text(info: RunInfo) -> str:
    published = f"`offline/results/{info.judge.published_dir}/`"
    if not info.judged:
        return (
            f"not run (`--skip-judge`); published results by `{info.judge.model}` from {published}"
        )
    text = (
        f"`{info.judge.model}` through `{info.judge.base_url}`; CodeAtlas's results in "
        f"`offline/results/{model_dir_name(info.judge.model)}/`, published results from {published}"
    )
    return text + (f". FAILED: {_cell(info.judge_error)}" if info.judge_error else "")


def render_report(
    info: RunInfo, results: Sequence[ItemResult], tables: Mapping[str, Sequence[Totals]]
) -> str:
    day = info.started_at[:10]
    flagged = [r for r in results if r.item.flagged]
    reviewed = [r for r in results if r.reviewed]
    lines = [
        "# Code Review Bench evaluation",
        "",
        f"- Started: {info.started_at}; duration {info.seconds:.0f} s",
        f"- Benchmark: `{BENCH_REPOSITORY}` at `{info.bench_commit}`; golden comments from "
        + " and ".join(f"`offline/golden_comments/{name}`" for name in GOLDEN_FILES),
        f"- Items: {len(reviewed)} reviewed of {len(results)} selected ({len(flagged)} flagged)"
        + (f"; limit {info.limit}" if info.limit is not None else "")
        + ("; flagged items excluded" if info.exclude_flagged else ""),
        f"- Review model: `{info.model}` (thinking `{info.thinking_level}`), prompt "
        f"`{PROMPT_VERSION}`; one candidate per CodeAtlas risk, tool name `{TOOL}`",
        f"- Judge: {_judge_text(info)}",
        "",
        "## Repository limits",
        "",
        "The repository limits were raised for this process only; the per-file size limit and "
        "the review limits are the application's.",
        "",
        *_limit_lines(info.limits),
        "",
        "## Comparison",
        "",
        "For every tool, true positives (golden comments matched), false positives (candidates "
        "matching no golden comment), and false negatives are summed over the same pull "
        "requests, golden comments of every category, as the benchmark's step 3 sums them. "
        "Precision is TP / (TP + FP) and recall TP / (TP + FN). Pull requests whose CodeAtlas "
        "review failed or was not fully judged are left out for every tool and listed under "
        "Failures. PRs counts the compared pull requests that each tool has a result for.",
        "",
        "Some published evaluations were made against an older version of the golden comments "
        "(their matched and missed golden comments differ from the current ones). Tools with any "
        "such evaluation are listed in a separate table, with the number of those pull requests "
        "under Older; their numbers do not measure the same labels.",
        "",
    ]
    for name, rows in tables.items():
        current = [row for row in rows if not row.older]
        older = [row for row in rows if row.older]
        lines += [
            f"### {TABLE_TITLES.get(name, name)}",
            "",
            "| Tool | PRs | TP | FP | FN | Precision | Recall |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            *(
                f"| {row.tool} | {row.prs} | {row.tp} | {row.fp} | {row.fn} "
                f"| {_rate_text(row.precision)} | {_rate_text(row.recall)} |"
                for row in current
            ),
            "",
        ]
        if older:
            lines += [
                "Judged against an older version of the golden comments on some pull requests:",
                "",
                "| Tool | PRs | Older | TP | FP | FN | Precision | Recall |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
                *(
                    f"| {row.tool} | {row.prs} | {row.older} | {row.tp} | {row.fp} | {row.fn} "
                    f"| {_rate_text(row.precision)} | {_rate_text(row.recall)} |"
                    for row in older
                ),
                "",
            ]
    lines += [
        "## Flagged items",
        "",
        *(
            [
                f"- {r.item.url}: {_cell(r.item.az_comment)}"
                + (f" (original: {r.item.original_url})" if r.item.original_url else "")
                for r in flagged
            ]
            or ["None."]
        ),
        "",
        "## Per-item results",
        "",
        "| Pull request | Flagged | Files reviewed | Partial | Risks | Golden | TP | FP | FN "
        "| Seconds | Input tokens | Output tokens | Thinking tokens | Est. cost | Failure |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- "
        "| --- |",
    ]
    for r in results:
        evaluation = r.evaluation if r.reviewed and r.judge_failure is None else None
        counts = (
            " | ".join(str(int(evaluation.get(key) or 0)) for key in ("tp", "fp", "fn"))
            if evaluation is not None
            else " |  | "
        )
        failure = "; ".join([*r.failures, *([r.judge_failure] if r.judge_failure else [])])
        lines.append(
            f"| {r.item.url} | {'yes' if r.item.flagged else ''} "
            f"| {f'{r.reviewed_files}/{r.changed_files}' if r.reviewed else ''} "
            f"| {'yes' if r.partial else ''} | {len(r.risks) if r.reviewed else ''} "
            f"| {len(r.item.comments)} | {counts} | {r.seconds:.1f} "
            f"| {_tokens(r, 'input_tokens')} | {_tokens(r, 'output_tokens')} "
            f"| {_tokens(r, 'thinking_tokens')} | ${review_cost(r.usage, day):.3f} "
            f"| {_cell(failure)} |"
        )
    pairs, dedup = _judge_calls(results)
    _, input_price, output_price = next(price for price in PRICES if day >= price[0])
    total = sum(review_cost(r.usage, day) for r in results)
    timed = [r.seconds for r in reviewed]
    lines += [
        "",
        "## Cost",
        "",
        f"Review model: {sum(_tokens(r, 'model_calls') for r in results)} calls; tokens "
        f"{sum(_tokens(r, 'input_tokens') for r in results)} input, "
        f"{sum(_tokens(r, 'output_tokens') for r in results)} output, "
        f"{sum(_tokens(r, 'thinking_tokens') for r in results)} thinking; estimated "
        f"${total:.2f} at ${input_price:.2f} input and ${output_price:.2f} output (thinking "
        "included) per million tokens (001 research R11). Review time: mean "
        f"{sum(timed) / len(timed) if timed else 0.0:.1f} s, max {max(timed, default=0.0):.1f} s.",
        "",
        f"Judge: {pairs} comparisons of a golden comment with a candidate, and {dedup} "
        "deduplication calls"
        + ("" if info.judged else " if the judge runs")
        + ". The judge's provider bills them; this runner does not measure their cost.",
        "",
        "## Caveats",
        "",
        *(f"- {caveat}" for caveat in CAVEATS),
        "",
        "## Failures",
        "",
        *(
            [
                f"- {r.item.url}: {_cell('; '.join(r.failures) or r.judge_failure or '')}"
                for r in results
                if r.failures or r.judge_failure
            ]
            or ["None."]
        ),
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def report_json(
    info: RunInfo, results: Sequence[ItemResult], tables: Mapping[str, Sequence[Totals]]
) -> dict[str, Any]:
    day = info.started_at[:10]
    pairs, dedup = _judge_calls(results)

    def row(totals: Totals) -> dict[str, Any]:
        return {
            "tool": totals.tool,
            "prs": totals.prs,
            "tp": totals.tp,
            "fp": totals.fp,
            "fn": totals.fn,
            "older": totals.older,
            "precision": totals.precision.value,
            "recall": totals.recall.value,
        }

    return {
        "started_at": info.started_at,
        "duration_seconds": round(info.seconds, 1),
        "benchmark": {
            "repository": BENCH_REPOSITORY,
            "commit": info.bench_commit,
            "golden_files": list(GOLDEN_FILES),
        },
        "selection": {
            "limit": info.limit,
            "exclude_flagged": info.exclude_flagged,
            "items": len(results),
        },
        "review_model": {
            "model": info.model,
            "thinking_level": info.thinking_level,
            "prompt_version": PROMPT_VERSION,
        },
        "judge": {
            "model": info.judge.model,
            "base_url": info.judge.base_url,
            "ran": info.judged,
            "error": info.judge_error,
            "results_dir": f"offline/results/{model_dir_name(info.judge.model)}",
            "published_dir": f"offline/results/{info.judge.published_dir}",
        },
        "limits": {
            name: {
                "default": Settings.model_fields[name].default,
                "used": getattr(info.limits, name),
            }
            for name in (*RAISED_LIMITS, *KEPT_LIMITS)
        },
        "comparison": {name: [row(totals) for totals in rows] for name, rows in tables.items()},
        "items": [
            {
                "url": r.item.url,
                "source_file": r.item.source_file,
                "flagged": r.item.flagged,
                "az_comment": r.item.az_comment,
                "original_url": r.item.original_url,
                "pull_request": (
                    {
                        "full_name": r.pull_request.full_name,
                        "base_sha": r.pull_request.base_sha,
                        "head_sha": r.pull_request.head_sha,
                        "merge_base_sha": r.pull_request.merge_base_sha,
                    }
                    if r.pull_request is not None
                    else None
                ),
                "quality_state": r.quality_state,
                "changed_files": r.changed_files,
                "reviewed_files": r.reviewed_files,
                "partial": r.partial,
                "golden_comments": len(r.item.comments),
                "candidates": [c.as_json() for c in r.candidates],
                "evaluation": (
                    {key: r.evaluation.get(key) for key in ("tp", "fp", "fn", "errors_count")}
                    if r.evaluation is not None
                    else None
                ),
                "seconds": round(r.seconds, 1),
                "usage": r.usage,
                "estimated_cost_usd": round(review_cost(r.usage, day), 4),
                "failures": r.failures,
                "judge_failure": r.judge_failure,
            }
            for r in results
        ],
        "cost": {
            "review_estimated_usd": round(sum(review_cost(r.usage, day) for r in results), 4),
            "judge_comparisons": pairs,
            "dedup_calls": dedup,
        },
        "caveats": list(CAVEATS),
    }


# --- Running ------------------------------------------------------------------------------------


class BenchSettings(BaseSettings):
    """Keys for this runner only, read like the application's from the environment or `.env`."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    github_token: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""


def _review_model(settings: Settings) -> AnswerModel:
    if settings.fake_externals:
        raise EvalError(
            "CODEATLAS_FAKE_EXTERNALS is on, so the run would not reach Gemini. Set "
            "CODEATLAS_FAKE_EXTERNALS=0."
        )
    if not settings.gemini_api_key:
        raise EvalError("Set GEMINI_API_KEY in the environment or .env (evals/README.md).")
    return GeminiAnswerModel(settings)


def _read_evaluations(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not an object keyed by golden URL")
    return data


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return path.name


def _progress(index: int, total: int, result: ItemResult) -> str:
    parts = [f"[{index}/{total}] {result.item.url}"]
    if result.reviewed:
        partial = " (partial)" if result.partial else ""
        parts.append(f"{result.reviewed_files}/{result.changed_files} files reviewed{partial}")
        parts.append(f"{len(result.risks)} risks")
    if result.failures:
        parts.append("FAILED: " + "; ".join(result.failures))
    parts.append(f"{result.seconds:.1f}s")
    return "  ".join(parts)


def run(args: argparse.Namespace) -> int:
    started_at = datetime.now(UTC)
    clock = time.monotonic()
    settings = get_settings()
    model = _review_model(settings)  # before any download, so a missing key fails fast
    judge = JUDGES[args.judge_model]
    keys = BenchSettings()
    api_key, uv = "", ""
    if not args.skip_judge:
        api_key = str(getattr(keys, judge.key_name.lower()))
        if not api_key:
            raise EvalError(f"Set {judge.key_name} for the judge, or pass --skip-judge.")
        uv = shutil.which("uv") or ""
        if not uv:
            raise EvalError("uv is not on PATH; the benchmark's steps run with it.")
    if not keys.github_token:
        print("GITHUB_TOKEN is not set: GitHub allows 60 unauthenticated requests an hour.")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    offline = download_benchmark(args.bench_commit, out_dir)
    try:
        golden = load_golden(offline / "golden_comments")
        published = _read_evaluations(
            offline / "results" / judge.published_dir / "evaluations.json"
        )
        items = select_items(
            golden.values(), exclude_flagged=args.exclude_flagged, limit=args.limit
        )
        data = json.loads((offline / "results" / "benchmark_data.json").read_text(encoding="utf-8"))
        for item in items:
            check_entry(data, item)
    except (OSError, ValueError) as exc:
        raise EvalError(str(exc)) from exc
    del data
    if not items:
        raise EvalError("No items are selected.")
    print(f"Selected {len(items)} pull requests ({sum(i.flagged for i in items)} flagged).")

    with github_client(keys.github_token) as client:
        pulls = {item.url: resolve_pull_request(client, item) for item in items}
    # Every archive is in the cache before the first model call.
    for item in items:
        pull = pulls[item.url]
        for sha in dict.fromkeys((pull.head_sha, pull.merge_base_sha)):
            print(f"Reading {pull.full_name} at {sha[:12]} ...", flush=True)
            download_archive(pull.full_name, sha)

    limits = raised_settings(settings)
    results: list[ItemResult] = []
    for index, item in enumerate(items, start=1):
        pull = pulls[item.url]
        result = review_archives(
            item,
            pull,
            download_archive(pull.full_name, pull.head_sha),
            download_archive(pull.full_name, pull.merge_base_sha),
            limits,
            model,
        )
        results.append(result)
        print(_progress(index, len(items), result), flush=True)

    exports = [
        Export(r.item.url, pulls[r.item.url].full_name, tuple(r.candidates))
        for r in results
        if r.reviewed
    ]
    created_at = started_at.isoformat(timespec="seconds")
    try:
        write_exports(offline, model_dir_name(judge.model), exports, golden, created_at)
    except (OSError, ValueError) as exc:
        raise EvalError(f"Writing the candidates failed: {exc}") from exc
    print(f"Wrote {len(exports)} reviews for the tool {TOOL} into the benchmark's results.")

    ours: dict[str, Any] | None = None
    judge_error: str | None = None
    if not args.skip_judge:
        try:
            run_judge(offline, judge, api_key, uv)
            ours = _read_evaluations(
                offline / "results" / model_dir_name(judge.model) / "evaluations.json"
            )
        except EvalError as exc:
            judge_error = str(exc)
        except (OSError, ValueError) as exc:
            judge_error = f"Reading the judge's evaluations failed: {exc}"
    if ours is not None:
        for result in results:
            if result.reviewed:
                result.evaluation = (ours.get(result.item.url) or {}).get(TOOL)
                result.judge_failure = judge_problem(result.evaluation)

    compared = [r.item for r in results if r.reviewed and (ours is None or r.judge_failure is None)]
    tables = comparisons(compared, published, ours)
    info = RunInfo(
        started_at=created_at,
        seconds=time.monotonic() - clock,
        bench_commit=args.bench_commit,
        judge=judge,
        limit=args.limit,
        exclude_flagged=args.exclude_flagged,
        model=settings.answer_model,
        thinking_level=settings.answer_thinking_level,
        limits=limits,
        judged=not args.skip_judge,
        judge_error=judge_error,
    )
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    report = out_dir / f"bench-eval-{stamp}.md"
    report.write_text(render_report(info, results, tables), encoding="utf-8")
    report_data = out_dir / f"bench-eval-{stamp}.json"
    report_data.write_text(
        json.dumps(report_json(info, results, tables), indent=2) + "\n", encoding="utf-8"
    )

    print()
    if ours is not None:
        ours_row = tables["all"][0]
        print(
            f"CodeAtlas on {ours_row.prs} pull requests: precision "
            f"{_rate_text(ours_row.precision)}, recall {_rate_text(ours_row.recall)}"
        )
    print(f"Report: {_display(report)}")
    print(f"Data: {_display(report_data)}")
    if judge_error is not None:
        print(f"error: {judge_error}", file=sys.stderr)
        return EXIT_SETUP
    return EXIT_DONE


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run_bench_eval",
        description="Review the Code Review Bench pull requests and judge the reviews with the "
        "benchmark's own steps.",
    )
    parser.add_argument(
        "--bench-commit",
        required=True,
        help="the benchmark commit to read, as a full 40-character SHA",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="directory for the benchmark copy and the reports (default: evals/out/bench)",
    )
    parser.add_argument(
        "--limit", type=int, help="review at most N pull requests, in file order (Sentry first)"
    )
    parser.add_argument(
        "--judge-model",
        default=DEFAULT_JUDGE,
        choices=sorted(JUDGES),
        help=f"the judge (default: {DEFAULT_JUDGE})",
    )
    parser.add_argument(
        "--exclude-flagged",
        action="store_true",
        help="skip the pull requests that the benchmark flags with a data warning",
    )
    parser.add_argument(
        "--skip-judge",
        action="store_true",
        help="review and export only; do not run the benchmark's steps 2.5 and 3",
    )
    args = parser.parse_args(argv)
    if not _FULL_SHA.fullmatch(args.bench_commit):
        parser.error("--bench-commit must be a full 40-character lowercase SHA")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(logging.WARNING)
    try:
        return run(args)
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_SETUP


if __name__ == "__main__":
    sys.exit(main())
