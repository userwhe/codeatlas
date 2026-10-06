"""Route: inbound GitHub webhook deliveries (contracts/github-webhooks.md, research R2).

GitHub calls this endpoint, browsers never do. The HMAC signature replaces the session cookie
and the Origin check. Request bodies are never logged, and delivery rows keep only safe fields.
"""

import json
from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from codeatlas.api.deps import DbSession, RequestId
from codeatlas.api.errors import ApiError
from codeatlas.config import get_settings
from codeatlas.db import session_scope
from codeatlas.github import webhooks
from codeatlas.models import WebhookDelivery
from codeatlas.workspace import sync
from codeatlas.workspace.audit import record

router = APIRouter(tags=["webhooks"])

# The event header of an unverified request is attacker-controlled; keep the audited copy short.
MAX_AUDITED_EVENT_LENGTH = 100

DeliveryOutcome = Literal["processed", "ignored", "duplicate"]


class DeliveryOut(BaseModel):
    outcome: DeliveryOutcome


@router.post("/webhooks/github", status_code=202, include_in_schema=False)
async def receive_github_delivery(
    request: Request, db: DbSession, request_id: RequestId
) -> DeliveryOut:
    body = await _read_body(request)
    # Hashing up to 25 MiB and the database work run off the event loop.
    outcome = await run_in_threadpool(
        _handle_delivery,
        db,
        body=body,
        signature=request.headers.get("x-hub-signature-256"),
        event=request.headers.get("x-github-event"),
        delivery_id=request.headers.get("x-github-delivery"),
        request_id=request_id,
    )
    return DeliveryOut(outcome=outcome)


async def _read_body(request: Request) -> bytes:
    """Step 1: read the raw body, stopping as soon as it is over the limit."""
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared = 0
    if declared > webhooks.MAX_BODY_BYTES:
        raise _payload_too_large()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > webhooks.MAX_BODY_BYTES:
            raise _payload_too_large()
    return bytes(body)


def _handle_delivery(
    db: Session,
    *,
    body: bytes,
    signature: str | None,
    event: str | None,
    delivery_id: str | None,
    request_id: str | None,
) -> DeliveryOutcome:
    """Steps 2 to 5 of the contract's processing order."""
    if not webhooks.verify_signature(get_settings().github_webhook_secret, body, signature):
        _record_rejection(
            event, "signature_missing" if not signature else "signature_mismatch", request_id
        )
        raise ApiError(401, "invalid_signature", "The webhook signature is missing or wrong.")

    if not delivery_id or not event:
        raise _invalid_delivery()
    payload = _json_object(body)
    try:
        parsed = webhooks.parse(event, payload)
    except webhooks.InvalidDelivery:
        raise _invalid_delivery() from None

    # Claiming the delivery ID first makes a concurrent redelivery wait for this transaction,
    # then find the row and report a duplicate.
    safe_detail = _safe_detail(event, payload)
    claimed = db.execute(
        insert(WebhookDelivery)
        .values(
            delivery_id=delivery_id,
            event=event,
            action=_str_or_none(payload.get("action")),
            github_installation_id=_id_of(payload.get("installation")),
            github_repository_id=_id_of(payload.get("repository")),
            outcome="ignored",
            detail=safe_detail,
        )
        .on_conflict_do_nothing(index_elements=[WebhookDelivery.delivery_id])
        .returning(WebhookDelivery.delivery_id)
    ).scalar_one_or_none()
    if claimed is None:
        return "duplicate"

    result = sync.apply_event(db, parsed)
    db.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.delivery_id == delivery_id)
        .values(outcome=result.outcome, detail={**safe_detail, **result.detail})
    )
    db.commit()
    return result.outcome


def _record_rejection(event: str | None, reason: str, request_id: str | None) -> None:
    """Audit an unverified request in its own transaction, since the request then fails."""
    with session_scope() as audit_db:
        record(
            audit_db,
            action="webhook_rejected",
            outcome="denied",
            resource_type="webhook",
            request_id=request_id,
            detail={
                "event": event[:MAX_AUDITED_EVENT_LENGTH] if event else None,
                "reason": reason,
            },
        )


def _json_object(body: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        raise _invalid_delivery() from None
    if not isinstance(payload, dict):
        raise _invalid_delivery()
    return payload


def _safe_detail(event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The payload fields a delivery row may keep: a push's `ref` and `after`, nothing else."""
    if event != "push":
        return {}
    return {key: payload[key] for key in ("ref", "after") if isinstance(payload.get(key), str)}


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _id_of(value: object) -> int | None:
    """`value["id"]` when `value` is an object with an integer ID."""
    if not isinstance(value, Mapping):
        return None
    github_id = value.get("id")
    if not isinstance(github_id, int) or isinstance(github_id, bool):
        return None
    return github_id


def _invalid_delivery() -> ApiError:
    return ApiError(
        400, "invalid_delivery", "The delivery lacks a required header or is not valid JSON."
    )


def _payload_too_large() -> ApiError:
    return ApiError(413, "payload_too_large", "The delivery body is over 25 MiB.")
