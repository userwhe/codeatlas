"""Integration tests for `ops migrate`, `api`'s pre-deploy command (T009, research R7).

Each test runs on its own scratch database, created and dropped by the test, so it can leave the
schema at any revision. The command reads `DATABASE_URL` from the settings, as on the platform.
"""

import logging
import uuid
from collections.abc import Iterator

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import make_url

from alembic import command
from codeatlas.config import Settings
from codeatlas.ops import release
from codeatlas.ops.__main__ import main
from tests.conftest import BACKEND_DIR, TEST_DATABASE_URL

pytestmark = pytest.mark.integration

UNKNOWN_REVISION = "ffffffffffff"


def head_revision() -> str:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


@pytest.fixture
def scratch(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[URL]:
    """An empty database for this test only, which the settings name as `DATABASE_URL`.

    The name is unique, so parallel test sessions on one server never share it.
    """
    url = make_url(TEST_DATABASE_URL).set(database=f"codeatlas_migrate_{uuid.uuid4().hex[:12]}")
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    monkeypatch.setattr(settings, "database_url", url.render_as_string(hide_password=False))
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        admin.dispose()


def schema(url: URL) -> tuple[str | None, list[str]]:
    """The database's Alembic revision and its tables."""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            tables = list(
                conn.scalars(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                        " ORDER BY tablename"
                    )
                )
            )
            revision = None
            if "alembic_version" in tables:
                revision = conn.scalar(text("SELECT version_num FROM alembic_version"))
            return revision, tables
    finally:
        engine.dispose()


def test_migrate_upgrades_an_empty_database_to_the_head_and_then_changes_nothing(
    scratch: URL,
) -> None:
    # No `capsys` here: Alembic's env.py configures logging from alembic.ini with a handler on the
    # standard error of the moment, which `capsys` would close after this test.
    assert main(["migrate"]) == 0

    migrated = schema(scratch)
    assert migrated[0] == head_revision()
    assert "pilot_users" in migrated[1]

    assert main(["migrate"]) == 0

    assert schema(scratch) == migrated


def test_migrate_skips_a_database_ahead_of_the_release(
    scratch: URL, caplog: pytest.LogCaptureFixture
) -> None:
    # A rollback after a migration: the database has a revision this release does not know.
    engine = create_engine(scratch)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        conn.execute(
            text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": UNKNOWN_REVISION}
        )
    engine.dispose()

    with caplog.at_level(logging.INFO, logger=release.__name__):
        assert main(["migrate"]) == 0

    assert schema(scratch) == (UNKNOWN_REVISION, ["alembic_version"])
    skipped = [record for record in caplog.records if record.getMessage() == "migrations_skipped"]
    assert [record.fields for record in skipped] == [
        {"database_revision": UNKNOWN_REVISION, "release_revision": head_revision()}
    ]


def test_migrate_exits_1_when_a_migration_fails(
    scratch: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing_upgrade(config: Config, revision: str) -> None:
        raise RuntimeError("relation already exists")

    monkeypatch.setattr(command, "upgrade", failing_upgrade)

    assert main(["migrate"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert captured.err.count("\n") == 1, captured.err
    assert schema(scratch) == (None, [])
