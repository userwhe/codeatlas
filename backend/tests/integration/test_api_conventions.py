from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from codeatlas.api.errors import ApiError
from codeatlas.models import Workspace
from codeatlas.workspace.idempotency import run_idempotent

pytestmark = pytest.mark.integration


def test_unknown_route_uses_error_shape(client: TestClient) -> None:
    response = client.get("/v1/does-not-exist")

    assert response.status_code == 404
    error = response.json()["error"]
    assert set(error) == {"code", "message", "retryable", "request_id", "details"}
    assert error["code"] == "not_found"
    assert error["request_id"] == response.headers["X-Request-ID"]


def test_validation_error_uses_error_shape(signed_in: Callable[[str], TestClient]) -> None:
    response = signed_in("octocat").get("/v1/jobs/not-a-uuid/events", params={"after": "x"})

    assert response.status_code in (404, 422)
    assert "error" in response.json()


def make_workspace(db: Session) -> Workspace:
    workspace = Workspace(name="w")
    db.add(workspace)
    db.commit()
    return workspace


class Counter:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> tuple[int, dict[str, Any]]:
        self.calls += 1
        return 202, {"value": self.calls}


def test_idempotent_replay_returns_stored_response(db: Session) -> None:
    workspace = make_workspace(db)
    operation = Counter()

    first = run_idempotent(
        db, workspace_id=workspace.id, route="r", key="k1", payload={"a": 1}, operation=operation
    )
    db.commit()
    second = run_idempotent(
        db, workspace_id=workspace.id, route="r", key="k1", payload={"a": 1}, operation=operation
    )
    db.commit()

    assert first == second == (202, {"value": 1})
    assert operation.calls == 1


def test_idempotency_key_reused_with_different_body_conflicts(db: Session) -> None:
    workspace = make_workspace(db)
    run_idempotent(
        db, workspace_id=workspace.id, route="r", key="k1", payload={"a": 1}, operation=Counter()
    )
    db.commit()

    with pytest.raises(ApiError) as excinfo:
        run_idempotent(
            db,
            workspace_id=workspace.id,
            route="r",
            key="k1",
            payload={"a": 2},
            operation=Counter(),
        )
    assert (excinfo.value.status, excinfo.value.code) == (409, "idempotency_conflict")


def test_idempotency_key_length_is_validated(db: Session) -> None:
    workspace = make_workspace(db)

    with pytest.raises(ApiError) as excinfo:
        run_idempotent(
            db,
            workspace_id=workspace.id,
            route="r",
            key="x" * 129,
            payload={},
            operation=Counter(),
        )
    assert excinfo.value.status == 422


def test_without_key_the_operation_always_runs(db: Session) -> None:
    workspace = make_workspace(db)
    operation = Counter()

    for _ in range(2):
        run_idempotent(
            db, workspace_id=workspace.id, route="r", key=None, payload={}, operation=operation
        )

    assert operation.calls == 2
