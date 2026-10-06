"""FastAPI dependencies: database session, signed-in user, and workspace resolution."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.api.errors import unauthenticated
from codeatlas.auth.sessions import COOKIE_NAME, get_valid_session
from codeatlas.config import Settings, get_settings
from codeatlas.db import new_session
from codeatlas.models import Membership, User, Workspace


def get_db() -> Iterator[Session]:
    """One session per request. Routes commit explicitly; uncommitted work is rolled back."""
    session = new_session()
    try:
        yield session
    finally:
        session.close()


def get_settings_dep() -> Settings:
    return get_settings()


DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings_dep)]


def get_request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


RequestId = Annotated[str | None, Depends(get_request_id)]


def current_user(request: Request, db: DbSession) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise unauthenticated()
    session = get_valid_session(db, token)
    if session is None:
        raise unauthenticated()
    user = db.get(User, session.user_id)
    if user is None:
        raise unauthenticated()
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def current_workspace(db: DbSession, user: CurrentUser) -> Workspace:
    workspace = db.scalar(
        select(Workspace)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(Membership.user_id == user.id, Membership.role == "owner")
    )
    if workspace is None:
        raise unauthenticated()
    return workspace


CurrentWorkspace = Annotated[Workspace, Depends(current_workspace)]
