from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.config import Settings
from codeatlas.github.fake import (
    HUBOT_TOOLS_ID,
    NO_CODE_ID,
    PUBLIC_UNINSTALLED_ID,
    SAMPLE_APP_ID,
    SAMPLE_APP_PRIVATE_ID,
    SOLO_ID,
    get_fake_github,
)
from codeatlas.models import AuditEvent, Job, Repository

pytestmark = pytest.mark.integration


def connect(client: TestClient, github_id: int, **extra: object) -> object:
    return client.post("/v1/repositories", json={"github_repository_id": github_id, **extra})


def test_lists_connectable_repositories(signed_in: Callable[[str], TestClient]) -> None:
    client = signed_in("octocat")
    connect(client, SAMPLE_APP_ID)

    response = client.get("/v1/github/repositories")

    assert response.status_code == 200
    items = {item["github_repository_id"]: item for item in response.json()["items"]}
    assert HUBOT_TOOLS_ID not in items
    assert items[SAMPLE_APP_ID]["connected_repository_id"] is not None
    assert items[NO_CODE_ID]["connected_repository_id"] is None
    assert items[SAMPLE_APP_PRIVATE_ID]["private"] is True


def test_connect_queues_indexing_and_audits(
    db: Session, signed_in: Callable[[str], TestClient]
) -> None:
    client = signed_in("octocat")

    response = client.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["repository"]["full_name"] == "octo-org/sample-app"
    assert body["repository"]["state"] == "indexing"
    assert body["job"]["status"] == "queued"
    job = db.get(Job, body["job"]["id"])
    assert job is not None
    assert job.payload == {"branch": "main"}
    audit = db.scalars(select(AuditEvent).where(AuditEvent.action == "repository_connect")).one()
    assert audit.outcome == "success"


def test_connect_makes_three_github_calls(signed_in: Callable[[str], TestClient]) -> None:
    client = signed_in("octocat")
    fake = get_fake_github()
    fake.calls.clear()

    assert connect(client, SAMPLE_APP_ID).status_code == 202  # type: ignore[attr-defined]

    assert dict(fake.calls) == {
        "get_repository": 1,
        "get_installation_id": 1,
        "list_installation_ids": 1,
    }


@pytest.mark.parametrize(
    ("login", "github_id"),
    [
        ("octocat", HUBOT_TOOLS_ID),  # public, but installed only on another account
        ("hubot", SOLO_ID),  # public, but installed only on another account
        ("octocat", PUBLIC_UNINSTALLED_ID),  # public, and the App is not installed
        ("hubot", SAMPLE_APP_PRIVATE_ID),  # private, and the user cannot see it
    ],
)
def test_inaccessible_repository_is_not_found_and_audited(
    db: Session, signed_in: Callable[[str], TestClient], login: str, github_id: int
) -> None:
    client = signed_in(login)
    fake = get_fake_github()
    fake.calls.clear()

    response = client.post(
        "/v1/repositories",
        json={"github_repository_id": github_id, "accept_external_processing": True},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert "open_tarball" not in fake.calls
    assert db.scalar(select(Repository)) is None
    denied = db.scalars(select(AuditEvent).where(AuditEvent.action == "access_denied")).one()
    assert denied.outcome == "denied"


def test_private_repository_requires_acceptance(signed_in: Callable[[str], TestClient]) -> None:
    client = signed_in("octocat")

    refused = client.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_PRIVATE_ID})
    accepted = client.post(
        "/v1/repositories",
        json={"github_repository_id": SAMPLE_APP_PRIVATE_ID, "accept_external_processing": True},
    )

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "external_processing_not_accepted"
    assert accepted.status_code == 202


def test_duplicate_connection_conflicts(signed_in: Callable[[str], TestClient]) -> None:
    client = signed_in("octocat")
    client.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})

    response = client.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_connected"


def test_repository_limit(
    signed_in: Callable[[str], TestClient], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "max_repositories_per_workspace", 2)
    client = signed_in("octocat")
    client.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})
    client.post("/v1/repositories", json={"github_repository_id": NO_CODE_ID})

    response = client.post(
        "/v1/repositories",
        json={"github_repository_id": SAMPLE_APP_PRIVATE_ID, "accept_external_processing": True},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "repository_limit_reached"


def test_idempotent_connect_replays_response(
    db: Session, signed_in: Callable[[str], TestClient]
) -> None:
    client = signed_in("octocat")
    headers = {"Idempotency-Key": "connect-1"}
    body = {"github_repository_id": SAMPLE_APP_ID}

    first = client.post("/v1/repositories", json=body, headers=headers)
    second = client.post("/v1/repositories", json=body, headers=headers)

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert len(db.scalars(select(Repository)).all()) == 1


def test_reindex_makes_no_github_call_and_reuses_active_job(
    signed_in: Callable[[str], TestClient],
) -> None:
    client = signed_in("octocat")
    connected = client.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})
    repository_id = connected.json()["repository"]["id"]
    first_job = connected.json()["job"]["id"]
    fake = get_fake_github()
    fake.calls.clear()

    response = client.post(f"/v1/repositories/{repository_id}/index", json={})

    assert response.status_code == 202
    assert response.json()["job"]["id"] == first_job
    assert sum(fake.calls.values()) == 0


def test_other_workspace_cannot_see_repository(signed_in: Callable[[str], TestClient]) -> None:
    owner = signed_in("octocat")
    other = signed_in("hubot")
    repository_id = owner.post(
        "/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID}
    ).json()["repository"]["id"]

    assert other.get(f"/v1/repositories/{repository_id}").status_code == 404
    assert other.post(f"/v1/repositories/{repository_id}/index", json={}).status_code == 404
    assert other.get("/v1/repositories").json()["items"] == []


def test_unreadable_github_credentials_ask_the_user_to_sign_in_again(
    db: Session, signed_in: Callable[[str], TestClient]
) -> None:
    from cryptography.fernet import Fernet
    from sqlalchemy import update

    from codeatlas.models import GitHubCredential

    client = signed_in("octocat")
    # Simulate a credential stored under a different TOKEN_ENCRYPTION_KEY.
    other_key = Fernet(Fernet.generate_key())
    db.execute(update(GitHubCredential).values(access_token_enc=other_key.encrypt(b"old-token")))
    db.commit()

    listed = client.get("/v1/github/repositories")

    assert listed.status_code == 401
    assert listed.json()["error"]["code"] == "github_sign_in_required"
    assert client.get("/v1/me").status_code == 401
    assert db.scalar(select(GitHubCredential)) is None

    again = signed_in("octocat")
    assert again.get("/v1/github/repositories").status_code == 200
    connected = again.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})
    assert connected.status_code == 202
