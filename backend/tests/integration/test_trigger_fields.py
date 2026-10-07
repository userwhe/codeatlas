"""Integration tests for triggers and the latest push in the API (T025, contracts/http-api.md)."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from codeatlas.github.fake import SAMPLE_APP_ID, commit_sha
from codeatlas.models import Job, Repository, Snapshot

pytestmark = pytest.mark.integration

PUSHED_SHA = "9b1c" * 10
RECEIVED_AT = datetime(2026, 10, 6, 9, 10, 41, tzinfo=UTC)


def run_all(run_worker_once: Callable[[], bool]) -> None:
    while run_worker_once():
        pass


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


def indexed_sample_app(client: TestClient, run_worker_once: Callable[[], bool]) -> dict[str, Any]:
    """Connect and index sample-app; return its repository representation."""
    response = client.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})
    assert response.status_code == 202, response.text
    run_all(run_worker_once)
    repository = client.get(f"/v1/repositories/{response.json()['repository']['id']}")
    assert repository.status_code == 200, repository.text
    body: dict[str, Any] = repository.json()
    assert body["state"] == "ready"
    return body


def push_job(db: Session, repository: Repository, **fields: Any) -> Job:
    """Insert an indexing job started by a push, as the webhook handler would queue it."""
    job = Job(
        workspace_id=repository.workspace_id,
        kind="index_repository",
        repository_id=repository.id,
        payload={"branch": repository.default_branch},
        trigger="push",
        **fields,
    )
    db.add(job)
    db.flush()
    return job


def record_push(db: Session, repository_id: str, job: Job | None) -> Repository:
    repository = db.get(Repository, uuid.UUID(repository_id))
    assert repository is not None
    repository.latest_push_sha = PUSHED_SHA
    repository.latest_push_at = RECEIVED_AT
    repository.latest_push_job_id = job.id if job is not None else None
    db.commit()
    return repository


def test_user_indexing_is_labeled_user_and_has_no_push(
    octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository = indexed_sample_app(octocat, run_worker_once)

    assert repository["latest_push"] is None
    latest = repository["latest_indexing_job"]
    assert (latest["status"], latest["trigger"]) == ("succeeded", "user")
    listed = octocat.get("/v1/repositories").json()["items"]
    assert [(item["id"], item["latest_push"]) for item in listed] == [(repository["id"], None)]

    versions = octocat.get(f"/v1/repositories/{repository['id']}/snapshots").json()["items"]
    assert versions
    assert all(version["trigger"] == "user" for version in versions)

    job = octocat.get(f"/v1/jobs/{latest['id']}")
    assert job.status_code == 200, job.text
    assert job.json()["trigger"] == "user"


def test_latest_push_reports_its_commit_and_job(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    indexed = indexed_sample_app(octocat, run_worker_once)
    repository = db.get(Repository, uuid.UUID(indexed["id"]))
    assert repository is not None
    job = push_job(db, repository)
    record_push(db, indexed["id"], job)

    body = octocat.get(f"/v1/repositories/{indexed['id']}").json()

    expected_job = {"id": str(job.id), "status": "queued", "trigger": "push", "error": None}
    assert body["latest_push"] == {
        "commit_sha": PUSHED_SHA,
        "received_at": body["latest_push"]["received_at"],
        "job": expected_job,
    }
    assert datetime.fromisoformat(body["latest_push"]["received_at"]) == RECEIVED_AT
    assert body["latest_indexing_job"] == expected_job
    assert body["state"] == "indexing"
    assert octocat.get(f"/v1/jobs/{job.id}").json()["trigger"] == "push"


def test_a_failed_push_job_reports_its_error_and_keeps_the_previous_version(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    indexed = indexed_sample_app(octocat, run_worker_once)
    repository = db.get(Repository, uuid.UUID(indexed["id"]))
    assert repository is not None
    job = push_job(
        db,
        repository,
        status="failed",
        error_code="limit_exceeded",
        error_message="The repository exceeds the limit max_files_per_snapshot (5000).",
        error_retryable=False,
        finished_at=datetime.now(UTC),
    )
    record_push(db, indexed["id"], job)

    body = octocat.get(f"/v1/repositories/{indexed['id']}").json()

    assert body["latest_push"]["job"] == {
        "id": str(job.id),
        "status": "failed",
        "trigger": "push",
        "error": {
            "code": "limit_exceeded",
            "message": "The repository exceeds the limit max_files_per_snapshot (5000).",
            "retryable": False,
        },
    }
    # The version built before the push is still the default (US1-6).
    assert body["state"] == "ready"
    assert body["active_snapshot"] == indexed["active_snapshot"]
    assert body["active_snapshot"]["commit_sha"] == commit_sha(SAMPLE_APP_ID, "initial")


def test_a_push_without_a_job_has_a_null_job(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    indexed = indexed_sample_app(octocat, run_worker_once)
    record_push(db, indexed["id"], None)

    latest_push = octocat.get(f"/v1/repositories/{indexed['id']}").json()["latest_push"]

    assert latest_push["commit_sha"] == PUSHED_SHA
    assert latest_push["job"] is None


def test_each_version_reports_the_trigger_of_the_job_that_built_it(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    indexed = indexed_sample_app(octocat, run_worker_once)
    repository = db.get(Repository, uuid.UUID(indexed["id"]))
    assert repository is not None
    built_by_user = db.get(Snapshot, uuid.UUID(indexed["active_snapshot"]["id"]))
    assert built_by_user is not None
    job = push_job(db, repository, status="succeeded", finished_at=datetime.now(UTC))

    def ready_version(sha: str, job_id: uuid.UUID | None, minute: int) -> Snapshot:
        return Snapshot(
            workspace_id=repository.workspace_id,
            repository_id=repository.id,
            commit_sha=sha,
            branch=repository.default_branch,
            index_version=built_by_user.index_version,
            status="ready",
            job_id=job_id,
            ready_at=datetime(2099, 1, 1, 0, minute, tzinfo=UTC),
        )

    after_push = ready_version(PUSHED_SHA, job.id, 2)
    without_job = ready_version("0" * 40, None, 1)
    db.add_all([after_push, without_job])
    db.commit()

    response = octocat.get(f"/v1/repositories/{indexed['id']}/snapshots")

    assert response.status_code == 200, response.text
    assert [(item["id"], item["trigger"]) for item in response.json()["items"]] == [
        (str(after_push.id), "push"),
        (str(without_job.id), None),
        (str(built_by_user.id), "user"),
    ]
