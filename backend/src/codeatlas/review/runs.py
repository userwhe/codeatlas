"""Requesting pull request reviews and reading them (specs/003-pr-review, research R2, R6, and R9).

A review is an analysis run of kind `pull_request_review`. Submission pins the pull request's base
and head commits; the review job resolves the merge base and reads both sides from GitHub.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.api.errors import ApiError, not_found
from codeatlas.config import get_settings
from codeatlas.github.gateway import GitHubGateway, PullRequest
from codeatlas.jobs.queue import enqueue
from codeatlas.models import AnalysisRun, EvidenceItem, Job, Repository, User, Workspace
from codeatlas.review import pulls
from codeatlas.review.prompt import PROMPT_VERSION
from codeatlas.workspace import repositories as repos
from codeatlas.workspace.access import ensure_readable
from codeatlas.workspace.audit import record
from codeatlas.workspace.quotas import reserve_review

ANALYSIS_KIND = pulls.ANALYSIS_KIND
JOB_KIND = "review_pull_request"
RUN_TTL = timedelta(days=30)
MAX_BODY_CHARS = 8000
# The pull request fields returned with a review; the stored description is for the prompt only.
PULL_REQUEST_OUT_FIELDS = (
    "title",
    "author",
    "draft",
    "base_ref",
    "head_ref",
    "head_repository",
    "is_fork",
    "html_url",
)


def _pull_request_record(pull: PullRequest) -> dict[str, Any]:
    """What a review keeps of its pull request (data-model.md). Title and description are
    untrusted text: shown as text and quoted in the prompt, never logged.
    """
    return {
        "title": pull.title,
        "body": pull.body[:MAX_BODY_CHARS],
        "author": pull.author,
        "base_ref": pull.base_ref,
        "head_ref": pull.head_ref,
        "head_repository": pull.head_repository,
        "is_fork": pull.is_fork,
        "draft": pull.draft,
        "html_url": pull.html_url,
        "additions": pull.additions,
        "deletions": pull.deletions,
        "changed_files": pull.changed_files,
    }


def submit(
    db: Session,
    *,
    user: User,
    workspace: Workspace,
    repository_id: uuid.UUID,
    pull_request_number: int,
    request_id: str | None,
    gateway: GitHubGateway,
) -> tuple[AnalysisRun, Job]:
    """Pin an open pull request, reserve a review, and queue the review job. The caller commits.

    Stored state is checked before GitHub is called, so a lost, rejected, or paused repository
    is refused without a GitHub call or any allowance used. The one GitHub call reads the pull
    request with the owner's user token (research R9).
    """
    repository = repos.get_scoped(
        db,
        user=user,
        workspace=workspace,
        repository_id=repository_id,
        request_id=request_id,
        content=True,
    )
    pulls.refuse_rejected(db, repository)
    if (
        repository.access_state == "paused"
        and repository.access_reason == "external_processing_not_accepted"
    ):
        raise repos.external_processing_not_accepted()
    pull = pulls.read(
        db, user=user, repository=repository, number=pull_request_number, gateway=gateway
    )
    if pull.state != "open":
        raise ApiError(409, "pull_request_not_open", "Only open pull requests can be reviewed.")

    # Serialize submissions for the repository, then recheck it: a disconnect or loss of access
    # may have been committed while GitHub was being read.
    db.execute(select(Repository.id).where(Repository.id == repository.id).with_for_update())
    db.refresh(repository)
    if repository.deleted_at is not None:
        raise not_found()
    ensure_readable(repository)

    reserve_review(db, workspace.id)
    settings = get_settings()
    now = datetime.now(UTC)
    run = AnalysisRun(
        workspace_id=workspace.id,
        repository_id=repository.id,
        kind=ANALYSIS_KIND,
        commit_sha=pull.head_sha,
        pull_request_number=pull.number,
        base_sha=pull.base_sha,
        head_sha=pull.head_sha,
        pull_request=_pull_request_record(pull),
        model=settings.answer_model,
        thinking_level=settings.answer_thinking_level,
        prompt_version=PROMPT_VERSION,
        created_by=user.id,
        created_at=now,
        expires_at=now + RUN_TTL,
    )
    db.add(run)
    db.flush()
    job, _ = enqueue(
        db,
        workspace_id=workspace.id,
        kind=JOB_KIND,
        repository_id=repository.id,
        analysis_run_id=run.id,
        created_by=user.id,
        dedupe_key=f"run:{run.id}",
    )
    run.job_id = job.id
    record(
        db,
        action="pull_request_review_submit",
        outcome="success",
        workspace_id=workspace.id,
        actor_user_id=user.id,
        resource_type="analysis_run",
        resource_id=str(run.id),
        request_id=request_id,
        detail={"pull_request_number": pull.number},
    )
    return run, job


def cited_labels(result: dict[str, Any]) -> list[str]:
    """Every evidence label a stored review result cites, in label order (`E1`, `E2`, ...)."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence_ids" and isinstance(item, list):
                    found.update(label for label in item if isinstance(label, str))
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(result)
    return sorted(found, key=lambda label: (len(label), label))


def github_url(full_name: str, item: EvidenceItem) -> str:
    """The cited lines on GitHub at the item's commit (research R6)."""
    return (
        f"https://github.com/{full_name}/blob/{item.commit_sha}/{quote(item.path)}"
        f"#L{item.start_line}-L{item.end_line}"
    )


def citations(db: Session, run: AnalysisRun) -> list[dict[str, Any]]:
    """The evidence items the review cites, built from stored evidence and never from model
    text (FR-016).
    """
    if run.result is None:
        return []
    labels = cited_labels(run.result)
    repository = db.get(Repository, run.repository_id)
    if not labels or repository is None:
        return []
    items = {
        item.label: item
        for item in db.scalars(
            select(EvidenceItem).where(
                EvidenceItem.analysis_run_id == run.id, EvidenceItem.label.in_(labels)
            )
        )
    }
    return [
        {
            "label": item.label,
            "source_type": item.source_type,
            "side": item.side,
            "path": item.path,
            "commit_sha": item.commit_sha,
            "start_line": item.start_line,
            "end_line": item.end_line,
            "excerpt": item.excerpt,
            "github_url": github_url(repository.full_name, item),
        }
        for label in labels
        if (item := items.get(label)) is not None
    ]


def _with_citations(value: Any) -> Any:
    """`value` with each `evidence_ids` key renamed `citations`, as the API returns them."""
    if isinstance(value, dict):
        return {
            ("citations" if key == "evidence_ids" else key): _with_citations(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_with_citations(item) for item in value]
    return value


def _review(result: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    """The `review` and `coverage` parts of the response for a stored result. Parts that a
    result predates are returned empty.
    """
    review = _with_citations(result)
    coverage = review.pop("coverage", None)
    return {
        "checklist": [],
        "tests": {"changed": [], "candidates": [], "new_cases": []},
        **review,
    }, coverage


def review_out(db: Session, run: AnalysisRun) -> dict[str, Any]:
    """The review response of contracts/http-api.md, from the database only (research R10).

    Review and coverage are null until the run succeeds; the merge base is null until the job
    resolves it.
    """
    job = db.get(Job, run.job_id) if run.job_id is not None else None
    repository = db.get(Repository, run.repository_id)
    if repository is None:
        raise not_found()
    stored = run.pull_request or {}
    review, coverage = _review(run.result) if run.result is not None else (None, None)
    error = None
    if job is not None and job.error_code is not None:
        error = {
            "code": job.error_code,
            "message": job.error_message or "",
            "retryable": bool(job.error_retryable),
        }
    return {
        "id": run.id,
        "kind": run.kind,
        "status": job.status if job is not None else "queued",
        "quality_state": run.quality_state,
        "repository_id": run.repository_id,
        "repository_full_name": repository.full_name,
        "pull_request": {
            "number": run.pull_request_number,
            **{field: stored.get(field) for field in PULL_REQUEST_OUT_FIELDS},
        },
        "commits": {
            "base_sha": run.base_sha,
            "head_sha": run.head_sha,
            "merge_base_sha": run.merge_base_sha,
        },
        "review": review,
        "coverage": coverage,
        "citations": citations(db, run),
        "error": error,
        "job_id": run.job_id,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
    }
