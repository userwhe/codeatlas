"""Integration tests for outdated reviews and reuse (T050, research R9 step 5 and R10).

`octocat` connects `octo-org/review-app` and reviews its fixture pull requests. Pushing to a pull
request, closing it, or merging it changes only the fake GitHub: a review is never updated, and
its freshness is read from GitHub each time it is asked for. A request for a head that already has
a waiting, running, or finished review returns that review unless a new one is asked for.
"""

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from codeatlas.github.fake import REVIEW_APP_ID, REVIEW_APP_PRIVATE_ID, commit_sha, get_fake_github
from codeatlas.github.gateway import GitHubError, PullRequest
from codeatlas.models import AnalysisRun, AuditEvent
from tests.integration.test_questions import drain
from tests.integration.test_review_access import add_review
from tests.integration.test_review_submit import connect, error_code

pytestmark = pytest.mark.integration

HEAD = commit_sha(REVIEW_APP_ID, "pr-1")
WAIT_SECONDS = 30


def request(
    client: TestClient,
    repository_id: str,
    number: int = 1,
    *,
    mode: str | None = None,
    key: str | None = None,
) -> Response:
    """Request a review of pull request `number`, in `mode` when given."""
    body: dict[str, Any] = {
        "repository_id": repository_id,
        "kind": "pull_request_review",
        "target": {"pull_request_number": number},
    }
    if mode is not None:
        body["mode"] = mode
    headers = {"Idempotency-Key": key} if key is not None else None
    return client.post("/v1/analysis-runs", json=body, headers=headers)


def review(
    client: TestClient, run_worker_once: Callable[[], bool], repository_id: str, number: int = 1
) -> dict[str, Any]:
    """Request a review, run it to the end, and return the submission."""
    response = request(client, repository_id, number)
    assert response.status_code == 202, response.text
    drain(run_worker_once)
    submitted: dict[str, Any] = response.json()
    return submitted


def freshness(client: TestClient, run_id: str | uuid.UUID) -> Response:
    return client.get(f"/v1/analysis-runs/{run_id}/freshness")


def fresh(client: TestClient, run_id: str | uuid.UUID) -> dict[str, Any]:
    """The freshness of a review, without `checked_at`, which must be the time of the call."""
    before = datetime.now(UTC)
    response = freshness(client, run_id)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    checked_at = datetime.fromisoformat(body.pop("checked_at"))
    assert before - timedelta(seconds=1) <= checked_at <= datetime.now(UTC) + timedelta(seconds=1)
    return body


def reviews_used(client: TestClient) -> int:
    used: int = client.get("/v1/usage").json()["reviews_used"]
    return used


def run_count(db: Session) -> int | None:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(AnalysisRun))


def submit_audits(db: Session) -> list[tuple[str | None, Any]]:
    """The `(resource_id, detail)` of each submission's audit event, oldest first."""
    db.expire_all()
    rows = db.execute(
        select(AuditEvent.resource_id, AuditEvent.detail)
        .where(AuditEvent.action == "pull_request_review_submit")
        .order_by(AuditEvent.created_at, AuditEvent.id)
    )
    return [(resource_id, detail) for resource_id, detail in rows]


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


@pytest.fixture
def repository_id(octocat: TestClient, run_worker_once: Callable[[], bool]) -> str:
    return connect(octocat, run_worker_once)


# Freshness ----------------------------------------------------------------------------------


def test_review_of_the_current_head_is_not_outdated(
    octocat: TestClient, run_worker_once: Callable[[], bool], repository_id: str
) -> None:
    submitted = review(octocat, run_worker_once, repository_id)

    assert fresh(octocat, submitted["run_id"]) == {
        "pull_request_state": "open",
        "draft": False,
        "current_head_sha": HEAD,
        "outdated": False,
    }


def test_freshness_reports_a_draft(db: Session, octocat: TestClient, repository_id: str) -> None:
    run = add_review(db, repository_id, number=8, head_sha=commit_sha(REVIEW_APP_ID, "pr-8"))

    body = fresh(octocat, run.id)

    assert (body["draft"], body["outdated"]) == (True, False)


def test_push_makes_the_review_outdated_and_leaves_it_unchanged(
    octocat: TestClient, run_worker_once: Callable[[], bool], repository_id: str
) -> None:
    submitted = review(octocat, run_worker_once, repository_id)
    run_url = submitted["result_url"]
    before = octocat.get(run_url).json()
    assert before["status"] == "succeeded" and before["review"] is not None

    new_head = get_fake_github().push_to_pull_request(REVIEW_APP_ID, 1)

    assert fresh(octocat, submitted["run_id"]) == {
        "pull_request_state": "open",
        "draft": False,
        "current_head_sha": new_head,
        "outdated": True,
    }
    # The review keeps its result, its evidence, and the head it examined.
    after = octocat.get(run_url).json()
    assert after == before
    assert after["commits"]["head_sha"] == HEAD != new_head
    # The pull request list agrees.
    listed = octocat.get(f"/v1/repositories/{repository_id}/pull-requests").json()["items"]
    (item,) = [item for item in listed if item["number"] == 1]
    assert item["head_sha"] == new_head
    assert (item["review"]["run_id"], item["review"]["state"]) == (submitted["run_id"], "outdated")


@pytest.mark.parametrize(
    ("change", "state"),
    [
        (lambda fake: fake.merge_pull_request(REVIEW_APP_ID, 1), "merged"),
        (lambda fake: fake.close_pull_request(REVIEW_APP_ID, 1), "closed"),
    ],
    ids=["merged", "closed"],
)
def test_freshness_reports_closed_and_merged_pull_requests(
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    repository_id: str,
    change: Callable[[Any], None],
    state: str,
) -> None:
    submitted = review(octocat, run_worker_once, repository_id)

    change(get_fake_github())

    assert fresh(octocat, submitted["run_id"]) == {
        "pull_request_state": state,
        "draft": False,
        "current_head_sha": HEAD,
        "outdated": False,
    }


def test_each_check_reads_the_pull_request_once(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    run = add_review(db, repository_id)
    fake = get_fake_github()
    fake.calls.clear()

    fresh(octocat, run.id)
    assert dict(fake.calls) == {"get_pull_request": 1}
    fresh(octocat, run.id)
    assert dict(fake.calls) == {"get_pull_request": 2}


def test_github_unavailable(db: Session, octocat: TestClient, repository_id: str) -> None:
    run = add_review(db, repository_id)
    get_fake_github().set_unavailable(True)

    response = freshness(octocat, run.id)

    assert error_code(response) == (502, "github_unavailable")
    assert response.json()["error"]["retryable"] is True


def test_revoked_authorization_needs_sign_in(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    run = add_review(db, repository_id)
    get_fake_github().revoke_authorization("octocat")

    assert error_code(freshness(octocat, run.id)) == (401, "github_sign_in_required")


def test_refused_read_is_access_denied(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    private_id = connect(
        octocat, run_worker_once, REVIEW_APP_PRIVATE_ID, accept_external_processing=True
    )
    run = add_review(db, private_id, head_sha=commit_sha(REVIEW_APP_PRIVATE_ID, "pr-1"))
    get_fake_github().revoke_access("octocat", REVIEW_APP_PRIVATE_ID)

    assert error_code(freshness(octocat, run.id)) == (409, "github_access_denied")


def test_any_other_github_error_is_access_denied(
    db: Session, octocat: TestClient, repository_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = add_review(db, repository_id)

    def get_pull_request(user_token: str, full_name: str, number: int) -> PullRequest:
        raise GitHubError("GitHub returned 451")

    monkeypatch.setattr(get_fake_github(), "get_pull_request", get_pull_request)

    assert error_code(freshness(octocat, run.id)) == (409, "github_access_denied")


def test_an_answer_has_no_freshness(octocat: TestClient, repository_id: str) -> None:
    asked = octocat.post(
        "/v1/analysis-runs",
        json={"repository_id": repository_id, "question": "Where is can_write defined?"},
    )
    assert asked.status_code == 202, asked.text
    fake = get_fake_github()
    fake.calls.clear()

    assert error_code(freshness(octocat, asked.json()["run_id"])) == (404, "not_found")
    assert fake.calls["get_pull_request"] == 0


# Reuse --------------------------------------------------------------------------------------


@pytest.mark.parametrize("job_status", ["queued", "running", "retry_wait", "succeeded"])
def test_review_of_the_same_head_is_reused(
    db: Session, octocat: TestClient, repository_id: str, job_status: str
) -> None:
    run = add_review(db, repository_id, job_status=job_status)

    response = request(octocat, repository_id)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "run_id": str(run.id),
        "job_id": str(run.job_id),
        "status": job_status,
        "reused": True,
        "snapshot_id": None,
        "pull_request_number": 1,
        "head_sha": HEAD,
        "result_url": f"/v1/analysis-runs/{run.id}",
        "events_url": f"/v1/jobs/{run.job_id}/events",
    }
    assert run_count(db) == 1
    assert reviews_used(octocat) == 0
    assert submit_audits(db) == [
        (str(run.id), {"pull_request_number": 1, "mode": "reuse", "reused": True})
    ]


def test_requested_review_is_reused_while_waiting_and_once_finished(
    octocat: TestClient, run_worker_once: Callable[[], bool], repository_id: str
) -> None:
    first = request(octocat, repository_id, mode="reuse")
    assert first.status_code == 202, first.text
    submitted = first.json()

    waiting = request(octocat, repository_id)
    drain(run_worker_once)
    finished = request(octocat, repository_id)

    for response, status in ((waiting, "queued"), (finished, "succeeded")):
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["run_id"], body["job_id"]) == (submitted["run_id"], submitted["job_id"])
        assert (body["status"], body["reused"]) == (status, True)
    assert reviews_used(octocat) == 1


def test_newest_review_of_the_head_is_reused(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    now = datetime.now(UTC)
    add_review(db, repository_id, created_at=now - timedelta(hours=2))
    newest = add_review(db, repository_id, created_at=now - timedelta(hours=1))
    # Reviews of other pull requests or other heads are never reused.
    add_review(db, repository_id, number=2, head_sha=HEAD)
    add_review(db, repository_id, head_sha=commit_sha(REVIEW_APP_ID, "pr-2"))

    response = request(octocat, repository_id)

    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == str(newest.id)


@pytest.mark.parametrize(
    ("job_status", "age"),
    [("failed", timedelta(0)), ("canceled", timedelta(0)), ("succeeded", timedelta(days=31))],
    ids=["failed", "canceled", "expired"],
)
def test_failed_and_expired_reviews_are_not_reused(
    db: Session, octocat: TestClient, repository_id: str, job_status: str, age: timedelta
) -> None:
    earlier = add_review(
        db, repository_id, job_status=job_status, created_at=datetime.now(UTC) - age
    )

    response = request(octocat, repository_id)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["reused"] is False
    assert body["run_id"] != str(earlier.id)
    assert run_count(db) == 2
    assert reviews_used(octocat) == 1
    assert submit_audits(db) == [
        (body["run_id"], {"pull_request_number": 1, "mode": "reuse", "reused": False})
    ]


def test_new_mode_creates_a_separate_review(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], repository_id: str
) -> None:
    submitted = review(octocat, run_worker_once, repository_id)

    response = request(octocat, repository_id, mode="new")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["reused"] is False
    assert body["run_id"] != submitted["run_id"]
    assert body["head_sha"] == HEAD
    assert run_count(db) == 2
    assert reviews_used(octocat) == 2
    assert submit_audits(db)[-1] == (
        body["run_id"],
        {"pull_request_number": 1, "mode": "new", "reused": False},
    )


def test_reused_response_is_replayed_with_its_status(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    run = add_review(db, repository_id)

    responses = [request(octocat, repository_id, key="reuse-once") for _ in range(3)]

    assert {response.status_code for response in responses} == {200}
    assert {response.json()["run_id"] for response in responses} == {str(run.id)}
    assert all(response.json()["reused"] is True for response in responses)
    # The replays are answered from the idempotency record.
    assert len(submit_audits(db)) == 1


def test_unknown_mode_is_invalid(db: Session, octocat: TestClient, repository_id: str) -> None:
    response = request(octocat, repository_id, mode="sometimes")

    assert error_code(response) == (422, "invalid_request")
    assert run_count(db) == 0


def test_concurrent_requests_for_one_head_create_one_review(
    db: Session,
    signed_in: Callable[[str], TestClient],
    repository_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two sessions of the same user, so the requests share no connection or cookie jar.
    clients = [signed_in("octocat"), signed_in("octocat")]
    fake = get_fake_github()
    original = fake.get_pull_request
    # Both requests read the pull request before either one goes on to look for a review.
    both_read = threading.Barrier(2, timeout=WAIT_SECONDS)

    def get_pull_request(user_token: str, full_name: str, number: int) -> PullRequest:
        pull = original(user_token, full_name, number)
        both_read.wait()
        return pull

    monkeypatch.setattr(fake, "get_pull_request", get_pull_request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(request, client, repository_id, key=f"request-{index}")
            for index, client in enumerate(clients)
        ]
        responses = [future.result(timeout=WAIT_SECONDS) for future in futures]

    assert sorted(response.status_code for response in responses) == [200, 202], [
        response.text for response in responses
    ]
    assert len({response.json()["run_id"] for response in responses}) == 1
    assert run_count(db) == 1
    assert reviews_used(clients[0]) == 1


# A new head, and closed or merged pull requests ----------------------------------------------


def test_after_a_push_a_review_of_the_new_head_is_created(
    octocat: TestClient, run_worker_once: Callable[[], bool], repository_id: str
) -> None:
    earlier = review(octocat, run_worker_once, repository_id)
    new_head = get_fake_github().push_to_pull_request(REVIEW_APP_ID, 1)

    response = request(octocat, repository_id)

    assert response.status_code == 202, response.text
    later = response.json()
    assert later["reused"] is False
    assert later["run_id"] != earlier["run_id"]
    assert later["head_sha"] == new_head
    assert fresh(octocat, earlier["run_id"])["outdated"] is True
    assert fresh(octocat, later["run_id"])["outdated"] is False
    # The new head now has a review of its own, which is reused.
    again = request(octocat, repository_id)
    assert again.status_code == 200, again.text
    assert (again.json()["run_id"], again.json()["reused"]) == (later["run_id"], True)
    assert reviews_used(octocat) == 2


def test_review_of_a_merged_pull_request_stays_readable(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], repository_id: str
) -> None:
    submitted = review(octocat, run_worker_once, repository_id)
    before = octocat.get(submitted["result_url"]).json()

    get_fake_github().merge_pull_request(REVIEW_APP_ID, 1)

    assert octocat.get(submitted["result_url"]).json() == before
    assert fresh(octocat, submitted["run_id"])["pull_request_state"] == "merged"
    for mode in ("reuse", "new"):
        assert error_code(request(octocat, repository_id, mode=mode)) == (
            409,
            "pull_request_not_open",
        )
    assert run_count(db) == 1
    assert reviews_used(octocat) == 1
