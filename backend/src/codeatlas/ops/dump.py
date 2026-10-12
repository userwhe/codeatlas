"""The database side of `ops backup` and `ops restore` (specs/004-pilot-deployment, research R12).

`pg_dump` and `pg_restore` 17, the server's major version, connect with the libpq environment
derived from `DATABASE_URL`, so no password appears on a command line. `snapshot_dump` dumps the
database and counts its rows in one snapshot; `restore_into_empty_schema` replaces the `public`
schema, whatever revision it had, with a dump's.
"""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, create_engine, func, select, table, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from codeatlas.config import get_settings
from codeatlas.ops.backup import BackupError

LIBPQ_VARIABLES = ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE")


def libpq_environment(database_url: str) -> dict[str, str]:
    """This process's environment, with the connection of `database_url` as libpq variables.

    A libpq variable already set is replaced, or removed when the URL lacks that part.
    """
    url = make_url(database_url)
    parts = (url.host, url.port, url.username, url.password, url.database)
    environment = {name: value for name, value in os.environ.items() if name not in LIBPQ_VARIABLES}
    for name, value in zip(LIBPQ_VARIABLES, parts, strict=True):
        if value is not None:
            environment[name] = str(value)
    return environment


def _first_error(stderr: str) -> str:
    """The tool's first error line: the lines after it may quote a statement or a row."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    errors = [line for line in lines if "error:" in line]
    return (errors or lines or ["no message"])[0]


def _run(tool: str, *arguments: str) -> None:
    """Run a PostgreSQL client tool on `DATABASE_URL`; raise `BackupError` if it fails."""
    path = shutil.which(tool)
    if path is None:
        raise BackupError(f"{tool} is not installed")
    completed = subprocess.run(  # noqa: S603 - a fixed program and arguments, without a shell
        [path, "--no-password", *arguments],
        env=libpq_environment(get_settings().database_url),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise BackupError(
            f"{tool} failed with exit status {completed.returncode}: "
            f"{_first_error(completed.stderr)}"
        )


def database_counts(connection: Connection) -> dict[str, Any]:
    """The counts document: the Alembic revision and the row count of each `public` table.

    `backup.build_manifest` reads it, and `backup.compare` checks a restored database with it. The
    revision is None in a database without one.
    """
    names = connection.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    row_counts = {
        name: connection.scalar(select(func.count()).select_from(table(name, schema="public"))) or 0
        for name in names.all()
    }
    revision = None
    if "alembic_version" in row_counts:
        revision = connection.scalar(text("SELECT version_num FROM public.alembic_version"))
    return {"alembic_revision": revision, "row_counts": row_counts}


def snapshot_dump(path: Path) -> dict[str, Any]:
    """Dump the database into `path` (`pg_dump -Fc`), and return the dump's counts document.

    The dump and the counts share one snapshot: a repeatable-read transaction exports it and stays
    open while `pg_dump` reads through it, then counts in it, so the counts describe exactly the
    dumped data even while users write.
    """
    engine = create_engine(
        get_settings().database_url, poolclass=NullPool, isolation_level="REPEATABLE READ"
    )
    try:
        with engine.connect() as connection:
            snapshot = connection.scalar(text("SELECT pg_export_snapshot()"))
            _run("pg_dump", "--format=custom", f"--snapshot={snapshot}", f"--file={path}")
            return database_counts(connection)
    finally:
        engine.dispose()


def check_dump(path: Path) -> None:
    """Read the dump's table of contents (`pg_restore --list`); raise `BackupError` if it fails."""
    _run("pg_restore", "--list", str(path))


def sha256_of(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def restore_into_empty_schema(path: Path) -> None:
    """Replace the database's `public` schema with the dump at `path`.

    Every other session on the database ends first, so no open transaction writes or holds a
    lock. The schema is dropped and created again, so the restore starts empty whatever revision
    the database had; the dump creates the extensions in it (`vector` and `pg_trgm`) again.
    `pg_restore` stops at its first error.
    """
    database_url = get_settings().database_url
    engine = create_engine(database_url, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
            )
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()
    database = make_url(database_url).database
    _run("pg_restore", "--no-owner", "--exit-on-error", f"--dbname={database}", str(path))
