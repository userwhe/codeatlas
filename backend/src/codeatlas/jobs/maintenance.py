"""Periodic purge and retention (FR-035, research R17, data-model "Retention summary").

Runs in the worker every 10 minutes. A transaction-level advisory lock makes sure only one
worker runs a pass at a time; others skip.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import CursorResult, delete, exists, func, or_, select
from sqlalchemy.orm import Session

from codeatlas.db import session_scope
from codeatlas.jobs.worker import register_periodic
from codeatlas.models import (
    AnalysisRun,
    IdempotencyRecord,
    Repository,
    Snapshot,
    UserSession,
)

logger = logging.getLogger(__name__)

INTERVAL = timedelta(minutes=10)
LOCK_KEY = 0x0C0DE_A7_1A5  # arbitrary, fixed advisory lock id for maintenance
INACTIVE_SNAPSHOT_RETENTION = timedelta(days=14)
FAILED_SNAPSHOT_RETENTION = timedelta(days=1)


@dataclass(frozen=True)
class MaintenanceResult:
    repositories_purged: int
    runs_expired: int
    snapshots_removed: int
    failed_snapshots_removed: int
    sessions_removed: int
    idempotency_records_removed: int


def _count(result: object) -> int:
    return result.rowcount if isinstance(result, CursorResult) else 0


def _purge(db: Session, now: datetime) -> MaintenanceResult:
    # Disconnected repositories: deleting the row cascades to snapshots, files, chunks, jobs,
    # runs, and evidence. Audit events are kept.
    repositories = db.execute(delete(Repository).where(Repository.deleted_at.is_not(None)))

    runs = db.execute(delete(AnalysisRun).where(AnalysisRun.expires_at < now))

    referenced = exists().where(
        AnalysisRun.snapshot_id == Snapshot.id, AnalysisRun.expires_at >= now
    )
    active = exists().where(Repository.active_snapshot_id == Snapshot.id)
    snapshots = db.execute(
        delete(Snapshot).where(
            Snapshot.status == "ready",
            Snapshot.created_at < now - INACTIVE_SNAPSHOT_RETENTION,
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
    return MaintenanceResult(
        repositories_purged=_count(repositories),
        runs_expired=_count(runs),
        snapshots_removed=_count(snapshots),
        failed_snapshots_removed=_count(failed),
        sessions_removed=_count(sessions),
        idempotency_records_removed=_count(idempotency),
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


register_periodic("maintenance", INTERVAL, _periodic)
