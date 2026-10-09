"""Sign-in with the GitHub App's user authorization flow (research R5, FR-001, FR-002)."""

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from codeatlas.auth.crypto import DecryptionError, decrypt, encrypt
from codeatlas.auth.sessions import create_session
from codeatlas.config import get_settings
from codeatlas.db import session_scope
from codeatlas.github.gateway import GitHubGateway, UserAuthorizationInvalid, UserTokens
from codeatlas.jobs.queue import request_automatic_run
from codeatlas.models import (
    GitHubCredential,
    Membership,
    PilotUser,
    Repository,
    User,
    UserSession,
    Workspace,
)
from codeatlas.workspace.access import resume
from codeatlas.workspace.audit import record

STATE_COOKIE = "oauth_state"
STATE_TTL = timedelta(minutes=10)
TOKEN_REFRESH_MARGIN = timedelta(minutes=1)


class SignInError(Exception):
    """Sign-in could not be completed; the user is sent back to the sign-in page."""


class NotInvited(SignInError):
    """The GitHub user is not on the pilot's access list (specs/004-pilot-deployment, FR-003)."""

    def __init__(self, github_login: str) -> None:
        super().__init__("the GitHub user is not on the access list")
        self.github_login = github_login


@dataclass(frozen=True)
class LoginStart:
    redirect_url: str
    state_cookie: str


def start_login(gateway: GitHubGateway) -> LoginStart:
    state = secrets.token_urlsafe(32)
    return LoginStart(
        redirect_url=gateway.authorize_url(state),
        state_cookie=encrypt(state).decode(),
    )


def _check_state(state: str, state_cookie: str | None) -> None:
    if not state or not state_cookie:
        raise SignInError("missing OAuth state")
    expected = decrypt(state_cookie.encode(), max_age_seconds=int(STATE_TTL.total_seconds()))
    if not secrets.compare_digest(expected, state):
        raise SignInError("OAuth state mismatch")


def _store_tokens(db: Session, user_id: uuid.UUID, tokens: UserTokens) -> None:
    credential = db.get(GitHubCredential, user_id)
    if credential is None:
        credential = GitHubCredential(user_id=user_id)
        db.add(credential)
    credential.access_token_enc = encrypt(tokens.access_token)
    credential.access_token_expires_at = tokens.access_token_expires_at
    credential.refresh_token_enc = (
        encrypt(tokens.refresh_token) if tokens.refresh_token is not None else None
    )
    credential.refresh_token_expires_at = tokens.refresh_token_expires_at


def complete_login(
    db: Session,
    gateway: GitHubGateway,
    *,
    code: str,
    state: str,
    state_cookie: str | None,
    request_id: str | None,
) -> str:
    """Finish sign-in and return a new session token. The caller commits.

    Signing in again also resumes the automatic updates that paused because sign-in was required.
    When the access list applies, a user not on it gets `NotInvited` before any row is written,
    and the token GitHub issued is discarded (research R13).
    """
    _check_state(state, state_cookie)
    tokens = gateway.exchange_code(code)
    github_user = gateway.get_authenticated_user(tokens.access_token)
    if get_settings().access_list_enforced:
        # Imported here because access_list imports this module.
        from codeatlas.auth import access_list

        if not access_list.is_allowed(db, github_user.id):
            raise NotInvited(github_user.login)
        # Keep the listed login current, so a rename stays visible to the developer.
        db.execute(
            update(PilotUser)
            .where(
                PilotUser.github_user_id == github_user.id,
                PilotUser.github_login != github_user.login,
            )
            .values(github_login=github_user.login)
        )

    now = datetime.now(UTC)
    user = db.scalar(select(User).where(User.github_user_id == github_user.id))
    if user is None:
        user = User(github_user_id=github_user.id, github_login=github_user.login)
        db.add(user)
    user.github_login = github_user.login
    user.name = github_user.name
    user.avatar_url = github_user.avatar_url
    user.last_sign_in_at = now
    db.flush()

    workspace_id = db.scalar(
        select(Membership.workspace_id).where(
            Membership.user_id == user.id, Membership.role == "owner"
        )
    )
    if workspace_id is None:
        workspace = Workspace(name=github_user.login)
        db.add(workspace)
        db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        workspace_id = workspace.id

    _store_tokens(db, user.id, tokens)
    token = create_session(db, user.id)
    record(
        db,
        action="sign_in",
        outcome="success",
        workspace_id=workspace_id,
        actor_user_id=user.id,
        resource_type="user",
        resource_id=str(user.id),
        request_id=request_id,
    )
    _resume_after_sign_in(db, user.id, workspace_id)
    return token


def _resume_after_sign_in(db: Session, user_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """Resume the workspace's repositories paused until the owner signs in again, and queue a
    `check` run for each, so access is checked at once (FR-016, research R8).

    A pause for the external processing disclosure stays: only accepting it lifts that pause.
    Rows are locked in ID order, as change notifications lock them.
    """
    repositories = db.scalars(
        select(Repository)
        .where(
            Repository.workspace_id == workspace_id,
            Repository.deleted_at.is_(None),
            Repository.access_state == "paused",
            Repository.access_reason == "sign_in_required",
        )
        .order_by(Repository.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    for repository in repositories:
        if resume(db, repository, via="sign_in", actor_user_id=user_id):
            request_automatic_run(
                db, repository=repository, branch=repository.default_branch, trigger="check"
            )


def forget_github_credential(db: Session, user_id: uuid.UUID) -> None:
    """Delete the user's stored GitHub credential once it no longer works (research R8).

    Sessions stay valid: browsing needs no GitHub call, and the owner must still see why
    automatic updates paused. Does not commit.
    """
    db.execute(delete(GitHubCredential).where(GitHubCredential.user_id == user_id))


def _forget_credentials(user_id: uuid.UUID) -> None:
    """Drop unreadable GitHub credentials and end the user's sessions, so they sign in again.

    Runs in its own transaction so it holds even though the request then fails.
    """
    with session_scope() as forget_db:
        forget_github_credential(forget_db, user_id)
        forget_db.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )


def get_user_token(db: Session, user: User, gateway: GitHubGateway) -> str:
    """Return the user's GitHub token, refreshing it when it is about to expire.

    A refreshed credential is stored in its own transaction, so it is kept even when the caller
    then fails and rolls back: GitHub replaces the refresh token on every refresh. The caller's
    session is refreshed to match it.

    Raises `UserAuthorizationInvalid` when there is no usable credential or GitHub refuses the
    refresh. A credential that can no longer be decrypted (for example after
    `TOKEN_ENCRYPTION_KEY` changed) is removed and the user's sessions are revoked, so the next
    request leads them back to sign-in.
    """
    credential = db.get(GitHubCredential, user.id)
    if credential is None:
        raise UserAuthorizationInvalid("no stored GitHub credentials")
    try:
        expires_at = credential.access_token_expires_at
        if expires_at is None or expires_at > datetime.now(UTC) + TOKEN_REFRESH_MARGIN:
            return decrypt(credential.access_token_enc)
        if credential.refresh_token_enc is None:
            raise UserAuthorizationInvalid("the GitHub token expired and cannot be refreshed")
        refresh_token = decrypt(credential.refresh_token_enc)
    except DecryptionError as exc:
        db.rollback()
        _forget_credentials(user.id)
        raise UserAuthorizationInvalid("stored GitHub credentials cannot be read") from exc
    tokens = gateway.refresh_user_token(refresh_token)
    with session_scope() as refresh_db:
        _store_tokens(refresh_db, user.id, tokens)
    db.refresh(credential)
    return tokens.access_token
