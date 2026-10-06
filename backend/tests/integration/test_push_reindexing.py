"""Integration tests for re-indexing after pushes (T018, US1, contracts/github-webhooks.md)."""

import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from codeatlas.config import Settings
from codeatlas.github.fake import (
    SAMPLE_APP_ID,
    SAMPLE_APP_RENAMED,
    commit_sha,
    get_fake_github,
)
from codeatlas.jobs.worker import JobContext
from codeatlas.models import AuditEvent, Job, Repository, Snapshot, WebhookDelivery
from codeatlas.workspace import access
from tests.webhooks import push_payload, send_delivery

pytestmark = pytest.mark.integration

INITIAL = commit_sha(SAMPLE_APP_ID, "initial")
SECOND = commit_sha(SAMPLE_APP_ID, "second")
QUESTION = "Where are repository permissions checked?"

DuringRun = Callable[[str, Callable[[], None]], None]


def run_all(run_worker_once: Callable[[], bool]) -> None:
    while run_worker_once():
        pass


def connect_and_index(
    client: TestClient, run_worker_once: Callable[[], bool], github_id: int = SAMPLE_APP_ID
) -> str:
    response = client.post("/v1/repositories", json={"github_repository_id": github_id})
    assert response.status_code == 202, response.text
    run_all(run_worker_once)
    repository_id: str = response.json()["repository"]["id"]
    return repository_id


def deliver_push(
    client: TestClient,
    after: str,
    *,
    ref: str = "refs/heads/main",
    default_branch: str = "main",
    delivery_id: str | None = None,
) -> httpx.Response:
    payload = push_payload(SAMPLE_APP_ID, after, ref=ref, default_branch=default_branch)
    response = send_delivery(client, "push", payload, delivery_id=delivery_id)
    assert response.status_code == 202, response.text
    return response


def push(client: TestClient) -> str:
    """Move the fake's default branch forward and deliver the matching push; returns its SHA."""
    sha = get_fake_github().push(SAMPLE_APP_ID)
    assert deliver_push(client, sha).json() == {"outcome": "processed"}
    return sha


def repository_row(db: Session, repository_id: str | uuid.UUID) -> Repository:
    db.expire_all()
    repository = db.get(Repository, repository_id)
    assert repository is not None
    return repository


def active_snapshot(db: Session, repository_id: str | uuid.UUID) -> Snapshot:
    repository = repository_row(db, repository_id)
    snapshot = db.get(Snapshot, repository.active_snapshot_id)
    assert snapshot is not None
    return snapshot


def push_jobs(db: Session) -> list[Job]:
    db.expire_all()
    return list(
        db.scalars(select(Job).where(Job.trigger == "push").order_by(Job.created_at, Job.id))
    )


def job_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Job)) or 0


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


@pytest.fixture
def during_run(monkeypatch: pytest.MonkeyPatch) -> DuringRun:
    """Run an action once, when the next attempt to report a stage is about to report it.

    The attempt holds no database transaction at that point, so the action can deliver pushes.
    """
    actions: dict[str, Callable[[], None]] = {}
    original = JobContext.event

    def event(
        self: JobContext,
        stage: str,
        message: str,
        data: dict[str, Any] | None = None,
        *,
        event_type: str = "stage_started",
    ) -> None:
        action = actions.pop(stage, None)
        if action is not None:
            action()
        original(self, stage, message, data, event_type=event_type)

    monkeypatch.setattr(JobContext, "event", event)
    return actions.__setitem__


def test_a_push_is_indexed_and_published_with_the_push_trigger(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    sha = get_fake_github().push(SAMPLE_APP_ID)

    response = deliver_push(octocat, sha, delivery_id="push-1")

    assert response.json() == {"outcome": "processed"}
    delivery = db.get(WebhookDelivery, "push-1")
    assert delivery is not None
    assert (delivery.outcome, delivery.detail) == (
        "processed",
        {"ref": "refs/heads/main", "after": sha, "repositories": 1},
    )
    repository = repository_row(db, repository_id)
    assert repository.latest_push_sha == sha
    assert repository.latest_push_at is not None
    [job] = push_jobs(db)
    assert repository.latest_push_job_id == job.id
    assert (job.status, job.created_by) == ("queued", None)
    assert job.payload == {"branch": "main", "pushed_commit_sha": sha}

    run_all(run_worker_once)

    snapshot = active_snapshot(db, repository_id)
    assert (snapshot.commit_sha, snapshot.job_id) == (sha, job.id)
    old_path, new_path = SAMPLE_APP_RENAMED
    hits = octocat.post(
        "/v1/search", json={"snapshot_id": str(snapshot.id), "query": "slugify", "mode": "symbol"}
    ).json()["results"]
    assert {hit["path"] for hit in hits} == {new_path}

    detail = octocat.get(f"/v1/repositories/{repository_id}").json()
    assert detail["active_snapshot"]["commit_sha"] == sha
    versions = octocat.get(f"/v1/repositories/{repository_id}/snapshots").json()["items"]
    active = next(item for item in versions if item["is_active"])
    assert (active["commit_sha"], active.get("trigger")) == (sha, "push")
    latest_push = detail.get("latest_push")
    assert latest_push is not None, "latest_push is missing from the repository response"
    assert latest_push["commit_sha"] == sha
    assert (latest_push["job"]["id"], latest_push["job"]["status"]) == (str(job.id), "succeeded")
    assert latest_push["job"]["trigger"] == "push"


@pytest.mark.parametrize(
    ("ref", "reason"),
    [("refs/heads/feature", "not_default_branch"), ("refs/tags/v1.0.0", "tag")],
)
def test_pushes_to_other_refs_start_nothing(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    ref: str,
    reason: str,
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    jobs_before = job_count(db)

    response = deliver_push(octocat, "f" * 40, ref=ref, delivery_id="other-ref")

    assert response.json() == {"outcome": "ignored"}
    delivery = db.get(WebhookDelivery, "other-ref")
    assert delivery is not None
    assert delivery.detail["reason"] == reason
    assert job_count(db) == jobs_before
    assert repository_row(db, repository_id).latest_push_sha is None


def test_a_burst_of_pushes_during_a_run_creates_at_most_two_runs(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    during_run: DuringRun,
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    pushed = [push(octocat)]

    def burst() -> None:
        pushed.extend(push(octocat) for _ in range(19))

    during_run("fetching_source", burst)
    run_all(run_worker_once)

    assert len(pushed) == len(set(pushed)) == 20
    jobs = push_jobs(db)
    assert len(jobs) == 2
    assert [job.status for job in jobs] == ["succeeded", "succeeded"]
    assert [job.payload["commit_sha"] for job in jobs] == [pushed[0], pushed[-1]]
    assert jobs[1].payload["pushed_commit_sha"] == pushed[-1]
    assert active_snapshot(db, repository_id).commit_sha == pushed[-1]
    repository = repository_row(db, repository_id)
    assert (repository.latest_push_sha, repository.latest_push_job_id) == (pushed[-1], jobs[1].id)


def test_a_push_during_a_run_leads_to_exactly_one_follow_up(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    during_run: DuringRun,
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    first = push(octocat)
    seen: dict[str, Any] = {}

    def push_again() -> None:
        [running] = push_jobs(db)
        seen["running"] = (running.status, running.payload.get("commit_sha"))
        seen["second"] = push(octocat)
        seen["jobs"] = [(job.id, job.status) for job in push_jobs(db)]

    during_run("fetching_source", push_again)
    run_all(run_worker_once)

    assert seen["running"] == ("running", first)
    [(_, running_status), (follow_up_id, follow_up_status)] = seen["jobs"]
    assert (running_status, follow_up_status) == ("running", "queued")
    jobs = push_jobs(db)
    assert len(jobs) == 2 and jobs[1].id == follow_up_id
    assert [job.payload["commit_sha"] for job in jobs] == [first, seen["second"]]
    assert active_snapshot(db, repository_id).commit_sha == seen["second"]


def test_a_repeated_delivery_creates_one_job(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    connect_and_index(octocat, run_worker_once)
    sha = get_fake_github().push(SAMPLE_APP_ID)

    outcomes = [
        deliver_push(octocat, sha, delivery_id="same-delivery").json()["outcome"]
        for _ in range(100)
    ]

    assert outcomes == ["processed"] + ["duplicate"] * 99
    assert len(push_jobs(db)) == 1


def test_a_failed_automatic_run_keeps_the_previous_version(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    previous = active_snapshot(db, repository_id).id
    limit = settings.max_files_per_snapshot
    monkeypatch.setattr(settings, "max_files_per_snapshot", 3)

    push(octocat)
    run_all(run_worker_once)

    [job] = push_jobs(db)
    assert (job.status, job.error_code) == ("failed", "limit_exceeded")
    repository = repository_row(db, repository_id)
    assert (repository.active_snapshot_id, repository.latest_push_job_id) == (previous, job.id)

    detail = octocat.get(f"/v1/repositories/{repository_id}").json()
    assert detail["state"] == "ready"
    assert detail["active_snapshot"]["id"] == str(previous)
    latest_push = detail.get("latest_push")
    assert latest_push is not None, "latest_push is missing from the repository response"
    assert latest_push["job"]["error"]["code"] == "limit_exceeded"

    # Later pushes are still processed.
    monkeypatch.setattr(settings, "max_files_per_snapshot", limit)
    later = push(octocat)
    run_all(run_worker_once)
    assert active_snapshot(db, repository_id).commit_sha == later


def test_an_answer_stays_pinned_to_its_commit_after_a_push(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    submitted = octocat.post(
        "/v1/analysis-runs", json={"repository_id": repository_id, "question": QUESTION}
    )
    assert submitted.status_code == 202, submitted.text
    run_url = submitted.json()["result_url"]
    run_all(run_worker_once)
    before = octocat.get(run_url).json()
    assert before["status"] == "succeeded"

    push(octocat)
    run_all(run_worker_once)

    assert active_snapshot(db, repository_id).commit_sha == SECOND
    after = octocat.get(run_url).json()
    assert after["commit_sha"] == INITIAL
    assert after["citations"] == before["citations"]


def test_a_push_for_an_unconnected_repository_is_ignored(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    fake = get_fake_github()
    fake.calls.clear()

    response = deliver_push(octocat, SECOND, delivery_id="nobody")

    assert response.json() == {"outcome": "ignored"}
    delivery = db.get(WebhookDelivery, "nobody")
    assert delivery is not None
    assert delivery.detail["reason"] == "not_connected"
    assert sum(fake.calls.values()) == 0
    assert job_count(db) == 0

    # A disconnected repository is no longer connected.
    repository_id = connect_and_index(octocat, run_worker_once)
    assert octocat.delete(f"/v1/repositories/{repository_id}").status_code == 204
    jobs_before = job_count(db)
    assert deliver_push(octocat, SECOND).json() == {"outcome": "ignored"}
    assert job_count(db) == jobs_before


def test_each_workspace_gets_its_own_run(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    octocat = signed_in("octocat")
    hubot = signed_in("hubot")
    repository_ids = {
        connect_and_index(octocat, run_worker_once),
        connect_and_index(hubot, run_worker_once),
    }
    sha = get_fake_github().push(SAMPLE_APP_ID)

    response = deliver_push(octocat, sha, delivery_id="both")

    assert response.json() == {"outcome": "processed"}
    delivery = db.get(WebhookDelivery, "both")
    assert delivery is not None
    assert delivery.detail["repositories"] == 2
    jobs = push_jobs(db)
    assert {str(job.repository_id) for job in jobs} == repository_ids
    assert len({job.workspace_id for job in jobs}) == 2

    run_all(run_worker_once)

    for repository_id in repository_ids:
        snapshot = active_snapshot(db, repository_id)
        assert snapshot.commit_sha == sha
        repository = repository_row(db, repository_id)
        assert snapshot.workspace_id == repository.workspace_id
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 4


def test_a_manual_reindex_joins_a_waiting_automatic_run(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    push(octocat)
    [waiting] = push_jobs(db)

    response = octocat.post(f"/v1/repositories/{repository_id}/index", json={})

    assert response.status_code == 202, response.text
    assert response.json()["job"]["id"] == str(waiting.id)
    assert [job.id for job in push_jobs(db)] == [waiting.id]
    assert db.scalar(select(func.count()).where(Job.status == "queued")) == 1


def test_a_renamed_default_branch_is_followed(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    fake = get_fake_github()
    fake.rename_default_branch(SAMPLE_APP_ID, "trunk")
    sha = fake.push(SAMPLE_APP_ID)

    response = deliver_push(octocat, sha, ref="refs/heads/trunk", default_branch="trunk")

    assert response.json() == {"outcome": "processed"}
    assert repository_row(db, repository_id).default_branch == "trunk"
    [job] = push_jobs(db)
    assert job.payload["branch"] == "trunk"

    run_all(run_worker_once)

    snapshot = active_snapshot(db, repository_id)
    assert (snapshot.commit_sha, snapshot.branch) == (sha, "trunk")
    jobs_before = job_count(db)
    old_branch = deliver_push(octocat, "f" * 40, ref="refs/heads/main", default_branch="trunk")
    assert old_branch.json() == {"outcome": "ignored"}
    assert job_count(db) == jobs_before


def test_a_force_push_to_an_indexed_commit_reuses_its_version(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    first_version = active_snapshot(db, repository_id).id
    push(octocat)
    run_all(run_worker_once)
    assert active_snapshot(db, repository_id).commit_sha == SECOND

    # Resetting the fake moves `main` back to the initial commit, as a force push does.
    get_fake_github().reset()
    deliver_push(octocat, INITIAL)
    run_all(run_worker_once)

    assert active_snapshot(db, repository_id).id == first_version
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 2
    force_push_job = push_jobs(db)[-1]
    assert (force_push_job.status, force_push_job.payload["commit_sha"]) == ("succeeded", INITIAL)


def test_an_automatic_run_verifies_the_workspace_owners_access(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    octocat = signed_in("octocat")
    octocat_repository = connect_and_index(octocat, run_worker_once)
    hubot_repository = connect_and_index(signed_in("hubot"), run_worker_once)
    fake = get_fake_github()
    # hubot can still see the public repository, but no longer through an installation.
    fake.revoke_access("hubot", SAMPLE_APP_ID)

    sha = fake.push(SAMPLE_APP_ID)
    deliver_push(octocat, sha)
    run_all(run_worker_once)

    jobs = {str(job.repository_id): job for job in push_jobs(db)}
    assert all(job.created_by is None for job in jobs.values())
    assert jobs[octocat_repository].status == "succeeded"
    assert active_snapshot(db, octocat_repository).commit_sha == sha
    hubot_job = jobs[hubot_repository]
    assert (hubot_job.status, hubot_job.error_code) == ("failed", "access_denied")
    assert active_snapshot(db, hubot_repository).commit_sha == INITIAL

    hubot_workspace = repository_row(db, hubot_repository).workspace_id
    [denied] = db.scalars(select(AuditEvent).where(AuditEvent.action == "access_denied")).all()
    assert (denied.workspace_id, denied.actor_user_id) == (hubot_workspace, None)
    assert denied.resource_id == hubot_repository
    assert denied.detail["trigger"] == "push"


def test_a_paused_repository_records_the_push_without_a_run(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    push(octocat)
    assert repository_row(db, repository_id).latest_push_job_id is not None
    run_all(run_worker_once)
    access.pause(db, repository_row(db, repository_id), "sign_in_required")
    db.commit()
    jobs_before = job_count(db)

    sha = get_fake_github().push(SAMPLE_APP_ID)
    response = deliver_push(octocat, sha)

    assert response.json() == {"outcome": "processed"}
    repository = repository_row(db, repository_id)
    assert (repository.latest_push_sha, repository.latest_push_job_id) == (sha, None)
    assert repository.latest_push_at is not None
    assert job_count(db) == jobs_before


def test_a_lost_repository_still_gets_a_run(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    # The run's access check may restore the repository (US2); queuing it is this story's part.
    repository_id = connect_and_index(octocat, run_worker_once)
    access.mark_access_lost(
        db, repository_row(db, repository_id), "app_uninstalled", trigger="notification"
    )
    db.commit()

    sha = get_fake_github().push(SAMPLE_APP_ID)
    response = deliver_push(octocat, sha)

    assert response.json() == {"outcome": "processed"}
    [job] = push_jobs(db)
    assert (job.status, job.trigger) == ("queued", "push")
    assert repository_row(db, repository_id).latest_push_job_id == job.id


def test_a_pause_in_one_workspace_does_not_stop_the_other(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    octocat = signed_in("octocat")
    octocat_repository = connect_and_index(octocat, run_worker_once)
    hubot_repository = connect_and_index(signed_in("hubot"), run_worker_once)
    access.pause(db, repository_row(db, hubot_repository), "sign_in_required")
    db.commit()

    sha = get_fake_github().push(SAMPLE_APP_ID)
    deliver_push(octocat, sha)

    [job] = push_jobs(db)
    assert str(job.repository_id) == octocat_repository
    paused = repository_row(db, hubot_repository)
    assert (paused.latest_push_sha, paused.latest_push_job_id) == (sha, None)
