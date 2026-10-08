"""Integration tests for readiness and the running version (T006, contracts/http-api.md).

`/healthz` answers while the process runs; `/readyz` also needs the database, and nothing else
(FR-011); `/version` reports the release baked into the image (FR-019).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from codeatlas import db as db_module
from codeatlas.config import Settings

pytestmark = pytest.mark.integration

RELEASE = "3f9c2e1d4b5a69788c7d0e1f2a3b4c5d6e7f8091"


def test_ready_with_the_database_up(client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_not_ready_without_the_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable() -> Session:
        raise OperationalError("SELECT 1", {}, ConnectionRefusedError("connection refused"))

    monkeypatch.setattr(db_module, "get_sessionmaker", lambda: unreachable)

    response = client.get("/readyz")

    assert response.status_code == 503
    error = response.json()["error"]
    assert (error["code"], error["message"], error["retryable"]) == (
        "not_ready",
        "The database is unavailable.",
        True,
    )
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert client.get("/healthz").status_code == 200


def test_ready_while_the_model_providers_are_down(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "fake_answer_model_mode", "unavailable")
    monkeypatch.setattr(settings, "fake_embedder_mode", "unavailable")

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_version_defaults_to_development(client: TestClient) -> None:
    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {"commit": "development"}


def test_version_reports_the_release(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "release", RELEASE)

    assert client.get("/version").json() == {"commit": RELEASE}


def test_health_paths_are_not_in_the_openapi_document(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert {"/healthz", "/readyz", "/version"}.isdisjoint(paths)
