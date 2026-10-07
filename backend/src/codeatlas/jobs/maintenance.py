"""Periodic purge and retention (FR-035, research R17, data-model "Retention summary").

Ready versions are also capped per repository, and webhook deliveries expire after 14 days
(specs/002-push-reindexing, research R9). A repository whose access stays lost past the 7-day
grace period is disconnected at the start of a pass, so the same pass purges it (research R7).

Runs in the worker every 10 minutes. A transaction-level advisory lock makes sure only one
worker runs a pass at a time; others skip.

The worker also queues the daily access checks every 30 minutes, under their own advisory lock
(research R6).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import CursorResult, delete, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from codeatlas.db import session_scope
from codeatlas.jobs.queue import cancel_for_repository
from codeatlas.jobs.worker import register_periodic
from codeatlas.models import (
    AnalysisRun,
    IdempotencyRecord,
    Repository,
    Snapshot,
    UserSession,
    WebhookDelivery,
)
from codeatlas.workspace.access import GRACE_PERIOD
from codeatlas.workspace.audit import record
from codeatlas.workspace.sync import CHECK_SCHEDULER_INTERVAL, schedule_checks

logger = logging.getLogger(__name__)

INTERVAL = timedelta(minutes=10)
LOCK_KEY = 0x0C0DE_A7_1A5  # arbitrary, fixed advisory lock id for maintenance
INACTIVE_SNAPSHOT_RETENTION = timedelta(days=14)
FAILED_SNAPSHOT_RETENTION = timedelta(days=1)
# A ready version is removed early once this many newer ready versions of its repository exist.
MAX_SUPERSEDED_VERSIONS = 5
WEBHOOK_DELIVERY_RETENTION = timedelta(days=14)
GRACE_EXPIRED_CANCEL_MESSAGE = (
    "Canceled because the repository was disconnected after access to it was lost."
)


@dataclass(frozen=True)
class MaintenanceResult:
    repositories_disconnected_after_access_loss: int
    repositories_purged: int
    runs_expired: int
    snapshots_removed: int
    failed_snapshots_removed: int
    sessions_removed: int
    idempotency_records_removed: int
    webhook_deliveries_removed: int


def _count(result: object) -> int:
    return result.rowcount if isinstance(result, CursorResult) else 0


def _disconnect_lost(db: Session, now: datetime) -> int:
    """Tombstone repositories whose access stayed lost past the grace period (research R7).

    Each is disconnected as a user would, with no actor. Locked rows, such as one a run is
    restoring, are skipped until the next pass. Returns the count.
    """
    repositories = db.scalars(
        select(Repository)
        .where(
            Repository.deleted_at.is_(None),
            Repository.access_state == "access_lost",
            Repository.access_lost_at < now - GRACE_PERIOD,
        )
        .order_by(Repository.id)
        .with_for_update(skip_locked=True)
    ).all()
    for repository in repositories:
        repository.deleted_at = now
        cancel_for_repository(db, repository.id, message=GRACE_EXPIRED_CANCEL_MESSAGE)
        record(
            db,
            action="repository_disconnect",
            outcome="success",
            workspace_id=repository.workspace_id,
            resource_type="repository",
            resource_id=str(repository.id),
            detail={"reason": "access_lost"},
        )
    return len(repositories)


def _purge(db: Session, now: datetime) -> MaintenanceResult:
    # Grace expiry comes first, so the purge below removes those repositories in this pass.
    disconnected = _disconnect_lost(db, now)
    # Disconnected repositories: deleting the row cascades to snapshots, files, chunks, jobs,
    # runs, and evidence. Audit events are kept.
    repositories = db.execute(delete(Repository).where(Repository.deleted_at.is_not(None)))

    runs = db.execute(delete(AnalysisRun).where(AnalysisRun.expires_at < now))

    referenced = exists().where(
        AnalysisRun.snapshot_id == Snapshot.id, AnalysisRun.expires_at >= now
    )
    active = exists().where(Repository.active_snapshot_id == Snapshot.id)
    # Rank every ready version, the active one included, newest first within its repository.
    # A rank above the cap means at least MAX_SUPERSEDED_VERSIONS newer ready versions exist.
    ready = aliased(Snapshot)
    rank = func.row_number().over(
        partition_by=ready.repository_id, order_by=(ready.ready_at.desc(), ready.id.desc())
    )
    ranked = select(ready.id, rank.label("rank")).where(ready.status == "ready").subquery()
    superseded = select(ranked.c.id).where(ranked.c.rank > MAX_SUPERSEDED_VERSIONS)
    snapshots = db.execute(
        delete(Snapshot).where(
            Snapshot.status == "ready",
            or_(
                Snapshot.created_at < now - INACTIVE_SNAPSHOT_RETENTION,
                Snapshot.id.in_(superseded),
            ),
            ~active,
            ~referenced,
        )
    )
    failed = db.execute(
        delete(Snapshot).where(
            Snapshot.status.in_(("failed", "discarded")),
            Snapshot.created_at < now - FAILED_SNAPSHOT_RETENTION,
        )
    )
    sessions = db.execute(
        delete(UserSession).where(
            or_(UserSession.expires_at < now, UserSession.revoked_at.is_not(None))
        )
    )
    idempotency = db.execute(delete(IdempotencyRecord).where(IdempotencyRecord.expires_at < now))
    deliveries = db.execute(
        delete(WebhookDelivery).where(
            WebhookDelivery.received_at < now - WEBHOOK_DELIVERY_RETENTION
        )
    )
    return MaintenanceResult(
        repositories_disconnected_after_access_loss=disconnected,
        repositories_purged=_count(repositories),
        runs_expired=_count(runs),
        snapshots_removed=_count(snapshots),
        failed_snapshots_removed=_count(failed),
        sessions_removed=_count(sessions),
        idempotency_records_removed=_count(idempotency),
        webhook_deliveries_removed=_count(deliveries),
    )


def run_maintenance(*, now: datetime | None = None) -> MaintenanceResult | None:
    """Run one pass; returns None when another worker holds the lock."""
    now = now or datetime.now(UTC)
    with session_scope() as db:
        if not db.scalar(select(func.pg_try_advisory_xact_lock(LOCK_KEY))):
            return None
        result = _purge(db, now)
    logger.info("maintenance pass finished: %s", result)
    return result


def _periodic() -> None:
    run_maintenance()


def _schedule_checks() -> None:
    with session_scope() as db:
        queued = schedule_checks(db, now=datetime.now(UTC))
    logger.info("daily check pass queued %s runs", queued)


register_periodic("maintenance", INTERVAL, _periodic)
register_periodic("access_checks", CHECK_SCHEDULER_INTERVAL, _schedule_checks)
