"""The alert conditions that the platform cannot see, and the worker's reporter.

They follow specs/004-pilot-deployment, research R10, and data-model.md ("Alert conditions"):

- Each condition is a pure function of plain values, read from the database at a given time by
  `read_condition`. `GET /status/{condition}` answers 503 while it fails, and an external monitor
  alerts on that.
- The worker's reporter thread records the process's heartbeat in `worker_heartbeats`, which the
  `worker` condition reads, and logs the queue in a `worker_status` line (research R9).
"""

import logging
import os
import socket
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import FrameClause, FrameClauseType, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from codeatlas.config import get_settings
from codeatlas.db import session_scope
from codeatlas.jobs.queue import counted_failure, runnable_waiting
from codeatlas.models import WAITING_JOB_STATUSES, Backup, Job, WorkerHeartbeat

logger = logging.getLogger(__name__)

CONDITIONS = ("worker", "jobs-waiting", "jobs-failing", "backup")
# The failing-jobs window: counted failures within 60 minutes, ending at one of the last hour.
FAILURE_WINDOW = timedelta(minutes=60)
# Rows of replaced replicas are deleted once silent this long.
HEARTBEAT_RETENTION = timedelta(hours=24)
# When this process started, near enough: the worker imports this module as it starts.
PROCESS_STARTED_AT = datetime.now(UTC)


@dataclass(frozen=True)
class Condition:
    """A condition's state, as `GET /status/{condition}` reports it."""

    name: str
    failing: bool
    # Seconds, hours, or a count, by condition (contracts/http-api.md); None when nothing is
    # recorded yet.
    value: float | None
    threshold: int


# Conditions ------------------------------------------------------------------------------------


def worker_condition(
    newest_beat_at: datetime | None, now: datetime, silent_seconds: int
) -> Condition:
    """Fails when no worker reported within `silent_seconds`. Value: seconds since the newest
    heartbeat, or None without one.
    """
    if newest_beat_at is None:
        return Condition("worker", failing=True, value=None, threshold=silent_seconds)
    silence = now - newest_beat_at
    return Condition(
        "worker",
        failing=silence > timedelta(seconds=silent_seconds),
        value=silence.total_seconds(),
        threshold=silent_seconds,
    )


def jobs_waiting_condition(
    oldest_due_at: datetime | None, now: datetime, alert_seconds: int
) -> Condition:
    """Fails when the oldest runnable job became due more than `alert_seconds` ago. Value: seconds
    since it became due, 0 without one.
    """
    if oldest_due_at is None:
        return Condition("jobs-waiting", failing=False, value=0, threshold=alert_seconds)
    waiting = max(now - oldest_due_at, timedelta(0))
    return Condition(
        "jobs-waiting",
        failing=waiting > timedelta(seconds=alert_seconds),
        value=waiting.total_seconds(),
        threshold=alert_seconds,
    )


def jobs_failing_condition(
    failures: Sequence[tuple[datetime, int]], now: datetime, alert_count: int
) -> Condition:
    """Fails when a counted failure of the last hour ends a run of `alert_count` within 60 minutes.

    `failures` holds the counted failures' finish times, each with the number of counted failures
    finished in the 60 minutes up to and including it. Value: the largest such number among the
    failures of the last hour, 0 without one. The condition therefore holds for an hour after the
    failure that completes a run, so a check every 3 minutes sees it (research R10).
    """
    value = max(
        (in_window for finished_at, in_window in failures if finished_at >= now - FAILURE_WINDOW),
        default=0,
    )
    return Condition(
        "jobs-failing", failing=value >= alert_count, value=value, threshold=alert_count
    )


def backup_condition(
    newest_completed_at: datetime | None, now: datetime, missing_hours: int
) -> Condition:
    """Fails when no backup completed within `missing_hours`. Value: hours since the newest
    backup, or None without one.
    """
    if newest_completed_at is None:
        return Condition("backup", failing=True, value=None, threshold=missing_hours)
    age = now - newest_completed_at
    return Condition(
        "backup",
        failing=age > timedelta(hours=missing_hours),
        value=age / timedelta(hours=1),
        threshold=missing_hours,
    )


# Readers ---------------------------------------------------------------------------------------


def newest_heartbeat(db: Session) -> datetime | None:
    return db.scalar(select(func.max(WorkerHeartbeat.beat_at)))


def oldest_runnable_due(db: Session, now: datetime) -> datetime | None:
    """When the oldest job that `claim_next` could claim at `now` became due."""
    return db.scalar(select(func.min(Job.run_after)).where(runnable_waiting(now)))


def counted_failures(db: Session, now: datetime) -> list[tuple[datetime, int]]:
    """The counted failures that finished in the last two hours, oldest first, each with the
    number of counted failures finished in the 60 minutes up to and including it.

    Two hours hold the whole window of every failure of the last hour.
    """
    # RANGE BETWEEN INTERVAL '60 minutes' PRECEDING AND CURRENT ROW.
    window = FrameClause(FAILURE_WINDOW, None, FrameClauseType.PRECEDING, FrameClauseType.CURRENT)
    in_window = func.count().over(order_by=Job.finished_at, range_=window)
    rows = db.execute(
        select(Job.finished_at, in_window)
        .where(
            Job.status == "failed",
            counted_failure(),
            Job.finished_at >= now - 2 * FAILURE_WINDOW,
        )
        .order_by(Job.finished_at)
    )
    return [(finished_at, count) for finished_at, count in rows if finished_at is not None]


def newest_backup(db: Session) -> datetime | None:
    return db.scalar(select(func.max(Backup.completed_at)))


def read_condition(db: Session, name: str, now: datetime) -> Condition:
    """Read the condition `name`, one of `CONDITIONS`, with the thresholds from the settings."""
    settings = get_settings()
    if name == "worker":
        return worker_condition(newest_heartbeat(db), now, settings.worker_silent_seconds)
    if name == "jobs-waiting":
        return jobs_waiting_condition(
            oldest_runnable_due(db, now), now, settings.jobs_waiting_alert_seconds
        )
    if name == "jobs-failing":
        return jobs_failing_condition(
            counted_failures(db, now), now, settings.jobs_failing_alert_count
        )
    if name == "backup":
        return backup_condition(newest_backup(db), now, settings.backup_missing_hours)
    raise ValueError(f"unknown condition {name!r}")


# The worker's reporter -------------------------------------------------------------------------


def worker_id() -> str:
    """This process's row in `worker_heartbeats`: `<hostname>:<pid>`."""
    return f"{socket.gethostname()}:{os.getpid()}"


def report_once(*, now: datetime | None = None) -> None:
    """Record this worker's heartbeat, delete rows silent for 24 hours, and log the queue.

    The `worker_status` line holds the waiting jobs (`queued` or `retry_wait`) and the age of the
    oldest runnable one, which is 0 when there is none.
    """
    current = now if now is not None else datetime.now(UTC)
    with session_scope() as db:
        upsert = insert(WorkerHeartbeat).values(
            worker_id=worker_id(), started_at=PROCESS_STARTED_AT, beat_at=current
        )
        db.execute(
            upsert.on_conflict_do_update(
                index_elements=[WorkerHeartbeat.worker_id], set_={"beat_at": current}
            )
        )
        db.execute(
            delete(WorkerHeartbeat).where(WorkerHeartbeat.beat_at < current - HEARTBEAT_RETENTION)
        )
        queued = db.scalar(
            select(func.count()).select_from(Job).where(Job.status.in_(WAITING_JOB_STATUSES))
        )
        oldest = oldest_runnable_due(db, current)
    age = max((current - oldest).total_seconds(), 0.0) if oldest is not None else 0
    logger.info(
        "worker_status",
        extra={"fields": {"queued_jobs": queued or 0, "oldest_runnable_job_age_seconds": age}},
    )


def _report(interval: float, stop: threading.Event) -> None:
    while True:
        try:
            report_once()
        except Exception:
            logger.exception("worker status report failed")
        if stop.wait(interval):
            return


def start_reporter(interval: float) -> threading.Event:
    """Report every `interval` seconds from a daemon thread until the returned event is set.

    The job loop runs one job at a time, and an indexing job may take 15 minutes, so the reports
    cannot wait for it (research R10).
    """
    stop = threading.Event()
    threading.Thread(
        target=_report, args=(interval, stop), name="status-reporter", daemon=True
    ).start()
    return stop
