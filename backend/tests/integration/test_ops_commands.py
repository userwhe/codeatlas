"""Integration tests for the operator commands (T016, FR-004, contracts/operations.md).

They call `codeatlas.ops.__main__.main` as `python -m codeatlas.ops` would, and check the exit
status, the output, and the database.
"""

import uuid
from collections.abc import Callable
from datetime import UTC

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.config import Settings
from codeatlas.github.fake import NO_CODE_ID, SAMPLE_APP_ID, SOLO_ID
from codeatlas.models import AuditEvent, GitHubCredential, PilotUser, Repository, User, UserSession
from codeatlas.ops.__main__ import main

pytestmark = pytest.mark.integration

OCTOCAT_ID = 1001
HUBOT_ID = 1002


def pilot_users(db: Session) -> list[tuple[int, str, str | None]]:
    db.expire_all()
    rows = db.scalars(select(PilotUser).order_by(PilotUser.github_user_id))
    return [(row.github_user_id, row.github_login, row.note) for row in rows]


def audit(db: Session, action: str) -> list[AuditEvent]:
    db.expire_all()
    return list(db.scalars(select(AuditEvent).where(AuditEvent.action == action)))


def refused(capsys: pytest.CaptureFixture[str], argv: list[str]) -> str:
    """Run a command that must exit 1, and return its one line of error output."""
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.endswith("\n") and captured.err.count("\n") == 1, captured.err
    return captured.err


def octocat_user(db: Session) -> User:
    return db.scalars(select(User).where(User.github_login == "octocat")).one()


def connect(client: TestClient, github_id: int) -> str:
    """Connect a repository (its indexing stays queued) and return its ID."""
    response = client.post("/v1/repositories", json={"github_repository_id": github_id})
    assert response.status_code == 202, response.text
    repository_id: str = response.json()["repository"]["id"]
    return repository_id


# add and list ------------------------------------------------------------------------------


def test_add_resolves_the_login_and_records_it(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["pilot-users", "add", "octocat", "--note", "friend"]) == 0

    assert "octocat" in capsys.readouterr().out
    assert pilot_users(db) == [(OCTOCAT_ID, "octocat", "friend")]
    [event] = audit(db, "pilot_user_add")
    assert event.outcome == "success"
    assert event.actor_user_id is None
    assert event.detail == {"github_user_id": OCTOCAT_ID, "github_login": "octocat"}


def test_add_refusals_change_nothing(
    db: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["pilot-users", "add", "octocat"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(settings, "pilot_user_limit", 1)

    assert "octocat" in refused(capsys, ["pilot-users", "add", "octocat", "--note", "again"])
    assert "nobody-by-this-name" in refused(capsys, ["pilot-users", "add", "nobody-by-this-name"])
    assert "at most 1 users" in refused(capsys, ["pilot-users", "add", "hubot"])

    assert pilot_users(db) == [(OCTOCAT_ID, "octocat", None)]
    assert len(audit(db, "pilot_user_add")) == 1


def test_add_refuses_a_long_note(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    refused(capsys, ["pilot-users", "add", "octocat", "--note", "x" * 201])

    assert pilot_users(db) == []


def test_list_prints_every_user(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pilot-users", "add", "octocat", "--note", "friend"]) == 0
    assert main(["pilot-users", "add", "hubot"]) == 0
    capsys.readouterr()
    added = {
        row.github_login: row.added_at.astimezone(UTC).date().isoformat()
        for row in db.scalars(select(PilotUser))
    }

    assert main(["pilot-users", "list"]) == 0

    lines = capsys.readouterr().out.splitlines()
    rows = [line.split("\t") for line in lines[1:]]
    assert lines[0].split("\t") == ["login", "github_user_id", "note", "added_at"]
    assert sorted(rows) == sorted(
        [
            ["octocat", str(OCTOCAT_ID), "friend", added["octocat"]],
            ["hubot", str(HUBOT_ID), "", added["hubot"]],
        ]
    )


def test_list_with_nobody_listed(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pilot-users", "list"]) == 0

    assert capsys.readouterr().out.splitlines() == ["login\tgithub_user_id\tnote\tadded_at"]


# remove ------------------------------------------------------------------------------------


def test_remove_deletes_the_row_and_ends_every_session(
    db: Session, signed_in: Callable[[str], TestClient], capsys: pytest.CaptureFixture[str]
) -> None:
    clients = [signed_in("octocat"), signed_in("octocat")]
    hubot = signed_in("hubot")
    assert main(["pilot-users", "add", "octocat"]) == 0
    assert main(["pilot-users", "add", "hubot"]) == 0

    assert main(["pilot-users", "remove", "octocat"]) == 0

    assert pilot_users(db) == [(HUBOT_ID, "hubot", None)]
    for client in clients:
        assert client.get("/v1/me").status_code == 401
    assert hubot.get("/v1/me").status_code == 200
    user = octocat_user(db)
    sessions = db.scalars(select(UserSession).where(UserSession.user_id == user.id)).all()
    assert len(sessions) == 2
    assert all(session.revoked_at is not None for session in sessions)
    [event] = audit(db, "pilot_user_remove")
    assert (event.outcome, event.actor_user_id) == ("success", None)
    assert event.detail == {"github_user_id": OCTOCAT_ID, "github_login": "octocat"}


def test_remove_before_the_first_sign_in(db: Session) -> None:
    assert main(["pilot-users", "add", "octocat"]) == 0

    assert main(["pilot-users", "remove", "octocat"]) == 0

    assert pilot_users(db) == []
    assert len(audit(db, "pilot_user_remove")) == 1


def test_remove_refuses_an_unlisted_login(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pilot-users", "add", "hubot"]) == 0
    capsys.readouterr()

    assert "octocat" in refused(capsys, ["pilot-users", "remove", "octocat"])

    assert pilot_users(db) == [(HUBOT_ID, "hubot", None)]
    assert audit(db, "pilot_user_remove") == []


# delete-data -------------------------------------------------------------------------------


def test_delete_data_refuses_a_listed_user(
    db: Session, signed_in: Callable[[str], TestClient], capsys: pytest.CaptureFixture[str]
) -> None:
    client = signed_in("octocat")
    connect(client, SAMPLE_APP_ID)
    assert main(["pilot-users", "add", "octocat"]) == 0
    capsys.readouterr()

    assert "octocat" in refused(capsys, ["pilot-users", "delete-data", "octocat"])

    db.expire_all()
    assert db.scalars(select(Repository)).one().deleted_at is None
    assert db.get(GitHubCredential, octocat_user(db).id) is not None
    assert client.get("/v1/me").status_code == 200
    assert audit(db, "pilot_user_delete_data") == []


def test_delete_data_refuses_an_unknown_user(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "octocat" in refused(capsys, ["pilot-users", "delete-data", "octocat"])

    assert audit(db, "pilot_user_delete_data") == []


def test_delete_data_disconnects_every_repository_and_forgets_the_user(
    db: Session, signed_in: Callable[[str], TestClient], capsys: pytest.CaptureFixture[str]
) -> None:
    client = signed_in("octocat")
    hubot = signed_in("hubot")
    repository_ids = [
        connect(client, github_id) for github_id in (SAMPLE_APP_ID, SOLO_ID, NO_CODE_ID)
    ]
    hubot_repository_id = connect(hubot, SAMPLE_APP_ID)
    # One repository was already disconnected by its owner.
    assert client.delete(f"/v1/repositories/{repository_ids[2]}").status_code == 204
    assert main(["pilot-users", "add", "octocat"]) == 0
    assert main(["pilot-users", "remove", "octocat"]) == 0
    capsys.readouterr()

    assert main(["pilot-users", "delete-data", "octocat"]) == 0

    assert "2 repositories" in capsys.readouterr().out
    db.expire_all()
    user = octocat_user(db)
    for repository_id in repository_ids:
        repository = db.get(Repository, uuid.UUID(repository_id))
        assert repository is not None and repository.deleted_at is not None
    hubot_repository = db.get(Repository, uuid.UUID(hubot_repository_id))
    assert hubot_repository is not None and hubot_repository.deleted_at is None
    # One by the owner, and two by the command, still with the user as actor (data-model.md).
    disconnects = audit(db, "repository_disconnect")
    assert len(disconnects) == 3
    assert {event.actor_user_id for event in disconnects} == {user.id}
    assert db.get(GitHubCredential, user.id) is None
    assert client.get("/v1/me").status_code == 401
    assert hubot.get("/v1/me").status_code == 200
    sessions = db.scalars(select(UserSession).where(UserSession.user_id == user.id)).all()
    assert all(session.revoked_at is not None for session in sessions)
    [event] = audit(db, "pilot_user_delete_data")
    assert (event.outcome, event.actor_user_id) == ("success", None)
    assert event.detail == {"github_user_id": OCTOCAT_ID, "github_login": "octocat"}
