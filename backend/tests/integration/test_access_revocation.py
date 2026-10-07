"""Integration tests for detecting, enforcing, and recovering from lost GitHub access (T027, US2).

Runs start from manual re-index requests, so these tests do not depend on pushes, except the
push-to-a-lost-repository case. Reads of a lost repository are covered in
test_access_read_denial.py.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from codeatlas.github.fake import (
    NO_CODE_ID,
    SAMPLE_APP_ID,
    SAMPLE_APP_PRIVATE_ID,
    SOLO_ID,
    FakeGitHub,
    commit_sha,
    get_fake_github,
)
from codeatlas.github.gateway import AppCredentialsRejected, GitHubAccessDenied, GitHubNotFound
from codeatlas.jobs import queue
from codeatlas.jobs.worker import JobContext
from codeatlas.models import (
    AuditEvent,
    GitHubCredential,
    Job,
    JobEvent,
    Repository,
    Snapshot,
    WebhookDelivery,
)
from codeatlas.workspace.access import ACCESS_LOST_CANCEL_MESSAGE
from tests.webhooks import (
    installation_payload,
    installation_repositories_payload,
    push_payload,
    send_delivery,
)

pytestmark = pytest.mark.integration

# Installations in the fake: `octo-org` repositories are under 5001, `octocat/solo` under 5002.
OCTO_ORG_INSTALLATION = 5001
OCTOCAT_INSTALLATION = 5002
INITIAL = commit_sha(SAMPLE_APP_ID, "initial")
QUESTION = "Where are repository permissions checked?"
RUN_CANCELED = "Access to the repository was lost."

Switch = Callable[[FakeGitHub], object]
DuringRun = Callable[[str, Callable[[], None]], None]


def run_all(run_worker_once: Callable[[], bool]) -> None:
    while run_worker_once():
        pass


def connect(client: TestClient, github_id: int) -> str:
    response = client.post(
        "/v1/repositories",
        json={"github_repository_id": github_id, "accept_external_processing": True},
    )
    assert response.status_code == 202, response.text
    repository_id: str = response.json()["repository"]["id"]
    return repository_id


def connect_and_index(
    client: TestClient, run_worker_once: Callable[[], bool], github_id: int = SAMPLE_APP_ID
) -> str:
    repository_id = connect(client, github_id)
    run_all(run_worker_once)
    return repository_id


def reindex(client: TestClient, repository_id: str) -> str:
    response = client.post(f"/v1/repositories/{repository_id}/index", json={})
    assert response.status_code == 202, response.text
    job_id: str = response.json()["job"]["id"]
    return job_id


def repository_row(db: Session, repository_id: str | uuid.UUID) -> Repository:
    db.expire_all()
    repository = db.get(Repository, repository_id)
    assert repository is not None
    return repository


def job_row(db: Session, job_id: str | uuid.UUID) -> Job:
    db.expire_all()
    job = db.get(Job, job_id)
    assert job is not None
    return job


def last_event(db: Session, job_id: str | uuid.UUID) -> JobEvent:
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


def assert_lost(db: Session, repository_id: str, reason: str) -> Repository:
    repository = repository_row(db, repository_id)
    assert (repository.access_state, repository.access_reason) == ("access_lost", reason)
    assert repository.access_lost_at is not None
    return repository


def assert_active(db: Session, repository_id: str) -> Repository:
    repository = repository_row(db, repository_id)
    assert (repository.access_state, repository.access_reason) == ("active", None)
    assert repository.access_lost_at is None
    return repository


def deliver(client: TestClient, event: str, payload: dict[str, Any], delivery_id: str) -> None:
    response = send_delivery(client, event, payload, delivery_id=delivery_id)
    assert response.status_code == 202, response.text


def uninstall_notification(client: TestClient, delivery_id: str = "uninstalled") -> None:
    payload = installation_payload("deleted", OCTO_ORG_INSTALLATION)
    deliver(client, "installation", payload, delivery_id)


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry failed attempts at once, so a test can run a job until it settles."""
    monkeypatch.setattr(queue, "backoff_seconds", lambda attempt: 0.0)


@pytest.fixture
def during_run(monkeypatch: pytest.MonkeyPatch) -> DuringRun:
    """Run an action once, just before the next attempt reports the given stage.

    The attempt holds no database transaction at that point, so the action can deliver
    notifications.
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


# Detection by a run (US2-1, FR-011) ------------------------------------------------------------

LOSSES: dict[str, tuple[Switch, str]] = {
    "revoke_access": (
        lambda fake: fake.revoke_access("octocat", SAMPLE_APP_PRIVATE_ID),
        "repository_not_visible",
    ),
    "uninstall": (lambda fake: fake.uninstall(SAMPLE_APP_PRIVATE_ID), "app_not_installed"),
    "remove_from_installation": (
        lambda fake: fake.remove_from_installation(SAMPLE_APP_PRIVATE_ID),
        "app_not_installed",
    ),
    "suspend": (lambda fake: fake.suspend(SAMPLE_APP_PRIVATE_ID), "installation_not_accessible"),
}


@pytest.mark.parametrize(("switch", "reason"), LOSSES.values(), ids=LOSSES)
def test_a_run_detects_lost_access_before_fetching_anything(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    switch: Switch,
    reason: str,
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once, SAMPLE_APP_PRIVATE_ID)
    indexed = assert_active(db, repository_id)
    active_snapshot = indexed.active_snapshot_id
    fake = get_fake_github()
    switch(fake)
    fake.calls.clear()
    started = datetime.now(UTC)

    job_id = reindex(octocat, repository_id)
    run_all(run_worker_once)

    job = job_row(db, job_id)
    assert (job.status, job.error_code, job.error_retryable) == ("failed", "access_denied", False)
    assert fake.calls["open_tarball"] == 0
    repository = assert_lost(db, repository_id, reason)
    assert repository.access_lost_at is not None and repository.access_lost_at >= started
    assert repository.access_checked_at == repository.access_lost_at
    assert repository.active_snapshot_id == active_snapshot
    [lost] = audit(db, "repository_access_lost")
    assert (lost.workspace_id, lost.actor_user_id) == (repository.workspace_id, None)
    assert (lost.resource_type, lost.resource_id) == ("repository", repository_id)
    assert lost.detail == {"reason": reason, "trigger": "user"}
    # The run's own denial is still audited, as in the first increment.
    [denied] = audit(db, "access_denied")
    assert denied.resource_id == repository_id


def _deny_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve_commit(self: FakeGitHub, installation_id: int, full_name: str, branch: str) -> str:
        raise GitHubAccessDenied(f"installation {installation_id} cannot read {full_name}")

    monkeypatch.setattr(FakeGitHub, "resolve_commit", resolve_commit)


def _hide_tarball(monkeypatch: pytest.MonkeyPatch) -> None:
    def open_tarball(self: FakeGitHub, installation_id: int, full_name: str, sha: str) -> Any:
        self.calls["open_tarball"] += 1
        raise GitHubNotFound(f"{full_name} has no tarball for {sha}")

    monkeypatch.setattr(FakeGitHub, "open_tarball", open_tarball)


@pytest.mark.parametrize(
    ("deny", "stage", "built"),
    [(_deny_branch, "resolving_commit", []), (_hide_tarball, "fetching_source", ["failed"])],
    ids=["branch", "tarball"],
)
def test_an_installation_that_cannot_read_marks_the_repository_lost(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    monkeypatch: pytest.MonkeyPatch,
    deny: Callable[[pytest.MonkeyPatch], None],
    stage: str,
    built: list[str],
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    previous = repository_row(db, repository_id).active_snapshot_id
    get_fake_github().advance(SAMPLE_APP_ID)
    deny(monkeypatch)

    job_id = reindex(octocat, repository_id)
    run_all(run_worker_once)

    job = job_row(db, job_id)
    assert (job.status, job.error_code) == ("failed", "access_denied")
    repository = assert_lost(db, repository_id, "installation_cannot_read")
    assert repository.active_snapshot_id == previous
    [lost] = audit(db, "repository_access_lost")
    assert lost.detail == {"reason": "installation_cannot_read", "trigger": "user"}
    [denied] = audit(db, "access_denied")
    assert denied.detail["stage"] == stage
    # A version started for the new commit never becomes ready.
    statuses = db.scalars(select(Snapshot.status).where(Snapshot.job_id == job.id)).all()
    assert list(statuses) == built


def test_a_token_refreshed_by_a_failed_check_is_kept(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    # GitHub replaces the refresh token on every refresh, so losing the new one would force the
    # owner to sign in again before a later check could restore the repository.
    repository_id = connect_and_index(octocat, run_worker_once)
    expired = datetime.now(UTC) - timedelta(minutes=5)
    db.execute(update(GitHubCredential).values(access_token_expires_at=expired))
    db.commit()
    fake = get_fake_github()
    fake.uninstall(SAMPLE_APP_ID)
    fake.calls.clear()

    reindex(octocat, repository_id)
    run_all(run_worker_once)

    assert_lost(db, repository_id, "app_not_installed")
    assert fake.calls["refresh_user_token"] == 1
    db.expire_all()
    credential = db.scalars(select(GitHubCredential)).one()
    assert credential.access_token_expires_at is not None
    assert credential.access_token_expires_at > datetime.now(UTC)


# Notifications (US2-2, SC-005) -----------------------------------------------------------------

NOTIFICATIONS: dict[str, tuple[str, dict[str, Any], str, set[int]]] = {
    "installation.deleted": (
        "installation",
        installation_payload("deleted", OCTO_ORG_INSTALLATION),
        "app_uninstalled",
        {SAMPLE_APP_ID, NO_CODE_ID},
    ),
    "installation.suspend": (
        "installation",
        installation_payload("suspend", OCTO_ORG_INSTALLATION),
        "app_suspended",
        {SAMPLE_APP_ID, NO_CODE_ID},
    ),
    # `octocat/solo` is listed, but under another installation, so it stays untouched.
    "installation_repositories.removed": (
        "installation_repositories",
        installation_repositories_payload(
            "removed", OCTO_ORG_INSTALLATION, [SAMPLE_APP_ID, SOLO_ID, 999_999]
        ),
        "repository_removed_from_installation",
        {SAMPLE_APP_ID},
    ),
}


@pytest.mark.parametrize(
    ("event", "payload", "reason", "lost_ids"), NOTIFICATIONS.values(), ids=NOTIFICATIONS
)
def test_a_notification_marks_repositories_lost_within_the_request(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    event: str,
    payload: dict[str, Any],
    reason: str,
    lost_ids: set[int],
) -> None:
    repositories = {
        github_id: connect(octocat, github_id) for github_id in (SAMPLE_APP_ID, NO_CODE_ID, SOLO_ID)
    }
    run_all(run_worker_once)
    checked = {
        github_id: repository_row(db, repository_id).access_checked_at
        for github_id, repository_id in repositories.items()
    }
    waiting = {
        github_id: reindex(octocat, repository_id)
        for github_id, repository_id in repositories.items()
    }
    fake = get_fake_github()
    fake.calls.clear()

    deliver(octocat, event, payload, delivery_id="notification")

    # No worker ran and nothing was fetched: the delivery itself marked the repositories.
    assert sum(fake.calls.values()) == 0
    delivery = db.get(WebhookDelivery, "notification")
    assert delivery is not None
    assert (delivery.outcome, delivery.detail) == ("processed", {"repositories": len(lost_ids)})
    for github_id, repository_id in repositories.items():
        job = job_row(db, waiting[github_id])
        snapshots = octocat.get(f"/v1/repositories/{repository_id}/snapshots")
        if github_id in lost_ids:
            repository = assert_lost(db, repository_id, reason)
            assert repository.access_checked_at == checked[github_id]
            assert job.status == "canceled"
            assert last_event(db, job.id).message == ACCESS_LOST_CANCEL_MESSAGE
            assert snapshots.status_code == 403, snapshots.text
            assert snapshots.json()["error"]["code"] == "repository_access_lost"
        else:
            assert_active(db, repository_id)
            assert job.status == "queued"
            assert snapshots.status_code == 200, snapshots.text
    lost = audit(db, "repository_access_lost")
    assert sorted(event.resource_id or "" for event in lost) == sorted(
        repositories[github_id] for github_id in lost_ids
    )
    assert all(event.detail == {"reason": reason, "trigger": "notification"} for event in lost)
    assert all(event.actor_user_id is None for event in lost)


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("installation", installation_payload("deleted", 9_999)),
        ("installation", installation_payload("suspend", 9_999)),
        (
            "installation_repositories",
            installation_repositories_payload("removed", OCTOCAT_INSTALLATION, [SAMPLE_APP_ID]),
        ),
    ],
    ids=["deleted", "suspend", "removed"],
)
def test_a_notification_for_unconnected_repositories_is_ignored(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    event: str,
    payload: dict[str, Any],
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)

    deliver(octocat, event, payload, delivery_id="elsewhere")

    delivery = db.get(WebhookDelivery, "elsewhere")
    assert delivery is not None
    assert (delivery.outcome, delivery.detail) == ("ignored", {"reason": "not_connected"})
    assert_active(db, repository_id)
    assert audit(db, "repository_access_lost") == []


# No publish after loss (US2-3, SC-007) ---------------------------------------------------------


def test_a_run_cannot_publish_after_access_is_lost(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    during_run: DuringRun,
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    previous = repository_row(db, repository_id).active_snapshot_id
    get_fake_github().advance(SAMPLE_APP_ID)
    # The files are filtered; the loss arrives before the run publishes.
    during_run("parsing", lambda: uninstall_notification(octocat))

    job_id = reindex(octocat, repository_id)
    run_all(run_worker_once)

    job = job_row(db, job_id)
    assert job.status == "canceled"
    assert last_event(db, job.id).message == RUN_CANCELED
    [built] = db.scalars(select(Snapshot).where(Snapshot.job_id == job.id)).all()
    assert built.status == "discarded"
    repository = assert_lost(db, repository_id, "app_uninstalled")
    assert repository.active_snapshot_id == previous


def test_a_run_cannot_reuse_a_version_after_access_is_lost(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    during_run: DuringRun,
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    first = repository_row(db, repository_id).active_snapshot_id
    fake = get_fake_github()
    fake.advance(SAMPLE_APP_ID)
    reindex(octocat, repository_id)
    run_all(run_worker_once)
    second = repository_row(db, repository_id).active_snapshot_id
    assert second != first
    # Moving `main` back to the first commit makes the next run reuse the first version.
    fake.reset()
    during_run("publishing", lambda: uninstall_notification(octocat))

    job_id = reindex(octocat, repository_id)
    run_all(run_worker_once)

    job = job_row(db, job_id)
    assert (job.status, job.payload["commit_sha"]) == ("canceled", INITIAL)
    assert last_event(db, job.id).message == RUN_CANCELED
    assert fake.calls["open_tarball"] == 0
    repository = assert_lost(db, repository_id, "app_uninstalled")
    assert repository.active_snapshot_id == second
    # The reused version is an earlier result, not this run's, so it stays ready.
    statuses = {snapshot.id: snapshot.status for snapshot in db.scalars(select(Snapshot))}
    assert statuses == {first: "ready", second: "ready"}


def test_a_loss_reported_during_the_access_check_is_not_undone(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    original = FakeGitHub.resolve_commit

    def resolve_commit(self: FakeGitHub, installation_id: int, full_name: str, branch: str) -> str:
        # The run has verified access; the notification is newer than that answer.
        uninstall_notification(octocat)
        return original(self, installation_id, full_name, branch)

    monkeypatch.setattr(FakeGitHub, "resolve_commit", resolve_commit)
    fake = get_fake_github()
    fake.calls.clear()

    job_id = reindex(octocat, repository_id)
    run_all(run_worker_once)

    job = job_row(db, job_id)
    assert job.status == "canceled"
    assert last_event(db, job.id).message == RUN_CANCELED
    assert fake.calls["open_tarball"] == 0
    assert_lost(db, repository_id, "app_uninstalled")
    assert audit(db, "repository_access_restored") == []


# No false positives (US2-4, FR-013, SC-008) ---------------------------------------------------


def test_github_being_unavailable_is_not_a_loss(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], no_backoff: None
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    checked_at = repository_row(db, repository_id).access_checked_at
    assert checked_at is not None
    get_fake_github().set_unavailable(True)

    job_id = reindex(octocat, repository_id)
    run_all(run_worker_once)

    job = job_row(db, job_id)
    assert (job.status, job.error_code, job.error_retryable) == (
        "failed",
        "github_unavailable",
        True,
    )
    assert job.attempt == job.max_attempts
    repository = assert_active(db, repository_id)
    assert repository.access_checked_at == checked_at
    assert audit(db, "repository_access_lost") == []
    assert octocat.get(f"/v1/repositories/{repository_id}/snapshots").status_code == 200


def test_rejected_app_credentials_are_not_a_loss(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    monkeypatch: pytest.MonkeyPatch,
    no_backoff: None,
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    checked_at = repository_row(db, repository_id).access_checked_at

    def get_installation_id(self: FakeGitHub, full_name: str) -> int:
        raise AppCredentialsRejected("GitHub rejected the App JWT")

    monkeypatch.setattr(FakeGitHub, "get_installation_id", get_installation_id)

    job_id = reindex(octocat, repository_id)
    run_all(run_worker_once)

    job = job_row(db, job_id)
    assert (job.status, job.error_code, job.error_retryable) == (
        "failed",
        "github_app_misconfigured",
        True,
    )
    # Not permanent: every attempt was used.
    assert job.attempt == job.max_attempts
    repository = assert_active(db, repository_id)
    assert repository.access_checked_at == checked_at
    assert audit(db, "repository_access_lost") == []


def test_a_revoked_user_authorization_is_not_a_loss(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    checked_at = repository_row(db, repository_id).access_checked_at
    get_fake_github().revoke_authorization("octocat")

    job_id = reindex(octocat, repository_id)
    run_all(run_worker_once)

    job = job_row(db, job_id)
    assert (job.status, job.error_code, job.error_retryable) == (
        "failed",
        "github_sign_in_required",
        True,
    )
    assert job.attempt == 1
    assert "sign in" in (job.error_message or "").lower()
    repository = repository_row(db, repository_id)
    assert repository.access_state != "access_lost"
    assert repository.access_checked_at == checked_at
    assert audit(db, "repository_access_lost") == []


# Restoration (US2-5, SC-011) -------------------------------------------------------------------


def _reinstall(fake: FakeGitHub) -> object:
    return fake.reinstall(SAMPLE_APP_ID)


@pytest.mark.parametrize("restore", [FakeGitHub.reset, _reinstall], ids=["reset", "reinstall"])
def test_access_that_returns_restores_versions_and_answers_without_reindexing(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    restore: Switch,
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    version = repository_row(db, repository_id).active_snapshot_id
    submitted = octocat.post(
        "/v1/analysis-runs", json={"repository_id": repository_id, "question": QUESTION}
    )
    assert submitted.status_code == 202, submitted.text
    run_url = submitted.json()["result_url"]
    run_all(run_worker_once)
    answer = octocat.get(run_url).json()
    assert answer["status"] == "succeeded"
    fake = get_fake_github()
    fake.uninstall(SAMPLE_APP_ID)
    reindex(octocat, repository_id)
    run_all(run_worker_once)
    assert_lost(db, repository_id, "app_not_installed")
    assert octocat.get(run_url).status_code == 403

    restore(fake)
    fake.calls.clear()
    job_id = reindex(octocat, repository_id)
    run_all(run_worker_once)

    assert job_row(db, job_id).status == "succeeded"
    repository = assert_active(db, repository_id)
    assert repository.access_checked_at is not None
    assert repository.github_installation_id == fake.get_installation_id("octo-org/sample-app")
    [restored] = audit(db, "repository_access_restored")
    assert (restored.resource_id, restored.actor_user_id) == (repository_id, None)
    assert restored.detail == {"trigger": "user"}
    # The run reused the existing version instead of building a new one.
    assert fake.calls["open_tarball"] == 0
    assert snapshot_count(db) == 1
    assert repository.active_snapshot_id == version
    versions = octocat.get(f"/v1/repositories/{repository_id}/snapshots")
    assert versions.status_code == 200, versions.text
    assert [item["id"] for item in versions.json()["items"]] == [str(version)]
    again = octocat.get(run_url)
    assert again.status_code == 200, again.text
    assert again.json()["citations"] == answer["citations"]


# A push to a lost repository (spec edge case, US2-1) -----------------------------------------


def test_a_push_to_a_lost_repository_checks_access_again(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    fake = get_fake_github()
    fake.uninstall(SAMPLE_APP_ID)
    reindex(octocat, repository_id)
    run_all(run_worker_once)
    first_lost_at = assert_lost(db, repository_id, "app_not_installed").access_lost_at
    fake.calls.clear()

    sha = fake.push(SAMPLE_APP_ID)
    deliver(octocat, "push", push_payload(SAMPLE_APP_ID, sha), delivery_id="while-lost")
    job_id = repository_row(db, repository_id).latest_push_job_id
    assert job_id is not None
    assert job_row(db, job_id).trigger == "push"
    run_all(run_worker_once)

    job = job_row(db, job_id)
    assert (job.status, job.error_code) == ("failed", "access_denied")
    assert fake.calls["open_tarball"] == 0
    repository = assert_lost(db, repository_id, "app_not_installed")
    assert repository.access_lost_at == first_lost_at

    fake.reset()
    sha = fake.push(SAMPLE_APP_ID)
    deliver(octocat, "push", push_payload(SAMPLE_APP_ID, sha), delivery_id="after-restore")
    job_id = repository_row(db, repository_id).latest_push_job_id
    assert job_id is not None
    run_all(run_worker_once)

    job = job_row(db, job_id)
    assert (job.status, job.trigger) == ("succeeded", "push")
    repository = assert_active(db, repository_id)
    [restored] = audit(db, "repository_access_restored")
    assert restored.detail == {"trigger": "push"}
    snapshot = db.get(Snapshot, repository.active_snapshot_id)
    assert snapshot is not None
    assert (snapshot.commit_sha, snapshot.job_id) == (sha, job.id)


# First detection time --------------------------------------------------------------------------


def test_a_later_detection_keeps_the_first_loss(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect_and_index(octocat, run_worker_once)
    get_fake_github().uninstall(SAMPLE_APP_ID)
    reindex(octocat, repository_id)
    run_all(run_worker_once)
    first = assert_lost(db, repository_id, "app_not_installed")
    first_lost_at, first_checked_at = first.access_lost_at, first.access_checked_at

    uninstall_notification(octocat)
    after_notification = assert_lost(db, repository_id, "app_not_installed")
    assert after_notification.access_lost_at == first_lost_at
    assert after_notification.access_checked_at == first_checked_at

    job_id = reindex(octocat, repository_id)
    run_all(run_worker_once)

    assert job_row(db, job_id).error_code == "access_denied"
    repository = assert_lost(db, repository_id, "app_not_installed")
    assert repository.access_lost_at == first_lost_at
    assert first_checked_at is not None and repository.access_checked_at is not None
    assert repository.access_checked_at > first_checked_at
    [lost] = audit(db, "repository_access_lost")
    assert lost.detail == {"reason": "app_not_installed", "trigger": "user"}
