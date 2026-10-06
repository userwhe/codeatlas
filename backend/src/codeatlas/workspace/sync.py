"""Keeps connected repositories in step with GitHub (research R2, R3, R6, R7, R8).

`apply_event` applies verified webhook events. Handlers run inside the delivery's transaction
and touch only the database; they never call GitHub. The `detail` they return is stored on the
delivery row, so it must hold only safe fields such as counts and reasons.

`schedule_checks` queues the daily check, which catches what no notification reports.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import ColumnElement, exists, func, or_, select
from sqlalchemy.orm import Session

from codeatlas.auth.github_login import forget_github_credential
from codeatlas.github.webhooks import (
    AuthorizationRevoked,
    Ignored,
    InstallationLost,
    ParsedEvent,
    PushEvent,
    RepositoriesRemoved,
)
from codeatlas.jobs.queue import INDEX_JOB, request_automatic_run
from codeatlas.models import ACTIVE_JOB_STATUSES, Job, Membership, Repository, User
from codeatlas.workspace.access import mark_access_lost, pause

NOT_HANDLED = "not_handled"
NOT_CONNECTED = "not_connected"
UNKNOWN_USER = "unknown_user"
NOTIFICATION = "notification"

# A repository is checked again once its last access check is this old. The scheduler runs every
# CHECK_SCHEDULER_INTERVAL, so loss and missed pushes are found well within a day (SC-006,
# SC-009).
CHECK_INTERVAL = timedelta(hours=20)
CHECK_SCHEDULER_INTERVAL = timedelta(minutes=30)
CHECK_LOCK_KEY = 0x0C0DE_A7_C4EC  # arbitrary, fixed advisory lock id for the daily check


@dataclass(frozen=True)
class EventOutcome:
    outcome: Literal["processed", "ignored"]
    detail: dict[str, Any]


def apply_event(db: Session, event: ParsedEvent) -> EventOutcome:
    """Apply one parsed event and report whether it changed state or queued work."""
    # Pushes (R3), access loss (R7), and revoked authorizations (R8).
    match event:
        case PushEvent():
            return _apply_push(db, event)
        case InstallationLost():
            return _apply_installation_lost(db, event)
        case RepositoriesRemoved():
            return _apply_repositories_removed(db, event)
        case AuthorizationRevoked():
            return _apply_authorization_revoked(db, event)
        case Ignored(reason=reason):
            return EventOutcome("ignored", {"reason": reason})
        case _:
            return EventOutcome("ignored", {"reason": NOT_HANDLED})


def _lock_connected(db: Session, *conditions: ColumnElement[bool]) -> Sequence[Repository]:
    """Lock and return the connected repositories that match, in every workspace.

    Rows are locked in ID order, so concurrent deliveries that touch the same repositories
    apply one at a time.
    """
    return db.scalars(
        select(Repository)
        .where(*conditions, Repository.deleted_at.is_(None))
        .order_by(Repository.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()


def _apply_push(db: Session, event: PushEvent) -> EventOutcome:
    """Record a default-branch push on every connected copy of the repository, and request a
    `push` run for each copy whose automatic updates are not paused (research R3).

    Each workspace's copy is handled on its own (FR-010). A lost copy still gets a run, since
    the run's access check may restore it.
    """
    repositories = _lock_connected(
        db, Repository.github_repository_id == event.github_repository_id
    )
    if not repositories:
        return EventOutcome("ignored", {"reason": NOT_CONNECTED})
    received_at = datetime.now(UTC)
    for repository in repositories:
        repository.latest_push_sha = event.after
        repository.latest_push_at = received_at
        repository.default_branch = event.default_branch
        if repository.access_state == "paused":
            repository.latest_push_job_id = None
            continue
        job = request_automatic_run(
            db,
            repository=repository,
            branch=event.default_branch,
            trigger="push",
            pushed_commit_sha=event.after,
        )
        repository.latest_push_job_id = job.id
    return EventOutcome("processed", {"repositories": len(repositories)})


def _apply_installation_lost(db: Session, event: InstallationLost) -> EventOutcome:
    """Mark every connected repository under an uninstalled or suspended installation lost
    (research R7). Reads are denied as soon as the delivery commits (SC-005).
    """
    repositories = _lock_connected(
        db, Repository.github_installation_id == event.github_installation_id
    )
    return _mark_lost(db, repositories, event.reason)


def _apply_repositories_removed(db: Session, event: RepositoriesRemoved) -> EventOutcome:
    """Mark the listed repositories lost, if they are connected under that installation."""
    repositories = _lock_connected(
        db,
        Repository.github_installation_id == event.github_installation_id,
        Repository.github_repository_id.in_(event.github_repository_ids),
    )
    return _mark_lost(db, repositories, "repository_removed_from_installation")


def _mark_lost(db: Session, repositories: Sequence[Repository], reason: str) -> EventOutcome:
    """Record the loss on each repository and cancel its waiting jobs. Restoration happens only
    through a later run's access check, never through a notification (research R7).
    """
    if not repositories:
        return EventOutcome("ignored", {"reason": NOT_CONNECTED})
    for repository in repositories:
        mark_access_lost(db, repository, reason, trigger=NOTIFICATION)
    return EventOutcome("processed", {"repositories": len(repositories)})


def _apply_authorization_revoked(db: Session, event: AuthorizationRevoked) -> EventOutcome:
    """Pause the active repositories of the workspace the user owns until they sign in again,
    and delete their GitHub credential, which no longer works (research R8).

    Lost repositories stay lost. Sessions stay valid, so the owner sees the pause.
    """
    user = db.scalar(select(User).where(User.github_user_id == event.github_user_id))
    if user is None:
        return EventOutcome("ignored", {"reason": UNKNOWN_USER})
    forget_github_credential(db, user.id)
    owned = select(Membership.workspace_id).where(
        Membership.user_id == user.id, Membership.role == "owner"
    )
    repositories = _lock_connected(
        db, Repository.workspace_id.in_(owned), Repository.access_state == "active"
    )
    paused = sum(pause(db, repository, "sign_in_required") for repository in repositories)
    return EventOutcome("processed", {"repositories": paused})


def schedule_checks(db: Session, *, now: datetime) -> int:
    """Queue a `check` run on the default branch of every repository that is due (research R6).

    A repository is due when it is connected and not paused, its last access check is older than
    `CHECK_INTERVAL` or never happened, and no indexing job of it waits or runs. Lost
    repositories are checked too, so a restored access is found. Rows locked elsewhere, such as
    one a run is updating, wait for the next pass.

    A transaction-level advisory lock lets one runner work at a time; another returns 0 at once.
    Returns the number of repositories queued. Does not commit.
    """
    if not db.scalar(select(func.pg_try_advisory_xact_lock(CHECK_LOCK_KEY))):
        return 0
    busy = exists().where(
        Job.repository_id == Repository.id,
        Job.kind == INDEX_JOB,
        Job.status.in_(ACTIVE_JOB_STATUSES),
    )
    repositories = db.scalars(
        select(Repository)
        .where(
            Repository.deleted_at.is_(None),
            Repository.access_state != "paused",
            or_(
                Repository.access_checked_at.is_(None),
                Repository.access_checked_at < now - CHECK_INTERVAL,
            ),
            ~busy,
        )
        .order_by(Repository.id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    for repository in repositories:
        request_automatic_run(
            db, repository=repository, branch=repository.default_branch, trigger="check"
        )
    return len(repositories)
