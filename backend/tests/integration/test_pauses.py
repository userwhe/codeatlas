"""Integration tests for paused automatic updates (T037, US3-4, FR-016, FR-017, research R8).

Automatic updates pause when the owner's GitHub authorization lapses, found by a run or reported
by a `github_app_authorization.revoked` notification, and when a repository turns private with no
acceptance of the external processing disclosure on record. Pauses never mark a repository lost,
and a lost repository is never paused. Signing in again resumes sign-in pauses and checks access
at once. Accepting the disclosure through a re-index request is covered in
test_disclosure_acceptance.py.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from codeatlas.github.fake import NO_CODE_ID, SAMPLE_APP_ID, SOLO_ID, FakeGitHub, get_fake_github
from codeatlas.github.gateway import GitHubRepository, UserAuthorizationInvalid
from codeatlas.models import (
    AuditEvent,
    GitHubCredential,
    Job,
    JobEvent,
    Repository,
    Snapshot,
    User,
    WebhookDelivery,
)
from codeatlas.workspace import access
from codeatlas.workspace.sync import CHECK_INTERVAL, schedule_checks
from tests.webhooks import (
    authorization_revoked_payload,
    installation_payload,
    push_payload,
    send_delivery,
)

pytestmark = pytest.mark.integration

OCTOCAT_GITHUB_ID = 1001
OCTOCAT_INSTALLATION = 5002
OCTO_ORG_INSTALLATION = 5001
UP_TO_DATE = "Already up to date"
PAUSED_FOR_SIGN_IN = ("paused", "sign_in_required")
PAUSED_FOR_DISCLOSURE = ("paused", "external_processing_not_accepted")


def run_all(run_worker_once: Callable[[], bool]) -> None:
    for _ in range(50):
        if not run_worker_once():
            return
    raise AssertionError("jobs did not finish")


def connect(client: TestClient, github_id: int) -> str:
    response = client.post("/v1/repositories", json={"github_repository_id": github_id})
    assert response.status_code == 202, response.text
    repository_id: str = response.json()["repository"]["id"]
    return repository_id


def connect_and_index(
    client: TestClient, run_worker_once: Callable[[], bool], github_id: int = SAMPLE_APP_ID
) -> str:
    repository_id = connect(client, github_id)
    run_all(run_worker_once)
    return repository_id


def reindex(client: TestClient, repository_id: str) -> uuid.UUID:
    response = client.post(f"/v1/repositories/{repository_id}/index", json={})
    assert response.status_code == 202, response.text
    return uuid.UUID(response.json()["job"]["id"])


def deliver(client: TestClient, event: str, payload: dict[str, Any], delivery_id: str) -> None:
    response = send_delivery(client, event, payload, delivery_id=delivery_id)
    assert response.status_code == 202, response.text


def revoked_notification(client: TestClient, github_user_id: int = OCTOCAT_GITHUB_ID) -> None:
    payload = authorization_revoked_payload(github_user_id)
    deliver(client, "github_app_authorization", payload, delivery_id="revoked")


def repository_row(db: Session, repository_id: str | uuid.UUID) -> Repository:
    db.expire_all()
    repository = db.get(Repository, repository_id)
    assert repository is not None
    return repository


def state(db: Session, repository_id: str) -> tuple[str, str | None]:
    repository = repository_row(db, repository_id)
    return repository.access_state, repository.access_reason


def job_row(db: Session, job_id: uuid.UUID) -> Job:
    db.expire_all()
    job = db.get(Job, job_id)
    assert job is not None
    return job


def job_count(db: Session) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(Job)) or 0


def last_event(db: Session, job_id: uuid.UUID) -> JobEvent:
    event = db.scalars(
        select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.seq.desc())
    ).first()
    assert event is not None
    return event


def audit(db: Session, action: str) -> list[AuditEvent]:
    db.expire_all()
    return list(
        db.scalars(select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.id))
    )


def user_id(db: Session, login: str) -> uuid.UUID:
    return db.scalars(select(User.id).where(User.github_login == login)).one()


def has_credential(db: Session, login: str) -> bool:
    db.expire_all()
    return db.get(GitHubCredential, user_id(db, login)) is not None


def due_time(db: Session, repository_id: str) -> datetime:
    checked_at = repository_row(db, repository_id).access_checked_at
    assert checked_at is not None
    return checked_at + CHECK_INTERVAL + timedelta(minutes=1)


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


# Lapsed authorization found by a run (US3-4) --------------------------------------------------


@pytest.mark.parametrize("expired", [False, True], ids=["token-revoked", "refresh-refused"])
def test_a_daily_check_with_a_lapsed_authorization_pauses_updates(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], expired: bool
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    checked_at = repository_row(db, repository_id).access_checked_at
    fake = get_fake_github()
    fake.revoke_authorization("octocat")
    if expired:
        # The run refreshes the token first, and GitHub refuses the refresh.
        stale = datetime.now(UTC) - timedelta(minutes=5)
        db.execute(update(GitHubCredential).values(access_token_expires_at=stale))
        db.commit()
    fake.calls.clear()

    assert schedule_checks(db, now=due_time(db, repository_id)) == 1
    db.commit()
    run_all(run_worker_once)

    [job] = [job for job in db.scalars(select(Job)) if job.trigger == "check"]
    job = job_row(db, job.id)
    assert (job.status, job.error_code, job.error_retryable, job.attempt) == (
        "failed",
        "github_sign_in_required",
        True,
        1,
    )
    assert fake.calls["refresh_user_token"] == (1 if expired else 0)
    assert fake.calls["open_tarball"] == 0
    repository = repository_row(db, repository_id)
    assert (repository.access_state, repository.access_reason) == PAUSED_FOR_SIGN_IN
    assert repository.access_checked_at == checked_at
    assert audit(db, "repository_access_lost") == []
    [paused] = audit(db, "automatic_updates_paused")
    assert (paused.actor_user_id, paused.workspace_id) == (None, repository.workspace_id)
    assert (paused.resource_type, paused.resource_id) == ("repository", repository_id)
    assert (paused.outcome, paused.detail) == ("success", {"reason": "sign_in_required"})
    # The credential no longer works, so it is deleted; browsing needs no GitHub call, so the
    # session stays valid and the page shows the pause.
    assert not has_credential(db, "octocat")
    shown = octocat.get(f"/v1/repositories/{repository_id}")
    assert shown.status_code == 200, shown.text
    assert shown.json()["automatic_updates"] == {"state": "paused", "reason": "sign_in_required"}
    assert octocat.get(f"/v1/repositories/{repository_id}/snapshots").status_code == 200


def test_signing_in_while_a_run_fails_keeps_the_new_authorization(
    db: Session,
    signed_in: Callable[[str], TestClient],
    run_worker_once: Callable[[], bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    octocat = signed_in("octocat")
    repository_id = connect_and_index(octocat, run_worker_once)
    get_fake_github().revoke_authorization("octocat")
    original = FakeGitHub.get_repository

    def get_repository(
        self: FakeGitHub, user_token: str, github_repository_id: int
    ) -> GitHubRepository:
        try:
            return original(self, user_token, github_repository_id)
        except UserAuthorizationInvalid:
            # GitHub rejected the old token; the owner signs in before the run handles that.
            signed_in("octocat")
            raise

    monkeypatch.setattr(FakeGitHub, "get_repository", get_repository)

    job_id = reindex(octocat, repository_id)
    run_all(run_worker_once)

    job = job_row(db, job_id)
    assert (job.status, job.error_code) == ("failed", "github_sign_in_required")
    assert state(db, repository_id) == ("active", None)
    assert has_credential(db, "octocat")
    assert audit(db, "automatic_updates_paused") == []


# Authorization revoked notification (contracts/github-webhooks.md) ----------------------------


def test_a_revoked_authorization_notification_pauses_the_owners_active_repositories(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    octocat = signed_in("octocat")
    hubot = signed_in("hubot")
    sample, no_code, solo = (
        connect(octocat, github_id) for github_id in (SAMPLE_APP_ID, NO_CODE_ID, SOLO_ID)
    )
    hubots_sample = connect(hubot, SAMPLE_APP_ID)
    run_all(run_worker_once)
    deliver(octocat, "installation", installation_payload("deleted", OCTOCAT_INSTALLATION), "solo")
    assert state(db, solo) == ("access_lost", "app_uninstalled")
    fake = get_fake_github()
    fake.calls.clear()

    revoked_notification(octocat)

    assert sum(fake.calls.values()) == 0
    delivery = db.get(WebhookDelivery, "revoked")
    assert delivery is not None
    assert (delivery.outcome, delivery.detail) == ("processed", {"repositories": 2})
    assert state(db, sample) == state(db, no_code) == PAUSED_FOR_SIGN_IN
    assert state(db, solo) == ("access_lost", "app_uninstalled")
    assert state(db, hubots_sample) == ("active", None)
    paused = audit(db, "automatic_updates_paused")
    assert sorted(event.resource_id or "" for event in paused) == sorted([sample, no_code])
    assert all(event.detail == {"reason": "sign_in_required"} for event in paused)
    assert not has_credential(db, "octocat")
    assert has_credential(db, "hubot")
    assert octocat.get("/v1/me").status_code == 200


def test_a_revoked_authorization_of_an_unknown_user_is_ignored(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)

    revoked_notification(octocat, github_user_id=999_999)

    delivery = db.get(WebhookDelivery, "revoked")
    assert delivery is not None
    assert (delivery.outcome, delivery.detail) == ("ignored", {"reason": "unknown_user"})
    assert state(db, repository_id) == ("active", None)
    assert has_credential(db, "octocat")


# While paused (research R8) --------------------------------------------------------------------


def test_a_paused_repository_records_pushes_and_skips_checks(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    revoked_notification(octocat)
    assert state(db, repository_id) == PAUSED_FOR_SIGN_IN
    jobs_before = job_count(db)
    sha = get_fake_github().push(SAMPLE_APP_ID)

    deliver(octocat, "push", push_payload(SAMPLE_APP_ID, sha), delivery_id="while-paused")

    repository = repository_row(db, repository_id)
    assert (repository.latest_push_sha, repository.latest_push_job_id) == (sha, None)
    assert repository.latest_push_at is not None
    assert job_count(db) == jobs_before
    assert schedule_checks(db, now=due_time(db, repository_id)) == 0
    db.commit()
    assert job_count(db) == jobs_before


# Signing in again (FR-016) ---------------------------------------------------------------------


def test_signing_in_again_resumes_updates_and_checks_access_at_once(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    octocat = signed_in("octocat")
    sample, no_code, solo = (
        connect(octocat, github_id) for github_id in (SAMPLE_APP_ID, NO_CODE_ID, SOLO_ID)
    )
    run_all(run_worker_once)
    # A pause for the disclosure is lifted only by accepting it.
    access.pause(db, repository_row(db, solo), "external_processing_not_accepted")
    db.commit()
    checked_at = {
        repository_id: repository_row(db, repository_id).access_checked_at
        for repository_id in (sample, no_code)
    }
    get_fake_github().revoke_authorization("octocat")
    revoked_notification(octocat)
    assert state(db, sample) == state(db, no_code) == PAUSED_FOR_SIGN_IN
    jobs_before = {job.id for job in db.scalars(select(Job))}

    signed_in("octocat")

    assert state(db, sample) == state(db, no_code) == ("active", None)
    assert state(db, solo) == PAUSED_FOR_DISCLOSURE
    assert has_credential(db, "octocat")
    resumed = audit(db, "automatic_updates_resumed")
    assert sorted(event.resource_id or "" for event in resumed) == sorted([sample, no_code])
    for event in resumed:
        assert (event.actor_user_id, event.detail) == (user_id(db, "octocat"), {"via": "sign_in"})
    checks = [job for job in db.scalars(select(Job)) if job.id not in jobs_before]
    assert sorted(str(job.repository_id) for job in checks) == sorted([sample, no_code])
    for job in checks:
        assert (job.status, job.trigger, job.created_by, job.payload) == (
            "queued",
            "check",
            None,
            {"branch": "main"},
        )

    run_all(run_worker_once)

    for job in checks:
        finished = job_row(db, job.id)
        assert (finished.status, last_event(db, job.id).message) == ("succeeded", UP_TO_DATE)
        checked = repository_row(db, finished.repository_id).access_checked_at
        previous = checked_at[str(finished.repository_id)]
        assert checked is not None and previous is not None and checked > previous


def test_signing_in_resumes_only_the_users_own_workspace(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    octocat = signed_in("octocat")
    repository_id = connect_and_index(octocat, run_worker_once)
    revoked_notification(octocat)
    jobs_before = job_count(db)

    signed_in("hubot")

    assert state(db, repository_id) == PAUSED_FOR_SIGN_IN
    assert job_count(db) == jobs_before
    assert audit(db, "automatic_updates_resumed") == []


# A repository that became private (FR-017) -----------------------------------------------------


@pytest.mark.parametrize("trigger", ["push", "user"])
def test_a_repository_that_became_private_pauses_before_anything_is_fetched(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], trigger: str
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    indexed = repository_row(db, repository_id)
    version, checked_at = indexed.active_snapshot_id, indexed.access_checked_at
    assert (indexed.is_private, indexed.external_processing_accepted_at) == (False, None)
    fake = get_fake_github()
    fake.make_private(SAMPLE_APP_ID)
    sha = fake.push(SAMPLE_APP_ID)
    fake.calls.clear()

    if trigger == "push":
        deliver(octocat, "push", push_payload(SAMPLE_APP_ID, sha, private=True), "private-push")
        job_id = repository_row(db, repository_id).latest_push_job_id
        assert job_id is not None
    else:
        job_id = reindex(octocat, repository_id)
    run_all(run_worker_once)

    job = job_row(db, job_id)
    assert (job.status, job.trigger, job.error_code, job.error_retryable, job.attempt) == (
        "failed",
        trigger,
        "external_processing_not_accepted",
        False,
        1,
    )
    assert "private" in (job.error_message or "")
    assert fake.calls["open_tarball"] == 0
    assert db.scalars(select(Snapshot).where(Snapshot.job_id == job.id)).all() == []
    repository = repository_row(db, repository_id)
    assert (repository.access_state, repository.access_reason) == PAUSED_FOR_DISCLOSURE
    assert (repository.is_private, repository.external_processing_accepted_at) == (True, None)
    assert repository.active_snapshot_id == version
    # The access check itself passed.
    assert checked_at is not None and repository.access_checked_at is not None
    assert repository.access_checked_at > checked_at
    [paused] = audit(db, "automatic_updates_paused")
    assert paused.detail == {"reason": "external_processing_not_accepted"}
    assert audit(db, "repository_access_lost") == []

    # Accepting the disclosure resumes updates, and the new head is indexed.
    accepted = octocat.post(
        f"/v1/repositories/{repository_id}/index", json={"accept_external_processing": True}
    )
    assert accepted.status_code == 202, accepted.text
    run_all(run_worker_once)

    assert job_row(db, uuid.UUID(accepted.json()["job"]["id"])).status == "succeeded"
    repository = repository_row(db, repository_id)
    assert (repository.access_state, repository.access_reason) == ("active", None)
    snapshot = db.get(Snapshot, repository.active_snapshot_id)
    assert snapshot is not None and snapshot.commit_sha == sha


# Loss dominates (research R7) ------------------------------------------------------------------


def test_a_lost_repository_is_never_paused(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    uninstalled = installation_payload("deleted", OCTO_ORG_INSTALLATION)
    deliver(octocat, "installation", uninstalled, delivery_id="uninstalled")
    lost = ("access_lost", "app_uninstalled")
    assert state(db, repository_id) == lost
    get_fake_github().revoke_authorization("octocat")

    job_id = reindex(octocat, repository_id)
    run_all(run_worker_once)

    job = job_row(db, job_id)
    assert (job.status, job.error_code) == ("failed", "github_sign_in_required")
    assert state(db, repository_id) == lost
    assert not has_credential(db, "octocat")

    revoked_notification(octocat)

    delivery = db.get(WebhookDelivery, "revoked")
    assert delivery is not None
    assert (delivery.outcome, delivery.detail) == ("processed", {"repositories": 0})
    assert state(db, repository_id) == lost
    assert audit(db, "automatic_updates_paused") == []
