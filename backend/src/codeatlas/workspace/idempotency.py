"""Idempotent POST handling with the `Idempotency-Key` header (FR-029, contracts/http-api.md)."""

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from codeatlas.api.errors import ApiError
from codeatlas.models import IdempotencyRecord

RECORD_TTL = timedelta(hours=24)
MAX_KEY_LENGTH = 128

Operation = Callable[[], tuple[int, dict[str, Any]]]


def _payload_hash(payload: dict[str, Any]) -> bytes:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).digest()


def run_idempotent(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    route: str,
    key: str | None,
    payload: dict[str, Any],
    operation: Operation,
) -> tuple[int, dict[str, Any]]:
    """Run `operation` once per (workspace, route, key) and replay its response afterwards.

    The operation returns `(status, json_body)`. The stored record joins the caller's transaction,
    so the caller commits both together. Without a key, the operation simply runs.
    """
    if key is None:
        return operation()
    if not 1 <= len(key) <= MAX_KEY_LENGTH:
        raise ApiError(
            422, "invalid_idempotency_key", "Idempotency-Key must be 1 to 128 characters."
        )

    # Serialize concurrent requests that share a key.
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"{workspace_id}:{route}:{key}"))))

    payload_sha256 = _payload_hash(payload)
    now = datetime.now(UTC)
    existing = db.get(IdempotencyRecord, (workspace_id, route, key))
    if existing is not None and existing.expires_at <= now:
        db.delete(existing)
        db.flush()
        existing = None
    if existing is not None:
        if existing.payload_sha256 != payload_sha256:
            raise ApiError(
                409,
                "idempotency_conflict",
                "This Idempotency-Key was already used with a different request body.",
            )
        return existing.response_status, existing.response_body

    status, body = operation()
    db.add(
        IdempotencyRecord(
            workspace_id=workspace_id,
            route=route,
            key=key,
            payload_sha256=payload_sha256,
            response_status=status,
            response_body=body,
            created_at=now,
            expires_at=now + RECORD_TTL,
        )
    )
    return status, body
