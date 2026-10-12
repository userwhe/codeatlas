"""Integration tests for `GET /status/{condition}` (T027, contracts/http-api.md).

The external monitor alerts on 503 and sends a recovery notice on the next 200 (research R10).
Each condition is read from the database at the time of the request, with the default thresholds.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from codeatlas import db as db_module
from codeatlas.config import Settings
from codeatlas.models import Backup, Job, WorkerHeartbeat
from tests.conftest import APP_ORIGIN
from tests.integration.test_job_queue import add_job, make_repository

pytestmark = pytest.mark.integration

CONDITIONS = ("worker", "jobs-waiting", "jobs-failing", "backup")


def get_status(client: TestClient, condition: str) -> httpx.Response:
    response = client.get(f"/status/{condition}")
    assert response.headers["Cache-Control"] == "no-store"
    return response


def assert_status(
    response: httpx.Response, condition: str, status: str, threshold: int
) -> dict[str, Any]:
    """Check the response's code and body, and return the body."""
    assert response.status_code == (200 if status == "ok" else 503), response.text
    body: dict[str, Any] = response.json()
    assert set(body) == {"condition", "status", "value", "threshold"}
    assert (body["condition"], body["status"], body["threshold"]) == (condition, status, threshold)
    return body


def set_job(db: Session, job_id: uuid.UUID, **values: object) -> None:
    db.execute(update(Job).where(Job.id == job_id).values(**values))
    db.commit()


def add_heartbeat(db: Session, beat_at: datetime) -> None:
    db.add(WorkerHeartbeat(worker_id="worker-1:7", started_at=beat_at, beat_at=beat_at))
    db.commit()


def add_backup(db: Session, completed_at: datetime) -> None:
    db.add(
        Backup(
            object_key=f"backups/{completed_at:%Y-%m-%d/%H%M%S}/codeatlas.dump",
            completed_at=completed_at,
            bytes=734_003_200,
            sha256="0" * 64,
            alembic_revision="0005",
            release="development",
        )
    )
    db.commit()


def add_failed_jobs(db: Session, *failures: tuple[timedelta, str, bool]) -> None:
    """Add one failed job per `(age, error_code, error_permanent)`."""
    repository = make_repository(db)
    now = datetime.now(UTC)
    for age, code, permanent in failures:
        job = add_job(db, repository)
        set_job(
            db,
            job.id,
            status="failed",
            attempt=1,
            error_code=code,
            error_permanent=permanent,
            finished_at=now - age,
        )


# --- worker -------------------------------------------------------------------------------------


def test_a_recent_heartbeat_is_ok(db: Session, client: TestClient) -> None:
    add_heartbeat(db, datetime.now(UTC) - timedelta(seconds=10))

    body = assert_status(get_status(client, "worker"), "worker", "ok", 300)

    assert 10 <= body["value"] < 20


def test_a_silent_worker_fails(db: Session, client: TestClient) -> None:
    add_heartbeat(db, datetime.now(UTC) - timedelta(seconds=301))

    body = assert_status(get_status(client, "worker"), "worker", "failing", 300)

    assert 301 <= body["value"] < 311


def test_no_heartbeat_fails(db: Session, client: TestClient) -> None:
    body = assert_status(get_status(client, "worker"), "worker", "failing", 300)

    assert body["value"] is None


# --- jobs-waiting -------------------------------------------------------------------------------


def test_no_waiting_job_is_ok(db: Session, client: TestClient) -> None:
    body = assert_status(get_status(client, "jobs-waiting"), "jobs-waiting", "ok", 1800)

    assert body["value"] == 0


def test_a_job_due_10_minutes_ago_is_ok(db: Session, client: TestClient) -> None:
    job = add_job(db, make_repository(db))
    set_job(db, job.id, run_after=datetime.now(UTC) - timedelta(minutes=10))

    body = assert_status(get_status(client, "jobs-waiting"), "jobs-waiting", "ok", 1800)

    assert 600 <= body["value"] < 610


def test_a_job_due_31_minutes_ago_fails(db: Session, client: TestClient) -> None:
    job = add_job(db, make_repository(db))
    set_job(db, job.id, run_after=datetime.now(UTC) - timedelta(minutes=31))

    body = assert_status(get_status(client, "jobs-waiting"), "jobs-waiting", "failing", 1800)

    assert 1860 <= body["value"] < 1870


def test_a_job_held_back_by_its_workspace_is_not_runnable(db: Session, client: TestClient) -> None:
    repository = make_repository(db)
    running = add_job(db, repository, dedupe_key="index:running")
    waiting = add_job(db, repository, dedupe_key="index:waiting")
    now = datetime.now(UTC)
    set_job(
        db, running.id, status="running", attempt=1, lease_expires_at=now + timedelta(minutes=1)
    )
    set_job(db, waiting.id, run_after=now - timedelta(minutes=31))

    body = assert_status(get_status(client, "jobs-waiting"), "jobs-waiting", "ok", 1800)

    assert body["value"] == 0


# --- jobs-failing -------------------------------------------------------------------------------


def test_three_counted_failures_in_the_last_hour_fail(db: Session, client: TestClient) -> None:
    add_failed_jobs(
        db,
        (timedelta(minutes=40), "github_unavailable", False),
        (timedelta(minutes=20), "timeout", True),
        (timedelta(minutes=5), "timeout", True),
    )

    body = assert_status(get_status(client, "jobs-failing"), "jobs-failing", "failing", 3)

    assert body["value"] == 3


def test_two_counted_failures_are_ok(db: Session, client: TestClient) -> None:
    add_failed_jobs(
        db,
        (timedelta(minutes=20), "github_unavailable", False),
        (timedelta(minutes=5), "internal_error", True),
    )

    body = assert_status(get_status(client, "jobs-failing"), "jobs-failing", "ok", 3)

    assert body["value"] == 2


def test_permanent_failures_from_the_input_are_not_counted(db: Session, client: TestClient) -> None:
    add_failed_jobs(
        db, *[(timedelta(minutes=minutes), "limit_exceeded", True) for minutes in (1, 2, 3)]
    )

    body = assert_status(get_status(client, "jobs-failing"), "jobs-failing", "ok", 3)

    assert body["value"] == 0


def test_a_run_of_three_fails_while_its_last_failure_is_within_the_hour(
    db: Session, client: TestClient
) -> None:
    add_failed_jobs(
        db, *[(timedelta(minutes=minutes), "timeout", True) for minutes in (65, 62, 59)]
    )

    body = assert_status(get_status(client, "jobs-failing"), "jobs-failing", "failing", 3)

    assert body["value"] == 3


def test_a_run_of_three_is_ok_once_its_last_failure_is_over_an_hour_old(
    db: Session, client: TestClient
) -> None:
    add_failed_jobs(
        db, *[(timedelta(minutes=minutes), "timeout", True) for minutes in (65, 62, 61)]
    )

    body = assert_status(get_status(client, "jobs-failing"), "jobs-failing", "ok", 3)

    assert body["value"] == 0


# --- backup -------------------------------------------------------------------------------------


def test_no_backup_fails(db: Session, client: TestClient) -> None:
    body = assert_status(get_status(client, "backup"), "backup", "failing", 26)

    assert body["value"] is None


def test_a_backup_2_hours_old_is_ok(db: Session, client: TestClient) -> None:
    add_backup(db, datetime.now(UTC) - timedelta(hours=2))

    body = assert_status(get_status(client, "backup"), "backup", "ok", 26)

    assert 2 <= body["value"] < 2.01


def test_a_backup_27_hours_old_fails(db: Session, client: TestClient) -> None:
    add_backup(db, datetime.now(UTC) - timedelta(hours=27))
    add_backup(db, datetime.now(UTC) - timedelta(hours=50))

    body = assert_status(get_status(client, "backup"), "backup", "failing", 26)

    assert 27 <= body["value"] < 27.01


# --- Errors, caching, and the rate limit --------------------------------------------------------


def test_an_unknown_condition_is_not_found(client: TestClient) -> None:
    response = get_status(client, "disk")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize("condition", CONDITIONS)
def test_each_condition_is_not_ready_without_the_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    def unreachable() -> Session:
        raise OperationalError("SELECT 1", {}, ConnectionRefusedError("connection refused"))

    monkeypatch.setattr(db_module, "get_sessionmaker", lambda: unreachable)

    response = get_status(client, condition)

    assert response.status_code == 503
    error = response.json()["error"]
    assert (error["code"], error["message"], error["retryable"]) == (
        "not_ready",
        "The database is unavailable.",
        True,
    )
    assert error["request_id"] == response.headers["X-Request-ID"]


def test_status_paths_are_not_in_the_openapi_document(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert not [path for path in paths if path.startswith("/status")]


@pytest.fixture
def limited(
    db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """A client of a new app limited to 2 requests a minute (the module-level `app` was built
    at import, with the limit off).
    """
    from codeatlas.api.app import create_app

    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)
    app = create_app()
    with TestClient(app, base_url=APP_ORIGIN) as test_client:
        yield test_client


def test_status_paths_are_rate_limited_per_address(limited: TestClient) -> None:
    statuses = [limited.get(f"/status/{condition}").status_code for condition in CONDITIONS[:2]]

    refused = limited.get("/status/backup")

    assert statuses == [503, 200]
    assert refused.status_code == 429, refused.text
    assert refused.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(refused.headers["Retry-After"]) <= 60
