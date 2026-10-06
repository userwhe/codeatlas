"""Access-state transitions for repositories (specs/002-push-reindexing data model, research R7
and R8).

`access_state` is `active`, `paused` (automatic updates stop; reads continue), or `access_lost`
(content reads are denied until access returns or the grace period ends). Loss takes precedence
over a pause. Every transition locks the repository row and rereads it, so detections from runs
and change notifications apply one at a time. The row lock comes before any job lock, as in
`disconnect`. None of these functions commit.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.api.errors import ApiError
from codeatlas.jobs.queue import cancel_for_repository
from codeatlas.models import JOB_TRIGGERS, Repository
from codeatlas.workspace.audit import record

GRACE_PERIOD = timedelta(days=7)
LOSS_REASONS = (
    "repository_not_visible",
    "app_not_installed",
    "installation_not_accessible",
    "installation_cannot_read",
    "app_uninstalled",
    "app_suspended",
    "repository_removed_from_installation",
)
PAUSE_REASONS = ("sign_in_required", "external_processing_not_accepted")
ACCESS_LOST_CANCEL_MESSAGE = "Canceled because access to the repository was lost."


def _lock(db: Session, repository: Repository) -> None:
    """Lock the repository row, then reread it. The lock query flushes pending changes first,
    so the reread keeps them.
    """
    db.execute(select(Repository.id).where(Repository.id == repository.id).with_for_update())
    db.refresh(repository)


def _audit(
    db: Session,
    repository: Repository,
    *,
    action: str,
    outcome: str,
    detail: dict[str, Any],
    actor_user_id: uuid.UUID | None = None,
) -> None:
    record(
        db,
        action=action,
        outcome=outcome,
        workspace_id=repository.workspace_id,
        actor_user_id=actor_user_id,
        resource_type="repository",
        resource_id=str(repository.id),
        detail=detail,
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def mark_access_lost(
    db: Session,
    repository: Repository,
    reason: str,
    *,
    trigger: str,
    now: datetime | None = None,
) -> bool:
    """Record a definitive loss of access (research R4, R7); returns True on the transition.

    A later detection keeps the first `access_lost_at` and reason, so the grace period is not
    extended. `access_checked_at` is set only when a run found the loss (`trigger` is a job
    trigger), not for a change notification. Waiting jobs are canceled on every detection.
    """
    if reason not in LOSS_REASONS:
        raise ValueError(f"Not an access-loss reason: {reason!r}")
    current = now if now is not None else datetime.now(UTC)
    _lock(db, repository)
    transitioned = repository.access_state != "access_lost"
    if transitioned:
        repository.access_state = "access_lost"
        repository.access_reason = reason
        repository.access_lost_at = current
    if trigger in JOB_TRIGGERS:
        repository.access_checked_at = current
    cancel_for_repository(db, repository.id, message=ACCESS_LOST_CANCEL_MESSAGE)
    if transitioned:
        _audit(
            db,
            repository,
            action="repository_access_lost",
            outcome="denied",
            detail={"reason": reason, "trigger": trigger},
        )
    return transitioned


def restore_access(
    db: Session, repository: Repository, *, trigger: str, now: datetime | None = None
) -> bool:
    """Record a passed access check (research R6, R7); returns True when it restores access.

    Always sets `access_checked_at`. A lost repository becomes `active`; a paused one stays
    paused.
    """
    _lock(db, repository)
    repository.access_checked_at = now if now is not None else datetime.now(UTC)
    if repository.access_state != "access_lost":
        return False
    repository.access_state = "active"
    repository.access_reason = None
    repository.access_lost_at = None
    _audit(
        db,
        repository,
        action="repository_access_restored",
        outcome="success",
        detail={"trigger": trigger},
    )
    return True


def pause(db: Session, repository: Repository, reason: str) -> bool:
    """Stop automatic updates (research R8); returns True on the transition.

    A lost repository stays lost. A paused repository takes the new reason without a new audit
    event.
    """
    if reason not in PAUSE_REASONS:
        raise ValueError(f"Not a pause reason: {reason!r}")
    _lock(db, repository)
    if repository.access_state == "access_lost":
        return False
    transitioned = repository.access_state == "active"
    repository.access_state = "paused"
    repository.access_reason = reason
    if transitioned:
        _audit(
            db,
            repository,
            action="automatic_updates_paused",
            outcome="success",
            detail={"reason": reason},
        )
    return transitioned


def resume(
    db: Session,
    repository: Repository,
    *,
    via: Literal["sign_in", "acceptance"],
    actor_user_id: uuid.UUID | None,
) -> bool:
    """Restart automatic updates after the owner signs in or accepts the disclosure (R8).

    Returns True on the transition; a repository that is not paused is unchanged. The pause
    reason is not checked: callers choose the repositories that `via` resolves.
    """
    _lock(db, repository)
    if repository.access_state != "paused":
        return False
    repository.access_state = "active"
    repository.access_reason = None
    _audit(
        db,
        repository,
        action="automatic_updates_resumed",
        outcome="success",
        detail={"via": via},
        actor_user_id=actor_user_id,
    )
    return True


def purge_after(repository: Repository) -> datetime | None:
    """When a lost repository's grace period ends, or None while access is not lost."""
    if repository.access_lost_at is None:
        return None
    return repository.access_lost_at + GRACE_PERIOD


def ensure_readable(repository: Repository) -> None:
    """Raise 403 `repository_access_lost` for a lost repository (FR-014, research R7).

    Check workspace scope first: other workspaces still get 404. Paused repositories stay
    readable.
    """
    lost_at = repository.access_lost_at
    if repository.access_state != "access_lost" or lost_at is None:
        return
    raise ApiError(
        403,
        "repository_access_lost",
        "CodeAtlas can no longer read this repository on GitHub.",
        details={
            "repository_id": str(repository.id),
            "reason": repository.access_reason,
            "lost_at": _iso(lost_at),
            "purge_after": _iso(lost_at + GRACE_PERIOD),
        },
    )
