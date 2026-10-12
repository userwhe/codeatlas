"""Integration tests for `ops backup` and `ops restore` (T031, research R12).

They run both commands end to end against scratch databases, created and dropped by each test as
test_ops_migrate.py does, with an in-memory object store standing in for the bucket. The commands
read `DATABASE_URL` from the settings, as on the platform. They need `pg_dump` and `pg_restore` 17
or later, which the CI job installs, and are skipped otherwise.
"""

import hashlib
import json
import logging
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable, Collection, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import URL, create_engine, func, select, table, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from alembic import command
from codeatlas.config import Settings
from codeatlas.models import Backup
from codeatlas.ops import dump, release
from codeatlas.ops.__main__ import run_backup, run_restore
from codeatlas.ops.backup import backup_prefix, build_manifest
from tests.conftest import BACKEND_DIR, TEST_DATABASE_URL

MINIMUM_TOOL_VERSION = 17


def _tool_version(tool: str) -> int | None:
    """The major version that `<tool> --version` reports, or None when the tool is missing."""
    path = shutil.which(tool)
    if path is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - a fixed argument list
            [path, "--version"], capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\(PostgreSQL\) (\d+)", completed.stdout)
    return int(match.group(1)) if match else None


def _unusable_tools() -> list[str]:
    unusable = []
    for tool in ("pg_dump", "pg_restore"):
        version = _tool_version(tool)
        if version is None:
            unusable.append(f"{tool} not found")
        elif version < MINIMUM_TOOL_VERSION:
            unusable.append(f"{tool} {version}")
    return unusable


UNUSABLE_TOOLS = _unusable_tools()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        bool(UNUSABLE_TOOLS),
        reason=f"needs pg_dump and pg_restore {MINIMUM_TOOL_VERSION} or later "
        f"({', '.join(UNUSABLE_TOOLS)})",
    ),
]

RELEASE = "3f9c2e1d4b5a69788c7d0e1f2a3b4c5d6e7f8091"
NOW = datetime(2026, 10, 13, 3, 30, 12, tzinfo=UTC)
PREFIX = "backups/2026-10-13/033012/"
DUMP_KEY = PREFIX + "codeatlas.dump"
MANIFEST_KEY = PREFIX + "manifest.json"
HEAD = release.known_revisions_and_head()[1]


class MemoryStore:
    """An object store in memory, implementing `ObjectStore`, that records each stored key.

    An upload of the file named `failing`, such as `manifest.json`, fails.
    """

    def __init__(self, failing: str | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.stored: list[str] = []
        self.failing = failing

    def put(self, key: str, path: Path) -> None:
        if self.failing is not None and key.endswith(f"/{self.failing}"):
            raise ConnectionError(f"the upload of {key} failed")
        self.objects[key] = path.read_bytes()
        self.stored.append(key)

    def get(self, key: str, path: Path) -> None:
        path.write_bytes(self.objects[key])

    def delete(self, keys: Collection[str]) -> None:
        for key in keys:
            del self.objects[key]

    def list(self, prefix: str) -> list[str]:
        return sorted(key for key in self.objects if key.startswith(prefix))


def at(moment: datetime) -> Callable[[], datetime]:
    """A clock that always reads `moment`."""
    return lambda: moment


def alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return config


@pytest.fixture
def use(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Callable[[URL], None]:
    """Name a database as `DATABASE_URL`, which the commands and the migrations read."""

    def name(url: URL) -> None:
        monkeypatch.setattr(settings, "database_url", url.render_as_string(hide_password=False))

    return name


@pytest.fixture
def scratch(use: Callable[[URL], None]) -> Iterator[Callable[[str], URL]]:
    """Create databases for this test only, migrated to a revision, and drop them afterwards.

    The names are unique, so parallel test sessions on one server never share one. Creating one
    leaves `DATABASE_URL` naming it; tests name the database of each command themselves.
    """
    admin = create_engine(
        make_url(TEST_DATABASE_URL).set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    created: list[str] = []

    def create(revision: str) -> URL:
        url = make_url(TEST_DATABASE_URL).set(database=f"codeatlas_backup_{uuid.uuid4().hex[:12]}")
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
        assert url.database is not None
        created.append(url.database)
        use(url)
        command.upgrade(alembic_config(), revision)
        return url

    try:
        yield create
    finally:
        with admin.connect() as conn:
            for name in created:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def seed(url: URL, *logins: str) -> None:
    """A user, a pilot user, and an audit event per login, in SQL that revision 0004 accepts too."""
    engine = create_engine(url, poolclass=NullPool)
    with engine.begin() as conn:
        for number, login in enumerate(logins, start=1):
            parameters = {"id": 5000 + number, "login": login}
            conn.execute(
                text(
                    "INSERT INTO users (id, github_user_id, github_login)"
                    " VALUES (gen_random_uuid(), :id, :login)"
                ),
                parameters,
            )
            conn.execute(
                text(
                    "INSERT INTO pilot_users (github_user_id, github_login, note)"
                    " VALUES (:id, :login, 'invited')"
                ),
                parameters,
            )
            conn.execute(
                text(
                    "INSERT INTO audit_events (action, outcome, detail)"
                    " VALUES ('sign_out', 'success', CAST(:detail AS jsonb))"
                ),
                {"detail": json.dumps({"login": login})},
            )
    engine.dispose()


@pytest.fixture
def source(scratch: Callable[[str], URL]) -> URL:
    """A database at the head revision with rows for two users, which the backups dump."""
    url = scratch("head")
    seed(url, "octocat", "hubot")
    return url


@pytest.fixture
def target(scratch: Callable[[str], URL]) -> URL:
    """A second database at the head revision with rows of its own, which a restore replaces."""
    url = scratch("head")
    seed(url, "mona")
    return url


def read(url: URL, query: Callable[[Session], Any]) -> Any:
    engine = create_engine(url, poolclass=NullPool)
    try:
        with Session(engine) as db:
            return query(db)
    finally:
        engine.dispose()


def tables(url: URL) -> list[str]:
    return list(
        read(
            url,
            lambda db: db.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
            ).all(),
        )
    )


def revision(url: URL) -> str | None:
    result: str | None = read(
        url, lambda db: db.scalar(text("SELECT version_num FROM alembic_version"))
    )
    return result


def row_counts(url: URL) -> dict[str, int]:
    def count(db: Session) -> dict[str, int]:
        names = db.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        return {
            name: db.scalar(select(func.count()).select_from(table(name))) or 0
            for name in names.all()
        }

    counts: dict[str, int] = read(url, count)
    return counts


def logins(url: URL) -> list[str]:
    return list(
        read(
            url,
            lambda db: db.scalars(
                text("SELECT github_login FROM pilot_users ORDER BY github_login")
            ).all(),
        )
    )


def backup_rows(url: URL) -> list[Backup]:
    return list(
        read(
            url,
            lambda db: db.scalars(
                select(Backup).order_by(Backup.completed_at, Backup.object_key)
            ).all(),
        )
    )


def stored_backup(store: MemoryStore, url: URL, created_at: datetime) -> str:
    """A completed backup's objects and its row, with placeholder content; returns its prefix."""
    prefix = backup_prefix(created_at)
    store.objects[prefix + "codeatlas.dump"] = b"dump"
    store.objects[prefix + "manifest.json"] = b"{}"
    engine = create_engine(url, poolclass=NullPool)
    with Session(engine) as db, db.begin():
        db.add(
            Backup(
                object_key=prefix + "codeatlas.dump",
                completed_at=created_at,
                bytes=4,
                sha256=hashlib.sha256(b"dump").hexdigest(),
                alembic_revision=HEAD,
                release=RELEASE,
            )
        )
    engine.dispose()
    return prefix


def lines(caplog: pytest.LogCaptureFixture, message: str) -> list[dict[str, Any]]:
    return [record.fields for record in caplog.records if record.getMessage() == message]


# backup ------------------------------------------------------------------------------------


def test_backup_stores_the_dump_then_its_manifest_and_writes_one_row(
    source: URL,
    use: Callable[[URL], None],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    use(source)
    monkeypatch.setattr(settings, "release", RELEASE)
    snapshots: list[dict[str, Any]] = []
    snapshot_dump = dump.snapshot_dump

    def recording_snapshot_dump(path: Path) -> dict[str, Any]:
        counts = snapshot_dump(path)
        snapshots.append(counts)
        return counts

    monkeypatch.setattr(dump, "snapshot_dump", recording_snapshot_dump)
    store = MemoryStore()

    with caplog.at_level(logging.INFO, logger="codeatlas.ops"):
        assert run_backup(store, clock=at(NOW)) == 0

    assert store.stored == [DUMP_KEY, MANIFEST_KEY]
    stored_dump = store.objects[DUMP_KEY]
    sha256 = hashlib.sha256(stored_dump).hexdigest()
    [row] = backup_rows(source)
    assert (row.object_key, row.completed_at, row.bytes, row.sha256) == (
        DUMP_KEY,
        NOW,
        len(stored_dump),
        sha256,
    )
    assert (row.alembic_revision, row.release) == (HEAD, RELEASE)
    [counts] = snapshots
    assert json.loads(store.objects[MANIFEST_KEY]) == {
        "created_at": "2026-10-13T03:30:12Z",
        "release": RELEASE,
        "alembic_revision": HEAD,
        "dump": {"key": DUMP_KEY, "bytes": len(stored_dump), "sha256": sha256},
        "row_counts": counts["row_counts"],
    }
    # The counts describe the dump's snapshot, not the live tables, which now hold the new row.
    assert counts["alembic_revision"] == HEAD
    assert counts["row_counts"]["pilot_users"] == 2
    assert counts["row_counts"]["backups"] == 0
    assert row_counts(source) == {**counts["row_counts"], "backups": 1}
    assert lines(caplog, "backup_pruned") == [{"pruned": 0}]
    assert lines(caplog, "backup_completed") == [
        {"object_key": DUMP_KEY, "bytes": len(stored_dump)}
    ]


def test_a_second_run_on_the_same_day_keeps_the_first_and_a_used_prefix_stops_the_run(
    source: URL, use: Callable[[URL], None], capsys: pytest.CaptureFixture[str]
) -> None:
    use(source)
    store = MemoryStore()
    assert run_backup(store, clock=at(NOW)) == 0
    first = dict(store.objects)
    later = datetime(2026, 10, 13, 14, 5, 0, tzinfo=UTC)
    second = "backups/2026-10-13/140500/"

    assert run_backup(store, clock=at(later)) == 0

    assert {key: store.objects[key] for key in first} == first
    assert store.list("") == sorted([*first, second + "codeatlas.dump", second + "manifest.json"])
    assert [row.object_key for row in backup_rows(source)] == [
        DUMP_KEY,
        second + "codeatlas.dump",
    ]
    stored = dict(store.objects)
    capsys.readouterr()

    # Another run in the same second would reuse the prefix.
    assert run_backup(store, clock=at(later)) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ") and captured.err.count("\n") == 1, captured.err
    assert second in captured.err
    assert store.objects == stored
    assert len(backup_rows(source)) == 2


@pytest.mark.parametrize("dump_fails", [False, True], ids=["dump succeeds", "dump fails"])
def test_pruning_deletes_old_backups_and_their_rows_whatever_happens_next(
    source: URL,
    use: Callable[[URL], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    dump_fails: bool,
) -> None:
    use(source)
    store = MemoryStore()
    old = stored_backup(store, source, NOW - timedelta(days=8))
    recent = stored_backup(store, source, NOW - timedelta(days=3))
    if dump_fails:

        def failing_snapshot_dump(path: Path) -> dict[str, Any]:
            raise RuntimeError("pg_dump failed")

        monkeypatch.setattr(dump, "snapshot_dump", failing_snapshot_dump)

    with caplog.at_level(logging.INFO, logger="codeatlas.ops"):
        assert run_backup(store, clock=at(NOW)) == (1 if dump_fails else 0)

    assert old == "backups/2026-10-05/033012/"
    assert store.list(old) == []
    assert store.list(recent) == [recent + "codeatlas.dump", recent + "manifest.json"]
    assert lines(caplog, "backup_pruned") == [{"pruned": 2}]
    rows = [row.object_key for row in backup_rows(source)]
    if dump_fails:
        assert rows == [recent + "codeatlas.dump"]
        assert store.list(PREFIX) == []
        assert lines(caplog, "backup_completed") == []
    else:
        assert rows == [recent + "codeatlas.dump", DUMP_KEY]
        assert store.list(PREFIX) == [DUMP_KEY, MANIFEST_KEY]


def test_a_failed_manifest_upload_writes_no_row(
    source: URL, use: Callable[[URL], None], capsys: pytest.CaptureFixture[str]
) -> None:
    use(source)
    store = MemoryStore(failing="manifest.json")

    assert run_backup(store, clock=at(NOW)) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ") and captured.err.count("\n") == 1, captured.err
    assert backup_rows(source) == []
    # A dump without its manifest is incomplete: restore ignores it, and pruning removes it.
    assert store.list("") == [DUMP_KEY]


# restore -----------------------------------------------------------------------------------


def test_restore_into_another_database_ends_its_sessions_and_verifies_the_backup(
    source: URL, target: URL, use: Callable[[URL], None], capsys: pytest.CaptureFixture[str]
) -> None:
    use(source)
    store = MemoryStore()
    assert run_backup(store, clock=at(NOW)) == 0
    manifest = json.loads(store.objects[MANIFEST_KEY])
    use(target)
    engine = create_engine(target, poolclass=NullPool)
    other = engine.connect()
    # An open transaction holding a lock on a table that the restore drops.
    assert other.scalar(text("SELECT count(*) FROM pilot_users")) == 1
    capsys.readouterr()

    try:
        assert run_restore(store, "2026-10-13", yes=True) == 0

        with pytest.raises(OperationalError):
            other.execute(text("SELECT 1"))
    finally:
        other.close()
        engine.dispose()
    output = capsys.readouterr().out
    assert f"Restored {PREFIX}" in output
    assert f"matches the backup: Alembic revision {HEAD}" in output
    assert revision(target) == HEAD
    assert logins(target) == ["hubot", "octocat"]
    # The backup's own row is written after its dump, so the restored database has none.
    assert row_counts(target) == manifest["row_counts"]
    assert manifest["row_counts"]["backups"] == 0


def test_restore_of_an_older_revision_verifies_it_then_migrates_it(
    scratch: Callable[[str], URL],
    use: Callable[[URL], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # No `capsys` here: the migration runs Alembic's env.py, which configures logging with a
    # handler on the standard error of the moment, which `capsys` would close after this test.
    older = scratch("0004")
    seed(older, "octocat")
    use(older)
    # A database at 0004 has no `backups` table, so the backup is stored as `ops backup` stores
    # it, without the row.
    store = MemoryStore()
    dump_path = tmp_path / "codeatlas.dump"
    counts = dump.snapshot_dump(dump_path)
    dump.check_dump(dump_path)
    manifest = build_manifest(
        counts,
        release=RELEASE,
        dump_key=DUMP_KEY,
        bytes_=dump_path.stat().st_size,
        sha256=dump.sha256_of(dump_path),
        created_at=NOW,
    )
    store.objects[DUMP_KEY] = dump_path.read_bytes()
    store.objects[MANIFEST_KEY] = json.dumps(manifest).encode()
    assert manifest["alembic_revision"] == "0004"
    target = scratch("head")
    use(target)
    revisions_before_migrating: list[str | None] = []
    run_migrations = release.run_migrations

    def recording_run_migrations() -> release.Decision:
        revisions_before_migrating.append(revision(target))
        return run_migrations()

    monkeypatch.setattr(release, "run_migrations", recording_run_migrations)

    assert run_restore(store, "2026-10-13/033012", yes=True) == 0

    # Verified at 0004, since a difference stops the restore before the migration, then migrated.
    assert revisions_before_migrating == ["0004"]
    assert revision(target) == HEAD
    assert {"backups", "worker_heartbeats"} <= set(tables(target))
    assert logins(target) == ["octocat"]


def test_restore_refuses_a_dump_that_does_not_match_its_manifest_before_dropping_anything(
    source: URL, target: URL, use: Callable[[URL], None], capsys: pytest.CaptureFixture[str]
) -> None:
    use(source)
    store = MemoryStore()
    assert run_backup(store, clock=at(NOW)) == 0
    stored_dump = store.objects[DUMP_KEY]
    store.objects[DUMP_KEY] = stored_dump[:-1] + bytes([stored_dump[-1] ^ 0xFF])
    use(target)
    engine = create_engine(target, poolclass=NullPool)
    other = engine.connect()
    assert other.scalar(text("SELECT count(*) FROM pilot_users")) == 1
    capsys.readouterr()

    try:
        assert run_restore(store, "2026-10-13", yes=True) == 1

        # The other session was not ended.
        assert other.scalar(text("SELECT count(*) FROM pilot_users")) == 1
    finally:
        other.close()
        engine.dispose()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ") and captured.err.count("\n") == 1, captured.err
    assert "SHA-256" in captured.err
    assert revision(target) == HEAD
    assert logins(target) == ["mona"]
