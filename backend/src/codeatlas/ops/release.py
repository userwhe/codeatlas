"""The release steps, which the platform runs before a new version starts.

They follow specs/004-pilot-deployment, research R7, and contracts/operations.md ("Operator
commands"):

- `run_migrations` is `api`'s pre-deploy command. Under an advisory lock, it upgrades the database
  to this release's head when the database's revision is one this release knows, or when it has
  none. A database ahead of the release, after a rollback that follows a migration, is left as it
  is: the expand-then-contract rule keeps older versions working with newer schemas.
- `wait_for_api` is the release gate of `worker` and `backup`. It waits until the API reports this
  release's commit, which it does only after its migration and readiness check passed, so a failed
  API stops the whole release.
"""

import logging
import time
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Literal

import httpx
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.pool import NullPool

from alembic import command
from codeatlas.config import get_settings

logger = logging.getLogger(__name__)

Decision = Literal["upgrade", "current", "ahead"]

# `backend/` in the repository and `/app` in the image; both hold `alembic.ini` and `alembic/`.
BACKEND_DIR = Path(__file__).resolve().parents[3]
# Any fixed key works while nothing else takes this advisory lock.
MIGRATION_LOCK_KEY = 4_004_007
GATE_TIMEOUT_SECONDS = 20 * 60
GATE_INTERVAL_SECONDS = 5.0
GATE_REQUEST_TIMEOUT = httpx.Timeout(5.0)


def decide(database_revision: str | None, known_revisions: Collection[str], head: str) -> Decision:
    """What `ops migrate` does with a database at `database_revision` (None before any migration).

    A revision this release does not know comes from a later release: the database is ahead.
    """
    if database_revision == head:
        return "current"
    if database_revision is None or database_revision in known_revisions:
        return "upgrade"
    return "ahead"


def _alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return config


def known_revisions_and_head() -> tuple[frozenset[str], str]:
    """Every revision this release's migrations define, and the head revision among them."""
    scripts = ScriptDirectory.from_config(_alembic_config())
    head = scripts.get_current_head()
    if head is None:
        raise RuntimeError("this release defines no migrations")
    return frozenset(script.revision for script in scripts.walk_revisions()), head


def database_revision(connection: Connection) -> str | None:
    """`alembic_version.version_num`, or None when the database has no `alembic_version` table."""
    return MigrationContext.configure(connection).get_current_revision()


def run_migrations() -> Decision:
    """Bring the database to this release's head unless it is ahead, and return the decision.

    The lock makes a concurrent `ops migrate` wait, so two deployments never migrate at once. Its
    connection autocommits, so it holds no transaction while Alembic migrates on a connection of
    its own. A failed migration raises.
    """
    known_revisions, head = known_revisions_and_head()
    engine = create_engine(
        get_settings().database_url, poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
        try:
            revision = database_revision(connection)
            decision = decide(revision, known_revisions, head)
            if decision == "upgrade":
                command.upgrade(_alembic_config(), "head")
            elif decision == "ahead":
                logger.warning(
                    "migrations_skipped",
                    extra={"fields": {"database_revision": revision, "release_revision": head}},
                )
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY})
    return decision


def _reported_commit(client: httpx.Client, origin: str) -> str | None:
    """The commit in the API's `/version`, or None while it does not answer with one."""
    try:
        response = client.get(f"{origin.rstrip('/')}/version")
    except httpx.HTTPError:
        return None
    if not response.is_success:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    commit = body.get("commit") if isinstance(body, dict) else None
    return commit if isinstance(commit, str) else None


def wait_for_api(
    origin: str,
    commit: str,
    *,
    timeout: float = GATE_TIMEOUT_SECONDS,
    interval: float = GATE_INTERVAL_SECONDS,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll `<origin>/version` every `interval` seconds until it reports `commit`.

    Returns false when `timeout` seconds pass first. Another commit, an error status, or no answer
    means the API's new version does not serve yet. Without a commit there is nothing to wait for.
    """
    if not commit:
        return True
    http = client if client is not None else httpx.Client(timeout=GATE_REQUEST_TIMEOUT)
    try:
        deadline = clock() + timeout
        reported = _reported_commit(http, origin)
        if reported != commit:
            logger.info(
                "release_gate_waiting",
                extra={"fields": {"commit": commit, "reported_commit": reported}},
            )
        while reported != commit and clock() < deadline:
            sleep(interval)
            reported = _reported_commit(http, origin)
    finally:
        if client is None:
            http.close()
    passed = reported == commit
    logger.log(
        logging.INFO if passed else logging.ERROR,
        "release_gate_finished",
        extra={
            "fields": {
                "commit": commit,
                "reported_commit": reported,
                "outcome": "passed" if passed else "timed_out",
            }
        },
    )
    return passed
