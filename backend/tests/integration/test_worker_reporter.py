"""Integration tests for the worker's status reporter and job lines (T028, research R9 and R10).

The reporter records the worker's heartbeat in `worker_heartbeats`, which `GET /status/worker`
reads, and logs the queue in a `worker_status` line. Log lines are captured with a handler on the
root logger, because `configure_logging` binds its stream when it runs.
"""

import os
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import FrameType

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from codeatlas import status
from codeatlas.config import Settings
from codeatlas.jobs import queue, worker
from codeatlas.jobs.queue import JobFailure
from codeatlas.jobs.worker import JobContext
from codeatlas.models import Job, WorkerHeartbeat
from tests.integration.test_job_queue import add_job, claim, make_repository, reload
from tests.integration.test_logging import _Capture, _capture_logs

pytestmark = pytest.mark.integration


@pytest.fixture
def handlers(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, worker.Handler], None]:
    """Register handlers in a copy of the registry that is restored after the test."""
    worker.load_handlers()
    monkeypatch.setattr(worker, "_handlers", dict(worker._handlers))
    return worker.register


def heartbeats(db: Session) -> list[WorkerHeartbeat]:
    db.expire_all()
    return list(db.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.worker_id)))


def status_lines(captured: _Capture) -> list[dict[str, object]]:
    return [line for line in captured.json_lines if line["message"] == "worker_status"]


def job_lines(captured: _Capture) -> list[dict[str, object]]:
    return [line for line in captured.json_lines if line["message"] == "job_finished"]


def set_job(db: Session, job_id: uuid.UUID, **values: object) -> None:
    db.execute(update(Job).where(Job.id == job_id).values(**values))
    db.commit()


def counted(db: Session, job_id: uuid.UUID) -> bool:
    """Whether the failing-jobs check counts the job's failure (research R10)."""
    return db.scalar(select(Job.id).where(Job.id == job_id, queue.counted_failure())) is not None


# Heartbeats --------------------------------------------------------------------------------


def test_report_once_records_the_heartbeat_and_then_moves_only_beat_at(db: Session) -> None:
    now = datetime.now(UTC)

    status.report_once(now=now)
    status.report_once(now=now + timedelta(seconds=60))

    [row] = heartbeats(db)
    assert row.worker_id == f"{socket.gethostname()}:{os.getpid()}"
    assert row.started_at == status.PROCESS_STARTED_AT
    assert row.beat_at == now + timedelta(seconds=60)


def test_report_once_deletes_heartbeats_silent_for_more_than_24_hours(db: Session) -> None:
    now = datetime.now(UTC)
    for worker_id, age in (
        ("replaced:1", timedelta(hours=25)),
        ("draining:1", timedelta(hours=23)),
    ):
        db.add(WorkerHeartbeat(worker_id=worker_id, started_at=now - age, beat_at=now - age))
    db.commit()

    status.report_once(now=now)

    assert {row.worker_id for row in heartbeats(db)} == {
        "draining:1",
        f"{socket.gethostname()}:{os.getpid()}",
    }


# The worker_status line ------------------------------------------------------------------------


def test_report_once_logs_the_queue(db: Session) -> None:
    now = datetime.now(UTC)
    first = make_repository(db)
    second = make_repository(db, "hubot")
    # Created first, so neither is the oldest only by creation: one is not due yet, and the other
    # waits behind a running job of its workspace and kind.
    not_due = add_job(db, first, kind="review_pull_request")
    blocked = add_job(db, second, dedupe_key="index:blocked")
    running = add_job(db, second, dedupe_key="index:running")
    oldest = add_job(db, first)
    newer = add_job(db, first, kind="answer_question")
    finished = add_job(db, make_repository(db, "monalisa"))
    set_job(db, not_due.id, status="retry_wait", run_after=now + timedelta(minutes=1))
    set_job(db, blocked.id, run_after=now - timedelta(minutes=10))
    set_job(
        db, running.id, status="running", attempt=1, lease_expires_at=now + timedelta(minutes=1)
    )
    set_job(db, oldest.id, run_after=now - timedelta(seconds=42))
    set_job(db, newer.id, run_after=now - timedelta(seconds=5))
    set_job(db, finished.id, status="succeeded", finished_at=now)

    with _capture_logs() as captured:
        status.report_once(now=now)

    [line] = status_lines(captured)
    assert (line["logger"], line["queued_jobs"], line["oldest_runnable_job_age_seconds"]) == (
        "codeatlas.status",
        4,
        42.0,
    )


def test_report_once_logs_zero_age_without_a_runnable_job(db: Session) -> None:
    with _capture_logs() as captured:
        status.report_once()
        job = add_job(db, make_repository(db))
        set_job(db, job.id, status="retry_wait", run_after=datetime.now(UTC) + timedelta(minutes=1))
        status.report_once()

    assert [
        (line["queued_jobs"], line["oldest_runnable_job_age_seconds"])
        for line in status_lines(captured)
    ] == [(0, 0), (1, 0)]


# The reporter thread ---------------------------------------------------------------------------


def test_the_reporter_keeps_reporting_while_a_job_runs_and_stops_on_its_event(
    db: Session,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
) -> None:
    during: list[int] = []

    with _capture_logs() as captured:

        def handler(ctx: JobContext) -> None:
            # The job loop is blocked here, as during a long indexing job.
            before = len(status_lines(captured))
            time.sleep(0.5)
            during.append(len(status_lines(captured)) - before)

        handlers("index_repository", handler)
        add_job(db, make_repository(db))
        stop = status.start_reporter(0.1)
        try:
            assert run_worker_once()
        finally:
            stop.set()

        assert during[0] >= 3, during
        # A report already under way may still finish; nothing follows it.
        time.sleep(0.3)
        stopped = len(status_lines(captured))
        time.sleep(0.3)
        assert len(status_lines(captured)) == stopped
    assert len(heartbeats(db)) == 1


def test_the_worker_always_starts_the_reporter(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    intervals: list[float] = []
    reporter = threading.Event()
    signal_handlers: dict[int, Callable[[int, FrameType | None], None]] = {}

    def start_reporter(interval: float) -> threading.Event:
        intervals.append(interval)
        return reporter

    def stop_at_once() -> bool:
        signal_handlers[signal.SIGTERM](signal.SIGTERM, None)
        return False

    monkeypatch.setattr(worker, "start_reporter", start_reporter)
    monkeypatch.setattr(worker, "run_once", stop_at_once)
    monkeypatch.setattr(worker, "_periodic", {})
    monkeypatch.setattr(worker, "configure_logging", lambda: None)
    # Keep the process's own signal handlers.
    monkeypatch.setattr(worker.signal, "signal", signal_handlers.__setitem__)

    worker.main()

    assert intervals == [settings.worker_report_seconds] == [60]
    assert reporter.is_set()


# Job lines ---------------------------------------------------------------------------------


def test_a_succeeded_job_logs_its_outcome_and_duration(
    db: Session,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
) -> None:
    handlers("index_repository", lambda ctx: time.sleep(0.02))
    job = add_job(db, make_repository(db))

    with _capture_logs() as captured:
        assert run_worker_once()

    [line] = job_lines(captured)
    assert {key: line[key] for key in ("job_id", "kind", "outcome", "attempt")} == {
        "job_id": str(job.id),
        "kind": "index_repository",
        "outcome": "succeeded",
        "attempt": 1,
    }
    duration = line["duration_ms"]
    assert isinstance(duration, int) and duration >= 20


def test_a_retried_failure_logs_retry_wait(
    db: Session,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
) -> None:
    handlers("index_repository", _transient)
    job = add_job(db, make_repository(db))

    with _capture_logs() as captured:
        assert run_worker_once()

    row = reload(db, job.id)
    assert (row.status, row.error_permanent) == ("retry_wait", None)
    [line] = job_lines(captured)
    assert {key: line[key] for key in ("job_id", "kind", "outcome", "attempt")} == {
        "job_id": str(job.id),
        "kind": "index_repository",
        "outcome": "retry_wait",
        "attempt": 1,
    }
    assert isinstance(line["duration_ms"], int)


def _transient(ctx: JobContext) -> None:
    raise JobFailure("github_unavailable", "GitHub is unavailable.", permanent=False)


def _timeout(ctx: JobContext) -> None:
    raise queue.timeout_failure(ctx.kind)


def _unexpected(ctx: JobContext) -> None:
    raise RuntimeError("unexpected")


def _size_limit(ctx: JobContext) -> None:
    raise JobFailure("limit_exceeded", "The repository has too many files.", permanent=True)


# Handler, the job's error code, whether the failure was permanent, and whether it is counted.
FAILED_IN_THE_WORKER: dict[str, tuple[worker.Handler, str, bool, bool]] = {
    "no retry left": (_transient, "github_unavailable", False, True),
    "timeout": (_timeout, "timeout", True, True),
    "internal error": (_unexpected, "internal_error", False, True),
    "size limit": (_size_limit, "limit_exceeded", True, False),
}


@pytest.mark.parametrize(
    ("handler", "code", "permanent", "is_counted"),
    FAILED_IN_THE_WORKER.values(),
    ids=FAILED_IN_THE_WORKER.keys(),
)
def test_a_failed_job_logs_failed_and_counts_only_failures_that_matter(
    db: Session,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
    handler: worker.Handler,
    code: str,
    permanent: bool,
    is_counted: bool,
) -> None:
    handlers("index_repository", handler)
    job = add_job(db, make_repository(db))
    # The next attempt is the last one.
    set_job(db, job.id, attempt=2)

    with _capture_logs() as captured:
        assert run_worker_once()

    row = reload(db, job.id)
    assert (row.status, row.error_code, row.error_permanent) == ("failed", code, permanent)
    assert counted(db, job.id) is is_counted
    [line] = job_lines(captured)
    assert {key: line[key] for key in ("job_id", "kind", "outcome", "attempt")} == {
        "job_id": str(job.id),
        "kind": "index_repository",
        "outcome": "failed",
        "attempt": 3,
    }


def test_a_job_failed_at_claim_time_for_its_deadline_is_logged_and_counted(db: Session) -> None:
    job = add_job(db, make_repository(db), kind="answer_question")
    claimed = claim(db, now=datetime.now(UTC))

    # Outside any log context: the line takes the job ID from where the job is failed.
    with _capture_logs() as captured:
        assert queue.claim_next(db, now=claimed.deadline_at + timedelta(seconds=1)) is None

    assert reload(db, job.id).error_code == "timeout"
    assert counted(db, job.id)
    [line] = job_lines(captured)
    assert {key: line[key] for key in ("job_id", "kind", "outcome", "attempt")} == {
        "job_id": str(job.id),
        "kind": "answer_question",
        "outcome": "failed",
        "attempt": 1,
    }


def test_an_abandoned_last_attempt_is_logged_and_counted_as_an_internal_error(db: Session) -> None:
    job = add_job(db, make_repository(db))
    t0 = datetime.now(UTC)
    for attempt in range(3):
        claim(db, now=t0 + timedelta(seconds=61 * attempt))

    with _capture_logs() as captured:
        assert queue.claim_next(db, now=t0 + timedelta(seconds=61 * 3)) is None

    row = reload(db, job.id)
    assert (row.error_code, row.error_permanent) == ("internal_error", True)
    assert counted(db, job.id)
    [line] = job_lines(captured)
    assert (line["job_id"], line["outcome"], line["attempt"]) == (str(job.id), "failed", 3)
