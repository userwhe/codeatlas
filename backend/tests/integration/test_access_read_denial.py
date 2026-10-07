"""Integration tests for denying reads of a repository whose access is lost (FR-014, research R7).

`octocat` indexes `octo-org/sample-app` and asks a question. A change notification then marks the
repository lost. Every read of its versions, files, search results, answers, and progress returns
403 `repository_access_lost`, while the repository itself stays listed with `state: access_lost`
and its `access` object, so the owner can see what happened. `hubot` still gets 404 for all of it.
"""

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from codeatlas.github.fake import SAMPLE_APP_ID
from codeatlas.models import AnalysisRun, AuditEvent, Job, Repository
from codeatlas.workspace import access

pytestmark = pytest.mark.integration

QUESTION = "Where are repository permissions checked?"
CHECKED_AT = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
LOST_AT = CHECKED_AT + timedelta(hours=1)

Ids = Mapping[str, str]
# A request as (method, URL, keyword arguments); `{key}` placeholders are filled from the IDs.
Request = tuple[str, str, dict[str, Any]]

# The reads that return 403 for the owner of a lost repository (contracts/http-api.md).
CONTENT_READS: list[Request] = [
    ("GET", "/v1/repositories/{repository}/snapshots", {}),
    ("GET", "/v1/snapshots/{snapshot}", {}),
    ("GET", "/v1/snapshots/{snapshot}/coverage", {}),
    ("GET", "/v1/snapshots/{snapshot}/tree", {"params": {"path": "app"}}),
    ("GET", "/v1/snapshots/{snapshot}/file", {"params": {"path": "app/main.py"}}),
    (
        "POST",
        "/v1/search",
        {"json": {"snapshot_id": "{snapshot}", "query": "slugify", "mode": "text"}},
    ),
    (
        "POST",
        "/v1/analysis-runs",
        {"json": {"repository_id": "{repository}", "question": QUESTION}},
    ),
    (
        "POST",
        "/v1/analysis-runs",
        {
            "json": {
                "repository_id": "{repository}",
                "target": {"snapshot_id": "{snapshot}"},
                "question": QUESTION,
            }
        },
    ),
    ("GET", "/v1/analysis-runs", {"params": {"repository_id": "{repository}"}}),
    ("GET", "/v1/analysis-runs/{run}", {}),
    ("GET", "/v1/jobs/{index_job}", {}),
    ("GET", "/v1/jobs/{index_job}/events", {}),
    ("GET", "/v1/jobs/{answer_job}", {}),
    ("GET", "/v1/jobs/{answer_job}/events", {}),
]
# Requests about the repository itself, which stay allowed for its owner.
REPOSITORY_REQUESTS: list[Request] = [
    ("GET", "/v1/repositories/{repository}", {}),
    ("POST", "/v1/repositories/{repository}/index", {"json": {}}),
    ("DELETE", "/v1/repositories/{repository}", {}),
]


def _fill(value: Any, ids: Ids) -> Any:
    if isinstance(value, str):
        return value.format(**ids)
    if isinstance(value, dict):
        return {key: _fill(item, ids) for key, item in value.items()}
    return value


def send(client: TestClient, request: Request, ids: Ids) -> Response:
    method, url, kwargs = request
    return client.request(method, _fill(url, ids), **_fill(kwargs, ids))


def error_of(response: Response) -> dict[str, Any]:
    """The error object of an API response; empty for a success."""
    body = response.json() if response.content else {}
    error: dict[str, Any] = (body.get("error") if isinstance(body, dict) else None) or {}
    return error if response.status_code >= 400 else {}


def label(request: Request) -> str:
    method, url, kwargs = request
    return f"{method} {url} {kwargs or ''}".rstrip()


def drain(run_worker_once: Callable[[], bool]) -> None:
    for _ in range(50):
        if not run_worker_once():
            return
    raise AssertionError("jobs did not finish")


def as_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def access_of(body: dict[str, Any]) -> dict[str, Any]:
    """The repository's `access` object, with its times parsed."""
    found: dict[str, Any] = body["access"]
    times = {key: as_time(found[key]) for key in ("checked_at", "lost_at", "purge_after")}
    return found | times


def counts(db: Session) -> tuple[int | None, int | None]:
    db.expire_all()
    return (
        db.scalar(select(func.count()).select_from(Job)),
        db.scalar(select(func.count()).select_from(AnalysisRun)),
    )


def indexed_and_asked(client: TestClient, run_worker_once: Callable[[], bool]) -> dict[str, str]:
    """Connect and index sample-app, ask one question, and return the IDs involved."""
    connected = client.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})
    assert connected.status_code == 202, connected.text
    drain(run_worker_once)
    repository_id = connected.json()["repository"]["id"]
    repository = client.get(f"/v1/repositories/{repository_id}").json()
    assert repository["state"] == "ready", repository
    asked = client.post(
        "/v1/analysis-runs", json={"repository_id": repository_id, "question": QUESTION}
    )
    assert asked.status_code == 202, asked.text
    drain(run_worker_once)
    return {
        "repository": repository_id,
        "snapshot": repository["active_snapshot"]["id"],
        "index_job": connected.json()["job"]["id"],
        "answer_job": asked.json()["job_id"],
        "run": asked.json()["run_id"],
    }


def load(db: Session, repository_id: str) -> Repository:
    db.expire_all()
    repository = db.get(Repository, uuid.UUID(repository_id))
    assert repository is not None
    return repository


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


@pytest.fixture
def lost(db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]) -> dict[str, str]:
    """octocat's indexed and asked-about repository, then marked lost by a notification."""
    ids = indexed_and_asked(octocat, run_worker_once)
    repository = load(db, ids["repository"])
    repository.access_checked_at = CHECKED_AT
    assert access.mark_access_lost(
        db, repository, "app_uninstalled", trigger="notification", now=LOST_AT
    )
    db.commit()
    return ids


def test_content_reads_of_a_lost_repository_are_denied(
    db: Session, octocat: TestClient, lost: dict[str, str]
) -> None:
    before = counts(db)
    used = octocat.get("/v1/usage").json()["questions_used"]

    failures = []
    for request in CONTENT_READS:
        response = send(octocat, request, lost)
        error = error_of(response)
        if (response.status_code, error.get("code")) != (403, "repository_access_lost"):
            failures.append(f"{label(request)}: {response.status_code} {response.text}")
            continue
        details = error["details"]
        if (
            error["retryable"] is not False
            or details["repository_id"] != lost["repository"]
            or details["reason"] != "app_uninstalled"
            or as_time(details["lost_at"]) != LOST_AT
            or as_time(details["purge_after"]) != LOST_AT + timedelta(days=7)
        ):
            failures.append(f"{label(request)}: {error}")
    assert failures == [], "\n".join(failures)

    # Denied submissions queue nothing and use none of the question allowance.
    assert counts(db) == before
    assert octocat.get("/v1/usage").json()["questions_used"] == used
    # The owner's own repository is not another workspace's: these denials are not audited.
    denials = select(func.count()).select_from(AuditEvent)
    assert db.scalar(denials.where(AuditEvent.action == "access_denied")) == 0


def test_a_lost_repository_stays_listed_with_its_access(
    octocat: TestClient, lost: dict[str, str]
) -> None:
    expected = {
        "state": "lost",
        "reason": "app_uninstalled",
        "checked_at": CHECKED_AT,
        "lost_at": LOST_AT,
        "purge_after": LOST_AT + timedelta(days=7),
    }

    detail = octocat.get(f"/v1/repositories/{lost['repository']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["state"] == "access_lost"
    assert access_of(detail.json()) == expected

    listed = octocat.get("/v1/repositories")
    assert listed.status_code == 200, listed.text
    (item,) = listed.json()["items"]
    assert (item["id"], item["state"]) == (lost["repository"], "access_lost")
    assert access_of(item) == expected


def test_the_owner_can_still_reindex_and_disconnect_a_lost_repository(
    db: Session, octocat: TestClient, lost: dict[str, str]
) -> None:
    url = f"/v1/repositories/{lost['repository']}"

    reindexed = octocat.post(f"{url}/index", json={})
    assert reindexed.status_code == 202, reindexed.text
    # Access loss takes precedence over the waiting run in the display state.
    body = octocat.get(url).json()
    assert body["latest_indexing_job"]["id"] == reindexed.json()["job"]["id"]
    assert body["latest_indexing_job"]["status"] == "queued"
    assert body["state"] == "access_lost"
    # The run's own progress stays hidden until its access check restores the repository.
    job = octocat.get(f"/v1/jobs/{reindexed.json()['job']['id']}")
    assert (job.status_code, job.json()["error"]["code"]) == (403, "repository_access_lost")

    assert octocat.delete(url).status_code == 204
    assert octocat.get(url).status_code == 404
    assert load(db, lost["repository"]).deleted_at is not None


def test_other_workspaces_get_not_found(
    db: Session, signed_in: Callable[[str], TestClient], lost: dict[str, str]
) -> None:
    hubot = signed_in("hubot")

    failures = []
    for request in [*CONTENT_READS, *REPOSITORY_REQUESTS]:
        response = send(hubot, request, lost)
        error = error_of(response)
        if (response.status_code, error.get("code")) != (404, "not_found"):
            failures.append(f"{label(request)}: {response.status_code} {response.text}")
    assert failures == [], "\n".join(failures)
    assert load(db, lost["repository"]).deleted_at is None


def test_access_is_unknown_until_a_check_finishes_then_verified(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    ids = indexed_and_asked(octocat, run_worker_once)
    url = f"/v1/repositories/{ids['repository']}"
    nothing = {"reason": None, "lost_at": None, "purge_after": None}

    repository = load(db, ids["repository"])
    repository.access_checked_at = None
    db.commit()
    body = octocat.get(url).json()
    assert body["state"] == "ready"
    assert access_of(body) == {"state": "unknown", "checked_at": None, **nothing}

    repository.access_checked_at = CHECKED_AT
    db.commit()
    assert access_of(octocat.get(url).json()) == {
        "state": "verified",
        "checked_at": CHECKED_AT,
        **nothing,
    }

    # A pause stops automatic updates only: access still reads as verified, with no loss
    # reason, and content stays readable.
    assert access.pause(db, repository, "sign_in_required")
    db.commit()
    body = octocat.get(url).json()
    assert body["state"] == "ready"
    assert access_of(body) == {"state": "verified", "checked_at": CHECKED_AT, **nothing}
    for request in CONTENT_READS[:6]:
        response = send(octocat, request, ids)
        assert response.status_code == 200, f"{label(request)}: {response.text}"
