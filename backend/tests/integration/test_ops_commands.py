"""Integration tests for the operator commands (T016, T042, T032, FR-004, contracts/operations.md).

They call `codeatlas.ops.__main__.main` as `python -m codeatlas.ops` would, and check the exit
status, the output, and the database.
"""

import hashlib
import io
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, table, text
from sqlalchemy.orm import Session

from codeatlas.config import Settings
from codeatlas.github.fake import NO_CODE_ID, SAMPLE_APP_ID, SOLO_ID
from codeatlas.models import (
    AuditEvent,
    Backup,
    GitHubCredential,
    PilotUser,
    Repository,
    User,
    UserSession,
)
from codeatlas.ops import dump, storage
from codeatlas.ops.__main__ import main
from codeatlas.ops.backup import build_manifest
from codeatlas.ops.release import known_revisions_and_head
from tests.integration.test_backup_restore import MemoryStore

pytestmark = pytest.mark.integration

OCTOCAT_ID = 1001
HUBOT_ID = 1002
RELEASE = "3f9c2e1d4b5a69788c7d0e1f2a3b4c5d6e7f8091"


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


# verify-restore ----------------------------------------------------------------------------


def row_counts(db: Session) -> dict[str, int]:
    """The row count of every table in the `public` schema, as `ops backup` records them."""
    db.expire_all()
    names = db.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    counts: dict[str, int] = {}
    for name in names.all():
        counts[name] = db.scalar(select(func.count()).select_from(table(name))) or 0
    return counts


# The test database is migrated to the head revision.
HEAD = known_revisions_and_head()[1]


def write_counts(db: Session, path: Path) -> dict[str, int]:
    """Write the database's revision and row counts, which `verify-restore` reads as a manifest."""
    counts = row_counts(db)
    path.write_text(json.dumps({"alembic_revision": HEAD, "row_counts": counts}))
    return counts


def test_verify_restore_compares_every_row_count(
    db: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["pilot-users", "add", "octocat"]) == 0
    manifest_file = tmp_path / "manifest.json"
    counts = write_counts(db, manifest_file)
    assert counts["pilot_users"] == 1 and counts["alembic_version"] == 1
    capsys.readouterr()

    assert main(["verify-restore", str(manifest_file)]) == 0
    assert "matches" in capsys.readouterr().out

    db.add(AuditEvent(action="sign_out", outcome="success"))
    db.commit()

    assert main(["verify-restore", str(manifest_file)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "audit_events: row count 1 in the backup, 2 restored" in captured.err
    assert "pilot_users" not in captured.err


def test_verify_restore_reports_a_different_revision(
    db: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps({"alembic_revision": "0003", "row_counts": row_counts(db)}))

    assert main(["verify-restore", str(manifest_file)]) == 1

    assert f"Alembic revision: 0003 in the backup, {HEAD} restored" in capsys.readouterr().err


def test_verify_restore_refuses_an_invalid_manifest(
    db: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"alembic_revision": HEAD, "row_counts": {"users": "6"}}))
    truncated = tmp_path / "truncated.json"
    truncated.write_text('{"alembic_revision": "0004", "row_co')

    assert "users" in refused(capsys, ["verify-restore", str(invalid)])
    assert "manifest" in refused(capsys, ["verify-restore", str(truncated)])
    assert "manifest" in refused(capsys, ["verify-restore", str(tmp_path / "missing.json")])


# backup and restore ------------------------------------------------------------------------

BACKUP_PREFIX = "backups/2026-10-13/033012/"


@pytest.mark.parametrize(
    "argv", [["backup"], ["restore", "2026-10-13", "--yes"]], ids=["backup", "restore"]
)
def test_backup_and_restore_name_each_missing_bucket_setting_without_a_value(
    db: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.setattr(settings, "backup_endpoint_url", "https://bucket-endpoint.example.test")
    monkeypatch.setattr(settings, "backup_region", "")
    monkeypatch.setattr(settings, "backup_bucket", " ")
    monkeypatch.setattr(settings, "backup_access_key_id", "access-key-id-value")
    monkeypatch.setattr(settings, "backup_secret_access_key", "")
    assert main(["pilot-users", "add", "octocat"]) == 0
    capsys.readouterr()

    error = refused(capsys, argv)

    for name in ("BACKUP_REGION", "BACKUP_BUCKET", "BACKUP_SECRET_ACCESS_KEY"):
        assert name in error
    for name in ("BACKUP_ENDPOINT_URL", "BACKUP_ACCESS_KEY_ID"):
        assert name not in error
    assert "bucket-endpoint.example.test" not in error
    assert "access-key-id-value" not in error
    assert pilot_users(db) == [(OCTOCAT_ID, "octocat", None)]
    assert db.scalars(select(Backup)).all() == []


def test_restore_without_yes_restores_nothing_unless_the_answer_is_yes(
    db: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["pilot-users", "add", "octocat"]) == 0
    # The restore itself is replaced, so the dump never reaches `pg_restore`, and the manifest
    # holds the test database's own counts, which the comparison after the restore reads.
    stored_dump = b"a dump"
    manifest = build_manifest(
        {"alembic_revision": HEAD, "row_counts": row_counts(db)},
        release=RELEASE,
        dump_key=BACKUP_PREFIX + "codeatlas.dump",
        bytes_=len(stored_dump),
        sha256=hashlib.sha256(stored_dump).hexdigest(),
        created_at=datetime(2026, 10, 13, 3, 30, 12, tzinfo=UTC),
    )
    store = MemoryStore()
    store.objects = {
        BACKUP_PREFIX + "codeatlas.dump": stored_dump,
        BACKUP_PREFIX + "manifest.json": json.dumps(manifest).encode(),
    }
    monkeypatch.setattr(storage.S3Store, "from_settings", lambda: store)
    restored: list[Path] = []
    monkeypatch.setattr(dump, "restore_into_empty_schema", restored.append)
    capsys.readouterr()

    for answer in ["no\n", "y\n", ""]:
        monkeypatch.setattr("sys.stdin", io.StringIO(answer))

        assert main(["restore", "2026-10-13"]) == 1

        captured = capsys.readouterr()
        assert "Type yes" in captured.out
        assert captured.err.startswith("error: ") and captured.err.count("\n") == 1, captured.err
        assert "nothing was changed" in captured.err
    assert restored == []
    assert pilot_users(db) == [(OCTOCAT_ID, "octocat", None)]

    monkeypatch.setattr("sys.stdin", io.StringIO("yes\n"))

    assert main(["restore", "2026-10-13"]) == 0

    assert len(restored) == 1
    assert "matches the backup" in capsys.readouterr().out


def test_restore_refuses_a_missing_backup(
    db: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = MemoryStore()
    store.objects = {
        BACKUP_PREFIX + "codeatlas.dump": b"a dump",
        # Complete, but on another day.
        "backups/2026-10-12/033012/codeatlas.dump": b"a dump",
        "backups/2026-10-12/033012/manifest.json": b"{}",
    }
    monkeypatch.setattr(storage.S3Store, "from_settings", lambda: store)

    # The dump of 2026-10-13 has no manifest, so the day has no complete backup.
    assert "2026-10-13" in refused(capsys, ["restore", "2026-10-13", "--yes"])
    assert "2026-10-13/033012" in refused(capsys, ["restore", "2026-10-13/033012", "--yes"])
    assert "2026-10-11" in refused(capsys, ["restore", "2026-10-11", "--yes"])
    assert "UTC date" in refused(capsys, ["restore", "yesterday", "--yes"])
    assert "UTC date" in refused(capsys, ["restore", "2026-10-12/3:30", "--yes"])
