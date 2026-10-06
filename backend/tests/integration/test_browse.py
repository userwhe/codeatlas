import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from codeatlas.github.fake import SAMPLE_APP_ID, SAMPLE_APP_RENAMED, commit_sha, get_fake_github
from codeatlas.models import File, Snapshot

pytestmark = pytest.mark.integration

SAMPLE_APP_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "repos" / "sample-app"


def run_all(run_worker_once: Callable[[], bool]) -> None:
    while run_worker_once():
        pass


def fixture_lines(path: str) -> list[str]:
    lines = (SAMPLE_APP_DIR / path).read_text().split("\n")
    return lines[:-1] if lines[-1] == "" else lines


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


def tree(client: TestClient, snapshot_id: str, path: str | None = None) -> Any:
    params = {} if path is None else {"path": path}
    return client.get(f"/v1/snapshots/{snapshot_id}/tree", params=params)


def read_file(client: TestClient, snapshot_id: str, path: str, **lines: int) -> Any:
    return client.get(f"/v1/snapshots/{snapshot_id}/file", params={"path": path, **lines})


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


def test_root_and_nested_directories_list_their_children(
    octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    _, snapshot_id = indexed_sample_app(octocat, run_worker_once)

    root = tree(octocat, snapshot_id)
    assert root.status_code == 200, root.text
    body = root.json()
    assert body["path"] == ""
    types = [entry["type"] for entry in body["entries"]]
    assert types == sorted(types, key=lambda kind: kind != "directory")
    assert [e["name"] for e in body["entries"] if e["type"] == "directory"] == [
        "app",
        "docs",
        "src",
    ]
    readme = next(entry for entry in body["entries"] if entry["name"] == "README.md")
    assert readme == {
        "name": "README.md",
        "type": "file",
        "language": "markdown",
        "line_count": len(fixture_lines("README.md")),
    }
    # Excluded entries (node_modules, credentials, generated, binary) are not part of the tree.
    names = {entry["name"] for entry in body["entries"]}
    assert not names & {"node_modules", ".env", "id_rsa", "static", "assets"}

    nested = tree(octocat, snapshot_id, "./app/auth/")
    assert nested.status_code == 200, nested.text
    assert nested.json() == {
        "path": "app/auth",
        "entries": [
            {
                "name": "__init__.py",
                "type": "file",
                "language": "python",
                "line_count": len(fixture_lines("app/auth/__init__.py")),
            },
            {
                "name": "access.py",
                "type": "file",
                "language": "python",
                "line_count": len(fixture_lines("app/auth/access.py")),
            },
        ],
    }
    app_dir = tree(octocat, snapshot_id, "app").json()["entries"]
    assert [entry["name"] for entry in app_dir if entry["type"] == "directory"] == [
        "auth",
        "utils",
    ]

    assert tree(octocat, snapshot_id, "no/such/dir").status_code == 404
    assert tree(octocat, snapshot_id, "README.md").status_code == 404


def test_file_returns_exact_lines(octocat: TestClient, run_worker_once: Callable[[], bool]) -> None:
    _, snapshot_id = indexed_sample_app(octocat, run_worker_once)
    expected = fixture_lines("app/auth/access.py")

    whole = read_file(octocat, snapshot_id, "app/auth/access.py")
    assert whole.status_code == 200, whole.text
    assert whole.json() == {
        "path": "app/auth/access.py",
        "commit_sha": commit_sha(SAMPLE_APP_ID, "initial"),
        "language": "python",
        "line_count": len(expected),
        "start_line": 1,
        "end_line": len(expected),
        "lines": expected,
    }

    window = read_file(octocat, snapshot_id, "/app/auth/access.py", start_line=5, end_line=9)
    assert window.status_code == 200, window.text
    body = window.json()
    assert (body["path"], body["start_line"], body["end_line"]) == ("app/auth/access.py", 5, 9)
    assert body["lines"] == expected[4:9]

    clamped = read_file(octocat, snapshot_id, "app/auth/access.py", start_line=20, end_line=999)
    assert clamped.json()["end_line"] == len(expected)
    assert clamped.json()["lines"] == expected[19:]


def test_file_requests_are_capped_at_1000_lines(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    _, snapshot_id = indexed_sample_app(octocat, run_worker_once)
    snapshot = db.get(Snapshot, uuid.UUID(snapshot_id))
    assert snapshot is not None
    content = "".join(f"line {number}\n" for number in range(1, 1501))
    db.add(
        File(
            workspace_id=snapshot.workspace_id,
            snapshot_id=snapshot.id,
            path="data/long.txt",
            language="text",
            size_bytes=len(content),
            line_count=1500,
            content_sha256=hashlib.sha256(content.encode()).digest(),
            content=content,
        )
    )
    db.commit()

    default = read_file(octocat, snapshot_id, "data/long.txt").json()
    assert (default["start_line"], default["end_line"], len(default["lines"])) == (1, 1000, 1000)
    assert default["lines"][-1] == "line 1000"

    wide = read_file(octocat, snapshot_id, "data/long.txt", start_line=1, end_line=1500).json()
    assert (wide["end_line"], len(wide["lines"])) == (1000, 1000)

    tail = read_file(octocat, snapshot_id, "data/long.txt", start_line=1001, end_line=5000).json()
    assert (tail["start_line"], tail["end_line"], tail["line_count"]) == (1001, 1500, 1500)
    assert tail["lines"][0] == "line 1001"
    assert len(tail["lines"]) == 500


def test_invalid_paths_and_ranges(octocat: TestClient, run_worker_once: Callable[[], bool]) -> None:
    _, snapshot_id = indexed_sample_app(octocat, run_worker_once)

    unknown = read_file(octocat, snapshot_id, "app/missing.py")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"
    # Excluded files are not readable.
    assert read_file(octocat, snapshot_id, ".env").status_code == 404

    for path in ("app/../README.md", "../README.md", "app\\main.py"):
        response = read_file(octocat, snapshot_id, path)
        assert response.status_code == 422, path
        assert response.json()["error"]["code"] == "invalid_path"
    assert tree(octocat, snapshot_id, "app/..").status_code == 422

    for lines in ({"start_line": 0}, {"start_line": 5, "end_line": 4}, {"start_line": 500}):
        response = read_file(octocat, snapshot_id, "README.md", **lines)
        assert response.status_code == 422, lines
        assert "error" in response.json()


def test_older_snapshot_returns_its_own_content(
    octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id, older_id = indexed_sample_app(octocat, run_worker_once)
    get_fake_github().advance(SAMPLE_APP_ID)
    octocat.post(f"/v1/repositories/{repository_id}/index", json={})
    run_all(run_worker_once)
    newer_id = octocat.get(f"/v1/repositories/{repository_id}").json()["active_snapshot"]["id"]
    assert newer_id != older_id
    old_path, new_path = SAMPLE_APP_RENAMED

    older = read_file(octocat, older_id, old_path)
    assert older.status_code == 200, older.text
    assert older.json()["commit_sha"] == commit_sha(SAMPLE_APP_ID, "initial")
    assert older.json()["lines"] == fixture_lines(old_path)
    assert read_file(octocat, older_id, new_path).status_code == 404

    newer = read_file(octocat, newer_id, new_path)
    assert newer.status_code == 200, newer.text
    assert newer.json()["commit_sha"] == commit_sha(SAMPLE_APP_ID, "second")
    assert read_file(octocat, newer_id, old_path).status_code == 404

    def utils(snapshot_id: str) -> set[str]:
        return {
            entry["name"] for entry in tree(octocat, snapshot_id, "app/utils").json()["entries"]
        }

    assert utils(older_id) == {"__init__.py", "strings.py"}
    assert utils(newer_id) == {"__init__.py", "text.py"}


def test_snapshot_that_is_not_ready_returns_409(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id, ready_id = indexed_sample_app(octocat, run_worker_once)
    ready = db.get(Snapshot, uuid.UUID(ready_id))
    assert ready is not None
    building = Snapshot(
        workspace_id=ready.workspace_id,
        repository_id=uuid.UUID(repository_id),
        commit_sha="0" * 40,
        branch="main",
        index_version=ready.index_version,
        status="building",
    )
    db.add(building)
    db.commit()

    for response in (
        tree(octocat, str(building.id)),
        read_file(octocat, str(building.id), "README.md"),
    ):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "snapshot_not_ready"


def test_other_workspace_snapshot_returns_404(
    signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    _, snapshot_id = indexed_sample_app(signed_in("octocat"), run_worker_once)
    hubot = signed_in("hubot")

    for response in (
        tree(hubot, snapshot_id),
        read_file(hubot, snapshot_id, "README.md"),
        tree(hubot, str(uuid.uuid4())),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
