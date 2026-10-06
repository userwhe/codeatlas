import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from codeatlas.db import new_session
from codeatlas.github.fake import SAMPLE_APP_ID, get_fake_github
from codeatlas.jobs.maintenance import LOCK_KEY, run_maintenance
from codeatlas.models import (
    AnalysisRun,
    EvidenceItem,
    IdempotencyRecord,
    Repository,
    Snapshot,
    UserSession,
    Workspace,
)

pytestmark = pytest.mark.integration


def drain(run_worker_once: Callable[[], bool]) -> None:
    while run_worker_once():
        pass


def days_ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


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
