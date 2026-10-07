"""Open pull requests of a connected repository, read live from GitHub (specs/003-pr-review,
research R1 and R10), with the state of each one's latest review.

Pull requests are not stored. Every read uses the signed-in owner's user token, so GitHub answers
only what the owner can read. A refusal here never changes the repository's access state: loss is
decided by the next access check.
"""

import base64
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from codeatlas.api.errors import ApiError
from codeatlas.auth.github_login import get_user_token
from codeatlas.github.gateway import (
    AppCredentialsRejected,
    GitHubAccessDenied,
    GitHubGateway,
    GitHubNotFound,
    GitHubUnavailable,
    PullRequest,
    UserAuthorizationInvalid,
)
from codeatlas.models import AnalysisRun, Job, Repository, User, Workspace
from codeatlas.workspace import repositories as repos
from codeatlas.workspace.repositories import github_unavailable, sign_in_again

ANALYSIS_KIND = "pull_request_review"
PULL_REQUESTS_PERMISSION = "pull_requests"

ReviewState = Literal["queued", "running", "current", "outdated", "failed"]


@dataclass(frozen=True)
class LatestReview:
    """The newest review of a pull request, with its state for the pull request's current head."""

    run_id: uuid.UUID
    state: ReviewState
    head_sha: str
    created_at: datetime


@dataclass(frozen=True)
class OpenPullRequest:
    pull_request: PullRequest
    review: LatestReview | None


@dataclass(frozen=True)
class OpenPullRequests:
    items: list[OpenPullRequest]
    next_cursor: str | None


def github_access_denied() -> ApiError:
    """GitHub refused a read for a reason other than a missing permission (research R1)."""
    return ApiError(
        409,
        "github_access_denied",
        "GitHub refused to show this repository's pull requests.",
    )


def refuse_rejected(db: Session, repository: Repository) -> None:
    """Raise 409 `repository_rejected` for a repository rejected for a size limit (FR-003)."""
    latest = repos.latest_indexing_job(db, repository.id)
    failed = (
        repos.latest_failed_indexing_job(db, repository.id)
        if repository.active_snapshot_id is None
        else None
    )
    if repos.derive_state(repository, latest, failed) == "rejected":
        raise ApiError(
            409,
            "repository_rejected",
            "This repository was rejected because it exceeds a size limit.",
        )


def user_token(db: Session, user: User, gateway: GitHubGateway) -> str:
    """The user's GitHub token: 401 when it must be renewed by signing in, 502 when GitHub is
    unreachable.
    """
    try:
        return get_user_token(db, user, gateway)
    except GitHubAccessDenied as exc:
        raise sign_in_again() from exc
    except GitHubUnavailable as exc:
        raise github_unavailable() from exc


def read(
    db: Session, *, user: User, repository: Repository, number: int, gateway: GitHubGateway
) -> PullRequest:
    """One pull request, read with the user token. 404 `pull_request_not_found` when GitHub has
    no such pull request in the repository.
    """
    token = user_token(db, user, gateway)
    try:
        return gateway.get_pull_request(token, repository.full_name, number)
    except UserAuthorizationInvalid as exc:
        raise sign_in_again() from exc
    except GitHubNotFound as exc:
        raise ApiError(
            404, "pull_request_not_found", "This repository has no such pull request."
        ) from exc
    except GitHubAccessDenied as exc:
        raise github_access_denied() from exc
    except GitHubUnavailable as exc:
        raise github_unavailable() from exc


def _refusal(gateway: GitHubGateway, repository: Repository) -> ApiError:
    """Classify a refused list (research R1): a missing Pull requests permission links to the
    installation's settings, where the account owner approves it. Anything else is a plain denial.
    """
    try:
        installation = gateway.get_installation_permissions(repository.full_name)
    except (GitHubNotFound, GitHubAccessDenied, AppCredentialsRejected):
        return github_access_denied()
    except GitHubUnavailable:
        return github_unavailable()
    if PULL_REQUESTS_PERMISSION not in installation.permissions:
        return ApiError(
            409,
            "pull_requests_permission_missing",
            "The CodeAtlas GitHub App needs the Pull requests (read-only) permission on this "
            "repository. Ask the account owner to approve it.",
            details={"settings_url": installation.html_url},
        )
    return github_access_denied()


def encode_cursor(page: int) -> str:
    return base64.urlsafe_b64encode(f"p:{page}".encode()).decode()


def decode_cursor(cursor: str | None) -> int:
    """The GitHub page number of a cursor; page 1 without one."""
    if cursor is None:
        return 1
    try:
        prefix, value = base64.urlsafe_b64decode(cursor.encode()).decode().split(":", 1)
        page = int(value)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(422, "invalid_cursor", "The cursor is invalid.") from exc
    if prefix != "p" or page < 1:
        raise ApiError(422, "invalid_cursor", "The cursor is invalid.")
    return page


def review_state(job_status: str | None, head_sha: str, current_head_sha: str) -> ReviewState:
    """The state shown for a pull request's newest review (data-model.md)."""
    if job_status in ("running", "retry_wait"):
        return "running"
    if job_status == "succeeded":
        return "current" if head_sha == current_head_sha else "outdated"
    if job_status in ("failed", "canceled"):
        return "failed"
    return "queued"


def latest_reviews(
    db: Session, repository_id: uuid.UUID, heads: dict[int, str]
) -> dict[int, LatestReview]:
    """The newest review of each pull request, by number, in one query. `heads` maps each number
    to the pull request's current head commit.
    """
    if not heads:
        return {}
    rows = db.execute(
        select(
            AnalysisRun.pull_request_number,
            AnalysisRun.id,
            AnalysisRun.head_sha,
            AnalysisRun.created_at,
            Job.status,
        )
        .outerjoin(Job, Job.id == AnalysisRun.job_id)
        .where(
            AnalysisRun.repository_id == repository_id,
            AnalysisRun.kind == ANALYSIS_KIND,
            AnalysisRun.pull_request_number.in_(list(heads)),
        )
        .order_by(
            AnalysisRun.pull_request_number,
            AnalysisRun.created_at.desc(),
            AnalysisRun.id.desc(),
        )
        .ext(distinct_on(AnalysisRun.pull_request_number))
    ).all()
    latest = {}
    for number, run_id, head_sha, created_at, job_status in rows:
        # The kind's check constraint makes both columns non-null for reviews.
        key, pinned = cast(int, number), cast(str, head_sha)
        latest[key] = LatestReview(
            run_id=run_id,
            state=review_state(job_status, pinned, heads[key]),
            head_sha=pinned,
            created_at=created_at,
        )
    return latest


def list_open(
    db: Session,
    *,
    user: User,
    workspace: Workspace,
    repository_id: uuid.UUID,
    cursor: str | None,
    gateway: GitHubGateway,
    request_id: str | None,
) -> OpenPullRequests:
    """One page of the repository's open pull requests, drafts included, most recently updated
    first, each with its latest review (FR-001, FR-022).
    """
    page = decode_cursor(cursor)
    repository = repos.get_scoped(
        db,
        user=user,
        workspace=workspace,
        repository_id=repository_id,
        request_id=request_id,
        content=True,
    )
    refuse_rejected(db, repository)
    token = user_token(db, user, gateway)
    try:
        listed = gateway.list_pull_requests(token, repository.full_name, page)
    except UserAuthorizationInvalid as exc:
        raise sign_in_again() from exc
    except (GitHubAccessDenied, GitHubNotFound) as exc:
        raise _refusal(gateway, repository) from exc
    except GitHubUnavailable as exc:
        raise github_unavailable() from exc

    reviews = latest_reviews(
        db, repository.id, {pull.number: pull.head_sha for pull in listed.items}
    )
    items = [
        OpenPullRequest(pull_request=pull, review=reviews.get(pull.number)) for pull in listed.items
    ]
    return OpenPullRequests(
        items=items,
        next_cursor=encode_cursor(listed.next_page) if listed.next_page is not None else None,
    )
