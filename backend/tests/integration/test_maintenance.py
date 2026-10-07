import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from codeatlas.db import new_session
from codeatlas.github.fake import SAMPLE_APP_ID, SOLO_ID, get_fake_github
from codeatlas.jobs import maintenance
from codeatlas.jobs.maintenance import LOCK_KEY, run_maintenance
from codeatlas.jobs.queue import cancel_for_repository, enqueue
from codeatlas.models import (
    AnalysisRun,
    AuditEvent,
    EvidenceItem,
    IdempotencyRecord,
    Job,
    JobEvent,
    Repository,
    Snapshot,
    UserSession,
    WebhookDelivery,
    Workspace,
)
from codeatlas.workspace.access import GRACE_PERIOD

pytestmark = pytest.mark.integration


def drain(run_worker_once: Callable[[], bool]) -> None:
    while run_worker_once():
        pass


def days_ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def version_sha(number: int) -> str:
    return f"{number:040x}"


@pytest.fixture
def workspace(db: Session) -> Workspace:
    workspace = Workspace(name="octocat")
    db.add(workspace)
    db.commit()
    return workspace


def add_repository(db: Session, workspace: Workspace, github_repository_id: int) -> Repository:
    repository = Repository(
        workspace_id=workspace.id,
        github_repository_id=github_repository_id,
        github_installation_id=5002,
        full_name=f"octocat/repo-{github_repository_id}",
        default_branch="main",
        is_private=False,
    )
    db.add(repository)
    db.commit()
    return repository


def add_ready_versions(
    db: Session, repository: Repository, count: int, *, age_days: float = 1
) -> list[uuid.UUID]:
    """Add ready versions v1 to v<count>, one minute apart, and make the newest one active.

    Returns their IDs, oldest first. All are well inside the 14-day rule.
    """
    snapshots = [
        Snapshot(
            workspace_id=repository.workspace_id,
            repository_id=repository.id,
            commit_sha=version_sha(number),
            branch="main",
            index_version="idx-test",
            status="ready",
            coverage={},
            created_at=days_ago(age_days) + timedelta(minutes=number),
            ready_at=days_ago(age_days) + timedelta(minutes=number),
        )
        for number in range(1, count + 1)
    ]
    db.add_all(snapshots)
    db.flush()
    repository.active_snapshot_id = snapshots[-1].id
    db.commit()
    return [snapshot.id for snapshot in snapshots]


def add_answer(
    db: Session, repository: Repository, snapshot_id: uuid.UUID, *, expires_at: datetime
) -> uuid.UUID:
    run = AnalysisRun(
        workspace_id=repository.workspace_id,
        repository_id=repository.id,
        snapshot_id=snapshot_id,
        commit_sha=version_sha(1),
        index_version="idx-test",
        question="Where is access?",
        model="fake",
        thinking_level="low",
        prompt_version="test",
        expires_at=expires_at,
    )
    db.add(run)
    db.commit()
    return run.id


def remaining_versions(db: Session, repository: Repository) -> set[uuid.UUID]:
    return set(db.scalars(select(Snapshot.id).where(Snapshot.repository_id == repository.id)))


def add_queued_job(db: Session, repository: Repository) -> uuid.UUID:
    job, _ = enqueue(
        db,
        workspace_id=repository.workspace_id,
        kind="index_repository",
        repository_id=repository.id,
        created_by=None,
    )
    db.commit()
    return job.id


def lose_access(db: Session, repository: Repository, *, lost_at: datetime) -> None:
    repository.access_state = "access_lost"
    repository.access_reason = "installation_not_accessible"
    repository.access_lost_at = lost_at
    db.commit()


@pytest.fixture
def indexed(
    signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> tuple[TestClient, str]:
    client = signed_in("octocat")
    response = client.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})
    drain(run_worker_once)
    return client, response.json()["repository"]["id"]


def test_expired_answers_are_deleted_with_evidence(
    db: Session, indexed: tuple[TestClient, str], run_worker_once: Callable[[], bool]
) -> None:
    client, repository_id = indexed
    run = client.post(
        "/v1/analysis-runs", json={"repository_id": repository_id, "question": "Where is access?"}
    ).json()
    drain(run_worker_once)
    assert db.scalar(select(func.count()).select_from(EvidenceItem)) > 0
    db.execute(update(AnalysisRun).values(expires_at=days_ago(0.01)))
    db.commit()

    result = run_maintenance()

    assert result is not None and result.runs_expired == 1
    db.expire_all()
    assert db.get(AnalysisRun, run["run_id"]) is None
    assert db.scalar(select(func.count()).select_from(EvidenceItem)) == 0


def test_inactive_snapshots_kept_while_referenced_then_removed(
    db: Session, indexed: tuple[TestClient, str], run_worker_once: Callable[[], bool]
) -> None:
    client, repository_id = indexed
    first = client.get(f"/v1/repositories/{repository_id}").json()["active_snapshot"]["id"]
    run = client.post(
        "/v1/analysis-runs", json={"repository_id": repository_id, "question": "Where is access?"}
    ).json()
    drain(run_worker_once)
    get_fake_github().advance(SAMPLE_APP_ID)
    client.post(f"/v1/repositories/{repository_id}/index", json={})
    drain(run_worker_once)
    db.execute(update(Snapshot).where(Snapshot.id == first).values(created_at=days_ago(15)))
    db.commit()

    run_maintenance()
    db.expire_all()
    assert db.get(Snapshot, first) is not None  # referenced by an unexpired answer

    db.execute(
        update(AnalysisRun).where(AnalysisRun.id == run["run_id"]).values(expires_at=days_ago(1))
    )
    db.commit()
    run_maintenance()
    db.expire_all()
    assert db.get(Snapshot, first) is None
    active = db.get(Repository, repository_id)
    assert active is not None and active.active_snapshot_id is not None
    assert db.get(Snapshot, active.active_snapshot_id) is not None


def test_recent_inactive_snapshot_is_kept(
    db: Session, indexed: tuple[TestClient, str], run_worker_once: Callable[[], bool]
) -> None:
    client, repository_id = indexed
    first = client.get(f"/v1/repositories/{repository_id}").json()["active_snapshot"]["id"]
    get_fake_github().advance(SAMPLE_APP_ID)
    client.post(f"/v1/repositories/{repository_id}/index", json={})
    drain(run_worker_once)

    run_maintenance()

    db.expire_all()
    assert db.get(Snapshot, first) is not None


def test_failed_and_discarded_snapshots_removed_after_a_day(
    db: Session, indexed: tuple[TestClient, str]
) -> None:
    _, repository_id = indexed
    repository = db.get(Repository, repository_id)
    assert repository is not None
    for status, age in (("failed", 2), ("discarded", 2), ("failed", 0.1)):
        db.add(
            Snapshot(
                workspace_id=repository.workspace_id,
                repository_id=repository.id,
                commit_sha="f" * 40,
                branch="main",
                index_version="idx-test",
                status=status,
                coverage={},
                created_at=days_ago(age),
            )
        )
    db.commit()

    result = run_maintenance()

    assert result is not None and result.failed_snapshots_removed == 2
    remaining = db.scalars(select(Snapshot.status).where(Snapshot.status != "ready")).all()
    assert remaining == ["failed"]


def test_versions_with_five_newer_ready_versions_are_removed(
    db: Session, workspace: Workspace
) -> None:
    repository = add_repository(db, workspace, SAMPLE_APP_ID)
    versions = add_ready_versions(db, repository, 7)

    result = run_maintenance()

    assert result is not None and result.snapshots_removed == 2
    assert remaining_versions(db, repository) == set(versions[2:])


def test_version_referenced_by_unexpired_answer_is_kept_however_many_newer(
    db: Session, workspace: Workspace
) -> None:
    repository = add_repository(db, workspace, SAMPLE_APP_ID)
    versions = add_ready_versions(db, repository, 7)
    run_id = add_answer(db, repository, versions[0], expires_at=days_ago(-1))

    run_maintenance()

    assert remaining_versions(db, repository) == {versions[0], *versions[2:]}

    db.execute(update(AnalysisRun).where(AnalysisRun.id == run_id).values(expires_at=days_ago(1)))
    db.commit()
    run_maintenance()

    assert remaining_versions(db, repository) == set(versions[2:])


def test_versions_are_counted_per_repository(db: Session, workspace: Workspace) -> None:
    busy = add_repository(db, workspace, SAMPLE_APP_ID)
    quiet = add_repository(db, workspace, SOLO_ID)
    # The quiet repository's versions are older than every version of the busy one.
    quiet_versions = add_ready_versions(db, quiet, 3, age_days=3)
    busy_versions = add_ready_versions(db, busy, 7)

    result = run_maintenance()

    assert result is not None and result.snapshots_removed == 2
    assert remaining_versions(db, quiet) == set(quiet_versions)
    assert remaining_versions(db, busy) == set(busy_versions[2:])


def test_webhook_deliveries_removed_after_14_days(db: Session) -> None:
    for delivery_id, age in (("old", 14.1), ("recent", 13.9)):
        db.add(
            WebhookDelivery(
                delivery_id=delivery_id,
                event="push",
                outcome="processed",
                received_at=days_ago(age),
            )
        )
    db.commit()

    result = run_maintenance()

    assert result is not None and result.webhook_deliveries_removed == 1
    assert db.scalars(select(WebhookDelivery.delivery_id)).all() == ["recent"]


def test_repository_lost_past_grace_period_is_disconnected_and_purged(
    db: Session, workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    repository = add_repository(db, workspace, SAMPLE_APP_ID)
    add_ready_versions(db, repository, 2)
    job_id = add_queued_job(db, repository)
    lose_access(db, repository, lost_at=now - GRACE_PERIOD - timedelta(minutes=1))
    repository_id = repository.id
    # The pass purges the repository before it commits, so look inside its transaction.
    seen: list[tuple[datetime | None, str | None, str | None]] = []

    def cancel_and_look(session: Session, repository_id: uuid.UUID, *, message: str) -> int:
        canceled = cancel_for_repository(session, repository_id, message=message)
        last_event = (
            select(JobEvent.message)
            .where(JobEvent.job_id == job_id)
            .order_by(JobEvent.seq.desc())
            .limit(1)
        )
        seen.append(
            (
                session.scalar(select(Repository.deleted_at).where(Repository.id == repository_id)),
                session.scalar(select(Job.status).where(Job.id == job_id)),
                session.scalar(last_event),
            )
        )
        return canceled

    monkeypatch.setattr(maintenance, "cancel_for_repository", cancel_and_look)

    result = run_maintenance(now=now)

    assert result is not None
    assert result.repositories_disconnected_after_access_loss == 1
    assert result.repositories_purged == 1
    assert seen == [(now, "canceled", maintenance.GRACE_EXPIRED_CANCEL_MESSAGE)]
    db.expire_all()
    assert db.get(Repository, repository_id) is None
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 0
    assert db.get(Job, job_id) is None
    audit = db.scalars(select(AuditEvent).where(AuditEvent.action == "repository_disconnect")).one()
    assert (audit.workspace_id, audit.actor_user_id) == (workspace.id, None)
    assert (audit.resource_type, audit.resource_id) == ("repository", str(repository_id))
    assert (audit.outcome, audit.detail) == ("success", {"reason": "access_lost"})


@pytest.mark.parametrize("lost_days", [6, None], ids=["lost_for_6_days", "active"])
def test_repository_within_grace_period_or_active_is_untouched(
    db: Session, workspace: Workspace, lost_days: int | None
) -> None:
    now = datetime.now(UTC)
    repository = add_repository(db, workspace, SAMPLE_APP_ID)
    versions = add_ready_versions(db, repository, 2)
    job_id = add_queued_job(db, repository)
    if lost_days is not None:
        lose_access(db, repository, lost_at=now - timedelta(days=lost_days))
    access = (repository.access_state, repository.access_reason, repository.access_lost_at)

    result = run_maintenance(now=now)

    assert result is not None and result.repositories_disconnected_after_access_loss == 0
    db.expire_all()
    assert repository.deleted_at is None
    assert (repository.access_state, repository.access_reason, repository.access_lost_at) == access
    assert remaining_versions(db, repository) == set(versions)
    job = db.get(Job, job_id)
    assert job is not None and job.status == "queued"
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_expired_sessions_and_idempotency_records_removed(
    db: Session, signed_in: Callable[[str], TestClient]
) -> None:
    signed_in("octocat")
    workspace = db.scalars(select(Workspace)).one()
    db.add(
        IdempotencyRecord(
            workspace_id=workspace.id,
            route="r",
            key="old",
            payload_sha256=b"x",
            response_status=202,
            response_body={},
            expires_at=days_ago(0.01),
        )
    )
    db.execute(update(UserSession).values(expires_at=days_ago(0.01)))
    db.commit()

    result = run_maintenance()

    assert result is not None
    assert (result.sessions_removed, result.idempotency_records_removed) == (1, 1)


def test_second_runner_skips_while_lock_is_held(db: Session) -> None:
    holder = new_session()
    holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOCK_KEY})
    try:
        results: list[object] = []
        thread = threading.Thread(target=lambda: results.append(run_maintenance()))
        thread.start()
        thread.join(timeout=10)
        assert results == [None]
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
        holder.close()
    assert run_maintenance() is not None
