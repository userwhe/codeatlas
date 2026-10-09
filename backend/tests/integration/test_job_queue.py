"""Integration tests for the job queue and worker (T039, research R3 and R4)."""

import itertools
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from codeatlas.auth.sessions import COOKIE_NAME, create_session
from codeatlas.config import Settings
from codeatlas.db import new_session
from codeatlas.jobs import queue, worker
from codeatlas.jobs.queue import JobFailure, LeaseLost
from codeatlas.jobs.worker import JobContext
from codeatlas.models import Job, JobEvent, Membership, Repository, User, Workspace

pytestmark = pytest.mark.integration

_github_ids = itertools.count(1000)


def make_repository(db: Session, login: str = "octocat") -> Repository:
    """Insert a user, their workspace and membership, and one connected repository."""
    user = User(github_user_id=next(_github_ids), github_login=login)
    workspace = Workspace(name=login)
    db.add_all([user, workspace])
    db.flush()
    db.add(Membership(workspace_id=workspace.id, user_id=user.id))
    repository = Repository(
        workspace_id=workspace.id,
        github_repository_id=next(_github_ids),
        github_installation_id=1,
        full_name=f"{login}/service",
        default_branch="main",
        is_private=False,
        created_by=user.id,
    )
    db.add(repository)
    db.commit()
    return repository


def add_job(
    db: Session,
    repository: Repository,
    *,
    kind: str = "index_repository",
    dedupe_key: str | None = None,
) -> Job:
    job, created = queue.enqueue(
        db,
        workspace_id=repository.workspace_id,
        kind=kind,
        repository_id=repository.id,
        created_by=repository.created_by,
        dedupe_key=dedupe_key,
    )
    db.commit()
    assert created
    return job


def reload(db: Session, job_id: uuid.UUID) -> Job:
    job = db.get(Job, job_id, populate_existing=True)
    assert job is not None
    return job


def events(db: Session, job_id: uuid.UUID) -> list[JobEvent]:
    return list(
        db.scalars(select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.seq))
    )


def claim(db: Session, *, now: datetime | None = None) -> queue.Claim:
    claimed = queue.claim_next(db, now=now)
    assert claimed is not None
    return claimed


def finish(db: Session, claimed: queue.Claim) -> None:
    job = queue.fenced(db, claimed.job_id, claimed.fencing_token)
    queue.complete(db, job)
    db.commit()


def sign_in_as(client: TestClient, db: Session, user_id: uuid.UUID) -> TestClient:
    """Give `client` a session cookie for the user without going through GitHub sign-in."""
    token = create_session(db, user_id)
    db.commit()
    client.cookies.set(COOKIE_NAME, token)
    return client


@pytest.fixture
def handlers(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str, worker.Handler], None]]:
    """Register dummy handlers in a copy of the registry that is restored after the test."""
    worker.load_handlers()
    monkeypatch.setattr(worker, "_handlers", dict(worker._handlers))
    yield worker.register


# --- Queue ----------------------------------------------------------------------------------


def test_enqueue_returns_the_active_job_for_a_duplicate_dedupe_key(db: Session) -> None:
    repository = make_repository(db)
    key = f"index:{repository.id}:main"
    first = add_job(db, repository, dedupe_key=key)

    again, created = queue.enqueue(
        db,
        workspace_id=repository.workspace_id,
        kind="index_repository",
        repository_id=repository.id,
        created_by=repository.created_by,
        dedupe_key=key,
    )
    db.commit()

    assert not created
    assert again.id == first.id
    assert db.scalar(select(func.count()).select_from(Job)) == 1
    assert [e.event_type for e in events(db, first.id)] == ["queued"]

    # Once the job is finished, the same key starts a new job.
    finish(db, claim(db))
    newer = add_job(db, repository, dedupe_key=key)
    assert newer.id != first.id


def test_concurrent_enqueues_with_the_same_key_create_one_job(db: Session) -> None:
    repository = make_repository(db)
    key = f"index:{repository.id}:main"

    def enqueue_in(session: Session) -> tuple[Job, bool]:
        return queue.enqueue(
            session,
            workspace_id=repository.workspace_id,
            kind="index_repository",
            repository_id=repository.id,
            created_by=repository.created_by,
            dedupe_key=key,
        )

    with new_session() as other:
        # The other request has inserted the job but not committed: this enqueue waits on the
        # unique index, then returns the other request's job.
        winner, _ = enqueue_in(other)
        committer = threading.Timer(0.5, other.commit)
        committer.start()
        try:
            job, created = enqueue_in(db)
        finally:
            committer.join()
    db.commit()

    assert not created
    assert job.id == winner.id
    assert db.scalar(select(func.count()).select_from(Job)) == 1


def test_only_one_job_per_workspace_and_kind_runs(db: Session) -> None:
    repository = make_repository(db)
    first = add_job(db, repository, dedupe_key="index:a")
    second = add_job(db, repository, dedupe_key="index:b")
    question = add_job(db, repository, kind="answer_question")
    other_workspace_job = add_job(db, make_repository(db, "hubot"))

    claims = [queue.claim_next(db) for _ in range(4)]

    assert [c.job_id if c else None for c in claims] == [
        first.id,
        question.id,
        other_workspace_job.id,
        None,
    ]
    waiting = reload(db, second.id)
    assert waiting.status == "queued"
    assert queue.queued_behind(db, waiting) == 1
    third = add_job(db, repository, dedupe_key="index:c")
    assert queue.queued_behind(db, third) == 2

    # The partial unique index rejects a second running job of the same kind.
    with pytest.raises(IntegrityError):
        db.execute(update(Job).where(Job.id == second.id).values(status="running"))
    db.rollback()

    # When the running job finishes, the next one in line starts.
    first_claim = claims[0]
    assert first_claim is not None
    finish(db, first_claim)
    assert claim(db).job_id == second.id
    assert queue.queued_behind(db, reload(db, third.id)) == 1


def test_claim_skips_a_job_locked_by_another_transaction(db: Session) -> None:
    repository = make_repository(db)
    locked = add_job(db, repository)
    free = add_job(db, repository, kind="answer_question")

    with new_session() as other:
        other.execute(select(Job).where(Job.id == locked.id).with_for_update())
        assert claim(db).job_id == free.id
        other.rollback()

    assert claim(db).job_id == locked.id


def test_claim_retries_when_another_worker_starts_the_same_kind_first(db: Session) -> None:
    repository = make_repository(db)
    first = add_job(db, repository, dedupe_key="index:a")
    second = add_job(db, repository, dedupe_key="index:b")

    with new_session() as other:
        # Another worker has started `first` but not committed yet: this claim skips the locked
        # row, picks `second`, and then waits on the unique index until the other worker commits.
        other.execute(update(Job).where(Job.id == first.id).values(status="running"))
        committer = threading.Timer(0.5, other.commit)
        committer.start()
        try:
            assert queue.claim_next(db) is None
        finally:
            committer.join()

    assert reload(db, second.id).status == "queued"


def test_an_expired_lease_is_reclaimed_with_a_new_attempt_and_token(db: Session) -> None:
    repository = make_repository(db)
    job = add_job(db, repository)
    t0 = datetime.now(UTC)

    first = claim(db, now=t0)
    assert (first.attempt, first.fencing_token) == (1, 1)
    assert first.deadline_at == t0 + timedelta(minutes=15)
    assert queue.claim_next(db, now=t0 + timedelta(seconds=59)) is None

    second = claim(db, now=t0 + timedelta(seconds=61))
    assert second.job_id == job.id
    assert (second.attempt, second.fencing_token) == (2, 2)
    row = reload(db, job.id)
    assert row.status == "running"
    assert row.started_at == t0
    assert row.deadline_at == first.deadline_at
    assert row.lease_expires_at == t0 + timedelta(seconds=121)

    queue.renew_lease(db, job.id, second.fencing_token, now=t0 + timedelta(seconds=100))
    assert reload(db, job.id).lease_expires_at == t0 + timedelta(seconds=160)
    with pytest.raises(LeaseLost):
        queue.renew_lease(db, job.id, first.fencing_token)


def test_a_reclaimed_job_without_attempts_left_fails(db: Session) -> None:
    repository = make_repository(db)
    job = add_job(db, repository)
    t0 = datetime.now(UTC)
    for attempt in range(3):
        claim(db, now=t0 + timedelta(seconds=61 * attempt))

    assert queue.claim_next(db, now=t0 + timedelta(seconds=61 * 3)) is None

    row = reload(db, job.id)
    assert (row.status, row.attempt, row.error_code) == ("failed", 3, "internal_error")


def test_a_stale_token_cannot_complete_or_publish(db: Session) -> None:
    repository = make_repository(db)
    job = add_job(db, repository)
    t0 = datetime.now(UTC)
    stale = claim(db, now=t0)
    current = claim(db, now=t0 + timedelta(seconds=61))

    with pytest.raises(LeaseLost):
        queue.fenced(db, job.id, stale.fencing_token)
    db.rollback()
    with pytest.raises(LeaseLost):
        queue.fail(db, job.id, stale.fencing_token, code="x", message="Failed.", permanent=True)

    stale_ctx = JobContext.from_claim(stale)
    reached_body = False
    with pytest.raises(LeaseLost):
        with stale_ctx.publish() as (_, published):
            reached_body = True
            published.payload = {"commit_sha": "stale"}
    assert not reached_body
    with pytest.raises(LeaseLost):
        stale_ctx.event("parsing", "Parsing files")

    row = reload(db, job.id)
    assert (row.status, row.fencing_token, row.payload) == ("running", 2, {})

    with JobContext.from_claim(current).publish() as (_, published):
        published.payload = {"commit_sha": "current"}
    row = reload(db, job.id)
    assert (row.status, row.payload) == ("succeeded", {"commit_sha": "current"})
    assert [e.event_type for e in events(db, job.id)] == ["queued", "succeeded"]


def test_transient_failures_back_off_then_fail_after_three_attempts(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    caps: list[float] = []

    def upper_bound(low: float, high: float) -> float:
        assert low == 0
        caps.append(high)
        return high

    monkeypatch.setattr(queue.random, "uniform", upper_bound)
    repository = make_repository(db)
    job = add_job(db, repository)
    t = datetime.now(UTC)

    for attempt, cap in ((1, 5.0), (2, 10.0)):
        claimed = claim(db, now=t)
        assert claimed.attempt == attempt
        queue.fail(
            db,
            job.id,
            claimed.fencing_token,
            code="github_unavailable",
            message="GitHub is unavailable.",
            permanent=False,
            now=t,
        )
        row = reload(db, job.id)
        assert row.status == "retry_wait"
        assert row.run_after == t + timedelta(seconds=cap)
        assert row.error_code is None
        assert queue.claim_next(db, now=t + timedelta(seconds=cap - 1)) is None
        t += timedelta(seconds=cap)

    last = claim(db, now=t)
    assert last.attempt == 3
    queue.fail(
        db,
        job.id,
        last.fencing_token,
        code="github_unavailable",
        message="GitHub is unavailable.",
        permanent=False,
        now=t,
    )

    row = reload(db, job.id)
    assert (row.status, row.error_code, row.error_message, row.error_retryable) == (
        "failed",
        "github_unavailable",
        "GitHub is unavailable.",
        True,
    )
    assert row.finished_at == t
    assert caps == [5.0, 10.0]
    retry = events(db, job.id)[1]
    assert retry.event_type == "retry_scheduled"
    assert retry.data == {"code": "github_unavailable", "attempt": 1, "retry_in_seconds": 5.0}
    assert [e.event_type for e in events(db, job.id)] == [
        "queued",
        "retry_scheduled",
        "retry_scheduled",
        "failed",
    ]


def test_backoff_uses_full_jitter_under_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    for attempt in range(1, 8):
        delay = queue.backoff_seconds(attempt)
        assert 0 <= delay <= min(60, 5 * 2 ** (attempt - 1))

    monkeypatch.setattr(queue.random, "uniform", lambda low, high: high)
    assert [queue.backoff_seconds(attempt) for attempt in range(1, 7)] == [
        5.0,
        10.0,
        20.0,
        40.0,
        60.0,
        60.0,
    ]


def test_a_permanent_failure_fails_immediately(db: Session) -> None:
    repository = make_repository(db)
    job = add_job(db, repository)
    claimed = claim(db)

    queue.fail(
        db,
        job.id,
        claimed.fencing_token,
        code="branch_not_found",
        message="The branch does not exist.",
        permanent=True,
    )

    row = reload(db, job.id)
    assert (row.status, row.attempt, row.error_code, row.error_retryable) == (
        "failed",
        1,
        "branch_not_found",
        False,
    )
    assert row.lease_expires_at is None
    failed = events(db, job.id)[-1]
    assert (failed.event_type, failed.message, failed.data) == (
        "failed",
        "The branch does not exist.",
        {"code": "branch_not_found"},
    )
    assert queue.claim_next(db) is None


def test_a_passed_deadline_fails_the_job_with_timeout(db: Session) -> None:
    repository = make_repository(db)
    indexing = add_job(db, repository)
    question = add_job(db, repository, kind="answer_question")
    t0 = datetime.now(UTC)
    indexing_claim = claim(db, now=t0)
    question_claim = claim(db, now=t0)
    assert question_claim.deadline_at == t0 + timedelta(minutes=3)

    # A transient failure after the deadline is not retried.
    queue.fail(
        db,
        indexing.id,
        indexing_claim.fencing_token,
        code="github_unavailable",
        message="GitHub is unavailable.",
        permanent=False,
        now=indexing_claim.deadline_at + timedelta(seconds=1),
    )
    row = reload(db, indexing.id)
    assert (row.status, row.error_code, row.error_retryable) == ("failed", "timeout", True)

    # An abandoned attempt found after the deadline is failed instead of claimed again.
    assert queue.claim_next(db, now=question_claim.deadline_at + timedelta(seconds=1)) is None
    row = reload(db, question.id)
    assert (row.status, row.attempt, row.error_code) == ("failed", 1, "timeout")
    assert events(db, question.id)[-1].data == {"code": "timeout"}


@pytest.mark.parametrize(
    ("kind", "deadline_setting", "message"),
    [
        (
            "index_repository",
            "indexing_deadline",
            "Indexing did not finish within the time limit.",
        ),
        (
            "answer_question",
            "question_deadline",
            "Answering the question did not finish within the time limit.",
        ),
        (
            "review_pull_request",
            "review_deadline",
            "Reviewing the pull request did not finish within the time limit.",
        ),
    ],
)
def test_each_kind_has_its_own_deadline_and_timeout_message(
    db: Session, settings: Settings, kind: str, deadline_setting: str, message: str
) -> None:
    job = add_job(db, make_repository(db), kind=kind)
    t0 = datetime.now(UTC)

    claimed = claim(db, now=t0)

    row = reload(db, job.id)
    assert row.started_at == t0
    assert claimed.deadline_at == row.deadline_at == t0 + getattr(settings, deadline_setting)

    # An abandoned attempt found after the deadline fails with the kind's message.
    assert queue.claim_next(db, now=claimed.deadline_at + timedelta(seconds=1)) is None
    row = reload(db, job.id)
    assert (row.status, row.error_code, row.error_message, row.error_retryable) == (
        "failed",
        "timeout",
        message,
        True,
    )
    assert queue.timeout_failure(kind).message == message


def test_the_jobs_api_reports_a_review_job(db: Session, client: TestClient) -> None:
    repository = make_repository(db)
    job = add_job(db, repository, kind="review_pull_request")
    assert repository.created_by is not None
    sign_in_as(client, db, repository.created_by)

    response = client.get(f"/v1/jobs/{job.id}")

    assert response.status_code == 200, response.text
    assert (response.json()["kind"], response.json()["status"]) == ("review_pull_request", "queued")


def test_cancel_for_repository_cancels_waiting_jobs_only(db: Session) -> None:
    repository = make_repository(db)
    running = add_job(db, repository, dedupe_key="index:a")
    queued = add_job(db, repository, dedupe_key="index:b")
    retrying = add_job(db, repository, kind="answer_question")
    other = add_job(db, make_repository(db, "hubot"))
    claim(db)
    retry_claim = claim(db)
    assert retry_claim.job_id == retrying.id
    queue.fail(
        db, retrying.id, retry_claim.fencing_token, code="x", message="Failed.", permanent=False
    )

    assert queue.cancel_for_repository(db, repository.id) == 2
    db.commit()

    statuses = {
        job_id: reload(db, job_id).status for job_id in (running.id, queued.id, retrying.id)
    }
    assert statuses == {running.id: "running", queued.id: "canceled", retrying.id: "canceled"}
    assert reload(db, queued.id).finished_at is not None
    assert events(db, queued.id)[-1].event_type == "canceled"
    assert reload(db, other.id).status == "queued"


def test_event_seq_is_contiguous_and_after_filters(
    db: Session, client: TestClient, handlers: Callable[[str, worker.Handler], None]
) -> None:
    repository = make_repository(db)
    job = add_job(db, repository)
    attempts: list[int] = []

    def handler(ctx: JobContext) -> None:
        attempts.append(ctx.attempt)
        ctx.event("resolving_commit", "Resolving the branch")
        if ctx.attempt == 1:
            raise JobFailure("github_unavailable", "GitHub is unavailable.", permanent=False)
        ctx.event("parsing", "Parsing 2 files", {"files": 2}, event_type="stage_completed")

    handlers("index_repository", handler)
    assert worker.run_once()
    db.execute(update(Job).where(Job.id == job.id).values(run_after=datetime.now(UTC)))
    db.commit()
    assert worker.run_once()
    assert attempts == [1, 2]

    expected = [
        "queued",
        "stage_started",
        "retry_scheduled",
        "stage_started",
        "stage_completed",
        "succeeded",
    ]
    stored = events(db, job.id)
    assert [e.seq for e in stored] == list(range(1, len(expected) + 1))
    assert [e.event_type for e in stored] == expected

    assert repository.created_by is not None
    sign_in_as(client, db, repository.created_by)
    everything = client.get(f"/v1/jobs/{job.id}/events")
    assert everything.status_code == 200, everything.text
    body = everything.json()
    assert body["job_status"] == "succeeded"
    assert [item["seq"] for item in body["items"]] == [1, 2, 3, 4, 5, 6]
    assert body["next_after"] == 6

    missed = client.get(f"/v1/jobs/{job.id}/events", params={"after": 3}).json()
    assert [item["event_type"] for item in missed["items"]] == expected[3:]
    assert missed["items"][1] == {
        "seq": 5,
        "event_type": "stage_completed",
        "stage": "parsing",
        "message": "Parsing 2 files",
        "data": {"files": 2},
        "created_at": missed["items"][1]["created_at"],
    }
    assert missed["next_after"] == 6

    caught_up = client.get(f"/v1/jobs/{job.id}/events", params={"after": 6}).json()
    assert (caught_up["items"], caught_up["next_after"]) == ([], 6)


# --- Worker ---------------------------------------------------------------------------------


def test_run_once_runs_the_handler_and_completes_the_job(
    db: Session,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
) -> None:
    seen: list[tuple[uuid.UUID, int, str]] = []

    def handler(ctx: JobContext) -> None:
        seen.append((ctx.job_id, ctx.attempt, ctx.kind))
        ctx.event("parsing", "Parsing 1 file", {"files": 1})

    handlers("index_repository", handler)
    job = add_job(db, make_repository(db))

    assert run_worker_once() is True
    assert run_worker_once() is False

    assert seen == [(job.id, 1, "index_repository")]
    row = reload(db, job.id)
    assert (row.status, row.lease_expires_at) == ("succeeded", None)
    assert row.finished_at is not None
    stored = events(db, job.id)
    assert [e.event_type for e in stored] == ["queued", "stage_started", "succeeded"]
    assert (stored[1].stage, stored[1].data) == ("parsing", {"files": 1})


def test_publish_commits_results_with_the_completion(
    db: Session,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
) -> None:
    def handler(ctx: JobContext) -> None:
        with ctx.publish() as (_, job):
            job.payload = {**job.payload, "commit_sha": "4f2c"}

    handlers("index_repository", handler)
    job = add_job(db, make_repository(db))

    assert run_worker_once()

    row = reload(db, job.id)
    assert (row.status, row.payload) == ("succeeded", {"commit_sha": "4f2c"})
    assert [e.event_type for e in events(db, job.id)] == ["queued", "succeeded"]


def test_a_failure_inside_publish_rolls_back_and_fails_permanently(
    db: Session,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
) -> None:
    def handler(ctx: JobContext) -> None:
        with ctx.publish() as (_, job):
            job.payload = {"commit_sha": "4f2c"}
            raise JobFailure("limit_exceeded", "The repository has too many files.", permanent=True)

    handlers("index_repository", handler)
    job = add_job(db, make_repository(db))

    assert run_worker_once()

    row = reload(db, job.id)
    assert (row.status, row.error_code, row.error_retryable, row.payload) == (
        "failed",
        "limit_exceeded",
        False,
        {},
    )


def test_a_canceled_publish_keeps_the_canceled_status(
    db: Session,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
) -> None:
    def handler(ctx: JobContext) -> None:
        with ctx.publish() as (session, job):
            queue.cancel(session, job, message="The repository was disconnected.")

    handlers("index_repository", handler)
    job = add_job(db, make_repository(db))

    assert run_worker_once()

    assert reload(db, job.id).status == "canceled"
    assert [e.event_type for e in events(db, job.id)] == ["queued", "canceled"]


def test_a_transient_job_failure_schedules_a_retry(
    db: Session,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
) -> None:
    def handler(ctx: JobContext) -> None:
        raise JobFailure("provider_unavailable", "The model is unavailable.", permanent=False)

    handlers("answer_question", handler)
    job = add_job(db, make_repository(db), kind="answer_question")

    assert run_worker_once()

    row = reload(db, job.id)
    assert (row.status, row.attempt, row.error_code) == ("retry_wait", 1, None)
    assert events(db, job.id)[-1].data["code"] == "provider_unavailable"


def test_an_unexpected_error_is_logged_and_retried_as_internal_error(
    db: Session,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(ctx: JobContext) -> None:
        raise RuntimeError("unexpected")

    handlers("index_repository", handler)
    job = add_job(db, make_repository(db))

    # Alembic's `fileConfig` in the migration fixture disables loggers that already exist.
    monkeypatch.setattr(logging.getLogger("codeatlas.jobs.worker"), "disabled", False)
    with caplog.at_level(logging.ERROR, logger="codeatlas.jobs.worker"):
        assert run_worker_once()

    row = reload(db, job.id)
    assert row.status == "retry_wait"
    assert events(db, job.id)[-1].data["code"] == "internal_error"
    assert any(record.exc_info for record in caplog.records)


def test_a_kind_without_a_handler_fails_as_unsupported(
    db: Session, monkeypatch: pytest.MonkeyPatch, run_worker_once: Callable[[], bool]
) -> None:
    worker.load_handlers()
    monkeypatch.setattr(worker, "_handlers", {})
    job = add_job(db, make_repository(db))

    assert run_worker_once()

    row = reload(db, job.id)
    assert (row.status, row.error_code, row.error_retryable) == (
        "failed",
        "unsupported_input",
        False,
    )


def test_a_lost_lease_abandons_the_attempt_without_writing(
    db: Session,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
) -> None:
    def handler(ctx: JobContext) -> None:
        with new_session() as other:
            # Simulate another worker reclaiming the job.
            other.execute(
                update(Job).where(Job.id == ctx.job_id).values(fencing_token=Job.fencing_token + 1)
            )
            other.commit()
        ctx.event("parsing", "Parsing files")

    handlers("index_repository", handler)
    job = add_job(db, make_repository(db))

    assert run_worker_once()

    row = reload(db, job.id)
    assert (row.status, row.fencing_token, row.error_code) == ("running", 2, None)
    assert [e.event_type for e in events(db, job.id)] == ["queued"]


def test_the_heartbeat_renews_the_lease_and_detects_a_lost_lease(
    db: Session,
    handlers: Callable[[str, worker.Handler], None],
    run_worker_once: Callable[[], bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "HEARTBEAT_INTERVAL", timedelta(seconds=0.05))
    observed: dict[str, bool] = {}

    def lease_of(session: Session, job_id: uuid.UUID) -> datetime | None:
        return reload(session, job_id).lease_expires_at

    def handler(ctx: JobContext) -> None:
        with new_session() as other:
            first = lease_of(other, ctx.job_id)
            assert first is not None
            give_up = time.monotonic() + 5
            renewed = False
            while not renewed and time.monotonic() < give_up:
                time.sleep(0.02)
                latest = lease_of(other, ctx.job_id)
                renewed = latest is not None and latest > first
            observed["renewed"] = renewed
            other.execute(
                update(Job).where(Job.id == ctx.job_id).values(fencing_token=Job.fencing_token + 1)
            )
            other.commit()
        observed["lost"] = ctx.lease_lost.wait(5)
        ctx.check()

    handlers("index_repository", handler)
    job = add_job(db, make_repository(db))

    assert run_worker_once()

    assert observed == {"renewed": True, "lost": True}
    assert reload(db, job.id).status == "running"


def test_check_raises_a_timeout_after_the_deadline() -> None:
    ctx = JobContext(
        job_id=uuid.uuid4(),
        kind="answer_question",
        attempt=1,
        fencing_token=1,
        workspace_id=uuid.uuid4(),
        repository_id=uuid.uuid4(),
        analysis_run_id=None,
        payload={},
        created_by=None,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    ctx.check()

    expired = replace(ctx, deadline_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(JobFailure) as failure:
        expired.check()
    assert (failure.value.code, failure.value.permanent) == ("timeout", True)


def test_load_handlers_skips_only_missing_handler_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "HANDLER_MODULES", ("codeatlas.jobs.not_written_yet",))
    monkeypatch.setattr(worker, "_handlers_loaded", False)
    worker.load_handlers()
    assert worker._handlers_loaded

    def missing_dependency(name: str) -> None:
        raise ModuleNotFoundError("No module named 'missing_dependency'", name="missing_dependency")

    monkeypatch.setattr(worker, "_handlers_loaded", False)
    monkeypatch.setattr(worker.importlib, "import_module", missing_dependency)
    with pytest.raises(ModuleNotFoundError):
        worker.load_handlers()


def test_periodic_tasks_run_when_due(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "_periodic", {})
    calls: list[str] = []

    def broken() -> None:
        raise RuntimeError("purge failed")

    worker.register_periodic("purge", timedelta(minutes=10), lambda: calls.append("purge"))
    worker.register_periodic("broken", timedelta(minutes=10), broken)

    worker._run_due_periodic(1000.0)
    worker._run_due_periodic(1599.0)
    assert calls == ["purge"]
    worker._run_due_periodic(1600.0)
    assert calls == ["purge", "purge"]
