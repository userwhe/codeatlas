"""Webhook signature checks (research R2)."""

import hashlib
import hmac

import pytest

from codeatlas.github.webhooks import verify_signature
from tests.webhooks import signature_header

SECRET = "s3cret"
BODY = b'{"zen": "Keep it logically awesome.", "hook_id": 1}'


def test_correct_signature_is_accepted() -> None:
    assert verify_signature(SECRET, BODY, signature_header(BODY, SECRET))


def test_uppercase_hex_digest_is_accepted() -> None:
    digest = signature_header(BODY, SECRET).removeprefix("sha256=")
    assert verify_signature(SECRET, BODY, "sha256=" + digest.upper())


def _with_non_hex_digit() -> str:
    digest = signature_header(BODY, SECRET).removeprefix("sha256=")
    return "sha256=z" + digest[1:]


def _sha1_header() -> str:
    return "sha1=" + hmac.new(SECRET.encode(), BODY, hashlib.sha1).hexdigest()


@pytest.mark.parametrize(
    ("body", "header"),
    [
        pytest.param(BODY, signature_header(BODY, "not-the-secret"), id="wrong-secret"),
        pytest.param(BODY[:-1] + b"]", signature_header(BODY, SECRET), id="body-changed"),
        pytest.param(BODY, None, id="missing-header"),
        pytest.param(BODY, "", id="empty-header"),
        pytest.param(BODY, signature_header(BODY, SECRET).removeprefix("sha256="), id="no-prefix"),
        pytest.param(BODY, _with_non_hex_digit(), id="non-hex-digit"),
        pytest.param(BODY, "sha256=" + "é" * 64, id="non-ascii-digits"),
        pytest.param(BODY, signature_header(BODY, SECRET)[:-2], id="truncated"),
        pytest.param(BODY, _sha1_header(), id="sha1-value-in-sha256-header"),
    ],
)
def test_bad_signatures_are_rejected(body: bytes, header: str | None) -> None:
    assert not verify_signature(SECRET, body, header)


def test_request_with_only_the_sha1_header_is_rejected() -> None:
    # Only X-Hub-Signature-256 is read, so a SHA-1-only request has no signature at all.
    headers = {"X-Hub-Signature": _sha1_header()}
    assert not verify_signature(SECRET, BODY, headers.get("X-Hub-Signature-256"))


def test_empty_secret_rejects_every_request() -> None:
    assert not verify_signature("", BODY, signature_header(BODY, ""))
    assert not verify_signature("", BODY, signature_header(BODY, SECRET))
    assert not verify_signature("", BODY, None)
