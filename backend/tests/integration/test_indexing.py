from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from codeatlas.config import Settings
from codeatlas.github.fake import (
    EMPTY_ID,
    NO_CODE_ID,
    OVERSIZED_ID,
    SAMPLE_APP_DELETED,
    SAMPLE_APP_ID,
    SAMPLE_APP_RENAMED,
    VENDORED_HEAVY_ID,
    commit_sha,
    get_fake_github,
)
from codeatlas.models import (
    AuditEvent,
    CoverageEntry,
    DocChunk,
    File,
    Job,
    JobEvent,
    Repository,
    Snapshot,
    Symbol,
)

pytestmark = pytest.mark.integration

STAGES = [
    "resolving_commit",
    "fetching_source",
    "filtering",
    "parsing",
    "building_search_index",
    "embedding_docs",
    "publishing",
]


def connect(client: TestClient, github_id: int) -> dict[str, Any]:
    response = client.post("/v1/repositories", json={"github_repository_id": github_id})
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


def run_all(run_worker_once: Callable[[], bool]) -> None:
    while run_worker_once():
        pass


def job_of(db: Session, job_id: str) -> Job:
    db.expire_all()
    job = db.get(Job, job_id)
    assert job is not None
    return job


def coverage(db: Session, snapshot_id: Any) -> dict[str, str]:
    rows = db.scalars(select(CoverageEntry).where(CoverageEntry.snapshot_id == snapshot_id)).all()
    return {row.path: row.reason for row in rows}


def test_sample_app_indexes_with_coverage(
    db: Session,
    signed_in: Callable[[str], TestClient],
    run_worker_once: Callable[[], bool],
) -> None:
    client = signed_in("octocat")
    body = connect(client, SAMPLE_APP_ID)
    run_all(run_worker_once)

    job = job_of(db, body["job"]["id"])
    assert job.status == "succeeded", (job.error_code, job.error_message)
    repository = client.get(f"/v1/repositories/{body['repository']['id']}").json()
    assert repository["state"] == "ready"
    snapshot = db.get(Snapshot, repository["active_snapshot"]["id"])
    assert snapshot is not None
    assert snapshot.status == "ready"
    assert snapshot.commit_sha == commit_sha(SAMPLE_APP_ID, "initial")
    assert snapshot.coverage["embeddings_available"] is True
    assert snapshot.coverage["symbols"] > 0

    reasons = coverage(db, snapshot.id)
    assert reasons["node_modules"] == "excluded_directory"
    assert reasons[".env"] == "credential_file"
    assert reasons["id_rsa"] == "credential_file"
    assert reasons["static/vendor.min.js"] == "generated"
    assert reasons["src/generated/apiClient.ts"] == "generated"
    assert reasons["assets/logo.png"] == "binary"
    assert reasons["docs/latin1-notes.txt"] == "unsupported_encoding"
    assert reasons["data/large.txt"] == "too_large"
    assert reasons["app/legacy.py"] == "unsupported_syntax"

    paths = set(db.scalars(select(File.path).where(File.snapshot_id == snapshot.id)))
    assert {"app/auth/access.py", "app/legacy.py", ".env.example", "README.md"} <= paths
    assert ".env" not in paths
    names = set(db.scalars(select(Symbol.qualified_name).where(Symbol.snapshot_id == snapshot.id)))
    assert {"check_repository_access", "AccessPolicy.can_read", "UserService.getUserById"} <= names
    assert db.scalar(select(func.count()).where(DocChunk.snapshot_id == snapshot.id)) > 0

    stages = list(
        db.scalars(
            select(JobEvent.stage)
            .where(JobEvent.job_id == job.id, JobEvent.event_type == "stage_started")
            .order_by(JobEvent.seq)
        )
    )
    assert stages == STAGES

    api_coverage = client.get(f"/v1/snapshots/{snapshot.id}/coverage", params={"limit": 100})
    assert api_coverage.status_code == 200
    assert {item["path"] for item in api_coverage.json()["items"]} == set(reasons)


def test_oversized_repository_is_rejected(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    client = signed_in("octocat")
    body = connect(client, OVERSIZED_ID)
    run_all(run_worker_once)

    job = job_of(db, body["job"]["id"])
    assert (job.status, job.error_code) == ("failed", "limit_exceeded")
    assert "max_files_per_snapshot" in (job.error_message or "")
    repository = client.get(f"/v1/repositories/{body['repository']['id']}").json()
    assert repository["state"] == "rejected"
    assert repository["latest_indexing_job"]["error"]["code"] == "limit_exceeded"


def test_vendored_heavy_repository_indexes(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    client = signed_in("octocat")
    body = connect(client, VENDORED_HEAVY_ID)
    run_all(run_worker_once)

    assert job_of(db, body["job"]["id"]).status == "succeeded"
    snapshot = db.scalars(select(Snapshot).where(Snapshot.status == "ready")).one()
    entries = db.scalars(select(CoverageEntry).where(CoverageEntry.snapshot_id == snapshot.id))
    node_modules = [entry for entry in entries if entry.path.startswith("node_modules")]
    assert [(e.path, e.entry_type, e.reason) for e in node_modules] == [
        ("node_modules", "directory", "excluded_directory")
    ]


def test_no_code_repository_indexes_without_symbols(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    client = signed_in("octocat")
    body = connect(client, NO_CODE_ID)
    run_all(run_worker_once)

    assert job_of(db, body["job"]["id"]).status == "succeeded"
    snapshot = db.scalars(select(Snapshot).where(Snapshot.status == "ready")).one()
    assert snapshot.coverage["symbols"] == 0
    assert db.scalar(select(func.count()).where(File.snapshot_id == snapshot.id)) == 4
    assert db.scalar(select(func.count()).where(DocChunk.snapshot_id == snapshot.id)) > 0


def test_reindex_same_commit_reuses_snapshot(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    client = signed_in("octocat")
    body = connect(client, SAMPLE_APP_ID)
    run_all(run_worker_once)
    repository_id = body["repository"]["id"]

    again = client.post(f"/v1/repositories/{repository_id}/index", json={}).json()
    run_all(run_worker_once)

    assert job_of(db, again["job"]["id"]).status == "succeeded"
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 1


def test_failed_reindex_keeps_previous_snapshot(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    client = signed_in("octocat")
    body = connect(client, SAMPLE_APP_ID)
    run_all(run_worker_once)
    repository_id = body["repository"]["id"]
    active_before = client.get(f"/v1/repositories/{repository_id}").json()["active_snapshot"]
    get_fake_github().revoke_access("octocat", SAMPLE_APP_ID)

    again = client.post(f"/v1/repositories/{repository_id}/index", json={}).json()
    run_all(run_worker_once)

    job = job_of(db, again["job"]["id"])
    assert (job.status, job.error_code) == ("failed", "access_denied")
    repository = client.get(f"/v1/repositories/{repository_id}").json()
    assert repository["active_snapshot"] == active_before
    assert repository["state"] == "ready"
    denied = db.scalars(select(AuditEvent).where(AuditEvent.action == "access_denied")).all()
    assert denied


def test_second_commit_drops_deleted_and_renamed_paths(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    client = signed_in("octocat")
    body = connect(client, SAMPLE_APP_ID)
    run_all(run_worker_once)
    get_fake_github().advance(SAMPLE_APP_ID)

    client.post(f"/v1/repositories/{body['repository']['id']}/index", json={})
    run_all(run_worker_once)

    repository = db.get(Repository, body["repository"]["id"])
    assert repository is not None
    db.refresh(repository)
    paths = set(
        db.scalars(select(File.path).where(File.snapshot_id == repository.active_snapshot_id))
    )
    old_path, new_path = SAMPLE_APP_RENAMED
    assert new_path in paths
    assert old_path not in paths
    assert SAMPLE_APP_DELETED not in paths


def test_unknown_branch_and_empty_repository_fail_permanently(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    client = signed_in("octocat")
    branch_body = client.post(
        "/v1/repositories", json={"github_repository_id": NO_CODE_ID, "branch": "nope"}
    ).json()
    empty_body = connect(client, EMPTY_ID)
    run_all(run_worker_once)

    branch_job = job_of(db, branch_body["job"]["id"])
    empty_job = job_of(db, empty_body["job"]["id"])
    assert (branch_job.status, branch_job.error_code, branch_job.attempt) == (
        "failed",
        "branch_not_found",
        1,
    )
    assert (empty_job.status, empty_job.error_code) == ("failed", "repository_empty")


def test_rename_and_reinstall_keep_indexing_working(
    db: Session, signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    client = signed_in("octocat")
    body = connect(client, SAMPLE_APP_ID)
    run_all(run_worker_once)
    repository_id = body["repository"]["id"]
    fake = get_fake_github()
    fake.rename(SAMPLE_APP_ID, "octo-org/renamed-app")
    new_installation = fake.reinstall(SAMPLE_APP_ID)
    fake.advance(SAMPLE_APP_ID)

    again = client.post(f"/v1/repositories/{repository_id}/index", json={}).json()
    run_all(run_worker_once)

    assert job_of(db, again["job"]["id"]).status == "succeeded"
    repository = db.get(Repository, repository_id)
    assert repository is not None
    db.refresh(repository)
    assert repository.full_name == "octo-org/renamed-app"
    assert repository.github_installation_id == new_installation


def test_embeddings_unavailable_still_publishes(
    db: Session,
    signed_in: Callable[[str], TestClient],
    run_worker_once: Callable[[], bool],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "fake_embedder_mode", "unavailable")
    client = signed_in("octocat")
    body = connect(client, NO_CODE_ID)
    run_all(run_worker_once)

    assert job_of(db, body["job"]["id"]).status == "succeeded"
    snapshot = db.scalars(select(Snapshot).where(Snapshot.status == "ready")).one()
    assert snapshot.coverage["embeddings_available"] is False
    assert "embeddings_unavailable" in coverage(db, snapshot.id).values()
    embedded = db.scalar(
        select(func.count()).where(
            DocChunk.snapshot_id == snapshot.id, DocChunk.embedding.is_not(None)
        )
    )
    assert embedded == 0


def test_stale_attempt_cannot_publish(
    db: Session,
    signed_in: Callable[[str], TestClient],
    run_worker_once: Callable[[], bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codeatlas.ingestion import pipeline
    from codeatlas.providers.embeddings import FakeEmbedder

    client = signed_in("octocat")
    body = connect(client, NO_CODE_ID)
    job_id = body["job"]["id"]

    class TakeoverEmbedder(FakeEmbedder):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            # Another worker reclaims the job while this attempt is still embedding.
            from codeatlas.db import session_scope

            with session_scope() as other:
                other.execute(
                    update(Job).where(Job.id == job_id).values(fencing_token=Job.fencing_token + 1)
                )
            return super().embed_documents(texts)

    from codeatlas.config import get_settings

    monkeypatch.setattr(pipeline, "get_embedder", lambda: TakeoverEmbedder(get_settings()))
    run_worker_once()

    snapshots = db.scalars(select(Snapshot)).all()
    assert [snapshot.status for snapshot in snapshots] == ["discarded"]
    # The job now belongs to the newer attempt, which has not run yet.
    assert job_of(db, job_id).status == "running"
