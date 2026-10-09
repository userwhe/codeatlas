"""The pilot's access list (specs/004-pilot-deployment, FR-002 to FR-004, research R13).

Rows are keyed by GitHub's numeric user ID, because a login can be renamed and then claimed by
someone else. The developer changes the list with `python -m codeatlas.ops pilot-users`, and
sign-in checks it when `Settings.access_list_enforced`. Nothing here commits.
"""

from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from codeatlas.auth.github_login import forget_github_credential
from codeatlas.config import get_settings
from codeatlas.github.gateway import GitHubGateway, GitHubNotFound
from codeatlas.models import Membership, PilotUser, Repository, User, UserSession, Workspace
from codeatlas.workspace import repositories
from codeatlas.workspace.audit import record

MAX_NOTE_CHARS = 200


class AccessListError(Exception):
    """The access list refused a change; `message` tells the developer why."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def is_allowed(db: Session, github_user_id: int) -> bool:
    """Whether the GitHub user is on the list.

    A listed row stays locked (`FOR KEY SHARE`) until the caller's transaction ends, so a
    `remove` cannot commit between this check and the session a sign-in then creates: it waits,
    and then revokes that session too.
    """
    listed = db.scalar(
        select(PilotUser.github_user_id)
        .where(PilotUser.github_user_id == github_user_id)
        .with_for_update(read=True, key_share=True)
    )
    return listed is not None


def add(db: Session, gateway: GitHubGateway, login: str, note: str | None) -> PilotUser:
    """Resolve `login` on GitHub and add the user, unless the list is full (`pilot_user_limit`)."""
    if note is not None and len(note) > MAX_NOTE_CHARS:
        raise AccessListError(f"A note can have at most {MAX_NOTE_CHARS} characters.")
    try:
        github_user = gateway.get_user_by_login(login)
    except GitHubNotFound as exc:
        raise AccessListError(f"GitHub has no user named {login}.") from exc
    if db.get(PilotUser, github_user.id) is not None:
        raise AccessListError(f"{github_user.login} is already on the access list.")
    limit = get_settings().pilot_user_limit
    if (db.scalar(select(func.count()).select_from(PilotUser)) or 0) >= limit:
        raise AccessListError(f"The access list is full: it holds at most {limit} users.")
    pilot_user = PilotUser(github_user_id=github_user.id, github_login=github_user.login, note=note)
    db.add(pilot_user)
    _record(db, "pilot_user_add", pilot_user)
    return pilot_user


def remove(db: Session, login: str) -> PilotUser:
    """Take the user off the list and end their sessions, in the caller's transaction (FR-004)."""
    pilot_user = _listed(db, login)
    if pilot_user is None:
        raise AccessListError(f"{login} is not on the access list.")
    db.execute(delete(PilotUser).where(PilotUser.github_user_id == pilot_user.github_user_id))
    _revoke_sessions(db, pilot_user.github_user_id)
    _record(db, "pilot_user_remove", pilot_user)
    return pilot_user


def delete_data(db: Session, login: str) -> int:
    """For a user already removed from the list: disconnect every repository of their workspace
    (maintenance purges the data, 001 FR-035), delete their stored GitHub tokens, and end their
    sessions. Returns the number of repositories disconnected.

    The disconnections are audited as the user's own, as `DELETE /v1/repositories/{id}` would be
    (data-model.md); the `pilot_user_delete_data` event records that the developer started them.
    """
    # A login can pass to another account; the latest to sign in with it holds it now.
    user = db.scalars(
        select(User)
        .where(func.lower(User.github_login) == login.lower())
        .order_by(User.last_sign_in_at.desc())
    ).first()
    if user is None:
        raise AccessListError(f"CodeAtlas has no user named {login}.")
    if _listed(db, login) is not None or is_allowed(db, user.github_user_id):
        raise AccessListError(f"{login} is still on the access list; remove them first.")
    workspace = db.scalar(
        select(Workspace)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(Membership.user_id == user.id, Membership.role == "owner")
    )
    disconnected = 0
    if workspace is not None:
        repository_ids = db.scalars(
            select(Repository.id)
            .where(Repository.workspace_id == workspace.id, Repository.deleted_at.is_(None))
            .order_by(Repository.id)
        ).all()
        for repository_id in repository_ids:
            repositories.disconnect(
                db, user=user, workspace=workspace, repository_id=repository_id, request_id=None
            )
        disconnected = len(repository_ids)
    forget_github_credential(db, user.id)
    _revoke_sessions(db, user.github_user_id)
    record(
        db,
        action="pilot_user_delete_data",
        outcome="success",
        detail={"github_user_id": user.github_user_id, "github_login": user.github_login},
    )
    return disconnected


def list_users(db: Session) -> list[PilotUser]:
    return list(db.scalars(select(PilotUser).order_by(PilotUser.added_at, PilotUser.github_login)))


def _listed(db: Session, login: str) -> PilotUser | None:
    """The row for `login`, matched without regard to case, as GitHub matches logins."""
    return db.scalars(
        select(PilotUser)
        .where(func.lower(PilotUser.github_login) == login.lower())
        .order_by(PilotUser.added_at)
    ).first()


def _revoke_sessions(db: Session, github_user_id: int) -> None:
    db.execute(
        update(UserSession)
        .where(
            UserSession.user_id.in_(select(User.id).where(User.github_user_id == github_user_id)),
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )


def _record(db: Session, action: str, pilot_user: PilotUser) -> None:
    # The developer acts through the host, not a session, so there is no actor (data-model.md).
    record(
        db,
        action=action,
        outcome="success",
        detail={
            "github_user_id": pilot_user.github_user_id,
            "github_login": pilot_user.github_login,
        },
    )
