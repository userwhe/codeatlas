"""The per-address rate limit on the endpoints without a session (T013, FR-006, research R4)."""

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from codeatlas.api.ratelimit import SlidingWindowLimiter
from codeatlas.config import Settings
from codeatlas.models import AuditEvent, WebhookDelivery
from tests.conftest import APP_ORIGIN
from tests.webhooks import ping_payload, send_delivery

ADDRESS = "203.0.113.7"
OTHER_ADDRESS = "198.51.100.23"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def test_the_limit_is_allowed_then_refused_until_the_oldest_call_leaves(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60, clock=clock)

    assert limiter.check(ADDRESS) is None  # t=1000
    clock.advance(10.5)
    assert limiter.check(ADDRESS) is None  # t=1010.5
    clock.advance(10)
    assert limiter.check(ADDRESS) is None  # t=1020.5
    clock.advance(4.25)

    # The oldest call leaves the window at t=1060, 35.25 seconds from now.
    assert limiter.check(ADDRESS) == 36
    clock.advance(35.25)
    assert limiter.check(ADDRESS) is None
    # Now the call at t=1010.5 is the oldest; it leaves at t=1070.5.
    assert limiter.check(ADDRESS) == 11


def test_calls_are_allowed_again_after_the_window(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60, clock=clock)
    for _ in range(3):
        assert limiter.check(ADDRESS) is None
    assert limiter.check(ADDRESS) == 60

    clock.advance(60)

    assert [limiter.check(ADDRESS) for _ in range(3)] == [None, None, None]


def test_addresses_are_independent(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60, clock=clock)
    for _ in range(3):
        limiter.check(ADDRESS)

    assert limiter.check(ADDRESS) is not None
    assert [limiter.check(OTHER_ADDRESS) for _ in range(3)] == [None, None, None]


def test_idle_addresses_are_evicted(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60, clock=clock)
    limiter.check(ADDRESS)
    clock.advance(30)
    limiter.check(OTHER_ADDRESS)
    assert len(limiter) == 2

    clock.advance(30)
    limiter.sweep()
    assert len(limiter) == 1  # The other address's call is still in its window.

    clock.advance(30)
    limiter.sweep()
    assert len(limiter) == 0


def test_calls_sweep_idle_addresses_every_minute(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60, clock=clock)
    for index in range(100):
        limiter.check(f"192.0.2.{index}")
    assert len(limiter) == 100

    clock.advance(61)
    limiter.check(ADDRESS)

    assert len(limiter) == 1


def test_a_zero_limit_allows_everything(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(limit=0, window_seconds=60, clock=clock)

    assert all(limiter.check(ADDRESS) is None for _ in range(1000))
    assert len(limiter) == 0


# Through the app ---------------------------------------------------------------------------


@pytest.fixture
def limited(
    db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """A client of a new app limited to 2 requests a minute (the module-level `app` was built
    at import, with the limit off).
    """
    from codeatlas.api.app import create_app

    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)
    app = create_app()
    with TestClient(app, base_url=APP_ORIGIN, headers={"Origin": APP_ORIGIN}) as test_client:
        yield test_client


def assert_rate_limited(response: httpx.Response) -> None:
    assert response.status_code == 429, response.text
    error = response.json()["error"]
    assert (error["code"], error["retryable"]) == ("rate_limited", True)
    assert error["message"] == "Too many requests. Try again later."
    assert 1 <= int(response.headers["Retry-After"]) <= 60


@pytest.mark.integration
def test_sign_in_is_limited_per_address(limited: TestClient) -> None:
    statuses = [
        limited.get("/auth/github/login", follow_redirects=False).status_code for _ in range(2)
    ]

    assert statuses == [302, 302]
    assert_rate_limited(limited.get("/auth/github/login", follow_redirects=False))
    # The limit runs before every other check, the Origin check included.
    assert_rate_limited(limited.post("/auth/logout", headers={"Origin": "https://evil.example"}))


@pytest.mark.integration
def test_a_refused_webhook_delivery_is_not_recorded(limited: TestClient, db: Session) -> None:
    accepted = [send_delivery(limited, "ping", ping_payload()) for _ in range(2)]

    refused = send_delivery(limited, "ping", ping_payload(), delivery_id="delivery-refused")

    assert [response.status_code for response in accepted] == [202, 202]
    assert_rate_limited(refused)
    assert db.get(WebhookDelivery, "delivery-refused") is None
    assert db.scalar(select(func.count()).select_from(WebhookDelivery)) == 2
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


@pytest.mark.integration
def test_other_paths_are_never_limited(limited: TestClient) -> None:
    for _ in range(3):
        limited.get("/auth/github/login", follow_redirects=False)

    for _ in range(5):
        assert limited.get("/v1/me").status_code == 401
        assert limited.get("/healthz").status_code == 200
        assert limited.get("/readyz").json() == {"status": "ready"}
