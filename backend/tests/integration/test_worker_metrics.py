"""Integration tests for the worker's metrics and job lines (T040, research R9 and R10).

EMF lines go to standard output and are captured with `capsys`. Log lines are captured with a
handler on the root logger, because `configure_logging` binds its stream when it runs.
"""

import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from codeatlas import metrics
from codeatlas.config import Settings
from codeatlas.jobs import queue, worker
from codeatlas.jobs.queue import JobFailure
from codeatlas.jobs.worker import JobContext
from codeatlas.models import Job
from tests.integration.test_job_queue import add_job, claim, make_repository, reload
from tests.integration.test_logging import _Capture, _capture_logs

pytestmark = pytest.mark.integration


@pytest.fixture
def emitting(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(settings, "emit_metrics", True)
    monkeypatch.setattr(settings, "metrics_environment", "pilot")
    return settings


@pytest.fixture
def handlers(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, worker.Handler], None]:
    """Register handlers in a copy of the registry that is restored after the test."""
    worker.load_handlers()
    monkeypatch.setattr(worker, "_handlers", dict(worker._handlers))
    return worker.register


def emf_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def metric_values(line: dict[str, Any]) -> dict[str, Any]:
    """The metrics a line defines, with their values, after checking its dimension."""
    [definition] = line["_aws"]["CloudWatchMetrics"]
    assert (definition["Namespace"], definition["Dimensions"]) == ("CodeAtlas", [["Environment"]])
    assert line["Environment"] == "pilot"
    return {metric["Name"]: line[metric["Name"]] for metric in definition["Metrics"]}


def failures(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """The `JobsFailed` lines written since the last read."""
    return [metric_values(line) for line in emf_lines(capsys) if "JobsFailed" in line]


def job_lines(captured: _Capture) -> list[dict[str, object]]:
    return [line for line in captured.json_lines if line["message"] == "job_finished"]


def set_job(db: Session, job_id: uuid.UUID, **values: object) -> None:
    db.execute(update(Job).where(Job.id == job_id).values(**values))
    db.commit()


# Queue metrics -----------------------------------------------------------------------------


def test_report_once_reports_the_queue_and_a_heartbeat(
    db: Session, emitting: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
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

    metrics.report_once(now=now)

    [line] = emf_lines(capsys)
    assert metric_values(line) == {
        "WorkerHeartbeat": 1,
        "QueuedJobs": 4,
        "OldestRunnableJobAgeSeconds": 42.0,
    }


def test_report_once_reports_zero_age_without_a_runnable_job(
    db: Session, emitting: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    metrics.report_once()
    job = add_job(db, make_repository(db))
    set_job(db, job.id, status="retry_wait", run_after=datetime.now(UTC) + timedelta(minutes=1))
    metrics.report_once()

    assert [metric_values(line) for line in emf_lines(capsys)] == [
        {"WorkerHeartbeat": 1, "QueuedJobs": 0, "OldestRunnableJobAgeSeconds": 0},
        {"WorkerHeartbeat": 1, "QueuedJobs": 1, "OldestRunnableJobAgeSeconds": 0},
    ]


def test_the_reporter_keeps_reporting_while_a_job_runs_and_stops_on_its_event(
    db: Session,
    emitting: Settings,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
    capsys: pytest.CaptureFixture[str],
) -> None:
    during: list[dict[str, Any]] = []

    def handler(ctx: JobContext) -> None:
        # The job loop is blocked here, as during a long indexing job.
        capsys.readouterr()
        time.sleep(0.5)
        during.extend(emf_lines(capsys))

    handlers("index_repository", handler)
    add_job(db, make_repository(db))
    stop = metrics.start_reporter(interval=0.1)
    try:
        assert run_worker_once()
    finally:
        stop.set()

    heartbeats = [metric_values(line) for line in during if "WorkerHeartbeat" in line]
    assert len(heartbeats) >= 3, during
    assert all(values["WorkerHeartbeat"] == 1 for values in heartbeats)
    # A report already under way may still finish; nothing follows it.
    time.sleep(0.3)
    capsys.readouterr()
    time.sleep(0.3)
    assert capsys.readouterr().out == ""


# Job lines and failure metrics -------------------------------------------------------------


def test_a_succeeded_job_logs_its_outcome_and_duration(
    db: Session,
    emitting: Settings,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
    capsys: pytest.CaptureFixture[str],
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
    assert failures(capsys) == []


def test_a_retried_failure_logs_retry_wait_and_counts_no_failure(
    db: Session,
    emitting: Settings,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(ctx: JobContext) -> None:
        raise JobFailure("github_unavailable", "GitHub is unavailable.", permanent=False)

    handlers("index_repository", handler)
    job = add_job(db, make_repository(db))

    with _capture_logs() as captured:
        assert run_worker_once()

    assert reload(db, job.id).status == "retry_wait"
    [line] = job_lines(captured)
    assert {key: line[key] for key in ("job_id", "kind", "outcome", "attempt")} == {
        "job_id": str(job.id),
        "kind": "index_repository",
        "outcome": "retry_wait",
        "attempt": 1,
    }
    assert isinstance(line["duration_ms"], int)
    assert failures(capsys) == []


def _transient(ctx: JobContext) -> None:
    raise JobFailure("github_unavailable", "GitHub is unavailable.", permanent=False)


def _timeout(ctx: JobContext) -> None:
    raise queue.timeout_failure(ctx.kind)


def _unexpected(ctx: JobContext) -> None:
    raise RuntimeError("unexpected")


def _size_limit(ctx: JobContext) -> None:
    raise JobFailure("limit_exceeded", "The repository has too many files.", permanent=True)


# Handler, the job's error code, and how many `JobsFailed` it counts.
FAILED_IN_THE_WORKER: dict[str, tuple[worker.Handler, str, int]] = {
    "no retry left": (_transient, "github_unavailable", 1),
    "timeout": (_timeout, "timeout", 1),
    "internal error": (_unexpected, "internal_error", 1),
    "size limit": (_size_limit, "limit_exceeded", 0),
}


@pytest.mark.parametrize(
    ("handler", "code", "counted"), FAILED_IN_THE_WORKER.values(), ids=FAILED_IN_THE_WORKER.keys()
)
def test_a_failed_job_logs_failed_and_counts_only_failures_that_matter(
    db: Session,
    emitting: Settings,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
    capsys: pytest.CaptureFixture[str],
    handler: worker.Handler,
    code: str,
    counted: int,
) -> None:
    handlers("index_repository", handler)
    job = add_job(db, make_repository(db))
    # The next attempt is the last one.
    set_job(db, job.id, attempt=2)

    with _capture_logs() as captured:
        assert run_worker_once()

    row = reload(db, job.id)
    assert (row.status, row.error_code) == ("failed", code)
    [line] = job_lines(captured)
    assert {key: line[key] for key in ("job_id", "kind", "outcome", "attempt")} == {
        "job_id": str(job.id),
        "kind": "index_repository",
        "outcome": "failed",
        "attempt": 3,
    }
    assert failures(capsys) == [{"JobsFailed": 1}] * counted


def test_a_job_failed_at_claim_time_for_its_deadline_is_logged_and_counted(
    db: Session, emitting: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    job = add_job(db, make_repository(db), kind="answer_question")
    claimed = claim(db, now=datetime.now(UTC))

    # Outside any log context: the line takes the job ID from where the job is failed.
    with _capture_logs() as captured:
        assert queue.claim_next(db, now=claimed.deadline_at + timedelta(seconds=1)) is None

    assert reload(db, job.id).error_code == "timeout"
    [line] = job_lines(captured)
    assert {key: line[key] for key in ("job_id", "kind", "outcome", "attempt")} == {
        "job_id": str(job.id),
        "kind": "answer_question",
        "outcome": "failed",
        "attempt": 1,
    }
    assert failures(capsys) == [{"JobsFailed": 1}]


def test_an_abandoned_last_attempt_is_logged_and_counted_as_an_internal_error(
    db: Session, emitting: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    job = add_job(db, make_repository(db))
    t0 = datetime.now(UTC)
    for attempt in range(3):
        claim(db, now=t0 + timedelta(seconds=61 * attempt))

    with _capture_logs() as captured:
        assert queue.claim_next(db, now=t0 + timedelta(seconds=61 * 3)) is None

    assert reload(db, job.id).error_code == "internal_error"
    [line] = job_lines(captured)
    assert (line["job_id"], line["outcome"], line["attempt"]) == (str(job.id), "failed", 3)
    assert failures(capsys) == [{"JobsFailed": 1}]


def test_no_metric_lines_when_metrics_are_off(
    db: Session,
    settings: Settings,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert settings.emit_metrics is False
    handlers("index_repository", _unexpected)
    job = add_job(db, make_repository(db))
    set_job(db, job.id, attempt=2)

    assert run_worker_once()
    metrics.report_once()

    assert reload(db, job.id).status == "failed"
    assert capsys.readouterr().out == ""
