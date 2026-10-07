"""Integration tests for listing open pull requests (T025, research R1 and R10).

`octocat` connects `octo-org/review-app`, whose open pull requests are #1 to #8 (#8 is a draft) and
whose #9 is closed. Review states come from reviews written directly to the database.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from codeatlas.auth.github_login import TOKEN_REFRESH_MARGIN
from codeatlas.github import fake as fake_module
from codeatlas.github.fake import (
    OVERSIZED_ID,
    REVIEW_APP_ID,
    REVIEW_APP_PRIVATE_ID,
    commit_sha,
    get_fake_github,
)
from codeatlas.github.gateway import (
    GitHubAccessDenied,
    GitHubError,
    InstallationPermissions,
    PullRequestPage,
    UserAuthorizationInvalid,
    UserTokens,
)
from codeatlas.models import AuditEvent, GitHubCredential, Repository
from codeatlas.workspace import access
from tests.integration.test_review_access import add_review
from tests.integration.test_review_submit import connect, error_code

pytestmark = pytest.mark.integration

SETTINGS_URL = "https://github.com/organizations/octo-org/settings/installations/5001"


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


@pytest.fixture
def repository_id(octocat: TestClient, run_worker_once: Callable[[], bool]) -> str:
    return connect(octocat, run_worker_once)


def pull_requests(client: TestClient, repository_id: str, **params: str) -> Any:
    response = client.get(f"/v1/repositories/{repository_id}/pull-requests", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def by_number(page: Any) -> dict[int, dict[str, Any]]:
    return {item["number"]: item for item in page["items"]}


def test_lists_open_pull_requests_most_recently_updated_first(
    octocat: TestClient, repository_id: str
) -> None:
    page = pull_requests(octocat, repository_id)

    assert [item["number"] for item in page["items"]] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert page["next_cursor"] is None
    items = by_number(page)
    assert items[1] == {
        "number": 1,
        "title": "Simplify write checks",
        "author": "hubot",
        "draft": False,
        "base_ref": "main",
        "head_ref": "simplify-write-checks",
        "head_sha": commit_sha(REVIEW_APP_ID, "pr-1"),
        "is_fork": False,
        "updated_at": "2026-10-05T16:00:00Z",
        "html_url": "https://github.com/octo-org/review-app/pull/1",
        "review": None,
    }
    assert items[2]["base_ref"] == "release"
    assert items[3]["is_fork"] is True
    assert items[8]["draft"] is True
    assert all(item["review"] is None for item in items.values())


def test_pages_follow_github(
    octocat: TestClient, repository_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fake_module, "PULL_REQUEST_PAGE_SIZE", 3)

    pages = [pull_requests(octocat, repository_id)]
    while pages[-1]["next_cursor"] is not None:
        pages.append(pull_requests(octocat, repository_id, cursor=pages[-1]["next_cursor"]))

    assert [[item["number"] for item in page["items"]] for page in pages] == [
        [1, 2, 3],
        [4, 5, 6],
        [7, 8],
    ]
    invalid = octocat.get(
        f"/v1/repositories/{repository_id}/pull-requests", params={"cursor": "not-a-cursor"}
    )
    assert error_code(invalid) == (422, "invalid_cursor")


def test_review_states(db: Session, octocat: TestClient, repository_id: str) -> None:
    now = datetime.now(UTC)
    queued = add_review(
        db, repository_id, number=1, head_sha=commit_sha(REVIEW_APP_ID, "pr-1"), job_status="queued"
    )
    running = add_review(
        db,
        repository_id,
        number=2,
        head_sha=commit_sha(REVIEW_APP_ID, "pr-2"),
        job_status="running",
    )
    retrying = add_review(
        db,
        repository_id,
        number=3,
        head_sha=commit_sha(REVIEW_APP_ID, "pr-3"),
        job_status="retry_wait",
    )
    current = add_review(db, repository_id, number=4, head_sha=commit_sha(REVIEW_APP_ID, "pr-4"))
    outdated = add_review(db, repository_id, number=5, head_sha=commit_sha(REVIEW_APP_ID, "pr-5"))
    failed = add_review(
        db, repository_id, number=6, head_sha=commit_sha(REVIEW_APP_ID, "pr-6"), job_status="failed"
    )
    # Only the newest review of a pull request counts.
    add_review(
        db,
        repository_id,
        number=7,
        head_sha=commit_sha(REVIEW_APP_ID, "pr-7"),
        created_at=now - timedelta(hours=1),
    )
    newest = add_review(
        db,
        repository_id,
        number=7,
        head_sha=commit_sha(REVIEW_APP_ID, "pr-7"),
        job_status="canceled",
    )
    new_head = get_fake_github().push_to_pull_request(REVIEW_APP_ID, 5)

    items = by_number(pull_requests(octocat, repository_id))

    expected = {
        1: (queued, "queued"),
        2: (running, "running"),
        3: (retrying, "running"),
        4: (current, "current"),
        5: (outdated, "outdated"),
        6: (failed, "failed"),
        7: (newest, "failed"),
    }
    for number, (run, state) in expected.items():
        review = dict(items[number]["review"])
        created_at = datetime.fromisoformat(review.pop("created_at"))
        assert review == {"run_id": str(run.id), "state": state, "head_sha": run.head_sha}, number
        assert created_at == run.created_at
    assert items[5]["head_sha"] == new_head != outdated.head_sha
    assert items[8]["review"] is None


def test_missing_pull_request_permission_links_to_the_installation(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    private_id = connect(
        octocat, run_worker_once, REVIEW_APP_PRIVATE_ID, accept_external_processing=True
    )
    assert [item["number"] for item in pull_requests(octocat, private_id)["items"]] == [1]
    get_fake_github().withhold_permission(REVIEW_APP_PRIVATE_ID, "pull_requests")

    response = octocat.get(f"/v1/repositories/{private_id}/pull-requests")

    assert error_code(response) == (409, "pull_requests_permission_missing")
    assert response.json()["error"]["details"]["settings_url"] == SETTINGS_URL


def test_a_token_refreshed_before_a_refused_list_is_kept(
    db: Session,
    octocat: TestClient,
    repository_id: str,
    run_worker_once: Callable[[], bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_id = connect(
        octocat, run_worker_once, REVIEW_APP_PRIVATE_ID, accept_external_processing=True
    )
    fake = get_fake_github()
    fake.withhold_permission(REVIEW_APP_PRIVATE_ID, "pull_requests")
    expiring = datetime.now(UTC) + TOKEN_REFRESH_MARGIN / 2
    db.execute(update(GitHubCredential).values(access_token_expires_at=expiring))
    db.commit()
    refresh = fake.refresh_user_token
    used: list[str] = []

    def refresh_once(refresh_token: str) -> UserTokens:
        # GitHub replaces the refresh token on every refresh, so each one works once.
        if refresh_token in used:
            raise UserAuthorizationInvalid("the refresh token was already used")
        used.append(refresh_token)
        return refresh(refresh_token)

    monkeypatch.setattr(fake, "refresh_user_token", refresh_once)

    refused = octocat.get(f"/v1/repositories/{private_id}/pull-requests")

    assert error_code(refused) == (409, "pull_requests_permission_missing")
    assert len(used) == 1
    db.expire_all()
    credential = db.scalars(select(GitHubCredential)).one()
    assert credential.access_token_expires_at is not None
    assert credential.access_token_expires_at > expiring + timedelta(hours=1)
    # The next request uses the refreshed token instead of asking the owner to sign in again.
    assert pull_requests(octocat, repository_id)["items"]
    assert len(used) == 1


def test_other_refusals_leave_access_unchanged(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    private_id = connect(
        octocat, run_worker_once, REVIEW_APP_PRIVATE_ID, accept_external_processing=True
    )
    get_fake_github().revoke_access("octocat", REVIEW_APP_PRIVATE_ID)

    response = octocat.get(f"/v1/repositories/{private_id}/pull-requests")

    assert error_code(response) == (409, "github_access_denied")
    db.expire_all()
    repository = db.get(Repository, uuid.UUID(private_id))
    assert repository is not None
    assert (repository.access_state, repository.access_reason) == ("active", None)
    assert octocat.get(f"/v1/repositories/{private_id}").json()["state"] == "ready"


def test_any_other_github_error_is_access_denied(
    db: Session, octocat: TestClient, repository_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def list_pull_requests(user_token: str, full_name: str, page: int) -> PullRequestPage:
        raise GitHubError("GitHub returned 451")

    monkeypatch.setattr(get_fake_github(), "list_pull_requests", list_pull_requests)

    response = octocat.get(f"/v1/repositories/{repository_id}/pull-requests")

    assert error_code(response) == (409, "github_access_denied")
    db.expire_all()
    repository = db.get(Repository, uuid.UUID(repository_id))
    assert repository is not None
    assert (repository.access_state, repository.access_reason) == ("active", None)


def test_any_other_github_error_while_classifying_a_refusal_is_access_denied(
    octocat: TestClient, repository_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def list_pull_requests(user_token: str, full_name: str, page: int) -> PullRequestPage:
        raise GitHubAccessDenied("GitHub refused the list")

    def get_installation_permissions(full_name: str) -> InstallationPermissions:
        raise GitHubError("GitHub returned 451")

    fake = get_fake_github()
    monkeypatch.setattr(fake, "list_pull_requests", list_pull_requests)
    monkeypatch.setattr(fake, "get_installation_permissions", get_installation_permissions)

    response = octocat.get(f"/v1/repositories/{repository_id}/pull-requests")

    assert error_code(response) == (409, "github_access_denied")


def test_lost_repository(db: Session, octocat: TestClient, repository_id: str) -> None:
    repository = db.get(Repository, uuid.UUID(repository_id))
    assert repository is not None
    assert access.mark_access_lost(db, repository, "app_uninstalled", trigger="notification")
    db.commit()
    fake = get_fake_github()
    fake.calls.clear()

    response = octocat.get(f"/v1/repositories/{repository_id}/pull-requests")

    assert error_code(response) == (403, "repository_access_lost")
    assert fake.calls["list_pull_requests"] == 0


def test_rejected_repository(octocat: TestClient, run_worker_once: Callable[[], bool]) -> None:
    rejected_id = connect(octocat, run_worker_once, OVERSIZED_ID)
    fake = get_fake_github()
    fake.calls.clear()

    response = octocat.get(f"/v1/repositories/{rejected_id}/pull-requests")

    assert error_code(response) == (409, "repository_rejected")
    assert fake.calls["list_pull_requests"] == 0


def test_revoked_authorization_needs_sign_in(octocat: TestClient, repository_id: str) -> None:
    get_fake_github().revoke_authorization("octocat")

    response = octocat.get(f"/v1/repositories/{repository_id}/pull-requests")

    assert error_code(response) == (401, "github_sign_in_required")


def test_github_unavailable(octocat: TestClient, repository_id: str) -> None:
    get_fake_github().set_unavailable(True)

    response = octocat.get(f"/v1/repositories/{repository_id}/pull-requests")

    assert error_code(response) == (502, "github_unavailable")
    assert response.json()["error"]["retryable"] is True


def test_other_workspace_gets_not_found(
    db: Session,
    signed_in: Callable[[str], TestClient],
    octocat: TestClient,
    repository_id: str,
) -> None:
    hubot = signed_in("hubot")

    response = hubot.get(f"/v1/repositories/{repository_id}/pull-requests")

    assert error_code(response) == (404, "not_found")
    denied = db.scalars(select(AuditEvent).where(AuditEvent.action == "access_denied")).all()
    assert [(event.resource_type, event.resource_id) for event in denied] == [
        ("repository", repository_id)
    ]
