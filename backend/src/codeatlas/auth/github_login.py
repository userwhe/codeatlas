"""Sign-in with the GitHub App's user authorization flow (research R5, FR-001, FR-002)."""

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from codeatlas.auth.crypto import DecryptionError, decrypt, encrypt
from codeatlas.auth.sessions import create_session
from codeatlas.db import session_scope
from codeatlas.github.gateway import GitHubAccessDenied, GitHubGateway, UserTokens
from codeatlas.models import GitHubCredential, Membership, User, UserSession, Workspace
from codeatlas.workspace.audit import record

STATE_COOKIE = "oauth_state"
STATE_TTL = timedelta(minutes=10)
TOKEN_REFRESH_MARGIN = timedelta(minutes=1)


class SignInError(Exception):
    """Sign-in could not be completed; the user is sent back to the sign-in page."""


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
    """Finish sign-in and return a new session token. The caller commits."""
    _check_state(state, state_cookie)
    tokens = gateway.exchange_code(code)
    github_user = gateway.get_authenticated_user(tokens.access_token)

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
    return token


def _forget_credentials(user_id: uuid.UUID) -> None:
    """Drop unreadable GitHub credentials and end the user's sessions, so they sign in again.

    Runs in its own transaction so it holds even though the request then fails.
    """
    with session_scope() as forget_db:
        forget_db.execute(delete(GitHubCredential).where(GitHubCredential.user_id == user_id))
        forget_db.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )


def get_user_token(db: Session, user: User, gateway: GitHubGateway) -> str:
    """Return the user's GitHub token, refreshing it when it is about to expire.

    Raises `GitHubAccessDenied` when there is no usable credential. A credential that can no
    longer be decrypted (for example after `TOKEN_ENCRYPTION_KEY` changed) is removed and the
    user's sessions are revoked, so the next request leads them back to sign-in.
    """
    credential = db.get(GitHubCredential, user.id)
    if credential is None:
        raise GitHubAccessDenied("no stored GitHub credentials")
    try:
        expires_at = credential.access_token_expires_at
        if expires_at is None or expires_at > datetime.now(UTC) + TOKEN_REFRESH_MARGIN:
            return decrypt(credential.access_token_enc)
        if credential.refresh_token_enc is None:
            raise GitHubAccessDenied("the GitHub token expired and cannot be refreshed")
        refresh_token = decrypt(credential.refresh_token_enc)
    except DecryptionError as exc:
        db.rollback()
        _forget_credentials(user.id)
        raise GitHubAccessDenied("stored GitHub credentials cannot be read") from exc
    tokens = gateway.refresh_user_token(refresh_token)
    _store_tokens(db, user.id, tokens)
    db.flush()
    return tokens.access_token
