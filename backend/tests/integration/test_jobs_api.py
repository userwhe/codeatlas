"""Integration tests for the job endpoints (T031, contracts/http-api.md "Jobs")."""

import itertools
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.auth.sessions import COOKIE_NAME, create_session
from codeatlas.jobs import queue
from codeatlas.models import AuditEvent, Job, Membership, Repository, User, Workspace

pytestmark = pytest.mark.integration

_github_ids = itertools.count(5000)


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


def add_job(db: Session, repository: Repository, *, dedupe_key: str | None = None) -> Job:
    job, _ = queue.enqueue(
        db,
        workspace_id=repository.workspace_id,
        kind="index_repository",
        repository_id=repository.id,
        created_by=repository.created_by,
        dedupe_key=dedupe_key,
    )
    db.commit()
    return job


def sign_in_as(client: TestClient, db: Session, repository: Repository) -> TestClient:
    """Give `client` a session cookie for the repository's owner."""
    assert repository.created_by is not None
    token = create_session(db, repository.created_by)
    db.commit()
    client.cookies.set(COOKIE_NAME, token)
    return client


def denied_events(db: Session) -> list[AuditEvent]:
    return list(db.scalars(select(AuditEvent).where(AuditEvent.action == "access_denied")))


def test_a_queued_job_reports_how_many_jobs_are_ahead(db: Session, client: TestClient) -> None:
    repository = make_repository(db)
    running = add_job(db, repository, dedupe_key="index:a")
    waiting = add_job(db, repository, dedupe_key="index:b")
    assert queue.claim_next(db) is not None
    sign_in_as(client, db, repository)

    response = client.get(f"/v1/jobs/{waiting.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "id": str(waiting.id),
        "kind": "index_repository",
        "status": "queued",
        "attempt": 0,
        "queued_behind": 1,
        "error": None,
        "created_at": body["created_at"],
        "started_at": None,
        "finished_at": None,
    }
    running_body = client.get(f"/v1/jobs/{running.id}").json()
    assert (running_body["status"], running_body["attempt"]) == ("running", 1)
    assert running_body["queued_behind"] is None
    assert running_body["started_at"] is not None


def test_a_failed_job_returns_its_error(db: Session, client: TestClient) -> None:
    repository = make_repository(db)
    job = add_job(db, repository)
    claimed = queue.claim_next(db)
    assert claimed is not None
    queue.fail(
        db,
        job.id,
        claimed.fencing_token,
        code="repository_empty",
        message="The repository has no commits.",
        permanent=True,
    )
    sign_in_as(client, db, repository)

    body = client.get(f"/v1/jobs/{job.id}").json()

    assert body["status"] == "failed"
    assert body["error"] == {
        "code": "repository_empty",
        "message": "The repository has no commits.",
        "retryable": False,
    }
    assert body["finished_at"] is not None
    events = client.get(f"/v1/jobs/{job.id}/events").json()
    assert events["job_status"] == "failed"
    assert [item["event_type"] for item in events["items"]] == ["queued", "failed"]


def test_another_workspaces_job_is_not_found_and_audited(db: Session, client: TestClient) -> None:
    mine = make_repository(db)
    theirs = make_repository(db, "hubot")
    job = add_job(db, theirs)
    sign_in_as(client, db, mine)

    for path in (f"/v1/jobs/{job.id}", f"/v1/jobs/{job.id}/events"):
        response = client.get(path)
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "not_found"

    audit = denied_events(db)
    assert len(audit) == 2
    assert {(e.resource_type, e.resource_id, e.outcome) for e in audit} == {
        ("job", str(job.id), "denied")
    }
    assert {(e.workspace_id, e.actor_user_id) for e in audit} == {
        (mine.workspace_id, mine.created_by)
    }


def test_a_job_of_a_disconnected_repository_is_not_found(db: Session, client: TestClient) -> None:
    repository = make_repository(db)
    job = add_job(db, repository)
    repository.deleted_at = datetime.now(UTC)
    db.commit()
    sign_in_as(client, db, repository)

    response = client.get(f"/v1/jobs/{job.id}")

    assert response.status_code == 404
    assert len(denied_events(db)) == 1


@pytest.mark.parametrize("job_id", ["not-a-uuid", str(uuid.uuid4())])
def test_an_unknown_job_is_not_found(db: Session, client: TestClient, job_id: str) -> None:
    sign_in_as(client, db, make_repository(db))

    response = client.get(f"/v1/jobs/{job_id}/events")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert denied_events(db) == []


def test_job_endpoints_require_a_session(db: Session, client: TestClient) -> None:
    job = add_job(db, make_repository(db))

    response = client.get(f"/v1/jobs/{job.id}")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
