"""The `review_pull_request` job: check access, pin the merge base, read both commits, review the
change, and publish (specs/003-pr-review, research R2, R3, and R7 to R9).

`analyze` reviews two commit trees that are already read, with no database or GitHub access, so
the evaluation runner can call it as the job does. `handle_review` does the rest: the access
check, the comparison, the archives, and the fenced publish.

Stage messages and data hold counts only: never paths, names, or source text (research R9).
"""

import gzip
import tarfile
import uuid
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from codeatlas.config import Settings, get_settings
from codeatlas.db import session_scope
from codeatlas.github.gateway import (
    CommitUnavailable,
    Comparison,
    GitHubGateway,
    GitHubNotFound,
    GitHubRepository,
    NoCommonHistory,
    get_gateway,
    verify_access,
)
from codeatlas.ingestion.extract import LimitExceeded
from codeatlas.jobs.github_access import (
    ACCESS_LOST_MESSAGE,
    DISCONNECTED_MESSAGE,
    cancel_run,
    github_answers,
    owner_token,
    publishable_repository,
)
from codeatlas.jobs.queue import JobFailure, cancel, fenced
from codeatlas.jobs.worker import JobContext, register
from codeatlas.models import AnalysisRun, EvidenceItem, Repository
from codeatlas.providers.answer_model import AnswerModel, ReviewResult, get_answer_model
from codeatlas.providers.errors import ProviderRefused, ProviderUnavailable
from codeatlas.review.context import (
    candidate_tests,
    changed_declarations,
    changed_tests,
    module_names,
    related_code,
)
from codeatlas.review.diff import Skip, Tree, changed_files, read_tree, select_for_review
from codeatlas.review.evidence import ReviewEvidence, ReviewEvidenceSet, build_evidence
from codeatlas.review.prompt import SYSTEM_PROMPT, build_repair_content, build_user_content
from codeatlas.review.result import build_result, nothing_to_review_result, rule_risks
from codeatlas.review.schema import ReviewOutput
from codeatlas.review.validate import drop_invalid, usable, validate
from codeatlas.workspace.access import pause
from codeatlas.workspace.quotas import refund_review

JOB_KIND = "review_pull_request"
MISSING_RUN_MESSAGE = "The review no longer exists."
NO_SUMMARY_ERROR = "The review has no summary point. Summarize the change, citing changed lines."

QualityState = Literal["reviewed", "nothing_to_review"]
Progress = Callable[[str, str, dict[str, Any]], None]
"""Reports a stage: its name, a message, and counts. The job passes `JobContext.event`."""


@dataclass
class Usage:
    """Model calls and tokens, as for answers, plus the model items dropped by validation."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0
    omitted_items: int = 0

    def add(self, result: ReviewResult) -> None:
        self.calls += 1
        self.input_tokens += result.usage.input_tokens
        self.output_tokens += result.usage.output_tokens
        self.thinking_tokens += result.usage.thinking_tokens
        self.cached_tokens += result.usage.cached_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "cached_tokens": self.cached_tokens,
            "omitted_items": self.omitted_items,
        }


@dataclass(frozen=True)
class Analysis:
    """A finished review: the stored result, the evidence it may cite, and what it cost."""

    result: dict[str, Any]
    evidence: tuple[ReviewEvidence, ...]
    usage: Usage
    quality_state: QualityState


@dataclass(frozen=True)
class Pinned:
    """The pull request as its submission pinned it (research R2)."""

    run_id: uuid.UUID
    base_sha: str
    head_sha: str
    pull_request: dict[str, Any]

    @property
    def head_owner(self) -> str | None:
        """The owner of a fork's head repository. A fork's head commits are compared in the
        base repository, naming that owner."""
        if not self.pull_request.get("is_fork"):
            return None
        return str(self.pull_request.get("head_repository") or "").split("/", 1)[0] or None


@dataclass(frozen=True)
class Source:
    """Where this attempt reads both commits: the repository as the access check found it."""

    full_name: str
    installation_id: int


# The review itself -----------------------------------------------------------------------------


def _no_progress(stage: str, message: str, data: dict[str, Any]) -> None:
    pass


def _call(model: AnswerModel, content: str) -> ReviewResult:
    try:
        return model.review(system=SYSTEM_PROMPT, user_content=content)
    except ProviderUnavailable as exc:
        raise JobFailure(
            "provider_unavailable",
            "The review service is unavailable. Try again later.",
            permanent=False,
            retryable=True,
        ) from exc
    except ProviderRefused as exc:
        raise JobFailure(
            "model_refused",
            "The review service declined this pull request.",
            permanent=True,
            retryable=False,
        ) from exc


def _validation_failed() -> JobFailure:
    # Permanent for the job, as for answers; the user may request a new review (research R7).
    return JobFailure(
        "review_validation_failed",
        "The review could not be verified against the pull request's changes.",
        permanent=True,
        retryable=True,
    )


def _generate(
    model: AnswerModel,
    content: str,
    evidence: ReviewEvidenceSet,
    reviewed_paths: frozenset[str],
    usage: Usage,
    progress: Progress,
) -> ReviewOutput:
    """At most two model calls: the review and one repair (research R7).

    Items that still fail validation after the repair are dropped and counted in `usage`. The
    review fails when the repair does not parse, or when no overview or summary point is left.
    """
    labels, change_labels = evidence.labels, evidence.change_labels
    progress(
        "generating_review",
        f"Generating the review from {len(evidence.items)} evidence items",
        {"evidence_items": len(evidence.items), "context_items": evidence.context_items},
    )
    result = _call(model, content)
    usage.add(result)

    progress("validating_citations", "Checking every citation against the evidence", {})
    errors = validate(
        result.output,
        labels=labels,
        change_labels=change_labels,
        reviewed_paths=reviewed_paths,
        parse_error=result.parse_error,
    )
    if result.output is not None and not errors:
        if usable(result.output):
            return result.output
        errors = [NO_SUMMARY_ERROR]

    repair = _call(model, build_repair_content(content, result.output, errors))
    usage.add(repair)
    if repair.output is None:
        raise _validation_failed()
    output, usage.omitted_items = drop_invalid(
        repair.output, labels=labels, change_labels=change_labels, reviewed_paths=reviewed_paths
    )
    if not usable(output):
        raise _validation_failed()
    return output


def analyze(
    pull_request: Mapping[str, Any],
    head: Tree,
    base: Tree,
    rename_hints: Mapping[str, str],
    *,
    head_sha: str,
    merge_base_sha: str,
    settings: Settings,
    model: AnswerModel,
    progress: Progress | None = None,
) -> Analysis:
    """Review the change from `base` (the merge base) to `head`, without the database or GitHub.

    `pull_request` is the stored copy, of which the title and description are used. `progress`
    receives the `comparing`, `gathering_context`, `generating_review`, and
    `validating_citations` stages. Provider errors and a review that fails validation raise
    `JobFailure`.

    The model is not called when nothing can be reviewed (`nothing_to_review`), or when every
    reviewed file was renamed without changes: no line changed, so there is nothing to cite, and
    the rule points and risks, with the changed and candidate tests, are the whole review.
    """
    report = progress or _no_progress
    report(
        "comparing",
        f"Comparing {len(head.hashes)} files at the head with the merge base",
        {"head_files": len(head.hashes)},
    )
    changed = changed_files(head, base, rename_hints)
    selection = select_for_review(
        changed,
        max_files=settings.review_max_files,
        max_changed_lines=settings.review_max_changed_lines,
        max_hunks=settings.review_max_hunks,
        max_diff_tokens=settings.review_max_diff_tokens,
    )
    risks = rule_risks(selection)
    tests_changed = changed_tests(changed)
    changed_count = sum(entry.count for entry in selection.coverage)
    report(
        "gathering_context",
        f"Gathering context for {len(selection.reviewed)} of {changed_count} changed files",
        {"changed_files": changed_count, "reviewed_files": len(selection.reviewed)},
    )
    usage = Usage()
    if not selection.reviewed:
        return Analysis(
            result=nothing_to_review_result(selection, risks, changed_tests=tests_changed),
            evidence=(),
            usage=usage,
            quality_state="nothing_to_review",
        )

    # Every changed file is passed, so a changed file left out by the limits is never shown as
    # unchanged related code or as a candidate test.
    names = changed_declarations(selection.reviewed)
    modules = module_names(selection.reviewed)
    candidates = candidate_tests(head, changed, names, modules)
    if not any(file.hunks for file in selection.reviewed):
        # The overview is model text, so it stays empty; the server writes the rule points.
        rules_only = ReviewOutput.model_construct(overview="", summary_points=[], risks=[])
        return Analysis(
            result=build_result(
                rules_only,
                evidence=(),
                selection=selection,
                rule_risks=risks,
                omitted_items=0,
                changed_tests=tests_changed,
                candidate_tests=candidates,
            ),
            evidence=(),
            usage=usage,
            quality_state="reviewed",
        )

    related = related_code(head, changed, names, modules)
    evidence = build_evidence(
        selection,
        related,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        max_input_tokens=settings.review_max_input_tokens,
        tests=[candidate.excerpt for candidate in candidates if candidate.excerpt is not None],
    )
    content = build_user_content(
        title=str(pull_request.get("title") or ""),
        description=pull_request.get("body"),
        selection=selection,
        evidence=evidence,
        candidate_tests=candidates,
    )
    reviewed_paths = frozenset(
        path for file in selection.reviewed for path in (file.path, file.previous_path) if path
    )
    output = _generate(model, content, evidence, reviewed_paths, usage, report)
    result = build_result(
        output,
        evidence=evidence.items,
        selection=selection,
        rule_risks=risks,
        omitted_items=usage.omitted_items,
        changed_tests=tests_changed,
        candidate_tests=candidates,
    )
    # The result also counts any checklist item left with no reviewed file and no risk.
    usage.omitted_items = result["omitted_items"]
    return Analysis(result=result, evidence=evidence.items, usage=usage, quality_state="reviewed")


# The job ---------------------------------------------------------------------------------------


def _disclosure_not_accepted() -> JobFailure:
    # The same code as indexing (002 research R8), with the next step for a review. Retrying
    # cannot help until the owner accepts the disclosure.
    return JobFailure(
        "external_processing_not_accepted",
        "The repository became private. Accept that selected source excerpts, pull request "
        "descriptions, and pull request changes may be sent to an external model provider, then "
        "request the review again.",
        permanent=True,
    )


def _commit_unavailable() -> JobFailure:
    return JobFailure(
        "commit_unavailable",
        "GitHub no longer serves a commit of this pull request, for example after a force push. "
        "Request a new review of the current head.",
        permanent=True,
    )


def _no_common_history() -> JobFailure:
    return JobFailure(
        "no_common_history",
        "The pull request's base and head share no history, so there is no change to review.",
        permanent=True,
    )


def _record_access(ctx: JobContext, github_repo: GitHubRepository) -> str | JobFailure | None:
    """Record what a passed access check says about the repository, in one fenced transaction.

    Refreshes `is_private`. A repository that turned private without an accepted disclosure is
    paused, and the run fails before any code is fetched (FR-003, FR-030). A disconnected or lost
    repository could not publish, so the run stops here instead of after the model call.

    Returns a cancellation message or a failure when the run must stop, else None. The caller
    raises the failure after this transaction commits, so the pause outlives the failed run.
    """
    with session_scope() as db:
        fenced(db, ctx.job_id, ctx.fencing_token)
        repository = db.get(Repository, ctx.repository_id, with_for_update=True)
        if repository is None or repository.deleted_at is not None:
            return DISCONNECTED_MESSAGE
        if repository.access_state == "access_lost":
            return ACCESS_LOST_MESSAGE
        repository.is_private = github_repo.private
        if repository.is_private and repository.external_processing_accepted_at is None:
            pause(db, repository, "external_processing_not_accepted")
            return _disclosure_not_accepted()
    return None


def _check_access(ctx: JobContext, gateway: GitHubGateway) -> Source | None:
    """The workspace owner's access check (FR-003, 002 research R4). Returns where to read the
    commits, or None when the run was canceled instead.

    GitHub is called outside any transaction, and its answers become failures as for indexing.
    """
    ctx.event("checking_access", "Checking access to the repository")
    owner = owner_token(ctx, gateway, stage="checking_access")
    if owner is None:
        cancel_run(ctx, DISCONNECTED_MESSAGE)
        return None
    with github_answers(ctx, stage="checking_access", denied=None, credential=owner.credential):
        github_repo, installation_id = verify_access(
            gateway, owner.token, owner.github_repository_id
        )
    stop = _record_access(ctx, github_repo)
    if isinstance(stop, JobFailure):
        raise stop
    if stop is not None:
        cancel_run(ctx, stop)
        return None
    return Source(full_name=github_repo.full_name, installation_id=installation_id)


def _compare(ctx: JobContext, gateway: GitHubGateway, source: Source, pinned: Pinned) -> Comparison:
    """Resolve the merge base and record it on the run, fenced (research R2).

    The comparison is read with the installation token after the access check passed, so a
    missing commit means the commit is gone, not that access was lost. A denial means the
    installation can no longer read the repository, as for indexing.
    """
    ctx.event("resolving_commits", "Finding the merge base of the pull request")
    with github_answers(ctx, stage="resolving_commits", denied="installation_cannot_read"):
        try:
            comparison = gateway.compare_commits(
                source.installation_id,
                source.full_name,
                pinned.base_sha,
                pinned.head_sha,
                head_owner=pinned.head_owner,
            )
        except (CommitUnavailable, GitHubNotFound) as exc:
            raise _commit_unavailable() from exc
        except NoCommonHistory as exc:
            raise _no_common_history() from exc
    with session_scope() as db:
        fenced(db, ctx.job_id, ctx.fencing_token)
        stored = db.get(AnalysisRun, pinned.run_id)
        if stored is not None:
            stored.merge_base_sha = comparison.merge_base_sha
    return comparison


def _read_commit(
    ctx: JobContext,
    gateway: GitHubGateway,
    source: Source,
    sha: str,
    settings: Settings,
    *,
    skip: Skip | None = None,
) -> Tree:
    """Download one commit archive and read it with 001's extraction and filters (research R3).

    As for the comparison, a missing archive means the commit is gone. Exceeding one of 001's
    limits fails the run as it fails indexing (FR-009).
    """
    with github_answers(ctx, stage="fetching_source", denied="installation_cannot_read"):
        try:
            with gateway.open_tarball(source.installation_id, source.full_name, sha) as stream:
                return read_tree(stream, settings, skip=skip)
        except GitHubNotFound as exc:
            raise _commit_unavailable() from exc
        except (tarfile.TarError, EOFError, zlib.error, gzip.BadGzipFile) as exc:
            raise JobFailure(
                "archive_unreadable", "The repository archive could not be read.", permanent=False
            ) from exc
        except LimitExceeded as exc:
            raise JobFailure(
                "limit_exceeded",
                f"The repository exceeds the limit {exc.limit_name} ({exc.limit_value}).",
                permanent=True,
            ) from exc


def _fetch(
    ctx: JobContext, gateway: GitHubGateway, source: Source, head_sha: str, merge_base_sha: str
) -> tuple[Tree, Tree]:
    """The head tree, then the merge-base tree without the members that the head has unchanged,
    so memory holds the head plus the changed files (research R3)."""
    ctx.event("fetching_source", "Downloading the head and merge-base commits")
    settings = get_settings()
    head = _read_commit(ctx, gateway, source, head_sha, settings)
    ctx.check()
    base = _read_commit(
        ctx,
        gateway,
        source,
        merge_base_sha,
        settings,
        skip=lambda path, sha256: head.hashes.get(path) == sha256,
    )
    ctx.check()
    return head, base


def _publish(ctx: JobContext, run_id: uuid.UUID, analysis: Analysis) -> None:
    """Write the evidence and the result with the job's completion, in one fenced transaction.

    A review with nothing to review gives its allowance back, for the day it was counted on
    (FR-020).
    """
    ctx.event("publishing", "Saving the review", {"evidence_items": len(analysis.evidence)})
    with ctx.publish() as (db, job):
        if publishable_repository(db, ctx, job) is None:
            return
        stored = db.get(AnalysisRun, run_id)
        if stored is None:
            cancel(db, job, message=MISSING_RUN_MESSAGE)
            return
        db.add_all(
            EvidenceItem(
                analysis_run_id=stored.id,
                label=item.label,
                source_type=item.source_type,
                side=item.side,
                path=item.path,
                commit_sha=item.commit_sha,
                start_line=item.start_line,
                end_line=item.end_line,
                excerpt=item.excerpt,
                excerpt_sha256=item.excerpt_sha256,
                rank=item.rank,
            )
            for item in analysis.evidence
        )
        stored.result = analysis.result
        stored.quality_state = analysis.quality_state
        stored.usage = analysis.usage.as_dict()
        stored.completed_at = datetime.now(UTC)
        if analysis.quality_state == "nothing_to_review":
            refund_review(db, stored.workspace_id, stored.created_at.astimezone(UTC).date())


def _pinned(ctx: JobContext) -> Pinned | str:
    """The pull request the run pinned, or the message to cancel the run with."""
    with session_scope() as db:
        repository = db.get(Repository, ctx.repository_id)
        if repository is None or repository.deleted_at is not None:
            return DISCONNECTED_MESSAGE
        run = db.get(AnalysisRun, ctx.analysis_run_id) if ctx.analysis_run_id else None
        if run is None or run.base_sha is None or run.head_sha is None:
            return MISSING_RUN_MESSAGE
        return Pinned(
            run_id=run.id,
            base_sha=run.base_sha,
            head_sha=run.head_sha,
            pull_request=dict(run.pull_request or {}),
        )


def handle_review(ctx: JobContext) -> None:
    pinned = _pinned(ctx)
    if isinstance(pinned, str):
        cancel_run(ctx, pinned)
        return
    gateway = get_gateway()
    source = _check_access(ctx, gateway)
    if source is None:
        return

    comparison = _compare(ctx, gateway, source, pinned)
    head, base = _fetch(ctx, gateway, source, pinned.head_sha, comparison.merge_base_sha)
    analysis = analyze(
        pinned.pull_request,
        head,
        base,
        comparison.renamed,
        head_sha=pinned.head_sha,
        merge_base_sha=comparison.merge_base_sha,
        settings=get_settings(),
        model=get_answer_model(),
        progress=ctx.event,
    )
    _publish(ctx, pinned.run_id, analysis)


register(JOB_KIND, handle_review)
