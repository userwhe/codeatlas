"""Audit events (FR-033). Details must never contain secrets or source text."""

import uuid
from typing import Any

from sqlalchemy.orm import Session

from codeatlas.api.errors import ApiError, not_found
from codeatlas.db import session_scope
from codeatlas.models import AuditEvent


def record(
    db: Session,
    *,
    action: str,
    outcome: str,
    workspace_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    request_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Add an audit event to the caller's transaction."""
    db.add(
        AuditEvent(
            action=action,
            outcome=outcome,
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=request_id,
            detail=detail or {},
        )
    )


def deny(
    *,
    actor_user_id: uuid.UUID | None,
    workspace_id: uuid.UUID | None,
    resource_type: str,
    resource_id: str | None,
    request_id: str | None = None,
) -> ApiError:
    """Record an `access_denied` event and return the 404 error to raise (FR-004, FR-033).

    The event is written in its own transaction so it survives the failed request.
    """
    with session_scope() as audit_db:
        record(
            audit_db,
            action="access_denied",
            outcome="denied",
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=request_id,
        )
    return not_found()
