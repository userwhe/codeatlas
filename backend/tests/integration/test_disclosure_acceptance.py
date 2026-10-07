"""Integration tests for paused automatic updates in the API (T043, FR-016, FR-017, research R8).

`octocat` indexes `octo-org/sample-app`. The repository is then paused directly, as a run that
finds it private with no acceptance on record would pause it. The repository reports the pause in
`automatic_updates`. A re-index request must accept the external processing disclosure; accepting
records the acceptance, resumes automatic updates, and queues a `user` run. A pause for sign-in
is resumed only by signing in, never by a re-index request.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from codeatlas.github.fake import SAMPLE_APP_ID
from codeatlas.models import AuditEvent, Job, Repository, User
from codeatlas.workspace import access

pytestmark = pytest.mark.integration

ON = {"state": "on", "reason": None}
PAUSED_FOR_DISCLOSURE = {"state": "paused", "reason": "external_processing_not_accepted"}
PAUSED_FOR_SIGN_IN = {"state": "paused", "reason": "sign_in_required"}


def run_all(run_worker_once: Callable[[], bool]) -> None:
    for _ in range(50):
        if not run_worker_once():
            return
    raise AssertionError("jobs did not finish")


def load(db: Session, repository_id: str) -> Repository:
    db.expire_all()
    repository = db.get(Repository, uuid.UUID(repository_id))
    assert repository is not None
    return repository


def job_count(db: Session) -> int | None:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(Job))


def resumed_events(db: Session) -> list[AuditEvent]:
    db.expire_all()
    return list(
        db.scalars(select(AuditEvent).where(AuditEvent.action == "automatic_updates_resumed"))
    )


def octocat_id(db: Session) -> uuid.UUID:
    return db.scalars(select(User.id).where(User.github_login == "octocat")).one()


def queued_job(db: Session, response: Any) -> Job:
    assert response.status_code == 202, response.text
    db.expire_all()
    job = db.get(Job, uuid.UUID(response.json()["job"]["id"]))
    assert job is not None
    return job


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


@pytest.fixture
def repository_id(octocat: TestClient, run_worker_once: Callable[[], bool]) -> str:
    """octocat's sample-app, connected and indexed."""
    connected = octocat.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})
    assert connected.status_code == 202, connected.text
    run_all(run_worker_once)
    found: str = connected.json()["repository"]["id"]
    assert octocat.get(f"/v1/repositories/{found}").json()["state"] == "ready"
    return found


def pause_for_disclosure(db: Session, repository_id: str) -> Repository:
    """Pause the repository as a run that finds it private with no acceptance would."""
    repository = load(db, repository_id)
    assert access.pause(db, repository, "external_processing_not_accepted")
    repository.is_private = True
    repository.external_processing_accepted_at = None
    db.commit()
    return repository


@pytest.fixture
def paused_id(db: Session, repository_id: str) -> str:
    pause_for_disclosure(db, repository_id)
    return repository_id


def test_automatic_updates_report_pauses_but_not_losses(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    url = f"/v1/repositories/{repository_id}"

    def automatic_updates() -> tuple[Any, Any]:
        detail = octocat.get(url)
        assert detail.status_code == 200, detail.text
        (listed,) = octocat.get("/v1/repositories").json()["items"]
        return detail.json()["automatic_updates"], listed["automatic_updates"]

    assert automatic_updates() == (ON, ON)

    pause_for_disclosure(db, repository_id)
    assert automatic_updates() == (PAUSED_FOR_DISCLOSURE, PAUSED_FOR_DISCLOSURE)

    access.pause(db, load(db, repository_id), "sign_in_required")
    db.commit()
    assert automatic_updates() == (PAUSED_FOR_SIGN_IN, PAUSED_FOR_SIGN_IN)

    # Pushes still start access checks for a lost repository, so its updates read as on.
    access.mark_access_lost(db, load(db, repository_id), "app_uninstalled", trigger="push")
    db.commit()
    assert automatic_updates() == (ON, ON)


@pytest.mark.parametrize(
    "body",
    [None, {}, {"accept_external_processing": False}, {"branch": "main"}],
    ids=["no-body", "empty", "declined", "branch-only"],
)
def test_reindex_without_acceptance_is_refused(
    db: Session, octocat: TestClient, paused_id: str, body: dict[str, Any] | None
) -> None:
    before = job_count(db)

    response = octocat.post(f"/v1/repositories/{paused_id}/index", json=body)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "external_processing_not_accepted"
    assert "external model provider" in error["message"]
    assert job_count(db) == before
    repository = load(db, paused_id)
    assert (repository.access_state, repository.access_reason) == (
        "paused",
        "external_processing_not_accepted",
    )
    assert repository.external_processing_accepted_at is None
    assert resumed_events(db) == []


def test_reindex_with_acceptance_records_it_and_resumes(
    db: Session, octocat: TestClient, paused_id: str
) -> None:
    started = datetime.now(UTC)

    response = octocat.post(
        f"/v1/repositories/{paused_id}/index", json={"accept_external_processing": True}
    )

    job = queued_job(db, response)
    assert (job.status, job.trigger, job.payload) == ("queued", "user", {"branch": "main"})
    assert job.created_by == octocat_id(db)
    repository = load(db, paused_id)
    assert (repository.access_state, repository.access_reason) == ("active", None)
    accepted_at = repository.external_processing_accepted_at
    assert accepted_at is not None
    assert started <= accepted_at <= datetime.now(UTC)
    (event,) = resumed_events(db)
    assert event.actor_user_id == octocat_id(db)
    assert event.workspace_id == repository.workspace_id
    assert (event.resource_type, event.resource_id) == ("repository", paused_id)
    assert (event.outcome, event.detail) == ("success", {"via": "acceptance"})

    body = octocat.get(f"/v1/repositories/{paused_id}").json()
    assert body["automatic_updates"] == ON
    assert body["latest_indexing_job"]["id"] == str(job.id)
    assert body["latest_indexing_job"]["trigger"] == "user"


def test_an_accepted_reindex_replays_with_its_idempotency_key(
    db: Session, octocat: TestClient, paused_id: str
) -> None:
    url = f"/v1/repositories/{paused_id}/index"
    headers = {"Idempotency-Key": "accept-once"}
    accepted = {"accept_external_processing": True}

    first = octocat.post(url, json=accepted, headers=headers)
    replay = octocat.post(url, json=accepted, headers=headers)
    changed = octocat.post(url, json={}, headers=headers)

    assert first.status_code == replay.status_code == 202, (first.text, replay.text)
    assert replay.json() == first.json()
    assert (changed.status_code, changed.json()["error"]["code"]) == (409, "idempotency_conflict")
    assert job_count(db) == 2  # the first indexing run, and this one
    assert len(resumed_events(db)) == 1


def test_acceptance_on_an_active_repository_is_harmless(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    response = octocat.post(
        f"/v1/repositories/{repository_id}/index", json={"accept_external_processing": True}
    )

    job = queued_job(db, response)
    assert (job.status, job.trigger) == ("queued", "user")
    repository = load(db, repository_id)
    assert (repository.access_state, repository.access_reason) == ("active", None)
    # A public repository needs no acceptance, so none is recorded.
    assert repository.external_processing_accepted_at is None
    assert resumed_events(db) == []


def test_a_sign_in_pause_is_not_resumed_by_reindexing(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    access.pause(db, load(db, repository_id), "sign_in_required")
    db.commit()
    url = f"/v1/repositories/{repository_id}/index"

    plain = octocat.post(url, json={})
    accepting = octocat.post(url, json={"accept_external_processing": True})

    job = queued_job(db, plain)
    assert (job.status, job.trigger) == ("queued", "user")
    # The second request joins the waiting run.
    assert queued_job(db, accepting).id == job.id
    repository = load(db, repository_id)
    assert (repository.access_state, repository.access_reason) == ("paused", "sign_in_required")
    assert repository.external_processing_accepted_at is None
    assert resumed_events(db) == []
    automatic_updates = octocat.get(f"/v1/repositories/{repository_id}").json()
    assert automatic_updates["automatic_updates"] == PAUSED_FOR_SIGN_IN
