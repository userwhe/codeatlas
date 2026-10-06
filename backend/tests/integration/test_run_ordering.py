"""Fault-injection tests for the order in which runs publish (T019, SC-003, FR-006, research R3).

An attempt "stalls past its lease" when it stops at an injection point while the test expires its
lease, as a frozen worker would. Another worker then reclaims the job. Whatever the interleaving,
only the attempt that owns the job may publish, the waiting job starts only after the running
one ends, and the last pushed commit ends up as the default version.
"""

import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from codeatlas.db import new_session, session_scope
from codeatlas.github.fake import SAMPLE_APP_ID, commit_sha, get_fake_github
from codeatlas.jobs import queue, worker
from codeatlas.jobs.worker import JobContext
from codeatlas.models import Job, Repository, Snapshot
from tests.webhooks import push_payload, send_delivery

pytestmark = pytest.mark.integration

INITIAL = commit_sha(SAMPLE_APP_ID, "initial")
# Every stage an attempt reports after it resolved its commit, then the publish itself.
STALL_POINTS = (
    "fetching_source",
    "filtering",
    "parsing",
    "building_search_index",
    "embedding_docs",
    "publishing",
    "publish",
)
WAIT_SECONDS = 10


@dataclass
class Faults:
    """Actions to run when a given attempt of a job reaches a point, and what each start saw."""

    actions: dict[tuple[uuid.UUID, int, str], Callable[[], None]] = field(default_factory=dict)
    running_at_start: list[int] = field(default_factory=list)

    def at(self, job_id: uuid.UUID, attempt: int, point: str, action: Callable[[], None]) -> None:
        self.actions[(job_id, attempt, point)] = action

    def reached(self, ctx: JobContext, point: str) -> None:
        if point == "resolving_commit":
            with session_scope() as db:
                running = db.scalar(
                    select(func.count()).where(
                        Job.workspace_id == ctx.workspace_id,
                        Job.kind == ctx.kind,
                        Job.status == "running",
                    )
                )
            self.running_at_start.append(running or 0)
        action = self.actions.pop((ctx.job_id, ctx.attempt, point), None)
        if action is not None:
            action()


@pytest.fixture
def faults(monkeypatch: pytest.MonkeyPatch) -> Faults:
    """Run injected actions before each stage event and before each publish.

    The attempt holds no database transaction at these points.
    """
    injected = Faults()
    original_event = JobContext.event
    original_publish = JobContext.publish

    def event(
        self: JobContext,
        stage: str,
        message: str,
        data: dict[str, Any] | None = None,
        *,
        event_type: str = "stage_started",
    ) -> None:
        injected.reached(self, stage)
        original_event(self, stage, message, data, event_type=event_type)

    @contextmanager
    def publish(self: JobContext) -> Iterator[tuple[Session, Job]]:
        injected.reached(self, "publish")
        with original_publish(self) as locked:
            yield locked

    monkeypatch.setattr(JobContext, "event", event)
    monkeypatch.setattr(JobContext, "publish", publish)
    # A stalled attempt must not renew its lease while the test holds it.
    monkeypatch.setattr(worker, "HEARTBEAT_INTERVAL", timedelta(hours=1))
    return injected


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


def run_all(run_worker_once: Callable[[], bool]) -> None:
    while run_worker_once():
        pass


def connect_and_index(client: TestClient, run_worker_once: Callable[[], bool]) -> str:
    response = client.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})
    assert response.status_code == 202, response.text
    run_all(run_worker_once)
    repository_id: str = response.json()["repository"]["id"]
    return repository_id


def deliver_push(client: TestClient, sha: str) -> None:
    response = send_delivery(client, "push", push_payload(SAMPLE_APP_ID, sha))
    assert response.json() == {"outcome": "processed"}, response.text


def push(client: TestClient) -> str:
    """Move the fake's default branch forward and deliver the push; returns its SHA."""
    sha = get_fake_github().push(SAMPLE_APP_ID)
    deliver_push(client, sha)
    return sha


def push_jobs(db: Session) -> list[Job]:
    db.expire_all()
    return list(
        db.scalars(select(Job).where(Job.trigger == "push").order_by(Job.created_at, Job.id))
    )


def latest_push_job(db: Session) -> Job:
    return push_jobs(db)[-1]


def expire_lease(db: Session, job_id: uuid.UUID) -> None:
    db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    db.commit()


def active_commit(db: Session, repository_id: str) -> str:
    db.expire_all()
    repository = db.get(Repository, repository_id)
    assert repository is not None
    snapshot = db.get(Snapshot, repository.active_snapshot_id)
    assert snapshot is not None
    return snapshot.commit_sha


def snapshot_statuses(db: Session) -> dict[tuple[str, int], str]:
    """`(commit, attempt) -> status` for every snapshot built by a push run."""
    db.expire_all()
    rows = db.execute(
        select(Snapshot.commit_sha, Snapshot.job_attempt, Snapshot.status)
        .join(Job, Job.id == Snapshot.job_id)
        .where(Job.trigger == "push")
    )
    return {(sha, attempt): status for sha, attempt, status in rows}


@pytest.mark.parametrize("newer_attempt", ["runs_while_stalled", "runs_after_rejection"])
@pytest.mark.parametrize("stall_point", STALL_POINTS)
def test_a_stalled_attempt_never_publishes_over_a_newer_one(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    faults: Faults,
    stall_point: str,
    newer_attempt: str,
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    first = push(octocat)
    stalled = latest_push_job(db)
    seen: dict[str, Any] = {}

    def stall() -> None:
        seen["second"] = push(octocat)
        follow_up = latest_push_job(db)
        assert follow_up.id != stalled.id and follow_up.status == "queued"
        # The waiting job cannot start while the stalled attempt still holds its lease.
        assert run_worker_once() is False
        expire_lease(db, stalled.id)
        if newer_attempt == "runs_while_stalled":
            assert run_worker_once() is True
        else:
            with new_session() as session:
                seen["claim"] = queue.claim_next(session)
        assert latest_push_job(db).status == "queued"

    faults.at(stalled.id, 1, stall_point, stall)
    assert run_worker_once() is True  # the stalled attempt, rejected once it resumes
    if newer_attempt == "runs_after_rejection":
        claim = seen["claim"]
        assert (claim.job_id, claim.attempt) == (stalled.id, 2)
        worker._run(claim)
    run_all(run_worker_once)

    second = seen["second"]
    jobs = push_jobs(db)
    assert len(jobs) == 2 and jobs[0].id == stalled.id
    assert [(job.status, job.attempt) for job in jobs] == [("succeeded", 2), ("succeeded", 1)]
    assert [job.payload["commit_sha"] for job in jobs] == [second, second]
    assert snapshot_statuses(db) == {(first, 1): "discarded", (second, 2): "ready"}
    assert active_commit(db, repository_id) == second
    assert faults.running_at_start and set(faults.running_at_start) == {1}


def test_a_push_during_the_newer_attempt_is_indexed_last(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], faults: Faults
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    first = push(octocat)
    stalled = latest_push_job(db)
    pushed = [first]

    def push_during_newer_attempt() -> None:
        pushed.append(push(octocat))

    def stall() -> None:
        pushed.append(push(octocat))
        expire_lease(db, stalled.id)
        faults.at(stalled.id, 2, "publish", push_during_newer_attempt)
        assert run_worker_once() is True

    faults.at(stalled.id, 1, "filtering", stall)
    run_all(run_worker_once)

    first, second, third = pushed
    jobs = push_jobs(db)
    assert len(jobs) == 2
    assert [job.payload["commit_sha"] for job in jobs] == [second, third]
    assert jobs[1].payload["pushed_commit_sha"] == third
    assert snapshot_statuses(db) == {
        (first, 1): "discarded",
        (second, 2): "ready",
        (third, 1): "ready",
    }
    assert active_commit(db, repository_id) == third
    assert set(faults.running_at_start) == {1}


def test_a_stalled_reuse_never_publishes_over_a_newer_one(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], faults: Faults
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    second = push(octocat)
    run_all(run_worker_once)
    # Resetting the fake moves `main` back to the initial commit, as a force push does.
    get_fake_github().reset()
    deliver_push(octocat, INITIAL)
    stalled = latest_push_job(db)
    pushed: list[str] = []

    def stall() -> None:
        pushed.append(push(octocat))
        expire_lease(db, stalled.id)
        assert run_worker_once() is True

    faults.at(stalled.id, 1, "publish", stall)
    run_all(run_worker_once)

    assert pushed == [second]
    jobs = push_jobs(db)
    assert [job.payload["commit_sha"] for job in jobs] == [second, second, second]
    assert (jobs[1].status, jobs[1].attempt) == ("succeeded", 2)
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 2
    assert active_commit(db, repository_id) == second


def test_a_stale_resolution_does_not_hide_a_later_push(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    faults: Faults,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stalled attempt resolves the branch after the newer attempt did, while it runs.

    If the stale attempt could record its commit on the job, a push of that commit would look
    covered by the running job, which indexes an older commit, and no follow-up would run.
    """
    repository_id = connect_and_index(octocat, run_worker_once)
    first = push(octocat)
    stalled = latest_push_job(db)
    fake = get_fake_github()
    stale_resolving = threading.Event()
    release_stale = threading.Event()
    newer_running = threading.Event()
    release_newer = threading.Event()
    errors: list[BaseException] = []
    original_resolve = fake.resolve_commit
    resolves = iter(range(1_000))

    def resolve_commit(installation_id: int, full_name: str, branch: str) -> str:
        if next(resolves) == 0:  # the first attempt stalls before asking GitHub
            stale_resolving.set()
            assert release_stale.wait(WAIT_SECONDS)
        return original_resolve(installation_id, full_name, branch)

    def hold_newer() -> None:
        newer_running.set()
        assert release_newer.wait(WAIT_SECONDS)

    def in_thread(name: str) -> threading.Thread:
        def target() -> None:
            try:
                run_worker_once()
            except BaseException as exc:  # surfaced by the assertions below
                errors.append(exc)

        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        return thread

    monkeypatch.setattr(fake, "resolve_commit", resolve_commit)
    faults.at(stalled.id, 2, "fetching_source", hold_newer)
    try:
        stale = in_thread("stale-attempt")
        assert stale_resolving.wait(WAIT_SECONDS)
        expire_lease(db, stalled.id)
        newer = in_thread("newer-attempt")
        assert newer_running.wait(WAIT_SECONDS)

        later = fake.push(SAMPLE_APP_ID)
        release_stale.set()
        stale.join(WAIT_SECONDS)
        assert not stale.is_alive()
        deliver_push(octocat, later)
        release_newer.set()
        newer.join(WAIT_SECONDS)
        assert not newer.is_alive()
    finally:
        release_stale.set()
        release_newer.set()
    assert errors == []
    run_all(run_worker_once)

    jobs = push_jobs(db)
    assert [job.payload["commit_sha"] for job in jobs] == [first, later]
    assert active_commit(db, repository_id) == later
