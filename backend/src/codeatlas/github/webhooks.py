"""GitHub webhook deliveries: signature checks and payload parsing (research R2).

Pure functions with no database or web framework dependencies. `parse` reads only the fields
listed in contracts/github-webhooks.md, so commit messages, author data, and file names never
leave the payload.
"""

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

# GitHub caps webhook payloads at 25 MiB.
MAX_BODY_BYTES = 25 * 1024 * 1024

_SIGNATURE_PREFIX = "sha256="
_HEX_DIGEST = re.compile(r"[0-9a-fA-F]{64}")


class InvalidDelivery(Exception):
    """A payload that lacks a field the contract requires for its event."""


@dataclass(frozen=True)
class PushEvent:
    """A push to the repository's default branch that did not delete it."""

    github_repository_id: int
    ref: str
    after: str
    default_branch: str
    private: bool


@dataclass(frozen=True)
class InstallationLost:
    github_installation_id: int
    reason: Literal["app_uninstalled", "app_suspended"]


@dataclass(frozen=True)
class RepositoriesRemoved:
    github_installation_id: int
    github_repository_ids: tuple[int, ...]


@dataclass(frozen=True)
class AuthorizationRevoked:
    github_user_id: int


@dataclass(frozen=True)
class Ignored:
    reason: str


ParsedEvent = PushEvent | InstallationLost | RepositoriesRemoved | AuthorizationRevoked | Ignored


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Check `X-Hub-Signature-256` (`sha256=<hex>`) against the HMAC of `body`.

    An empty secret rejects every request. The legacy SHA-1 header is never consulted.
    """
    if not secret or not header or not header.startswith(_SIGNATURE_PREFIX):
        return False
    provided = header.removeprefix(_SIGNATURE_PREFIX)
    if not _HEX_DIGEST.fullmatch(provided):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.lower())


def parse(event: str, payload: Mapping[str, Any]) -> ParsedEvent:
    """Turn a verified delivery into the event the endpoint acts on.

    Raises `InvalidDelivery` when a field needed for a handled event or action is missing.
    """
    match event:
        case "push":
            return _parse_push(payload)
        case "installation":
            return _parse_installation(payload)
        case "installation_repositories":
            return _parse_installation_repositories(payload)
        case "github_app_authorization":
            return _parse_authorization(payload)
        case _:
            return Ignored("event_not_handled")


def _parse_push(payload: Mapping[str, Any]) -> PushEvent | Ignored:
    ref = _str(payload, "ref")
    after = _str(payload, "after")
    repository = _mapping(payload, "repository")
    github_repository_id = _int(repository, "id")
    default_branch = _str(repository, "default_branch")
    private = _bool(repository, "private")
    deleted = payload.get("deleted", False)
    if not isinstance(deleted, bool):
        raise InvalidDelivery("deleted")

    if ref.startswith("refs/tags/"):
        return Ignored("tag")
    if deleted:
        return Ignored("branch_deleted")
    if ref != f"refs/heads/{default_branch}":
        return Ignored("not_default_branch")
    return PushEvent(
        github_repository_id=github_repository_id,
        ref=ref,
        after=after,
        default_branch=default_branch,
        private=private,
    )


def _parse_installation(payload: Mapping[str, Any]) -> InstallationLost | Ignored:
    action = _str(payload, "action")
    reason: Literal["app_uninstalled", "app_suspended"]
    if action == "deleted":
        reason = "app_uninstalled"
    elif action == "suspend":
        reason = "app_suspended"
    else:
        return Ignored("action_not_handled")
    installation_id = _int(_mapping(payload, "installation"), "id")
    return InstallationLost(github_installation_id=installation_id, reason=reason)


def _parse_installation_repositories(
    payload: Mapping[str, Any],
) -> RepositoriesRemoved | Ignored:
    if _str(payload, "action") != "removed":
        return Ignored("action_not_handled")
    installation_id = _int(_mapping(payload, "installation"), "id")
    removed = payload.get("repositories_removed")
    if not isinstance(removed, list):
        raise InvalidDelivery("repositories_removed")
    ids = tuple(_int(_as_mapping(item, "repositories_removed"), "id") for item in removed)
    return RepositoriesRemoved(github_installation_id=installation_id, github_repository_ids=ids)


def _parse_authorization(payload: Mapping[str, Any]) -> AuthorizationRevoked | Ignored:
    if _str(payload, "action") != "revoked":
        return Ignored("action_not_handled")
    return AuthorizationRevoked(github_user_id=_int(_mapping(payload, "sender"), "id"))


# --- Field access -------------------------------------------------------------------------------
# Each helper raises `InvalidDelivery` naming only the field, never its value.


def _as_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidDelivery(name)
    return value


def _mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    return _as_mapping(payload.get(key), key)


def _str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidDelivery(key)
    return value


def _int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    # bool is a subclass of int, and JSON `true` is not an ID.
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidDelivery(key)
    return value


def _bool(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise InvalidDelivery(key)
    return value
