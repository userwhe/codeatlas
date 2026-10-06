"""The webhook endpoint skeleton (contracts/github-webhooks.md, research R2)."""

import json
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.config import Settings
from codeatlas.github.webhooks import MAX_BODY_BYTES
from codeatlas.models import AuditEvent, WebhookDelivery
from tests.webhooks import (
    AUTHOR_EMAIL,
    COMMIT_MESSAGE,
    DEFAULT_INSTALLATION_ID,
    delivery_headers,
    ping_payload,
    post_delivery,
    push_payload,
    send_delivery,
)

pytestmark = pytest.mark.integration

SHA = "b" * 40


def deliveries(db: Session) -> list[WebhookDelivery]:
    db.expire_all()
    return list(db.scalars(select(WebhookDelivery)))


def rejections(db: Session) -> list[AuditEvent]:
    return list(db.scalars(select(AuditEvent).where(AuditEvent.action == "webhook_rejected")))


def assert_error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert set(error) == {"code", "message", "retryable", "request_id", "details"}
    assert error["code"] == code


def test_ping_is_ignored_and_recorded(client: TestClient, db: Session) -> None:
    response = send_delivery(client, "ping", ping_payload(), delivery_id="delivery-1")

    assert response.status_code == 202, response.text
    assert response.json() == {"outcome": "ignored"}
    [row] = deliveries(db)
    assert row.delivery_id == "delivery-1"
    assert row.event == "ping"
    assert row.action is None
    assert row.outcome == "ignored"
    assert row.detail == {"reason": "event_not_handled"}
    assert row.received_at is not None


def test_redelivery_is_a_duplicate(client: TestClient, db: Session) -> None:
    first = send_delivery(client, "ping", ping_payload(), delivery_id="delivery-1")
    again = send_delivery(client, "ping", ping_payload(), delivery_id="delivery-1")

    assert first.json() == {"outcome": "ignored"}
    assert again.status_code == 202, again.text
    assert again.json() == {"outcome": "duplicate"}
    [row] = deliveries(db)
    assert row.outcome == "ignored"


def test_ignored_push_stores_only_safe_fields(client: TestClient, db: Session) -> None:
    payload = push_payload(2001, SHA, ref="refs/heads/feature")

    response = send_delivery(client, "push", payload, delivery_id="delivery-push")

    assert response.json() == {"outcome": "ignored"}
    row = db.get(WebhookDelivery, "delivery-push")
    assert row is not None
    assert row.event == "push"
    assert row.github_repository_id == 2001
    assert row.github_installation_id == DEFAULT_INSTALLATION_ID
    assert row.detail == {"ref": "refs/heads/feature", "after": SHA, "reason": "not_default_branch"}
    stored = json.dumps(row.detail)
    for unsafe in (COMMIT_MESSAGE, AUTHOR_EMAIL, "credentials.py", "README.md"):
        assert unsafe not in stored


def test_action_is_recorded(client: TestClient, db: Session) -> None:
    payload = {"action": "created", "installation": {"id": 5009}, "sender": {"id": 1001}}

    response = send_delivery(client, "installation", payload, delivery_id="delivery-created")

    assert response.json() == {"outcome": "ignored"}
    row = db.get(WebhookDelivery, "delivery-created")
    assert row is not None
    assert row.action == "created"
    assert row.github_installation_id == 5009
    assert row.github_repository_id is None
    assert row.detail == {"reason": "action_not_handled"}


@pytest.mark.parametrize(
    ("signature", "reason"),
    [(None, "signature_missing"), ("sha256=" + "0" * 64, "signature_mismatch")],
)
def test_bad_signature_is_rejected_and_audited(
    client: TestClient, db: Session, signature: str | None, reason: str
) -> None:
    body = json.dumps(push_payload(2001, SHA)).encode()
    headers = delivery_headers("push", body, delivery_id="forged")
    if signature is None:
        del headers["X-Hub-Signature-256"]
    else:
        headers["X-Hub-Signature-256"] = signature

    response = post_delivery(client, body, headers)

    assert_error(response, 401, "invalid_signature")
    [event] = rejections(db)
    assert event.workspace_id is None
    assert event.actor_user_id is None
    assert event.outcome == "denied"
    assert event.resource_type == "webhook"
    assert event.resource_id is None
    assert event.detail == {"event": "push", "reason": reason}
    assert event.request_id == response.headers["X-Request-ID"]
    assert deliveries(db) == []
    assert db.scalars(select(AuditEvent)).all() == [event]


def test_wrong_secret_is_rejected(client: TestClient, db: Session) -> None:
    response = send_delivery(client, "ping", ping_payload(), secret="not-the-secret")

    assert_error(response, 401, "invalid_signature")
    [event] = rejections(db)
    assert event.detail == {"event": "ping", "reason": "signature_mismatch"}


def test_rejection_without_event_header_records_null_event(client: TestClient, db: Session) -> None:
    body = b"{}"

    response = post_delivery(client, body, {"X-GitHub-Delivery": "forged"})

    assert_error(response, 401, "invalid_signature")
    [event] = rejections(db)
    assert event.detail == {"event": None, "reason": "signature_missing"}


def test_sha1_signature_alone_is_rejected(client: TestClient, db: Session) -> None:
    body = json.dumps(ping_payload()).encode()
    headers = delivery_headers("ping", body)
    headers["X-Hub-Signature"] = "sha1=" + headers.pop("X-Hub-Signature-256")[len("sha256=") :]

    response = post_delivery(client, body, headers)

    assert_error(response, 401, "invalid_signature")
    assert rejections(db)[0].detail["reason"] == "signature_missing"


def test_forged_request_cannot_claim_a_delivery_id(client: TestClient, db: Session) -> None:
    forged = send_delivery(client, "ping", ping_payload(), delivery_id="d-1", secret="guess")
    real = send_delivery(client, "ping", ping_payload(), delivery_id="d-1")

    assert forged.status_code == 401
    assert real.json() == {"outcome": "ignored"}
    assert [row.delivery_id for row in deliveries(db)] == ["d-1"]


def _chunks(total: int, size: int = 1024 * 1024) -> Iterator[bytes]:
    while total > 0:
        yield b" " * min(size, total)
        total -= size


def test_oversized_body_is_rejected(client: TestClient, db: Session) -> None:
    body = b" " * (MAX_BODY_BYTES + 1)

    response = post_delivery(client, body, delivery_headers("push", body))

    assert_error(response, 413, "payload_too_large")
    assert deliveries(db) == []
    assert rejections(db) == []


def test_oversized_streamed_body_is_rejected(client: TestClient, db: Session) -> None:
    # A chunked request has no Content-Length, so the limit applies while reading.
    headers = delivery_headers("push", b"")

    response = post_delivery(client, _chunks(MAX_BODY_BYTES + 1), headers)

    assert response.request.headers.get("transfer-encoding") == "chunked"
    assert "content-length" not in response.request.headers
    assert_error(response, 413, "payload_too_large")
    assert deliveries(db) == []


def test_body_at_the_limit_is_read(client: TestClient, db: Session) -> None:
    padding = MAX_BODY_BYTES - len(json.dumps({"zen": "", "pad": ""}))
    body = json.dumps({"zen": "", "pad": " " * padding}).encode()
    assert len(body) == MAX_BODY_BYTES

    response = send_delivery(client, "ping", None, raw_body=body)

    assert response.json() == {"outcome": "ignored"}


@pytest.mark.parametrize("missing", ["X-GitHub-Delivery", "X-GitHub-Event"])
def test_missing_delivery_headers_are_invalid(
    client: TestClient, db: Session, missing: str
) -> None:
    body = json.dumps(ping_payload()).encode()
    headers = delivery_headers("ping", body)
    del headers[missing]

    response = post_delivery(client, body, headers)

    assert_error(response, 400, "invalid_delivery")
    assert deliveries(db) == []
    assert rejections(db) == []


@pytest.mark.parametrize(
    "body",
    [b"not json", b"", b"[1, 2]", b'"ping"', b"\xff\xfe\x00"],
    ids=["text", "empty", "array", "string", "bad-encoding"],
)
def test_body_that_is_not_a_json_object_is_invalid(
    client: TestClient, db: Session, body: bytes
) -> None:
    response = send_delivery(client, "ping", None, raw_body=body)

    assert_error(response, 400, "invalid_delivery")
    assert deliveries(db) == []


def test_push_missing_required_fields_is_invalid(client: TestClient, db: Session) -> None:
    payload = push_payload(2001, SHA)
    del payload["after"]

    response = send_delivery(client, "push", payload)

    assert_error(response, 400, "invalid_delivery")
    assert deliveries(db) == []


def test_no_session_or_origin_is_needed(client: TestClient, db: Session) -> None:
    # The `client` fixture sends an Origin header by default; deliveries strip it.
    response = send_delivery(client, "ping", ping_payload())

    assert "origin" not in response.request.headers
    assert "cookie" not in response.request.headers
    assert response.status_code == 202, response.text


def test_origin_check_still_applies_elsewhere(client: TestClient) -> None:
    response = client.post("/auth/logout", headers={"Origin": "https://evil.example"})

    assert_error(response, 403, "origin_mismatch")


def test_empty_secret_rejects_every_delivery(
    client: TestClient, db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "github_webhook_secret", "")

    signed_with_test_secret = send_delivery(client, "ping", ping_payload())
    signed_with_empty_secret = send_delivery(client, "ping", ping_payload(), secret="")

    assert_error(signed_with_test_secret, 401, "invalid_signature")
    assert_error(signed_with_empty_secret, 401, "invalid_signature")
    assert deliveries(db) == []
    assert len(rejections(db)) == 2
