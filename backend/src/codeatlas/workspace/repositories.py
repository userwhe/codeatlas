"""Connecting, re-indexing, and looking up repositories within a workspace (US1, research R5)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from codeatlas.api.errors import ApiError, not_found
from codeatlas.auth.github_login import get_user_token
from codeatlas.config import get_settings
from codeatlas.github.gateway import (
    GitHubAccessDenied,
    GitHubNotFound,
    GitHubRepository,
    GitHubUnavailable,
    get_gateway,
    verify_access,
)
from codeatlas.jobs.queue import cancel_for_repository, enqueue, index_dedupe_key
from codeatlas.models import ACTIVE_JOB_STATUSES, Job, Repository, Snapshot, User, Workspace
from codeatlas.workspace.access import ensure_readable, resume
from codeatlas.workspace.audit import deny, record

INDEX_JOB = "index_repository"

RepositoryState = Literal["access_lost", "indexing", "ready", "rejected", "failed"]


def github_unavailable() -> ApiError:
    return ApiError(
        502, "github_unavailable", "GitHub is unavailable. Try again shortly.", retryable=True
    )


def sign_in_again() -> ApiError:
    """The stored GitHub authorization is missing, expired, or unreadable."""
    return ApiError(401, "github_sign_in_required", "Sign in with GitHub again.")


def external_processing_not_accepted() -> ApiError:
    """A private repository needs the external processing disclosure accepted (001 FR-006)."""
    return ApiError(
        422,
        "external_processing_not_accepted",
        "Private repositories require accepting that selected source excerpts, pull request "
        "descriptions, and pull request changes may be sent to an external model provider.",
    )


@dataclass(frozen=True)
class Connectable:
    repository: GitHubRepository
    connected_repository_id: uuid.UUID | None


def list_connectable(db: Session, user: User, workspace: Workspace) -> list[Connectable]:
    """Repositories the user can reach through installations of the App (connect dialog)."""
    gateway = get_gateway()
    try:
        token = get_user_token(db, user, gateway)
        repositories = gateway.list_accessible_repositories(token)
    except GitHubAccessDenied as exc:
        raise sign_in_again() from exc
    except GitHubUnavailable as exc:
        raise github_unavailable() from exc
    rows = db.execute(
        select(Repository.github_repository_id, Repository.id).where(
            Repository.workspace_id == workspace.id, Repository.deleted_at.is_(None)
        )
    )
    connected = {github_id: repository_id for github_id, repository_id in rows}
    return [Connectable(repo, connected.get(repo.id)) for repo in repositories]


def connect(
    db: Session,
    *,
    user: User,
    workspace: Workspace,
    github_repository_id: int,
    branch: str | None,
    accept_external_processing: bool,
    request_id: str | None,
) -> tuple[Repository, Job]:
    """Connect a repository and queue its first indexing job. Three GitHub calls (SC-007).

    The caller commits.
    """
    gateway = get_gateway()

    def denied() -> ApiError:
        return deny(
            actor_user_id=user.id,
            workspace_id=workspace.id,
            resource_type="github_repository",
            resource_id=str(github_repository_id),
            request_id=request_id,
        )

    try:
        token = get_user_token(db, user, gateway)
    except GitHubAccessDenied as exc:
        raise sign_in_again() from exc
    except GitHubUnavailable as exc:
        raise github_unavailable() from exc
    # Every GitHub call happens before the workspace lock below: a denial is audited in its own
    # transaction, which must not wait on a lock this request holds.
    try:
        github_repo, installation_id = verify_access(gateway, token, github_repository_id)
    except (GitHubNotFound, GitHubAccessDenied) as exc:
        raise denied() from exc
    except GitHubUnavailable as exc:
        raise github_unavailable() from exc

    if github_repo.private and not accept_external_processing:
        raise external_processing_not_accepted()

    # Serialize connections per workspace so the repository limit holds (FR-009). FOR NO KEY
    # UPDATE still lets other transactions insert rows that reference the workspace.
    db.execute(
        select(Workspace.id).where(Workspace.id == workspace.id).with_for_update(key_share=True)
    )
    active = select(Repository).where(
        Repository.workspace_id == workspace.id, Repository.deleted_at.is_(None)
    )
    if db.scalar(active.where(Repository.github_repository_id == github_repository_id)):
        raise ApiError(409, "already_connected", "This repository is already connected.")
    count = db.scalar(select(func.count()).select_from(active.subquery())) or 0
    limit = get_settings().max_repositories_per_workspace
    if count >= limit:
        raise ApiError(
            409,
            "repository_limit_reached",
            f"A workspace can connect at most {limit} repositories.",
            details={"limit": limit},
        )

    now = datetime.now(UTC)
    repository = Repository(
        workspace_id=workspace.id,
        github_repository_id=github_repo.id,
        github_installation_id=installation_id,
        full_name=github_repo.full_name,
        default_branch=github_repo.default_branch,
        is_private=github_repo.private,
        external_processing_accepted_at=now if github_repo.private else None,
        created_by=user.id,
    )
    db.add(repository)
    db.flush()
    selected_branch = branch or github_repo.default_branch
    job, _ = enqueue(
        db,
        workspace_id=workspace.id,
        kind=INDEX_JOB,
        repository_id=repository.id,
        created_by=user.id,
        payload={"branch": selected_branch},
        dedupe_key=index_dedupe_key(repository.id, selected_branch),
    )
    record(
        db,
        action="repository_connect",
        outcome="success",
        workspace_id=workspace.id,
        actor_user_id=user.id,
        resource_type="repository",
        resource_id=str(repository.id),
        request_id=request_id,
        detail={"github_repository_id": github_repo.id},
    )
    return repository, job


def reindex(
    db: Session,
    *,
    user: User,
    workspace: Workspace,
    repository_id: uuid.UUID,
    branch: str | None,
    request_id: str | None,
    accept_external_processing: bool = False,
) -> Job:
    """Queue indexing without calling GitHub; the job re-checks access (FR-003, FR-014).

    Allowed for a lost repository: the job's access check restores it if access has returned
    (research R7). A repository paused because it became private needs the disclosure accepted:
    the acceptance is recorded and automatic updates resume (FR-017, research R8). Acceptance
    is ignored otherwise, and a pause for sign-in is resumed only by signing in.
    """
    repository = get_scoped(
        db,
        user=user,
        workspace=workspace,
        repository_id=repository_id,
        request_id=request_id,
        content=False,
    )
    if repository.access_state == "paused":
        # Decide on the locked row: every pause and resume takes this lock, so the reason read
        # below cannot change before `resume`.
        db.execute(select(Repository.id).where(Repository.id == repository.id).with_for_update())
        db.refresh(repository)
    if _paused_for_disclosure(repository):
        if not accept_external_processing:
            raise external_processing_not_accepted()
        repository.external_processing_accepted_at = datetime.now(UTC)
        resume(db, repository, via="acceptance", actor_user_id=user.id)
    selected_branch = branch or repository.default_branch
    job, _ = enqueue(
        db,
        workspace_id=workspace.id,
        kind=INDEX_JOB,
        repository_id=repository.id,
        created_by=user.id,
        payload={"branch": selected_branch},
        dedupe_key=index_dedupe_key(repository.id, selected_branch),
    )
    return job


def _paused_for_disclosure(repository: Repository) -> bool:
    return (
        repository.access_state == "paused"
        and repository.access_reason == "external_processing_not_accepted"
    )


def disconnect(
    db: Session,
    *,
    user: User,
    workspace: Workspace,
    repository_id: uuid.UUID,
    request_id: str | None,
) -> None:
    """Tombstone a repository: reads stop at once, queued work is canceled, and running work
    cannot publish (its fenced publish checks the tombstone). Data is purged by maintenance.
    The caller commits (FR-034). Allowed for a lost repository.
    """
    repository = get_scoped(
        db,
        user=user,
        workspace=workspace,
        repository_id=repository_id,
        request_id=request_id,
        content=False,
    )
    db.execute(select(Repository.id).where(Repository.id == repository.id).with_for_update())
    repository.deleted_at = datetime.now(UTC)
    cancel_for_repository(db, repository.id)
    record(
        db,
        action="repository_disconnect",
        outcome="success",
        workspace_id=workspace.id,
        actor_user_id=user.id,
        resource_type="repository",
        resource_id=str(repository.id),
        request_id=request_id,
    )


def get_scoped(
    db: Session,
    *,
    user: User,
    workspace: Workspace,
    repository_id: uuid.UUID,
    request_id: str | None,
    content: bool = True,
) -> Repository:
    """Return a repository of this workspace; anything else is a 404 (FR-004).

    With `content`, the caller reads the repository's content (versions, answers), so a lost
    repository is a 403 `repository_access_lost` (FR-014). Pass `content=False` for the
    repository row itself: its detail, re-indexing, and disconnecting.
    """
    repository = db.get(Repository, repository_id)
    if repository is None:
        raise not_found()
    if repository.workspace_id != workspace.id or repository.deleted_at is not None:
        raise deny(
            actor_user_id=user.id,
            workspace_id=workspace.id,
            resource_type="repository",
            resource_id=str(repository_id),
            request_id=request_id,
        )
    if content:
        ensure_readable(repository)
    return repository


def get_scoped_snapshot(
    db: Session,
    *,
    user: User,
    workspace: Workspace,
    snapshot_id: uuid.UUID,
    request_id: str | None,
    require_ready: bool = True,
) -> Snapshot:
    """Return a snapshot whose repository belongs to this workspace, is connected, and is
    readable (FR-004, FR-014).
    """
    snapshot = db.get(Snapshot, snapshot_id)
    if snapshot is None:
        raise not_found()
    repository = db.get(Repository, snapshot.repository_id)
    if (
        snapshot.workspace_id != workspace.id
        or repository is None
        or repository.deleted_at is not None
    ):
        raise deny(
            actor_user_id=user.id,
            workspace_id=workspace.id,
            resource_type="snapshot",
            resource_id=str(snapshot_id),
            request_id=request_id,
        )
    ensure_readable(repository)
    if require_ready and snapshot.status != "ready":
        raise ApiError(409, "snapshot_not_ready", "This indexed version is not ready.")
    return snapshot


def latest_indexing_job(db: Session, repository_id: uuid.UUID) -> Job | None:
    return db.scalar(
        select(Job)
        .where(Job.repository_id == repository_id, Job.kind == INDEX_JOB)
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(1)
    )


def latest_failed_indexing_job(db: Session, repository_id: uuid.UUID) -> Job | None:
    return db.scalar(
        select(Job)
        .where(Job.repository_id == repository_id, Job.kind == INDEX_JOB, Job.status == "failed")
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(1)
    )


def derive_state(
    repository: Repository, latest_job: Job | None, latest_failed_job: Job | None = None
) -> RepositoryState:
    """Display state from data-model.md: `access_lost` first, then `indexing`, `ready`,
    `rejected`, and `failed`.

    Without a ready version, the latest failed run decides between `rejected` and `failed`: a
    later check that found its commit already settled ("Already up to date") changes nothing.
    """
    if repository.access_state == "access_lost":
        return "access_lost"
    if latest_job is not None and latest_job.status in ACTIVE_JOB_STATUSES:
        return "indexing"
    if repository.active_snapshot_id is not None:
        return "ready"
    failed = latest_failed_job or latest_job
    if failed is not None and failed.error_code == "limit_exceeded":
        return "rejected"
    return "failed"
