from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from codeatlas.db import session_scope
from codeatlas.github.fake import NO_CODE_ID, SAMPLE_APP_ID
from codeatlas.jobs import queue
from codeatlas.jobs.maintenance import run_maintenance
from codeatlas.jobs.worker import JobContext, load_handlers
from codeatlas.models import AuditEvent, File, Job, Repository, Snapshot

pytestmark = pytest.mark.integration


def drain(run_worker_once: Callable[[], bool]) -> None:
    while run_worker_once():
        pass


def index(client: TestClient, run_worker_once: Callable[[], bool], github_id: int) -> str:
    response = client.post("/v1/repositories", json={"github_repository_id": github_id})
    assert response.status_code == 202, response.text
    drain(run_worker_once)
    repository_id: str = response.json()["repository"]["id"]
    return repository_id


def test_disconnect_hides_everything_immediately(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    client = signed_in("octocat")
    repository_id = index(client, run_worker_once, SAMPLE_APP_ID)
    repository = client.get(f"/v1/repositories/{repository_id}").json()
    snapshot_id = repository["active_snapshot"]["id"]
    index_job = repository["latest_indexing_job"]["id"]
    run = client.post(
        "/v1/analysis-runs",
        json={"repository_id": repository_id, "question": "Where is access checked?"},
    ).json()

    response = client.delete(f"/v1/repositories/{repository_id}")

    assert response.status_code == 204
    for path in (
        f"/v1/repositories/{repository_id}",
        f"/v1/repositories/{repository_id}/snapshots",
        f"/v1/snapshots/{snapshot_id}",
        f"/v1/snapshots/{snapshot_id}/coverage",
        f"/v1/snapshots/{snapshot_id}/tree",
        f"/v1/snapshots/{snapshot_id}/file?path=README.md",
        f"/v1/analysis-runs/{run['run_id']}",
        f"/v1/analysis-runs?repository_id={repository_id}",
        f"/v1/jobs/{index_job}",
        f"/v1/jobs/{run['job_id']}/events",
    ):
        assert client.get(path).status_code == 404, path
    search = client.post(
        "/v1/search", json={"snapshot_id": snapshot_id, "query": "access", "mode": "text"}
    )
    assert search.status_code == 404
    assert client.get("/v1/repositories").json()["items"] == []
    audit = db.scalars(select(AuditEvent).where(AuditEvent.action == "repository_disconnect")).one()
    assert audit.resource_id == repository_id


def test_disconnect_cancels_queued_work(
    db: Session, signed_in: Callable[[str], TestClient]
) -> None:
    client = signed_in("octocat")
    connected = client.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})
    job_id = connected.json()["job"]["id"]

    client.delete(f"/v1/repositories/{connected.json()['repository']['id']}")

    job = db.get(Job, job_id)
    assert job is not None
    db.refresh(job)
    assert job.status == "canceled"


def test_running_index_attempt_cannot_publish_after_disconnect(
    db: Session, signed_in: Callable[[str], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    from codeatlas.ingestion import pipeline

    client = signed_in("octocat")
    connected = client.post("/v1/repositories", json={"github_repository_id": NO_CODE_ID}).json()
    repository_id = connected["repository"]["id"]
    original = pipeline._publish

    def disconnect_then_publish(*args: object, **kwargs: object) -> None:
        assert client.delete(f"/v1/repositories/{repository_id}").status_code == 204
        original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pipeline, "_publish", disconnect_then_publish)
    load_handlers()
    with session_scope() as session:
        claim = queue.claim_next(session)
    assert claim is not None
    pipeline.handle_index(JobContext.from_claim(claim))

    db.expire_all()
    job = db.get(Job, connected["job"]["id"])
    assert job is not None and job.status == "canceled"
    repository = db.get(Repository, repository_id)
    assert repository is not None and repository.active_snapshot_id is None
    assert [s.status for s in db.scalars(select(Snapshot))] == ["discarded"]


def test_running_answer_cannot_publish_after_disconnect(
    db: Session,
    signed_in: Callable[[str], TestClient],
    run_worker_once: Callable[[], bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codeatlas.qa import answer

    client = signed_in("octocat")
    repository_id = index(client, run_worker_once, SAMPLE_APP_ID)
    run = client.post(
        "/v1/analysis-runs",
        json={"repository_id": repository_id, "question": "Where is access checked?"},
    ).json()
    original = answer._generate

    def disconnect_then_generate(*args: object, **kwargs: object) -> object:
        assert client.delete(f"/v1/repositories/{repository_id}").status_code == 204
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(answer, "_generate", disconnect_then_generate)
    drain(run_worker_once)

    db.expire_all()
    job = db.get(Job, run["job_id"])
    assert job is not None and job.status == "canceled"


def test_reconnect_after_disconnect_indexes_from_scratch(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    client = signed_in("octocat")
    first = index(client, run_worker_once, SAMPLE_APP_ID)
    client.delete(f"/v1/repositories/{first}")

    second = index(client, run_worker_once, SAMPLE_APP_ID)

    assert second != first
    repository = client.get(f"/v1/repositories/{second}").json()
    assert repository["state"] == "ready"
    rows = db.scalars(select(Repository).where(Repository.id == second)).one()
    assert rows.deleted_at is None


def test_maintenance_purges_disconnected_repository(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    client = signed_in("octocat")
    repository_id = index(client, run_worker_once, SAMPLE_APP_ID)
    client.delete(f"/v1/repositories/{repository_id}")
    db.execute(
        update(Repository)
        .where(Repository.id == repository_id)
        .values(deleted_at=datetime.now(UTC) - timedelta(hours=25))
    )
    db.commit()

    result = run_maintenance()

    assert result is not None and result.repositories_purged == 1
    db.expire_all()
    assert db.get(Repository, repository_id) is None
    assert db.scalar(select(func.count()).select_from(File)) == 0
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 0
    assert db.scalars(select(AuditEvent).where(AuditEvent.action == "repository_disconnect")).one()
