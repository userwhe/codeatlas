"""Integration tests for requesting a pull request review (T026, research R2 and R9).

`octocat` connects `octo-org/review-app` and requests reviews through `POST /v1/analysis-runs`.
The review job itself is not run here, except to export a finished review as Markdown: these
tests check the stored run, its job, the audit event, the allowance, and every refusal.
"""

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from codeatlas.db import session_scope
from codeatlas.github.fake import (
    OVERSIZED_ID,
    REVIEW_APP_ID,
    commit_sha,
    get_fake_github,
)
from codeatlas.jobs import queue
from codeatlas.models import AnalysisRun, AuditEvent, Job, Repository
from codeatlas.workspace import access
from tests.integration.test_questions import drain

pytestmark = pytest.mark.integration

MAX_BODY_CHARS = 8000


def connect(
    client: TestClient,
    run_worker_once: Callable[[], bool],
    github_id: int = REVIEW_APP_ID,
    *,
    accept_external_processing: bool = False,
) -> str:
    """Connect a repository, run its first indexing, and return its ID."""
    response = client.post(
        "/v1/repositories",
        json={
            "github_repository_id": github_id,
            "accept_external_processing": accept_external_processing,
        },
    )
    assert response.status_code == 202, response.text
    drain(run_worker_once)
    repository_id: str = response.json()["repository"]["id"]
    return repository_id


def request_review(
    client: TestClient, repository_id: str, number: int = 1, **extra: Any
) -> Response:
    return client.post(
        "/v1/analysis-runs",
        json={
            "repository_id": repository_id,
            "kind": "pull_request_review",
            "target": {"pull_request_number": number},
        },
        **extra,
    )


def load_repository(db: Session, repository_id: str) -> Repository:
    db.expire_all()
    repository = db.get(Repository, uuid.UUID(repository_id))
    assert repository is not None
    return repository


def run_count(db: Session) -> int | None:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(AnalysisRun))


def error_code(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


@pytest.fixture
def repository_id(octocat: TestClient, run_worker_once: Callable[[], bool]) -> str:
    return connect(octocat, run_worker_once)


def test_review_request_pins_the_pull_request(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    head = commit_sha(REVIEW_APP_ID, "pr-1")

    response = request_review(octocat, repository_id)

    assert response.status_code == 202, response.text
    body = response.json()
    run_id, job_id = body["run_id"], body["job_id"]
    assert body == {
        "run_id": run_id,
        "job_id": job_id,
        "status": "queued",
        "reused": False,
        "snapshot_id": None,
        "pull_request_number": 1,
        "head_sha": head,
        "result_url": f"/v1/analysis-runs/{run_id}",
        "events_url": f"/v1/jobs/{job_id}/events",
    }

    run = db.get(AnalysisRun, uuid.UUID(run_id))
    assert run is not None
    assert run.kind == "pull_request_review"
    assert run.pull_request_number == 1
    assert run.base_sha == commit_sha(REVIEW_APP_ID, "initial")
    assert run.head_sha == run.commit_sha == head
    assert run.merge_base_sha is None
    assert (run.snapshot_id, run.index_version, run.question) == (None, None, None)
    assert run.prompt_version == "review-v1"
    assert run.expires_at - run.created_at == timedelta(days=30)
    assert run.job_id == uuid.UUID(job_id)
    assert run.pull_request == {
        "title": "Simplify write checks",
        "body": (
            "Anyone listed on a repository can already see it, so the role lookup in "
            "`can_write` is redundant. This drops it."
        ),
        "author": "hubot",
        "base_ref": "main",
        "head_ref": "simplify-write-checks",
        "head_repository": "octo-org/review-app",
        "is_fork": False,
        "draft": False,
        "html_url": "https://github.com/octo-org/review-app/pull/1",
        "additions": 2,
        "deletions": 2,
        "changed_files": 1,
    }

    job = db.get(Job, uuid.UUID(job_id))
    assert job is not None
    assert (job.kind, job.dedupe_key, job.status, job.trigger) == (
        "review_pull_request",
        f"run:{run_id}",
        "queued",
        "user",
    )
    assert job.analysis_run_id == run.id

    (audit,) = db.scalars(
        select(AuditEvent).where(AuditEvent.action == "pull_request_review_submit")
    ).all()
    assert (audit.outcome, audit.resource_type, audit.resource_id) == (
        "success",
        "analysis_run",
        run_id,
    )
    assert audit.detail == {"pull_request_number": 1}
    assert octocat.get("/v1/usage").json()["reviews_used"] == 1


def test_long_description_is_truncated(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    get_fake_github().set_pull_request_body(REVIEW_APP_ID, 1, "x" * 10_000)

    response = request_review(octocat, repository_id)

    assert response.status_code == 202, response.text
    run = db.get(AnalysisRun, uuid.UUID(response.json()["run_id"]))
    assert run is not None and run.pull_request is not None
    assert run.pull_request["body"] == "x" * MAX_BODY_CHARS


def test_fork_pull_request_records_its_head_repository(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    response = request_review(octocat, repository_id, 3)

    assert response.status_code == 202, response.text
    run = db.get(AnalysisRun, uuid.UUID(response.json()["run_id"]))
    assert run is not None and run.pull_request is not None
    assert run.pull_request["is_fork"] is True
    assert run.pull_request["head_repository"] == "hubot/review-app"


def test_submission_makes_one_github_call(octocat: TestClient, repository_id: str) -> None:
    fake = get_fake_github()
    fake.calls.clear()

    assert request_review(octocat, repository_id).status_code == 202

    assert dict(fake.calls) == {"get_pull_request": 1}


@pytest.mark.parametrize(
    ("change", "number"),
    [
        (lambda fake: None, 9),
        (lambda fake: fake.merge_pull_request(REVIEW_APP_ID, 2), 2),
    ],
    ids=["closed", "merged"],
)
def test_only_open_pull_requests_are_reviewed(
    db: Session,
    octocat: TestClient,
    repository_id: str,
    change: Callable[[Any], None],
    number: int,
) -> None:
    change(get_fake_github())

    response = request_review(octocat, repository_id, number)

    assert error_code(response) == (409, "pull_request_not_open")
    assert run_count(db) == 0
    assert octocat.get("/v1/usage").json()["reviews_used"] == 0


def test_unknown_pull_request(db: Session, octocat: TestClient, repository_id: str) -> None:
    response = request_review(octocat, repository_id, 99)

    assert error_code(response) == (404, "pull_request_not_found")
    assert run_count(db) == 0


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "pull_request_review", "target": {"pull_request_number": 1}, "question": "Why?"},
        {"kind": "pull_request_review", "target": {"pull_request_number": 0}},
        {"kind": "pull_request_review", "target": {"pull_request_number": -3}},
        {"kind": "pull_request_review", "target": {}},
        {"kind": "pull_request_review"},
        {"kind": "repository_qa"},
        {"kind": "repository_qa", "target": {"pull_request_number": 1}, "question": "Why?"},
        {"kind": "something_else", "question": "Why?"},
    ],
    ids=[
        "review-with-question",
        "zero",
        "negative",
        "no-number",
        "no-target",
        "question-missing",
        "question-with-number",
        "unknown-kind",
    ],
)
def test_fields_are_checked_per_kind(
    db: Session, octocat: TestClient, repository_id: str, body: dict[str, Any]
) -> None:
    fake = get_fake_github()
    fake.calls.clear()

    response = octocat.post("/v1/analysis-runs", json={"repository_id": repository_id, **body})

    assert error_code(response) == (422, "invalid_request")
    assert run_count(db) == 0
    assert fake.calls["get_pull_request"] == 0


def test_lost_repository_is_refused_before_any_allowance(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    repository = load_repository(db, repository_id)
    assert access.mark_access_lost(db, repository, "app_uninstalled", trigger="notification")
    db.commit()
    fake = get_fake_github()
    fake.calls.clear()

    response = request_review(octocat, repository_id)

    assert error_code(response) == (403, "repository_access_lost")
    assert run_count(db) == 0
    assert octocat.get("/v1/usage").json()["reviews_used"] == 0
    assert fake.calls["get_pull_request"] == 0


def test_rejected_repository(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = connect(octocat, run_worker_once, OVERSIZED_ID)
    assert octocat.get(f"/v1/repositories/{repository_id}").json()["state"] == "rejected"

    response = request_review(octocat, repository_id)

    assert error_code(response) == (409, "repository_rejected")
    assert run_count(db) == 0


def test_repository_paused_for_the_disclosure(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    repository = load_repository(db, repository_id)
    assert access.pause(db, repository, "external_processing_not_accepted")
    db.commit()

    response = request_review(octocat, repository_id)

    assert error_code(response) == (422, "external_processing_not_accepted")
    assert run_count(db) == 0


def test_revoked_authorization_needs_sign_in(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    get_fake_github().revoke_authorization("octocat")

    response = request_review(octocat, repository_id)

    assert error_code(response) == (401, "github_sign_in_required")
    assert run_count(db) == 0


def test_github_unavailable(db: Session, octocat: TestClient, repository_id: str) -> None:
    get_fake_github().set_unavailable(True)

    response = request_review(octocat, repository_id)

    assert error_code(response) == (502, "github_unavailable")
    assert response.json()["error"]["retryable"] is True
    assert run_count(db) == 0


def test_daily_review_allowance(octocat: TestClient, repository_id: str) -> None:
    statuses = [request_review(octocat, repository_id).status_code for _ in range(10)]
    refused = request_review(octocat, repository_id)

    assert statuses == [202] * 10
    assert error_code(refused) == (429, "daily_limit_reached")
    assert refused.json()["error"]["details"]["allowance"] == "reviews"
    usage = octocat.get("/v1/usage").json()
    assert (usage["reviews_used"], usage["reviews_limit"]) == (10, 10)
    # Questions have their own allowance.
    assert usage["questions_used"] == 0


def test_same_idempotency_key_creates_one_review(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    headers = {"Idempotency-Key": "review-once"}
    fake = get_fake_github()
    fake.calls.clear()

    responses = [request_review(octocat, repository_id, headers=headers) for _ in range(100)]

    assert {response.status_code for response in responses} == {202}
    assert len({response.json()["run_id"] for response in responses}) == 1
    assert run_count(db) == 1
    review_jobs = select(func.count()).select_from(Job).where(Job.kind == "review_pull_request")
    assert db.scalar(review_jobs) == 1
    assert fake.calls["get_pull_request"] == 1
    assert octocat.get("/v1/usage").json()["reviews_used"] == 1


def test_second_review_waits_while_one_runs(octocat: TestClient, repository_id: str) -> None:
    request_review(octocat, repository_id, 1)
    second = request_review(octocat, repository_id, 2).json()
    with session_scope() as db:
        claim = queue.claim_next(db)
        assert claim is not None and claim.kind == "review_pull_request"

    job = octocat.get(f"/v1/jobs/{second['job_id']}").json()

    assert (job["kind"], job["status"], job["queued_behind"]) == (
        "review_pull_request",
        "queued",
        1,
    )


def test_question_requests_are_unchanged(octocat: TestClient, repository_id: str) -> None:
    response = octocat.post(
        "/v1/analysis-runs",
        json={"repository_id": repository_id, "question": "Where is can_write defined?"},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    active = octocat.get(f"/v1/repositories/{repository_id}").json()["active_snapshot"]
    assert body["reused"] is False
    assert body["snapshot_id"] == active["id"]
    assert (body["pull_request_number"], body["head_sha"]) == (None, None)


def test_run_history_filters_by_kind(octocat: TestClient, repository_id: str) -> None:
    review = request_review(octocat, repository_id).json()
    question = octocat.post(
        "/v1/analysis-runs",
        json={"repository_id": repository_id, "question": "Where is can_write defined?"},
    ).json()

    def listed(**params: str) -> list[dict[str, Any]]:
        response = octocat.get(
            "/v1/analysis-runs", params={"repository_id": repository_id, **params}
        )
        assert response.status_code == 200, response.text
        items: list[dict[str, Any]] = response.json()["items"]
        return items

    assert {(item["id"], item["kind"]) for item in listed()} == {
        (review["run_id"], "pull_request_review"),
        (question["run_id"], "repository_qa"),
    }
    (only_question,) = listed(kind="repository_qa")
    assert only_question["id"] == question["run_id"]
    (only_review,) = listed(kind="pull_request_review")
    assert only_review["id"] == review["run_id"]
    assert only_review["question"] is None
    assert only_review["commit_sha"] == commit_sha(REVIEW_APP_ID, "pr-1")
    invalid = octocat.get(
        "/v1/analysis-runs", params={"repository_id": repository_id, "kind": "other"}
    )
    assert error_code(invalid) == (422, "invalid_request")


def test_markdown_export(
    octocat: TestClient, run_worker_once: Callable[[], bool], repository_id: str
) -> None:
    head = commit_sha(REVIEW_APP_ID, "pr-1")
    submitted = request_review(octocat, repository_id).json()
    markdown_url = f"{submitted['result_url']}/markdown"

    assert error_code(octocat.get(markdown_url)) == (409, "review_not_finished")

    drain(run_worker_once)
    response = octocat.get(markdown_url)

    assert response.status_code == 200, response.text
    markdown = response.json()["markdown"]
    assert markdown.startswith(f"## CodeAtlas review of #1 at {head[:7]}\n\n**Overall risk: high**")
    assert "### Checklist\n\n- [ ] " in markdown
    assert "`app/auth/permissions.py`; R1)" in markdown
    assert "- `tests/test_permissions.py`: refers to `can_write` ([E" in markdown
    # The changed lines are cited at the merge base, and the candidate test at the head.
    blob = "https://github.com/octo-org/review-app/blob"
    merge_base = commit_sha(REVIEW_APP_ID, "initial")
    assert f"{blob}/{merge_base}/app/auth/permissions.py#L" in markdown
    assert f"{blob}/{head}/tests/test_permissions.py#L" in markdown

    nothing = request_review(octocat, repository_id, 4).json()
    drain(run_worker_once)
    response = octocat.get(f"{nothing['result_url']}/markdown")

    assert response.status_code == 200, response.text
    assert "Nothing to review: no changed file could be reviewed." in response.json()["markdown"]

    question = octocat.post(
        "/v1/analysis-runs",
        json={"repository_id": repository_id, "question": "Where is can_write defined?"},
    ).json()

    assert error_code(octocat.get(f"/v1/analysis-runs/{question['run_id']}/markdown")) == (
        404,
        "not_found",
    )
