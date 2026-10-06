"""Integration tests for the daily check (T036, US3, FR-012, FR-018, research R6).

`schedule_checks` runs with an injected `now`; the queued `check` runs then go through the real
worker against the fake GitHub. A pass queues a check for each repository whose last access
check is more than 20 hours old, so a pass at `access_checked_at + CHECK_INTERVAL + 1 minute`
always finds the repository due.
"""

import itertools
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from codeatlas.db import new_session
from codeatlas.github.fake import (
    OVERSIZED_ID,
    SAMPLE_APP_ID,
    SAMPLE_APP_PRIVATE_ID,
    FakeGitHub,
    commit_sha,
    get_fake_github,
)
from codeatlas.github.gateway import GitHubNotFound
from codeatlas.jobs import maintenance, queue, worker
from codeatlas.models import AuditEvent, Job, JobEvent, Repository, Snapshot, Workspace
from codeatlas.workspace.sync import (
    CHECK_INTERVAL,
    CHECK_LOCK_KEY,
    CHECK_SCHEDULER_INTERVAL,
    schedule_checks,
)
from tests.webhooks import installation_payload, send_delivery

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
INITIAL = commit_sha(SAMPLE_APP_ID, "initial")
SECOND = commit_sha(SAMPLE_APP_ID, "second")
UP_TO_DATE = "Already up to date"
OCTO_ORG_INSTALLATION = 5001

_github_ids = itertools.count(8000)


def run_all(run_worker_once: Callable[[], bool]) -> None:
    for _ in range(50):
        if not run_worker_once():
            return
    raise AssertionError("jobs did not finish")


def repository_row(db: Session, repository_id: str | uuid.UUID) -> Repository:
    db.expire_all()
    repository = db.get(Repository, repository_id)
    assert repository is not None
    return repository


def job_row(db: Session, job_id: uuid.UUID) -> Job:
    db.expire_all()
    job = db.get(Job, job_id)
    assert job is not None
    return job


def jobs_of(db: Session, repository_id: str | uuid.UUID) -> list[Job]:
    db.expire_all()
    return list(
        db.scalars(
            select(Job).where(Job.repository_id == repository_id).order_by(Job.created_at, Job.id)
        )
    )


def last_event(db: Session, job_id: uuid.UUID) -> JobEvent:
    event = db.scalars(
        select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.seq.desc())
    ).first()
    assert event is not None
    return event


def audit(db: Session, action: str) -> list[AuditEvent]:
    return list(
        db.scalars(select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.id))
    )


def snapshot_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Snapshot)) or 0


# Scheduling, on repositories inserted directly -------------------------------------------------


@pytest.fixture
def workspace(db: Session) -> Workspace:
    workspace = Workspace(name="octocat")
    db.add(workspace)
    db.commit()
    return workspace


def add_repository(
    db: Session, workspace: Workspace, *, checked_at: datetime | None, **fields: Any
) -> Repository:
    github_id = next(_github_ids)
    repository = Repository(
        workspace_id=workspace.id,
        github_repository_id=github_id,
        github_installation_id=5002,
        full_name=f"octocat/repo-{github_id}",
        default_branch="trunk",
        is_private=False,
        access_checked_at=checked_at,
        **fields,
    )
    db.add(repository)
    db.commit()
    return repository


def add_job(db: Session, repository: Repository, status: str) -> Job:
    job = Job(
        workspace_id=repository.workspace_id,
        kind=queue.INDEX_JOB,
        repository_id=repository.id,
        payload={"branch": repository.default_branch},
        dedupe_key=queue.index_dedupe_key(repository.id, repository.default_branch),
        trigger="user",
        status=status,
        run_after=NOW,
        finished_at=NOW if status in ("succeeded", "failed", "canceled") else None,
    )
    db.add(job)
    db.commit()
    return job


def check_jobs(db: Session) -> dict[uuid.UUID, Job]:
    db.expire_all()
    jobs = db.scalars(select(Job).where(Job.trigger == "check")).all()
    return {job.repository_id: job for job in jobs}


def test_due_repositories_get_a_check_on_their_default_branch(
    db: Session, workspace: Workspace
) -> None:
    never_checked = add_repository(db, workspace, checked_at=None)
    stale = add_repository(db, workspace, checked_at=NOW - CHECK_INTERVAL - timedelta(minutes=1))
    on_the_boundary = add_repository(db, workspace, checked_at=NOW - CHECK_INTERVAL)
    recent = add_repository(db, workspace, checked_at=NOW - timedelta(hours=19))

    queued = schedule_checks(db, now=NOW)
    db.commit()

    assert queued == 2
    jobs = check_jobs(db)
    assert set(jobs) == {never_checked.id, stale.id}
    for repository_id, job in jobs.items():
        assert (job.kind, job.status, job.trigger, job.created_by) == (
            "index_repository",
            "queued",
            "check",
            None,
        )
        assert job.payload == {"branch": "trunk"}
        assert job.dedupe_key == queue.index_dedupe_key(repository_id, "trunk")
        assert last_event(db, job.id).event_type == "queued"
    assert jobs_of(db, on_the_boundary.id) == jobs_of(db, recent.id) == []


def test_paused_tombstoned_and_busy_repositories_are_skipped_but_lost_ones_are_checked(
    db: Session, workspace: Workspace
) -> None:
    due = NOW - timedelta(days=2)
    lost = add_repository(
        db,
        workspace,
        checked_at=due,
        access_state="access_lost",
        access_reason="app_uninstalled",
        access_lost_at=NOW - timedelta(days=1),
    )
    finished = add_repository(db, workspace, checked_at=due)
    add_job(db, finished, "failed")
    skipped = [
        add_repository(
            db,
            workspace,
            checked_at=due,
            access_state="paused",
            access_reason="sign_in_required",
        ),
        add_repository(
            db,
            workspace,
            checked_at=due,
            access_state="paused",
            access_reason="external_processing_not_accepted",
        ),
        add_repository(db, workspace, checked_at=due, deleted_at=NOW - timedelta(minutes=5)),
    ]
    for status in ("queued", "running", "retry_wait"):
        busy = add_repository(db, workspace, checked_at=due)
        add_job(db, busy, status)
        skipped.append(busy)

    queued = schedule_checks(db, now=NOW)
    db.commit()

    assert queued == 2
    assert set(check_jobs(db)) == {lost.id, finished.id}
    for repository in skipped:
        assert all(job.trigger == "user" for job in jobs_of(db, repository.id))


def test_a_second_runner_skips_while_the_lock_is_held(db: Session, workspace: Workspace) -> None:
    repository = add_repository(db, workspace, checked_at=None)
    # The daily check has its own lock, so it never waits for a maintenance pass.
    assert CHECK_LOCK_KEY != maintenance.LOCK_KEY
    holder = new_session()
    holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": CHECK_LOCK_KEY})
    try:
        assert schedule_checks(db, now=NOW) == 0
        db.commit()
        assert jobs_of(db, repository.id) == []
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": CHECK_LOCK_KEY})
        holder.close()

    assert schedule_checks(db, now=NOW) == 1
    db.commit()
    assert set(check_jobs(db)) == {repository.id}


def test_the_worker_schedules_checks_every_30_minutes(db: Session, workspace: Workspace) -> None:
    task = worker._periodic["access_checks"]
    assert task.interval == CHECK_SCHEDULER_INTERVAL == timedelta(minutes=30)
    repository = add_repository(db, workspace, checked_at=None)

    task.fn()

    assert set(check_jobs(db)) == {repository.id}


# Check runs, through the API and the worker ----------------------------------------------------


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry failed attempts at once, so a test can run a job until it settles."""
    monkeypatch.setattr(queue, "backoff_seconds", lambda attempt: 0.0)


def connect_and_index(
    client: TestClient, run_worker_once: Callable[[], bool], github_id: int = SAMPLE_APP_ID
) -> str:
    response = client.post(
        "/v1/repositories",
        json={"github_repository_id": github_id, "accept_external_processing": True},
    )
    assert response.status_code == 202, response.text
    run_all(run_worker_once)
    repository_id: str = response.json()["repository"]["id"]
    return repository_id


def due_time(db: Session, repository_id: str) -> datetime:
    """A time at which the repository's next check is due."""
    checked_at = repository_row(db, repository_id).access_checked_at
    assert checked_at is not None
    return checked_at + CHECK_INTERVAL + timedelta(minutes=1)


def schedule(db: Session, repository_id: str, *, now: datetime | None = None) -> Job:
    """Run one scheduler pass that queues a check for the repository; returns the job."""
    assert schedule_checks(db, now=now or due_time(db, repository_id)) == 1
    db.commit()
    [job] = [job for job in jobs_of(db, repository_id) if job.status == "queued"]
    assert (job.trigger, job.created_by) == ("check", None)
    return job


def run_check(db: Session, run_worker_once: Callable[[], bool], repository_id: str) -> Job:
    job = schedule(db, repository_id)
    run_all(run_worker_once)
    return job_row(db, job.id)


def visible_fields(repository: Repository) -> dict[str, Any]:
    return {
        "full_name": repository.full_name,
        "default_branch": repository.default_branch,
        "github_installation_id": repository.github_installation_id,
        "is_private": repository.is_private,
        "active_snapshot_id": repository.active_snapshot_id,
        "access_state": repository.access_state,
        "access_reason": repository.access_reason,
        "access_lost_at": repository.access_lost_at,
        "latest_push_sha": repository.latest_push_sha,
        "latest_push_job_id": repository.latest_push_job_id,
    }


def test_an_up_to_date_check_only_records_the_check(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    repository = repository_row(db, repository_id)
    main = db.get(Snapshot, repository.active_snapshot_id)
    assert main is not None and main.commit_sha == INITIAL
    # The active version is on another branch; the check must not move it to `main`.
    other = Snapshot(
        workspace_id=repository.workspace_id,
        repository_id=repository.id,
        commit_sha="f" * 40,
        branch="feature",
        index_version=main.index_version,
        status="ready",
        coverage={},
        ready_at=datetime.now(UTC),
    )
    db.add(other)
    db.flush()
    repository.active_snapshot_id = other.id
    db.commit()
    before = visible_fields(repository_row(db, repository_id))
    checked_at = repository_row(db, repository_id).access_checked_at
    assert checked_at is not None
    fake = get_fake_github()
    fake.calls.clear()

    job = run_check(db, run_worker_once, repository_id)

    assert (job.status, job.trigger, job.payload["commit_sha"]) == ("succeeded", "check", INITIAL)
    event = last_event(db, job.id)
    assert (event.event_type, event.message) == ("succeeded", UP_TO_DATE)
    assert fake.calls["open_tarball"] == 0
    after = repository_row(db, repository_id)
    assert visible_fields(after) == before
    assert after.access_checked_at is not None and after.access_checked_at > checked_at
    assert snapshot_count(db) == 2
    assert audit(db, "repository_access_restored") == audit(db, "repository_access_lost") == []
    shown = octocat.get(f"/v1/jobs/{job.id}")
    assert shown.status_code == 200, shown.text
    assert shown.json()["trigger"] == "check"


def test_a_check_indexes_a_push_whose_notification_never_arrived(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    assert get_fake_github().push(SAMPLE_APP_ID) == SECOND

    job = run_check(db, run_worker_once, repository_id)

    assert (job.status, job.trigger, job.created_by) == ("succeeded", "check", None)
    assert last_event(db, job.id).message != UP_TO_DATE
    repository = repository_row(db, repository_id)
    snapshot = db.get(Snapshot, repository.active_snapshot_id)
    assert snapshot is not None
    assert (snapshot.commit_sha, snapshot.job_id, snapshot.status) == (SECOND, job.id, "ready")
    versions = octocat.get(f"/v1/repositories/{repository_id}/snapshots").json()["items"]
    assert [(item["commit_sha"], item["trigger"]) for item in versions][0] == (SECOND, "check")


def test_a_check_detects_access_lost_without_a_notification(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once, SAMPLE_APP_PRIVATE_ID)
    fake = get_fake_github()
    fake.revoke_access("octocat", SAMPLE_APP_PRIVATE_ID)
    fake.calls.clear()

    job = run_check(db, run_worker_once, repository_id)

    assert (job.status, job.error_code) == ("failed", "access_denied")
    assert fake.calls["open_tarball"] == 0
    repository = repository_row(db, repository_id)
    assert (repository.access_state, repository.access_reason) == (
        "access_lost",
        "repository_not_visible",
    )
    assert repository.access_checked_at == repository.access_lost_at
    [lost] = audit(db, "repository_access_lost")
    assert lost.detail == {"reason": "repository_not_visible", "trigger": "check"}
    # Reads stop as soon as the loss is detected (SC-006).
    response = octocat.get(f"/v1/repositories/{repository_id}/snapshots")
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "repository_access_lost"


def test_a_check_restores_a_repository_whose_access_returned(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    version = repository_row(db, repository_id).active_snapshot_id
    uninstalled = installation_payload("deleted", OCTO_ORG_INSTALLATION)
    delivered = send_delivery(octocat, "installation", uninstalled, delivery_id="uninstalled")
    assert delivered.status_code == 202, delivered.text
    assert repository_row(db, repository_id).access_state == "access_lost"
    fake = get_fake_github()
    fake.calls.clear()

    job = run_check(db, run_worker_once, repository_id)

    assert (job.status, last_event(db, job.id).message) == ("succeeded", UP_TO_DATE)
    assert fake.calls["open_tarball"] == 0
    repository = repository_row(db, repository_id)
    assert (repository.access_state, repository.active_snapshot_id) == ("active", version)
    [restored] = audit(db, "repository_access_restored")
    assert restored.detail == {"trigger": "check"}


def test_a_check_indexes_a_commit_whose_run_lost_access(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    get_fake_github().push(SAMPLE_APP_ID)

    def open_tarball(self: FakeGitHub, installation_id: int, full_name: str, sha: str) -> Any:
        raise GitHubNotFound(f"{full_name} has no tarball for {sha}")

    with monkeypatch.context() as patch:
        patch.setattr(FakeGitHub, "open_tarball", open_tarball)
        response = octocat.post(f"/v1/repositories/{repository_id}/index", json={})
        assert response.status_code == 202, response.text
        run_all(run_worker_once)
    denied = jobs_of(db, repository_id)[-1]
    # Failed permanently and not retryably at the head, but because of access, not the commit.
    assert (denied.error_code, denied.error_retryable) == ("access_denied", False)
    assert denied.payload["commit_sha"] == SECOND
    assert repository_row(db, repository_id).access_state == "access_lost"

    job = run_check(db, run_worker_once, repository_id)

    assert job.status == "succeeded"
    repository = repository_row(db, repository_id)
    assert repository.access_state == "active"
    snapshot = db.get(Snapshot, repository.active_snapshot_id)
    assert snapshot is not None
    assert (snapshot.commit_sha, snapshot.job_id) == (SECOND, job.id)


def test_a_commit_that_failed_permanently_is_not_indexed_again(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once, OVERSIZED_ID)
    [rejected] = jobs_of(db, repository_id)
    head = commit_sha(OVERSIZED_ID, "initial")
    assert (rejected.error_code, rejected.error_retryable) == ("limit_exceeded", False)
    assert rejected.payload["commit_sha"] == head
    fake = get_fake_github()
    fake.calls.clear()

    first = run_check(db, run_worker_once, repository_id)
    # A run that was already up to date says nothing new about the commit, so the next check
    # still finds the failure and skips it too.
    second = run_check(db, run_worker_once, repository_id)

    for job in (first, second):
        assert (job.status, job.payload["commit_sha"]) == ("succeeded", head)
        assert last_event(db, job.id).message == UP_TO_DATE
    assert fake.calls["open_tarball"] == 0
    assert repository_row(db, repository_id).active_snapshot_id is None
    # The up-to-date checks do not hide why the repository has no version.
    assert octocat.get(f"/v1/repositories/{repository_id}").json()["state"] == "rejected"

    # A new commit is tried again.
    fake.push(OVERSIZED_ID)
    third = run_check(db, run_worker_once, repository_id)

    assert (third.status, third.error_code) == ("failed", "limit_exceeded")
    assert fake.calls["open_tarball"] == 1


def test_a_transient_failure_leaves_the_check_due(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], no_backoff: None
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    checked_at = repository_row(db, repository_id).access_checked_at
    now = due_time(db, repository_id)
    get_fake_github().set_unavailable(True)

    job = schedule(db, repository_id, now=now)
    run_all(run_worker_once)

    job = job_row(db, job.id)
    assert (job.status, job.error_code, job.attempt) == (
        "failed",
        "github_unavailable",
        job.max_attempts,
    )
    repository = repository_row(db, repository_id)
    assert (repository.access_state, repository.access_checked_at) == ("active", checked_at)
    assert octocat.get(f"/v1/repositories/{repository_id}/snapshots").status_code == 200

    retried = schedule(db, repository_id, now=now + CHECK_SCHEDULER_INTERVAL)
    assert retried.id != job.id


def test_a_silent_loss_is_detected_within_24_hours(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once, SAMPLE_APP_PRIVATE_ID)
    t0 = repository_row(db, repository_id).access_checked_at
    assert t0 is not None
    fake = get_fake_github()
    detected_at: datetime | None = None
    passes_with_work = 0

    for step in range(1, 49):  # one pass every 30 minutes for a day
        now = t0 + step * CHECK_SCHEDULER_INTERVAL
        if now >= t0 + timedelta(hours=1):
            fake.revoke_access("octocat", SAMPLE_APP_PRIVATE_ID)
        if schedule_checks(db, now=now):
            passes_with_work += 1
        db.commit()
        run_all(run_worker_once)
        if repository_row(db, repository_id).access_state == "access_lost":
            detected_at = now
            break

    assert detected_at is not None
    assert detected_at < t0 + timedelta(hours=24)
    assert passes_with_work == 1
    assert octocat.get(f"/v1/repositories/{repository_id}/snapshots").status_code == 403
