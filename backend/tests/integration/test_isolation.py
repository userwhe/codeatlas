"""Cross-workspace isolation with two users (SC-008, FR-004, FR-033).

`octocat` and `hubot` both connect and index `octo-org/sample-app` (2001) in their own
workspaces. `octocat` then re-indexes at a newer commit, so its active snapshot holds a path that
`hubot`'s does not, and asks a question. On every `/v1` endpoint that takes one of `octocat`'s
IDs, `hubot` gets exactly the response for a nonexistent resource, and the denial is audited.

To cover a new route, add a row to `CASES`; `test_every_id_route_has_a_case` fails until then.
"""

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from codeatlas.github.fake import SAMPLE_APP_ID, SAMPLE_APP_RENAMED, get_fake_github
from codeatlas.models import AnalysisRun, AuditEvent, File, Job

pytestmark = pytest.mark.integration

QUESTION = "Where are repository permissions checked?"
ONLY_IN_OCTOCAT_SNAPSHOT = SAMPLE_APP_RENAMED[1]  # `app/utils/text.py`, added by the new commit
SEARCH_QUERIES = {"text": "slugify", "path": "utils", "symbol": "slugify", "docs": "access"}

# Keys of the IDs a case can use, and the audit `resource_type` of a denial for each. Requests
# may also use `own_repository`, hubot's own repository.
RESOURCE_TYPES = {
    "repository": "repository",
    "snapshot": "snapshot",  # octocat's active snapshot
    "old_snapshot": "snapshot",  # octocat's previous snapshot, at the commit hubot indexed
    "index_job": "job",
    "answer_job": "job",
    "run": "analysis_run",
}

Ids = Mapping[str, str]
Workspaces = tuple[TestClient, TestClient, dict[str, str]]


def _fill(value: Any, ids: Ids) -> Any:
    if isinstance(value, str):
        return value.format(**ids)
    if isinstance(value, dict):
        return {key: _fill(item, ids) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class Case:
    """A request hubot makes for one of octocat's resources.

    The path parameter of `route` is that resource's ID; `{key}` placeholders in `query` and
    `body` are filled in from the IDs.
    """

    method: str
    route: str
    resource: str
    query: dict[str, str] | None = None
    body: dict[str, Any] | None = None

    def send(self, client: TestClient, ids: Ids) -> Response:
        return client.request(
            self.method,
            re.sub(r"\{\w+\}", ids[self.resource], self.route),
            params=_fill(self.query, ids),
            json=_fill(self.body, ids),
        )


CASES = [
    Case("GET", "/v1/repositories/{repository_id}", "repository"),
    Case("GET", "/v1/repositories/{repository_id}/snapshots", "repository"),
    Case("POST", "/v1/repositories/{repository_id}/index", "repository", body={}),
    Case("GET", "/v1/snapshots/{snapshot_id}", "snapshot"),
    Case("GET", "/v1/snapshots/{snapshot_id}", "old_snapshot"),
    Case("GET", "/v1/snapshots/{snapshot_id}/coverage", "snapshot"),
    Case("GET", "/v1/snapshots/{snapshot_id}/tree", "snapshot", query={"path": "app"}),
    Case("GET", "/v1/snapshots/{snapshot_id}/file", "snapshot", query={"path": "app/main.py"}),
    *(
        Case(
            "POST",
            "/v1/search",
            "snapshot",
            body={"snapshot_id": "{snapshot}", "query": query, "mode": mode},
        )
        for mode, query in SEARCH_QUERIES.items()
    ),
    Case("GET", "/v1/analysis-runs", "repository", query={"repository_id": "{repository}"}),
    Case("GET", "/v1/analysis-runs/{run_id}", "run"),
    Case(
        "POST",
        "/v1/analysis-runs",
        "repository",
        body={"repository_id": "{repository}", "question": QUESTION},
    ),
    Case(
        "POST",
        "/v1/analysis-runs",
        "snapshot",
        body={
            "repository_id": "{own_repository}",
            "target": {"snapshot_id": "{snapshot}"},
            "question": QUESTION,
        },
    ),
    Case("GET", "/v1/jobs/{job_id}", "index_job"),
    Case("GET", "/v1/jobs/{job_id}/events", "index_job"),
    Case("GET", "/v1/jobs/{job_id}", "answer_job"),
    Case("GET", "/v1/jobs/{job_id}/events", "answer_job"),
    Case("DELETE", "/v1/repositories/{repository_id}", "repository"),
]


def _id_routes() -> set[tuple[str, str]]:
    """`/v1` operations that take a workspace resource ID in the path, query, or JSON body."""
    from codeatlas.api.app import app

    schema = app.openapi()
    components = schema.get("components", {}).get("schemas", {})

    def fields(node: Any) -> set[str]:
        if not isinstance(node, dict):
            return set()
        if "$ref" in node:
            return fields(components[node["$ref"].rsplit("/", 1)[-1]])
        nested = [fields(option) for option in node.get("anyOf", [])]
        return set(node.get("properties", {})).union(*nested)

    routes = set()
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            body = operation.get("requestBody", {}).get("content", {}).get("application/json", {})
            names = {parameter["name"] for parameter in operation.get("parameters", [])}
            names |= fields(body.get("schema", {}))
            # GitHub repository IDs are checked against GitHub, not against the workspace.
            if path.startswith("/v1/") and any(
                name.endswith("_id") and not name.startswith("github_") for name in names
            ):
                routes.add((method.upper(), path))
    return routes


def test_every_id_route_has_a_case() -> None:
    assert len(_id_routes()) >= 12
    missing = _id_routes() - {(case.method, case.route) for case in CASES}
    assert missing == set(), "add a row to CASES for each of these routes"


def _drain(run_worker_once: Callable[[], bool]) -> None:
    for _ in range(50):
        if not run_worker_once():
            return
    raise AssertionError("jobs did not finish")


def _connect(client: TestClient) -> tuple[str, str]:
    response = client.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})
    assert response.status_code == 202, response.text
    body = response.json()
    return body["repository"]["id"], body["job"]["id"]


def _active_snapshot(client: TestClient, repository_id: str) -> str:
    repository = client.get(f"/v1/repositories/{repository_id}").json()
    assert repository["state"] == "ready", repository
    snapshot_id: str = repository["active_snapshot"]["id"]
    return snapshot_id


def _ids(response: Response) -> set[str]:
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["items"]}


def _snapshot_paths(db: Session, snapshot_id: str) -> set[str]:
    return set(db.scalars(select(File.path).where(File.snapshot_id == snapshot_id)))


def _row_counts(db: Session) -> tuple[int | None, int | None]:
    db.expire_all()
    return (
        db.scalar(select(func.count()).select_from(Job)),
        db.scalar(select(func.count()).select_from(AnalysisRun)),
    )


@pytest.fixture
def workspaces(
    signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> Workspaces:
    """Clients for octocat and hubot, and the IDs of what they created."""
    octocat, hubot = signed_in("octocat"), signed_in("hubot")
    repository, index_job = _connect(octocat)
    own_repository, _ = _connect(hubot)
    _drain(run_worker_once)
    old_snapshot = _active_snapshot(octocat, repository)

    get_fake_github().advance(SAMPLE_APP_ID)
    assert octocat.post(f"/v1/repositories/{repository}/index", json={}).status_code == 202
    _drain(run_worker_once)
    asked = octocat.post(
        "/v1/analysis-runs", json={"repository_id": repository, "question": QUESTION}
    )
    assert asked.status_code == 202, asked.text
    _drain(run_worker_once)

    ids = {
        "repository": repository,
        "snapshot": _active_snapshot(octocat, repository),
        "old_snapshot": old_snapshot,
        "index_job": index_job,
        "answer_job": asked.json()["job_id"],
        "run": asked.json()["run_id"],
        "own_repository": own_repository,
    }
    assert len(set(ids.values())) == len(ids)
    return octocat, hubot, ids


def test_lists_show_only_own_workspace(workspaces: Workspaces) -> None:
    octocat, hubot, ids = workspaces
    mine, theirs = ids["own_repository"], ids["repository"]

    assert hubot.get("/v1/me").json()["workspace"] != octocat.get("/v1/me").json()["workspace"]
    assert _ids(hubot.get("/v1/repositories")) == {mine}
    assert _ids(octocat.get("/v1/repositories")) == {theirs}
    # The same upstream commit is indexed separately in each workspace.
    own_snapshot = _active_snapshot(hubot, mine)
    assert own_snapshot != ids["old_snapshot"]
    assert _ids(hubot.get(f"/v1/repositories/{mine}/snapshots")) == {own_snapshot}
    assert _ids(octocat.get(f"/v1/repositories/{theirs}/snapshots")) == {
        ids["snapshot"],
        ids["old_snapshot"],
    }
    assert _ids(hubot.get("/v1/analysis-runs", params={"repository_id": mine})) == set()
    assert _ids(octocat.get("/v1/analysis-runs", params={"repository_id": theirs})) == {ids["run"]}
    connectable = hubot.get("/v1/github/repositories").json()["items"]
    connected = {
        item["github_repository_id"]: item["connected_repository_id"] for item in connectable
    }
    assert connected[SAMPLE_APP_ID] == mine
    assert hubot.get("/v1/usage").json()["questions_used"] == 0
    assert octocat.get("/v1/usage").json()["questions_used"] == 1


def test_search_returns_only_own_content(db: Session, workspaces: Workspaces) -> None:
    octocat, hubot, ids = workspaces
    own_snapshot = _active_snapshot(hubot, ids["own_repository"])
    own_paths = _snapshot_paths(db, own_snapshot)
    assert ONLY_IN_OCTOCAT_SNAPSHOT in _snapshot_paths(db, ids["snapshot"]) - own_paths

    for mode, query in SEARCH_QUERIES.items():
        request = {"snapshot_id": own_snapshot, "query": query, "mode": mode}
        response = hubot.post("/v1/search", json=request)

        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert results, f"{mode} search found nothing in hubot's own snapshot"
        assert {result["path"] for result in results} <= own_paths, mode
        hits = [(result["path"], result["start_line"], result["end_line"]) for result in results]
        assert len(hits) == len(set(hits)), f"{mode} results repeat, as if from two snapshots"

    # The path exists only in octocat's snapshot, where the same search finds it.
    request = {"snapshot_id": ids["snapshot"], "query": SEARCH_QUERIES["path"], "mode": "path"}
    found = octocat.post("/v1/search", json=request).json()["results"]
    assert ONLY_IN_OCTOCAT_SNAPSHOT in {result["path"] for result in found}


def test_other_workspace_ids_look_missing_and_are_audited(
    db: Session, workspaces: Workspaces
) -> None:
    octocat, hubot, ids = workspaces
    me = hubot.get("/v1/me").json()
    missing = {key: str(uuid.uuid4()) for key in ids} | {"own_repository": ids["own_repository"]}
    before = _row_counts(db)

    failures = []
    for case in CASES:
        label = f"{case.method} {case.route} with octocat's {case.resource}"
        nonexistent = case.send(hubot, missing)
        response = case.send(hubot, ids)
        if nonexistent.status_code != 404:
            failures.append(f"{label}: a random ID gave {nonexistent.status_code}")
            continue
        expected = nonexistent.json()["error"]
        error = response.json().get("error", {}) if response.content else {}
        if (response.status_code, error.get("code"), error.get("message")) != (
            404,
            expected["code"],
            expected["message"],
        ):
            failures.append(f"{label}: {response.status_code} {response.text}")
            continue
        events = db.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "access_denied",
                AuditEvent.request_id == response.headers["X-Request-ID"],
            )
        ).all()
        recorded = [
            (e.outcome, str(e.actor_user_id), str(e.workspace_id), e.resource_type, e.resource_id)
            for e in events
        ]
        audit = (
            "denied",
            me["user"]["id"],
            me["workspace"]["id"],
            RESOURCE_TYPES[case.resource],
            ids[case.resource],
        )
        if recorded != [audit]:
            failures.append(f"{label}: audit events {recorded}")
    assert failures == [], "\n".join(failures)

    # No denied request changed anything: no new jobs or runs, octocat's repository is intact,
    # and hubot's question allowance is unused.
    assert _row_counts(db) == before
    assert _active_snapshot(octocat, ids["repository"]) == ids["snapshot"]
    assert hubot.get("/v1/usage").json()["questions_used"] == 0
    denials = db.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == "access_denied", AuditEvent.actor_user_id == me["user"]["id"])
    )
    assert denials == len(CASES)
