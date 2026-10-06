"""Builders and a sender for signed GitHub webhook deliveries (contracts/github-webhooks.md).

Payloads follow GitHub's shapes for the fields the endpoint reads, plus a few harmless extras.
Push payloads include a commit message and author data so tests can check they are never stored.
"""

import hashlib
import hmac
import json
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

import httpx
from fastapi.testclient import TestClient

# Matches GITHUB_WEBHOOK_SECRET in tests/conftest.py.
TEST_WEBHOOK_SECRET = "test-webhook-secret"
WEBHOOK_PATH = "/webhooks/github"

DEFAULT_INSTALLATION_ID = 5002
SENDER_ID = 1001
SENDER_LOGIN = "octocat"
COMMIT_MESSAGE = "Rotate the deploy credentials"
AUTHOR_EMAIL = "octocat@example.com"


def _account(account_id: int = SENDER_ID, login: str = SENDER_LOGIN) -> dict[str, Any]:
    return {"id": account_id, "login": login, "type": "User", "site_admin": False}


def _repository(github_repository_id: int, *, default_branch: str, private: bool) -> dict[str, Any]:
    name = f"repository-{github_repository_id}"
    return {
        "id": github_repository_id,
        "node_id": f"R_{github_repository_id}",
        "name": name,
        "full_name": f"{SENDER_LOGIN}/{name}",
        "private": private,
        "owner": _account(),
        "default_branch": default_branch,
        "master_branch": default_branch,
        "fork": False,
    }


def push_payload(
    github_repository_id: int,
    after: str,
    *,
    ref: str = "refs/heads/main",
    default_branch: str = "main",
    private: bool = False,
    deleted: bool = False,
) -> dict[str, Any]:
    author = {"name": "The Octocat", "email": AUTHOR_EMAIL, "username": SENDER_LOGIN}
    commit = {
        "id": after,
        "message": COMMIT_MESSAGE,
        "timestamp": "2026-10-06T12:00:00Z",
        "author": author,
        "committer": author,
        "added": ["deploy/credentials.py"],
        "removed": [],
        "modified": ["README.md"],
    }
    return {
        "ref": ref,
        "before": hashlib.sha1(f"before:{after}".encode(), usedforsecurity=False).hexdigest(),
        "after": after,
        "created": False,
        "deleted": deleted,
        "forced": False,
        "base_ref": None,
        "commits": [] if deleted else [commit],
        "head_commit": None if deleted else commit,
        "repository": _repository(
            github_repository_id, default_branch=default_branch, private=private
        ),
        "pusher": {"name": SENDER_LOGIN, "email": AUTHOR_EMAIL},
        "sender": _account(),
        "installation": {"id": DEFAULT_INSTALLATION_ID, "node_id": "MDIzOkludGVncmF0aW9u"},
    }


def installation_payload(action: str, installation_id: int) -> dict[str, Any]:
    return {
        "action": action,
        "installation": {
            "id": installation_id,
            "account": _account(),
            "repository_selection": "selected",
            "app_id": 1,
            "target_type": "User",
            "permissions": {"contents": "read", "metadata": "read"},
            "events": ["push"],
        },
        "sender": _account(),
    }


def installation_repositories_payload(
    action: str, installation_id: int, removed_ids: Iterable[int]
) -> dict[str, Any]:
    removed = [
        {
            "id": github_repository_id,
            "node_id": f"R_{github_repository_id}",
            "name": f"repository-{github_repository_id}",
            "full_name": f"{SENDER_LOGIN}/repository-{github_repository_id}",
            "private": False,
        }
        for github_repository_id in removed_ids
    ]
    return {
        "action": action,
        "installation": {"id": installation_id, "account": _account()},
        "repository_selection": "selected",
        "repositories_added": [],
        "repositories_removed": removed,
        "requester": None,
        "sender": _account(),
    }


def authorization_revoked_payload(github_user_id: int) -> dict[str, Any]:
    return {"action": "revoked", "sender": _account(github_user_id, f"user-{github_user_id}")}


def ping_payload() -> dict[str, Any]:
    return {
        "zen": "Keep it logically awesome.",
        "hook_id": 1,
        "hook": {"type": "App", "id": 1, "active": True, "events": ["push"], "app_id": 1},
    }


def signature_header(body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    """The `X-Hub-Signature-256` value GitHub sends for `body`."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def delivery_headers(
    event: str, body: bytes, *, delivery_id: str | None = None, secret: str | None = None
) -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery_id or str(uuid.uuid4()),
        "X-Hub-Signature-256": signature_header(
            body, TEST_WEBHOOK_SECRET if secret is None else secret
        ),
    }


def post_delivery(
    client: TestClient, body: bytes | Iterable[bytes], headers: Mapping[str, str]
) -> httpx.Response:
    """POST to the webhook endpoint as GitHub does: no Origin header and no cookies."""
    request = client.build_request(
        "POST",
        WEBHOOK_PATH,
        content=body,
        headers={"Content-Type": "application/json", "User-Agent": "GitHub-Hookshot/test"},
    )
    for name in ("origin", "cookie"):
        if name in request.headers:
            del request.headers[name]
    request.headers.update(headers)
    return client.send(request)


def send_delivery(
    client: TestClient,
    event: str,
    payload: Mapping[str, Any] | None,
    *,
    delivery_id: str | None = None,
    secret: str | None = None,
    raw_body: bytes | None = None,
) -> httpx.Response:
    """Sign and send one delivery. `raw_body` replaces the JSON encoding of `payload`."""
    body = raw_body if raw_body is not None else json.dumps(payload).encode()
    headers = delivery_headers(event, body, delivery_id=delivery_id, secret=secret)
    return post_delivery(client, body, headers)
