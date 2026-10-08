"""Operator commands (specs/004-pilot-deployment, FR-004, contracts/operations.md).

The developer runs them on the host through Session Manager:

    codeatlas-compose run --rm api python -m codeatlas.ops pilot-users add <login> [--note TEXT]
    codeatlas-compose run --rm api python -m codeatlas.ops pilot-users remove <login>
    codeatlas-compose run --rm api python -m codeatlas.ops pilot-users delete-data <login>
    codeatlas-compose run --rm api python -m codeatlas.ops pilot-users list

Each command runs in one transaction, committed only when it succeeds. It exits 0 on success, and
1 on a refusal, which it explains in one line on standard error.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC

from sqlalchemy.orm import Session

from codeatlas.auth import access_list
from codeatlas.db import new_session
from codeatlas.github.gateway import GitHubError, get_gateway
from codeatlas.logging import configure_logging

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
    finally:
        db.close()
    print(output)
    return 0


if __name__ == "__main__":
    # Only here, so tests that call `main` keep their log capture (research R18).
    configure_logging()
    sys.exit(main())
