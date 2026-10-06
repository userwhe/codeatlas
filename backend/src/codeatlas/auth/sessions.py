"""Server-side sessions stored as SHA-256 hashes of a random cookie token (research R6)."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.config import get_settings
from codeatlas.models import UserSession

COOKIE_NAME = "codeatlas_session"


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def create_session(db: Session, user_id: uuid.UUID) -> str:
    """Create a session and return the cookie token (256 random bits)."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    db.add(
        UserSession(
            token_hash=_hash(token),
            user_id=user_id,
            created_at=now,
            expires_at=now + get_settings().session_ttl,
        )
    )
    return token


def get_valid_session(db: Session, token: str) -> UserSession | None:
    """Return the session when `revoked_at IS NULL AND expires_at > now()`."""
    return db.scalar(
        select(UserSession).where(
            UserSession.token_hash == _hash(token),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > datetime.now(UTC),
        )
    )


def revoke_session(db: Session, token: str) -> None:
    session = db.get(UserSession, _hash(token))
    if session is not None and session.revoked_at is None:
        session.revoked_at = datetime.now(UTC)


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(get_settings().session_ttl.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, httponly=True, secure=True, samesite="lax", path="/")
