"""The rate limit's client address (T016, FR-006, research R4).

In hosted environments every request reaches the API from the web app over the private network,
so the limit counts the header that the platform's edge sets when `client_address_header` names
one. Each test builds its own app limited to 2 requests a minute; the test client connects from
the address `testclient`.
"""

import pytest
from fastapi.testclient import TestClient

from codeatlas.config import Settings
from tests.conftest import APP_ORIGIN

LOGIN = "/auth/github/login"
ADDRESS = "203.0.113.7"
OTHER_ADDRESS = "198.51.100.23"
THIRD_ADDRESS = "192.0.2.41"


def limited_client(settings: Settings, monkeypatch: pytest.MonkeyPatch, header: str) -> TestClient:
    """A client of a new app limited to 2 requests a minute that trusts `header` (none if empty)."""
    from codeatlas.api.app import create_app

    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)
    monkeypatch.setattr(settings, "client_address_header", header)
    return TestClient(create_app(), base_url=APP_ORIGIN)


def sign_in_status(client: TestClient, real_ip: str | None = None) -> int:
    headers = {"X-Real-IP": real_ip} if real_ip is not None else {}
    return client.get(LOGIN, headers=headers, follow_redirects=False).status_code


def test_the_configured_header_is_counted_per_value(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with limited_client(settings, monkeypatch, "X-Real-IP") as client:
        statuses = [
            sign_in_status(client, ADDRESS),
            sign_in_status(client, OTHER_ADDRESS),
            sign_in_status(client, ADDRESS),
            sign_in_status(client, OTHER_ADDRESS),
        ]
        third = sign_in_status(client, ADDRESS)

    assert statuses == [302, 302, 302, 302]
    assert third == 429


def test_without_the_configured_header_the_connections_address_is_counted(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with limited_client(settings, monkeypatch, "X-Real-IP") as client:
        statuses = [sign_in_status(client), sign_in_status(client)]
        # A blank value counts as absent, so it shares the connection's window.
        blank = sign_in_status(client, " ")
        with_header = sign_in_status(client, ADDRESS)

    assert statuses == [302, 302]
    assert blank == 429
    assert with_header == 302


def test_the_header_is_ignored_when_none_is_configured(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with limited_client(settings, monkeypatch, "") as client:
        statuses = [sign_in_status(client, ADDRESS), sign_in_status(client, OTHER_ADDRESS)]
        third = sign_in_status(client, THIRD_ADDRESS)

    assert statuses == [302, 302]
    assert third == 429
