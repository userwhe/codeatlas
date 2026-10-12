"""Operator commands (specs/004-pilot-deployment, FR-004, contracts/operations.md).

The developer runs them in the `api` service's container through the platform's SSH, as
`railway ssh -s api -- python -m codeatlas.ops <command>`:

    pilot-users add <login> [--note TEXT]
    pilot-users remove <login>
    pilot-users delete-data <login>
    pilot-users list
    restore <backup> [--yes]
    verify-restore MANIFEST

The platform runs the release steps (research R7): `migrate` is `api`'s pre-deploy command, and
`wait-for-api` is the release gate of `worker` and `backup`. The `backup` service runs `backup`
every night, and the developer may run it by hand (research R12). `backup` and `restore` read only
`DATABASE_URL`, the release, and the `BACKUP_*` settings, so they run on a service without
`CODEATLAS_ENV` and the application's secrets.

Each command except the release steps, `backup`, and `restore` runs in one transaction, committed
only when it succeeds. A command exits 0 on success, and 1 on a refusal or a failure, which it
explains on standard error: in one line, or, when `verify-restore` or `restore` finds
differences, with one more line for each.
"""

import argparse
import json
import logging
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from codeatlas.auth import access_list
from codeatlas.config import get_settings
from codeatlas.db import new_session
from codeatlas.github.gateway import GitHubError, get_gateway
from codeatlas.logging import configure_logging
from codeatlas.models import Backup
from codeatlas.ops import backup, dump, release, storage

# Not `__name__`, which is `__main__` under `python -m codeatlas.ops`.
logger = logging.getLogger("codeatlas.ops")

Command = Callable[[Session, argparse.Namespace], str]
"""Runs one command in the session and returns what to print on success."""

MIGRATE_OUTPUT: dict[release.Decision, str] = {
    "upgrade": "Migrated the database to this release's revision.",
    "current": "The database is at this release's revision already.",
    "ahead": "The database is ahead of this release; skipped the migrations.",
}
# `restore`'s argument: a backup's UTC date, and optionally its UTC time (research R12).
BACKUP_NAME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})(?:/([0-9]{6}))?")


def _add(db: Session, args: argparse.Namespace) -> str:
    pilot_user = access_list.add(db, get_gateway(), args.login, args.note)
    return f"Added {pilot_user.github_login} (GitHub user {pilot_user.github_user_id})."


def _remove(db: Session, args: argparse.Namespace) -> str:
    pilot_user = access_list.remove(db, args.login)
    return f"Removed {pilot_user.github_login} and ended their sessions."


def _delete_data(db: Session, args: argparse.Namespace) -> str:
    count = access_list.delete_data(db, args.login)
    repositories = "repository" if count == 1 else "repositories"
    return (
        f"Disconnected {count} {repositories} of {args.login}, deleted their stored GitHub "
        "tokens, and ended their sessions. Maintenance purges the data within 24 hours."
    )


def _list(db: Session, args: argparse.Namespace) -> str:
    lines = ["login\tgithub_user_id\tnote\tadded_at"]
    for pilot_user in access_list.list_users(db):
        added = pilot_user.added_at.astimezone(UTC).date().isoformat()
        lines.append(
            f"{pilot_user.github_login}\t{pilot_user.github_user_id}\t{pilot_user.note or ''}\t"
            f"{added}"
        )
    return "\n".join(lines)


def _read_json(path: str, name: str) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise backup.BackupError(f"cannot read the {name} {path} ({type(exc).__name__})") from exc


def _compare_with_manifest(connection: Connection, manifest: object) -> str:
    """Check the database's revision and row counts against a backup's manifest.

    Raises `BackupError` listing every difference; returns what to print when there is none.
    """
    counts = dump.database_counts(connection)
    revision = counts["alembic_revision"]
    differences = backup.compare(manifest, counts["row_counts"], revision or "none")
    if differences:
        raise backup.BackupError(
            "the restored database does not match the backup:\n"
            + "\n".join(f"  {difference}" for difference in differences)
        )
    return (
        f"The restored database matches the backup: Alembic revision {revision}, "
        f"{len(counts['row_counts'])} tables."
    )


def _verify_restore(db: Session, args: argparse.Namespace) -> str:
    manifest = _read_json(args.manifest, "manifest")
    # Every count and the revision from one snapshot, even if something writes meanwhile.
    return _compare_with_manifest(
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"}), manifest
    )


def _failed(action: str, exc: Exception) -> int:
    # The first line only: SQLAlchemy adds the statement and its parameters, which may hold data,
    # on the lines after it.
    cause = str(exc).strip().split("\n", 1)[0]
    print(f"error: {action} failed ({type(exc).__name__}: {cause})", file=sys.stderr)
    return 1


def _migrate() -> int:
    try:
        decision = release.run_migrations()
    except Exception as exc:
        return _failed("the migration", exc)
    print(MIGRATE_OUTPUT[decision])
    return 0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _database() -> Engine:
    """An engine of its own on `DATABASE_URL`, without a pool, since a restore ends sessions."""
    return create_engine(get_settings().database_url, poolclass=NullPool)


def _prune(store: storage.ObjectStore, today: date) -> None:
    """Delete the backups dated before the retention from the bucket, then their rows.

    The rows go only if the database answers; any row whose key has expired goes, so rows left
    by a run without the database go in a later one.
    """
    retention_days = get_settings().backup_retention_days
    expired = backup.expired_keys(store.list(backup.BACKUPS_PREFIX), today, retention_days)
    if expired:
        store.delete(expired)
    engine = _database()
    try:
        with Session(engine) as db, db.begin():
            keys = backup.expired_keys(db.scalars(select(Backup.object_key)), today, retention_days)
            if keys:
                db.execute(delete(Backup).where(Backup.object_key.in_(keys)))
    except SQLAlchemyError:
        logger.warning("backup_rows_not_pruned")
    finally:
        engine.dispose()
    logger.info("backup_pruned", extra={"fields": {"pruned": len(expired)}})


def _store_backup(
    store: storage.ObjectStore, started_at: datetime, clock: Callable[[], datetime]
) -> tuple[str, int]:
    """Dump, check, and upload one backup under a new prefix, then record its row.

    Returns the dump's key and size. The manifest is uploaded after the dump, and the row
    inserted after both, so a manifest marks a complete backup and a row a recorded one.
    """
    settings = get_settings()
    prefix = backup.backup_prefix(started_at)
    if store.list(prefix):
        raise backup.BackupError(f"{prefix} already holds objects; this run stored nothing")
    dump_key = prefix + backup.DUMP_NAME
    with tempfile.TemporaryDirectory(prefix="codeatlas-backup-") as directory:
        dump_path = Path(directory) / backup.DUMP_NAME
        counts = dump.snapshot_dump(dump_path)
        dump.check_dump(dump_path)
        size = dump_path.stat().st_size
        sha256 = dump.sha256_of(dump_path)
        manifest = backup.build_manifest(
            counts,
            release=settings.release,
            dump_key=dump_key,
            bytes_=size,
            sha256=sha256,
            created_at=started_at,
        )
        manifest_path = Path(directory) / backup.MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        store.put(dump_key, dump_path)
        store.put(prefix + backup.MANIFEST_NAME, manifest_path)
    engine = _database()
    try:
        with Session(engine) as db, db.begin():
            db.add(
                Backup(
                    object_key=dump_key,
                    completed_at=clock(),
                    bytes=size,
                    sha256=sha256,
                    alembic_revision=manifest["alembic_revision"],
                    release=settings.release,
                )
            )
    finally:
        engine.dispose()
    logger.info("backup_completed", extra={"fields": {"object_key": dump_key, "bytes": size}})
    return dump_key, size


def run_backup(store: storage.ObjectStore, *, clock: Callable[[], datetime] = _utc_now) -> int:
    """`ops backup` (research R12): prune, then store one backup and record it in `backups`.

    Pruning runs first, whatever happens next. Any failure after it exits 1 with no row.
    """
    started_at = clock()
    try:
        _prune(store, started_at.astimezone(UTC).date())
    except Exception as exc:
        return _failed("pruning the old backups", exc)
    try:
        dump_key, size = _store_backup(store, started_at, clock)
    except backup.BackupError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    except Exception as exc:
        return _failed("the backup", exc)
    print(f"Stored {dump_key} ({size} bytes) and its manifest.")
    return 0


def _find_backup(store: storage.ObjectStore, name: str) -> str:
    """The prefix of the complete backup that `name` chooses: a UTC date, or a date and time.

    A date chooses the newest complete backup of that day.
    """
    match = BACKUP_NAME.fullmatch(name)
    try:
        day = date.fromisoformat(match.group(1)) if match else None
    except ValueError:
        day = None
    if match is None or day is None:
        raise backup.BackupError(
            f"{name} is not a backup: give its UTC date, such as 2026-10-13, or its UTC date and "
            "time, such as 2026-10-13/033012"
        )
    time = match.group(2)
    prefix = f"{backup.BACKUPS_PREFIX}{day.isoformat()}/{f'{time}/' if time else ''}"
    found = backup.newest_complete(store.list(prefix), day)
    if found is None:
        raise backup.BackupError(f"no complete backup (a dump and its manifest) for {name}")
    return found


def _manifest_sha256(manifest: Any) -> str:
    entry = manifest.get("dump") if isinstance(manifest, dict) else None
    sha256 = entry.get("sha256") if isinstance(entry, dict) else None
    if not isinstance(sha256, str):
        raise backup.BackupError("the manifest needs dump.sha256, the dump's SHA-256 checksum")
    return sha256


def _confirmed(prefix: str) -> bool:
    url = make_url(get_settings().database_url)
    try:
        answer = input(
            f"This replaces every table of the database {url.database} on {url.host} with the "
            f"backup {prefix}. Type yes to continue: "
        )
    except EOFError:
        return False
    return answer.strip() == "yes"


def run_restore(store: storage.ObjectStore, name: str, *, yes: bool = False) -> int:
    """`ops restore` (research R12): replace the database with a backup, verify it, and migrate.

    Nothing changes until the dump matches its manifest's SHA-256 and, unless `yes`, the
    developer confirms. A difference from the manifest stops the restore before the migration.
    """
    try:
        prefix = _find_backup(store, name)
        with tempfile.TemporaryDirectory(prefix="codeatlas-restore-") as directory:
            manifest_path = Path(directory) / backup.MANIFEST_NAME
            dump_path = Path(directory) / backup.DUMP_NAME
            store.get(prefix + backup.MANIFEST_NAME, manifest_path)
            manifest = _read_json(str(manifest_path), "manifest")
            store.get(prefix + backup.DUMP_NAME, dump_path)
            if dump.sha256_of(dump_path) != _manifest_sha256(manifest):
                raise backup.BackupError(
                    f"the dump of {prefix} does not match the SHA-256 in its manifest; nothing "
                    "was changed"
                )
            if not yes and not _confirmed(prefix):
                raise backup.BackupError("the restore was cancelled; nothing was changed")
            dump.restore_into_empty_schema(dump_path)
        engine = _database()
        try:
            with engine.connect() as connection:
                # Every count and the revision from one snapshot, as `verify-restore` reads them.
                connection.execution_options(isolation_level="REPEATABLE READ")
                verified = _compare_with_manifest(connection, manifest)
        finally:
            engine.dispose()
    except backup.BackupError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    except Exception as exc:
        return _failed("the restore", exc)
    try:
        decision = release.run_migrations()
    except Exception as exc:
        return _failed("the migration after the restore", exc)
    print(f"Restored {prefix}.")
    print(verified)
    print(MIGRATE_OUTPUT[decision])
    return 0


def _bucket() -> storage.ObjectStore | None:
    try:
        return storage.S3Store.from_settings()
    except storage.StorageError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return None


def _backup() -> int:
    store = _bucket()
    return 1 if store is None else run_backup(store)


def _restore(args: argparse.Namespace) -> int:
    store = _bucket()
    return 1 if store is None else run_restore(store, args.backup, yes=args.yes)


def _wait_for_api() -> int:
    settings = get_settings()
    commit = settings.platform_commit
    if not release.wait_for_api(settings.api_origin, commit):
        minutes = release.GATE_TIMEOUT_SECONDS // 60
        print(
            f"error: the API at {settings.api_origin} did not report commit {commit} "
            f"within {minutes} minutes.",
            file=sys.stderr,
        )
        return 1
    if commit:
        print(f"The API reports commit {commit}.")
    else:
        print("No commit from the platform: the release gate passes without waiting.")
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m codeatlas.ops", description="CodeAtlas operator commands."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    pilot_users = commands.add_parser("pilot-users", help="Keep the pilot's access list.")
    actions = pilot_users.add_subparsers(dest="action", required=True)

    add = actions.add_parser("add", help="Invite a GitHub user to the pilot.")
    add.add_argument("login", help="The GitHub login.")
    add.add_argument(
        "--note",
        metavar="TEXT",
        help=f"Free text, such as who invited the user (at most {access_list.MAX_NOTE_CHARS} "
        "characters).",
    )
    add.set_defaults(run=_add)

    remove = actions.add_parser(
        "remove", help="Take a user off the list and end their sessions at once."
    )
    remove.add_argument("login", help="The GitHub login.")
    remove.set_defaults(run=_remove)

    delete_data = actions.add_parser(
        "delete-data",
        help="For a user already removed: disconnect their repositories, delete their stored "
        "GitHub tokens, and end their sessions.",
    )
    delete_data.add_argument("login", help="The GitHub login.")
    delete_data.set_defaults(run=_delete_data)

    list_users = actions.add_parser("list", help="Print the access list.")
    list_users.set_defaults(run=_list)

    commands.add_parser(
        "backup",
        help="Prune the backups past the retention, then store one backup of the database in "
        "the bucket (the backup service's nightly command).",
    )
    restore = commands.add_parser(
        "restore",
        help="Replace the database with a backup, verify it against its manifest, and migrate "
        "it to this release's revision.",
    )
    restore.add_argument(
        "backup",
        metavar="BACKUP",
        help="A UTC date, such as 2026-10-13, for that day's newest complete backup, or a UTC "
        "date and time, such as 2026-10-13/033012.",
    )
    restore.add_argument("--yes", action="store_true", help="Restore without asking.")

    verify_restore = commands.add_parser(
        "verify-restore",
        help="Compare the restored database's Alembic revision and row counts with a backup's "
        "manifest.",
    )
    verify_restore.add_argument("manifest", metavar="MANIFEST", help="The manifest file.")
    verify_restore.set_defaults(run=_verify_restore)

    commands.add_parser(
        "migrate",
        help="Migrate the database to this release's revision, unless the database is ahead "
        "(api's pre-deploy command).",
    )
    commands.add_parser(
        "wait-for-api",
        help="Wait until the API reports this release's commit (the release gate of worker and "
        "backup).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    # The release steps open their own connections and need no session (research R7), and so do
    # the backup and the restore, which name only `DATABASE_URL` (research R12).
    if args.command == "migrate":
        return _migrate()
    if args.command == "wait-for-api":
        return _wait_for_api()
    if args.command == "backup":
        return _backup()
    if args.command == "restore":
        return _restore(args)
    run: Command = args.run
    db = new_session()
    try:
        output = run(db, args)
        db.commit()
    except access_list.AccessListError as exc:
        db.rollback()
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    except GitHubError as exc:
        db.rollback()
        print(
            f"error: the GitHub lookup failed ({type(exc).__name__}); try again.", file=sys.stderr
        )
        return 1
    except backup.BackupError as exc:
        db.rollback()
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    finally:
        db.close()
    print(output)
    return 0


if __name__ == "__main__":
    # Only here, so tests that call `main` keep their log capture (research R18).
    configure_logging()
    sys.exit(main())
