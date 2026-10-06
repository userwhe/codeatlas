"""Webhook payload parsing (contracts/github-webhooks.md, "Events")."""

from typing import Any

import pytest

from codeatlas.github.webhooks import (
    AuthorizationRevoked,
    Ignored,
    InstallationLost,
    InvalidDelivery,
    PushEvent,
    RepositoriesRemoved,
    parse,
)
from tests.webhooks import (
    authorization_revoked_payload,
    installation_payload,
    installation_repositories_payload,
    ping_payload,
    push_payload,
)

REPOSITORY_ID = 2001
SHA = "a" * 40
ZERO_SHA = "0" * 40


def test_push_to_default_branch_is_a_push_event() -> None:
    payload = push_payload(REPOSITORY_ID, SHA, ref="refs/heads/trunk", default_branch="trunk")

    assert parse("push", payload) == PushEvent(
        github_repository_id=REPOSITORY_ID,
        ref="refs/heads/trunk",
        after=SHA,
        default_branch="trunk",
        private=False,
    )


def test_push_carries_the_private_flag() -> None:
    event = parse("push", push_payload(REPOSITORY_ID, SHA, private=True))

    assert isinstance(event, PushEvent)
    assert event.private is True


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        pytest.param(
            push_payload(REPOSITORY_ID, SHA, ref="refs/heads/feature"),
            "not_default_branch",
            id="other-branch",
        ),
        pytest.param(
            push_payload(REPOSITORY_ID, SHA, ref="refs/heads/main-old"),
            "not_default_branch",
            id="branch-with-default-prefix",
        ),
        pytest.param(push_payload(REPOSITORY_ID, SHA, ref="refs/tags/v1.0.0"), "tag", id="tag"),
        pytest.param(
            push_payload(REPOSITORY_ID, SHA, ref="refs/tags/main"), "tag", id="tag-named-main"
        ),
        pytest.param(
            push_payload(REPOSITORY_ID, ZERO_SHA, deleted=True),
            "branch_deleted",
            id="default-branch-deleted",
        ),
    ],
)
def test_irrelevant_pushes_are_ignored(payload: dict[str, Any], reason: str) -> None:
    assert parse("push", payload) == Ignored(reason=reason)


@pytest.mark.parametrize(
    ("action", "reason"),
    [("deleted", "app_uninstalled"), ("suspend", "app_suspended")],
)
def test_installation_loss(action: str, reason: str) -> None:
    assert parse("installation", installation_payload(action, 5002)) == InstallationLost(
        github_installation_id=5002, reason=reason
    )


@pytest.mark.parametrize("action", ["created", "unsuspend", "new_permissions_accepted"])
def test_other_installation_actions_are_ignored(action: str) -> None:
    payload = installation_payload(action, 5002)

    assert parse("installation", payload) == Ignored(reason="action_not_handled")


def test_removed_repositories_are_listed() -> None:
    payload = installation_repositories_payload("removed", 5002, [2001, 2002])

    assert parse("installation_repositories", payload) == RepositoriesRemoved(
        github_installation_id=5002, github_repository_ids=(2001, 2002)
    )


def test_added_repositories_are_ignored() -> None:
    payload = installation_repositories_payload("added", 5002, [])

    assert parse("installation_repositories", payload) == Ignored(reason="action_not_handled")


def test_authorization_revoked_carries_the_sender() -> None:
    payload = authorization_revoked_payload(1001)

    assert parse("github_app_authorization", payload) == AuthorizationRevoked(github_user_id=1001)


def test_other_authorization_actions_are_ignored() -> None:
    payload = {**authorization_revoked_payload(1001), "action": "granted"}

    assert parse("github_app_authorization", payload) == Ignored(reason="action_not_handled")


@pytest.mark.parametrize("event", ["ping", "issues", "pull_request", "star", ""])
def test_ping_and_unknown_events_are_ignored(event: str) -> None:
    assert parse(event, ping_payload()) == Ignored(reason="event_not_handled")


def _without(payload: dict[str, Any], *path: str) -> dict[str, Any]:
    """A copy of `payload` with the key at `path` removed."""
    copy = {**payload}
    target = copy
    for key in path[:-1]:
        target[key] = {**target[key]}
        target = target[key]
    del target[path[-1]]
    return copy


@pytest.mark.parametrize(
    "path",
    [
        ("repository", "id"),
        ("after",),
        ("ref",),
        ("repository",),
        ("repository", "default_branch"),
        ("repository", "private"),
    ],
    ids=lambda path: ".".join(path),
)
def test_push_missing_required_fields_is_invalid(path: tuple[str, ...]) -> None:
    payload = _without(push_payload(REPOSITORY_ID, SHA), *path)

    with pytest.raises(InvalidDelivery):
        parse("push", payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", "2001"), ("id", True), ("id", None)],
)
def test_push_with_a_malformed_repository_id_is_invalid(field: str, value: object) -> None:
    payload = push_payload(REPOSITORY_ID, SHA)
    payload["repository"] = {**payload["repository"], field: value}

    with pytest.raises(InvalidDelivery):
        parse("push", payload)


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        pytest.param(
            "installation",
            _without(installation_payload("deleted", 5002), "installation"),
            id="installation-without-installation",
        ),
        pytest.param(
            "installation",
            _without(installation_payload("deleted", 5002), "action"),
            id="installation-without-action",
        ),
        pytest.param(
            "installation_repositories",
            _without(
                installation_repositories_payload("removed", 5002, [2001]), "repositories_removed"
            ),
            id="removed-without-list",
        ),
        pytest.param(
            "github_app_authorization",
            _without(authorization_revoked_payload(1001), "sender", "id"),
            id="revoked-without-sender-id",
        ),
    ],
)
def test_other_events_missing_required_fields_are_invalid(
    event: str, payload: dict[str, Any]
) -> None:
    with pytest.raises(InvalidDelivery):
        parse(event, payload)
