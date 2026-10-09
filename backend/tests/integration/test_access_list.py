"""Integration tests for the pilot's access decision at sign-in (T015, FR-002, FR-003,
data-model.md "Access decision").

The access list applies in production; these tests turn it on with `access_list_required`.
"""

from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from codeatlas.auth.sessions import COOKIE_NAME
from codeatlas.config import Settings
from codeatlas.models import (
    AuditEvent,
    GitHubCredential,
    Membership,
    PilotUser,
    User,
    UserSession,
    Workspace,
)
from tests.conftest import sign_in

pytestmark = pytest.mark.integration

OCTOCAT_ID = 1001


@pytest.fixture
def access_list(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "access_list_required", True)


def count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def complete_sign_in(client: TestClient, login: str) -> TestClient:
    """Start sign-in and return from GitHub as `login`, without following the redirect."""
    start = client.get("/auth/github/login", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return client.get(
        "/auth/github/callback",
        params={"code": f"fake:{login}", "state": state},
        follow_redirects=False,
    )


def test_a_listed_user_signs_in_and_their_login_is_refreshed(
    db: Session, client: TestClient, access_list: None
) -> None:
    db.add(PilotUser(github_user_id=OCTOCAT_ID, github_login="octocat-before-rename"))
    db.commit()

    sign_in(client, "octocat")

    assert client.get("/v1/me").json()["user"]["github_login"] == "octocat"
    pilot_user = db.get(PilotUser, OCTOCAT_ID)
    assert pilot_user is not None
    db.refresh(pilot_user)
    assert pilot_user.github_login == "octocat"
    events = db.scalars(select(AuditEvent).where(AuditEvent.action == "sign_in")).all()
    assert [event.outcome for event in events] == ["success"]


def test_an_unlisted_user_is_turned_away_without_leaving_data(
    db: Session, client: TestClient, access_list: None
) -> None:
    db.add(PilotUser(github_user_id=OCTOCAT_ID, github_login="octocat"))
    db.commit()

    response = complete_sign_in(client, "monalisa")

    assert response.status_code == 302
    assert response.headers["location"] == "/?error=not_invited"
    assert COOKIE_NAME not in response.cookies
    assert COOKIE_NAME not in client.cookies
    for model in (User, Workspace, Membership, GitHubCredential, UserSession):
        assert count(db, model) == 0, model.__tablename__
    [event] = db.scalars(select(AuditEvent)).all()
    assert (event.action, event.outcome) == ("sign_in", "denied")
    assert event.detail == {"reason": "not_invited", "github_login": "monalisa"}
    assert (event.actor_user_id, event.workspace_id) == (None, None)
    assert client.get("/v1/me").status_code == 401


def test_without_the_access_list_anyone_signs_in(
    db: Session, signed_in: Callable[[str], TestClient]
) -> None:
    client = signed_in("monalisa")

    assert client.get("/v1/me").json()["user"]["github_login"] == "monalisa"
    assert client.get("/v1/repositories").status_code == 200
    assert count(db, PilotUser) == 0
