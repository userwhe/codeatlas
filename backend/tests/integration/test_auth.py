from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from codeatlas.auth.github_login import get_user_token
from codeatlas.github.gateway import get_gateway
from codeatlas.models import AuditEvent, GitHubCredential, Membership, User, UserSession, Workspace

pytestmark = pytest.mark.integration


def count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def test_first_sign_in_creates_user_workspace_and_membership_once(
    db: Session, signed_in: Callable[[str], TestClient]
) -> None:
    signed_in("octocat")
    signed_in("octocat")

    assert count(db, User) == 1
    assert count(db, Workspace) == 1
    membership = db.scalars(select(Membership)).one()
    assert membership.role == "owner"
    assert count(db, UserSession) == 2
    events = db.scalars(select(AuditEvent).where(AuditEvent.action == "sign_in")).all()
    assert [event.outcome for event in events] == ["success", "success"]


def test_me_returns_user_workspace_and_install_url(signed_in: Callable[[str], TestClient]) -> None:
    response = signed_in("octocat").get("/v1/me")

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["github_login"] == "octocat"
    assert body["workspace"]["name"] == "octocat"
    assert body["github_app_install_url"] == (
        "https://github.com/apps/codeatlas-test/installations/new"
    )


def test_me_requires_a_session(client: TestClient) -> None:
    response = client.get("/v1/me")

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "unauthenticated"
    assert error["retryable"] is False
    assert error["request_id"] == response.headers["X-Request-ID"]


def test_bad_state_fails_sign_in(db: Session, client: TestClient) -> None:
    client.get("/auth/github/login", follow_redirects=False)
    response = client.get(
        "/auth/github/callback",
        params={"code": "fake:octocat", "state": "not-the-state"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/?error=sign_in_failed"
    assert count(db, User) == 0
    failure = db.scalars(select(AuditEvent)).one()
    assert (failure.action, failure.outcome) == ("sign_in", "failure")


def test_unknown_code_fails_sign_in(client: TestClient) -> None:
    start = client.get("/auth/github/login", follow_redirects=False)
    state = start.headers["location"].split("state=")[1]
    response = client.get(
        "/auth/github/callback",
        params={"code": "fake:nobody-known", "state": state},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/?error=sign_in_failed"


def test_logout_revokes_the_session_immediately(
    db: Session, signed_in: Callable[[str], TestClient]
) -> None:
    from codeatlas.api.app import app

    client = signed_in("octocat")
    cookie = client.cookies.get("codeatlas_session")
    assert cookie

    response = client.post("/auth/logout")

    assert response.status_code == 204
    replay = TestClient(app, base_url=str(client.base_url), cookies={"codeatlas_session": cookie})
    assert replay.get("/v1/me").status_code == 401
    session = db.scalars(select(UserSession)).one()
    db.refresh(session)
    assert session.revoked_at is not None
    assert db.scalars(select(AuditEvent).where(AuditEvent.action == "sign_out")).one()


def test_expired_session_is_rejected(db: Session, signed_in: Callable[[str], TestClient]) -> None:
    client = signed_in("octocat")
    db.execute(update(UserSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    db.commit()

    assert client.get("/v1/me").status_code == 401


def test_post_without_matching_origin_is_forbidden(signed_in: Callable[[str], TestClient]) -> None:
    client = signed_in("octocat")

    response = client.post("/auth/logout", headers={"Origin": "https://evil.example"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_mismatch"
    assert client.get("/v1/me").status_code == 200


def test_expired_user_token_is_refreshed(
    db: Session, signed_in: Callable[[str], TestClient]
) -> None:
    signed_in("octocat")
    user = db.scalars(select(User)).one()
    db.execute(
        update(GitHubCredential).values(
            access_token_expires_at=datetime.now(UTC) - timedelta(minutes=5)
        )
    )
    db.commit()

    token = get_user_token(db, user, get_gateway())

    assert token
    credential = db.get(GitHubCredential, user.id)
    assert credential is not None
    db.refresh(credential)
    assert credential.access_token_expires_at is not None
    assert credential.access_token_expires_at > datetime.now(UTC)


def test_a_refreshed_user_token_outlives_a_rollback_of_the_caller(
    db: Session, signed_in: Callable[[str], TestClient]
) -> None:
    # GitHub replaces the refresh token on every refresh, so a request that fails after the
    # refresh must not take the new tokens with it.
    signed_in("octocat")
    user = db.scalars(select(User)).one()
    expired = datetime.now(UTC) - timedelta(minutes=5)
    db.execute(update(GitHubCredential).values(access_token_expires_at=expired))
    db.commit()

    get_user_token(db, user, get_gateway())

    # The caller's session already shows the stored credential.
    credential = db.get(GitHubCredential, user.id)
    assert credential is not None
    assert credential.access_token_expires_at is not None
    assert credential.access_token_expires_at > datetime.now(UTC)
    db.rollback()
    credential = db.get(GitHubCredential, user.id)
    assert credential is not None
    assert credential.access_token_expires_at is not None
    assert credential.access_token_expires_at > datetime.now(UTC)
