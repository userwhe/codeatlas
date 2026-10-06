from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.config import Settings
from codeatlas.github.fake import SAMPLE_APP_ID, SAMPLE_APP_RENAMED, get_fake_github
from codeatlas.models import File

pytestmark = pytest.mark.integration

SAMPLE_APP_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "repos" / "sample-app"
README_SETUP_LINE = 6


def run_all(run_worker_once: Callable[[], bool]) -> None:
    while run_worker_once():
        pass


def indexed_sample_app(client: TestClient, run_worker_once: Callable[[], bool]) -> tuple[str, str]:
    """Connect and index sample-app; return the repository ID and its active snapshot ID."""
    response = client.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})
    assert response.status_code == 202, response.text
    run_all(run_worker_once)
    repository_id: str = response.json()["repository"]["id"]
    snapshot_id: str = client.get(f"/v1/repositories/{repository_id}").json()["active_snapshot"][
        "id"
    ]
    return repository_id, snapshot_id


def search(
    client: TestClient, snapshot_id: str, query: str, mode: str = "text", **extra: Any
) -> Any:
    return client.post(
        "/v1/search", json={"snapshot_id": snapshot_id, "query": query, "mode": mode, **extra}
    )


def results(client: TestClient, snapshot_id: str, query: str, mode: str) -> list[dict[str, Any]]:
    response = search(client, snapshot_id, query, mode)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == mode
    hits: list[dict[str, Any]] = body["results"]
    return hits


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


def test_text_mode_finds_case_insensitive_substrings_with_line_numbers(
    octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    _, snapshot_id = indexed_sample_app(octocat, run_worker_once)

    hits = results(octocat, snapshot_id, "default_policy", "text")

    lines = (SAMPLE_APP_DIR / "app/auth/access.py").read_text().split("\n")
    expected = [number for number, line in enumerate(lines, 1) if "DEFAULT_POLICY" in line]
    assert len(expected) >= 2
    assert [(hit["path"], hit["start_line"]) for hit in hits] == [
        ("app/auth/access.py", number) for number in expected
    ]
    for hit in hits:
        assert hit["end_line"] == hit["start_line"]
        assert hit["snippet"] == lines[hit["start_line"] - 1].strip()
        assert (hit["exact"], hit["symbol"]) == (False, None)

    many = results(octocat, snapshot_id, "import", "text")
    assert len({hit["path"] for hit in many}) > 1
    assert [(h["path"], h["start_line"]) for h in many] == sorted(
        (h["path"], h["start_line"]) for h in many
    )
    # LIKE wildcards in the query match themselves only.
    assert results(octocat, snapshot_id, "check%access", "text") == []
    assert results(octocat, snapshot_id, "check_repository_access", "text")


def test_path_mode_ranks_exact_matches_first(
    octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    _, snapshot_id = indexed_sample_app(octocat, run_worker_once)

    by_name = results(octocat, snapshot_id, "access.py", "path")
    assert by_name[0]["path"] == "app/auth/access.py"
    assert by_name[0]["exact"] is True
    assert by_name[0]["start_line"] == 1
    assert by_name[0]["snippet"].startswith('"""Repository access checks."""')

    by_full_path = results(octocat, snapshot_id, "src/types.ts", "path")
    assert (by_full_path[0]["path"], by_full_path[0]["exact"]) == ("src/types.ts", True)

    partial = results(octocat, snapshot_id, "auth", "path")
    assert {"app/auth/access.py", "app/auth/__init__.py"} <= {hit["path"] for hit in partial}
    assert not any(hit["exact"] for hit in partial)

    exact_flags = [hit["exact"] for hit in results(octocat, snapshot_id, "__init__.py", "path")]
    assert exact_flags[:3] == [True, True, True]
    assert exact_flags == sorted(exact_flags, reverse=True)


def test_symbol_mode_returns_exact_symbol_first(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    _, snapshot_id = indexed_sample_app(octocat, run_worker_once)

    hits = results(octocat, snapshot_id, "check_repository_access", "symbol")

    first = hits[0]
    assert first["path"] == "app/auth/access.py"
    assert first["exact"] is True
    assert first["symbol"] == {"name": "check_repository_access", "kind": "function"}
    assert first["snippet"].startswith("def check_repository_access(")
    assert not any(hit["exact"] for hit in hits[1:])
    # The declared lines open in the file browser.
    opened = octocat.get(
        f"/v1/snapshots/{snapshot_id}/file",
        params={
            "path": first["path"],
            "start_line": first["start_line"],
            "end_line": first["end_line"],
        },
    ).json()
    assert opened["lines"][0].startswith("def check_repository_access(")
    assert len(opened["lines"]) == first["end_line"] - first["start_line"] + 1

    method = results(octocat, snapshot_id, "AccessPolicy.can_read", "symbol")[0]
    assert method["exact"] is True
    assert method["symbol"] == {"name": "can_read", "kind": "method"}

    fuzzy = results(octocat, snapshot_id, "check_access", "symbol")
    assert fuzzy and not fuzzy[0]["exact"]
    assert fuzzy[0]["symbol"]["name"] == "check_repository_access"


def test_docs_mode_returns_the_setup_section(
    octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    _, snapshot_id = indexed_sample_app(octocat, run_worker_once)

    response = search(octocat, snapshot_id, "setup", "docs")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["degraded"] is False
    first = body["results"][0]
    assert first["path"] == "README.md"
    assert first["start_line"] <= README_SETUP_LINE <= first["end_line"]
    assert "Setup" in first["snippet"]
    assert len(first["snippet"]) <= 300


def test_docs_mode_without_embeddings_is_keyword_only_and_degraded(
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, snapshot_id = indexed_sample_app(octocat, run_worker_once)
    monkeypatch.setattr(settings, "fake_embedder_mode", "unavailable")

    body = search(octocat, snapshot_id, "setup", "docs").json()

    assert body["degraded"] is True
    first = body["results"][0]
    assert first["path"] == "README.md"
    assert first["start_line"] <= README_SETUP_LINE <= first["end_line"]
    # Only documentation search depends on embeddings.
    assert search(octocat, snapshot_id, "setup", "text").json()["degraded"] is False


def test_limit_and_query_bounds(octocat: TestClient, run_worker_once: Callable[[], bool]) -> None:
    _, snapshot_id = indexed_sample_app(octocat, run_worker_once)

    cases: list[tuple[str, dict[str, Any]]] = [
        ("import", {"limit": 0}),
        ("import", {"limit": 51}),
        ("", {}),
        ("x" * 201, {}),
    ]
    for query, extra in cases:
        response = search(octocat, snapshot_id, query, **extra)
        assert response.status_code == 422, (query, extra)
        assert response.json()["error"]["code"] == "invalid_request"
    assert search(octocat, snapshot_id, "import", "everything").status_code == 422

    limited = search(octocat, snapshot_id, "import", limit=2).json()["results"]
    assert len(limited) == 2
    assert search(octocat, snapshot_id, "x" * 200, limit=50).status_code == 200


def test_results_come_only_from_the_requested_snapshot(
    db: Session,
    signed_in: Callable[[str], TestClient],
    run_worker_once: Callable[[], bool],
) -> None:
    octocat = signed_in("octocat")
    repository_id, older_id = indexed_sample_app(octocat, run_worker_once)
    get_fake_github().advance(SAMPLE_APP_ID)
    octocat.post(f"/v1/repositories/{repository_id}/index", json={})
    run_all(run_worker_once)
    newer_id = octocat.get(f"/v1/repositories/{repository_id}").json()["active_snapshot"]["id"]
    assert newer_id != older_id
    old_path, new_path = SAMPLE_APP_RENAMED

    for snapshot_id, path in ((older_id, old_path), (newer_id, new_path)):
        paths = set(db.scalars(select(File.path).where(File.snapshot_id == snapshot_id)))
        for query, mode in (
            ("slugify", "text"),
            ("slugify", "symbol"),
            ("utils", "path"),
            ("access", "docs"),
        ):
            hits = results(octocat, snapshot_id, query, mode)
            assert hits, (query, mode)
            assert {hit["path"] for hit in hits} <= paths, (query, mode)
        assert {hit["path"] for hit in results(octocat, snapshot_id, "slugify", "symbol")} == {path}
        assert path in {hit["path"] for hit in results(octocat, snapshot_id, "slugify", "text")}

    hubot = signed_in("hubot")
    denied = search(hubot, older_id, "slugify")
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "not_found"
