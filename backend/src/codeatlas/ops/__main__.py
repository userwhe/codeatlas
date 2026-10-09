"""Operator commands (specs/004-pilot-deployment, FR-004, contracts/operations.md).

The developer runs them on the host through Session Manager:

    codeatlas-compose run --rm api python -m codeatlas.ops pilot-users add <login> [--note TEXT]
    codeatlas-compose run --rm api python -m codeatlas.ops pilot-users remove <login>
    codeatlas-compose run --rm api python -m codeatlas.ops pilot-users delete-data <login>
    codeatlas-compose run --rm api python -m codeatlas.ops pilot-users list

The backup scripts run the backup commands with the backup directory mounted at `/backup`
(`codeatlas-compose run --rm -v /var/lib/codeatlas/backup:/backup:ro api ...`, research R12):

    python -m codeatlas.ops backup-manifest --counts FILE --dump-key KEY --bytes N --sha256 HEX
    python -m codeatlas.ops verify-restore MANIFEST

Each command runs in one transaction, committed only when it succeeds. It exits 0 on success, and
1 on a refusal, which it explains on standard error: in one line, or, when `verify-restore` finds
differences, with one more line for each.
"""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, table, text
from sqlalchemy.orm import Session

from codeatlas.auth import access_list
from codeatlas.config import get_settings
from codeatlas.db import new_session
from codeatlas.github.gateway import GitHubError, get_gateway
from codeatlas.logging import configure_logging
from codeatlas.ops import backup

Command = Callable[[Session, argparse.Namespace], str]
"""Runs one command in the session and returns what to print on success."""


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


def _backup_manifest(db: Session, args: argparse.Namespace) -> str:
    # Needs no database: the counts come from the dump's own transaction (research R12).
    manifest = backup.build_manifest(
        _read_json(args.counts, "counts file"),
        release=get_settings().release,
        dump_key=args.dump_key,
        bytes_=args.bytes,
        sha256=args.sha256,
        created_at=datetime.now(UTC),
    )
    return json.dumps(manifest, indent=2)


def _verify_restore(db: Session, args: argparse.Namespace) -> str:
    manifest = _read_json(args.manifest, "manifest")
    # Every count and the revision from one snapshot, even if something writes meanwhile.
    db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    tables = db.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).all()
    row_counts = {
        name: db.scalar(select(func.count()).select_from(table(name, schema="public"))) or 0
        for name in tables
    }
    revision = None
    if "alembic_version" in row_counts:
        revision = db.scalar(text("SELECT version_num FROM public.alembic_version"))
    differences = backup.compare(manifest, row_counts, revision or "none")
    if differences:
        raise backup.BackupError(
            "the restored database does not match the backup:\n"
            + "\n".join(f"  {difference}" for difference in differences)
        )
    return (
        f"The restored database matches the backup: Alembic revision {revision}, "
        f"{len(row_counts)} tables."
    )


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

    backup_manifest = commands.add_parser(
        "backup-manifest", help="Print a backup's manifest, built from its counts file."
    )
    backup_manifest.add_argument(
        "--counts",
        required=True,
        metavar="FILE",
        help="The Alembic revision and every table's row count, from the dump's transaction.",
    )
    backup_manifest.add_argument(
        "--dump-key", required=True, metavar="KEY", help="The dump's key in the bucket."
    )
    backup_manifest.add_argument(
        "--bytes", required=True, type=int, metavar="N", help="The dump's size in bytes."
    )
    backup_manifest.add_argument(
        "--sha256", required=True, metavar="HEX", help="The dump's SHA-256 checksum."
    )
    backup_manifest.set_defaults(run=_backup_manifest)

    verify_restore = commands.add_parser(
        "verify-restore",
        help="Compare the restored database's Alembic revision and row counts with a backup's "
        "manifest.",
    )
    verify_restore.add_argument("manifest", metavar="MANIFEST", help="The manifest file.")
    verify_restore.set_defaults(run=_verify_restore)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
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
