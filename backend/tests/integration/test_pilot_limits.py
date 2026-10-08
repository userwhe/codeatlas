"""Integration tests for the pilot-wide daily limits (T017, FR-005, research R14).

The pilot-wide allowance is checked before the workspace allowance, in the same transaction, so
either refusal rolls back both counts.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.config import Settings
from codeatlas.github.fake import REVIEW_APP_ID
from codeatlas.models import PilotUsageCounter
from tests.integration.test_questions import ask, drain, indexed_repository
from tests.integration.test_review_submit import connect, request_review

pytestmark = pytest.mark.integration

WAIT_SECONDS = 60


@pytest.fixture
def pilot_limits(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(settings, "pilot_daily_question_limit", 2)
    monkeypatch.setattr(settings, "pilot_daily_review_limit", 1)
    return settings


def pilot_counts(db: Session) -> tuple[int, int]:
    """Today's pilot-wide `(questions_count, reviews_count)`; (0, 0) without a row."""
    db.expire_all()
    rows = db.execute(
        select(PilotUsageCounter.questions_count, PilotUsageCounter.reviews_count)
    ).all()
    assert len(rows) <= 1
    return (rows[0][0], rows[0][1]) if rows else (0, 0)


def usage(client: TestClient) -> tuple[int, int]:
    body = client.get("/v1/usage").json()
    return body["questions_used"], body["reviews_used"]


def error(response: Any) -> dict[str, Any]:
    assert response.status_code == 429, response.text
    body: dict[str, Any] = response.json()["error"]
    return body


# Questions ---------------------------------------------------------------------------------


def test_the_pilot_question_limit_counts_every_workspace(
    db: Session,
    signed_in: Callable[[str], TestClient],
    run_worker_once: Callable[[], bool],
    pilot_limits: Settings,
) -> None:
    octocat, hubot = signed_in("octocat"), signed_in("hubot")
    octocat_repository = indexed_repository(octocat, run_worker_once)
    hubot_repository = indexed_repository(hubot, run_worker_once)

    assert ask(octocat, octocat_repository).status_code == 202
    assert ask(hubot, hubot_repository).status_code == 202
    refused = ask(hubot, hubot_repository, "What does the README say about setup?")

    body = error(refused)
    assert (body["code"], body["retryable"]) == ("pilot_limit_reached", False)
    assert body["message"] == (
        "CodeAtlas has reached today's limit of 2 questions for all pilot users."
    )
    assert body["details"]["resets_at"].endswith("T00:00:00Z")
    assert (body["details"]["limit"], body["details"]["allowance"]) == (2, "questions")
    assert usage(hubot) == (1, 0)
    assert pilot_counts(db) == (2, 0)


def test_a_workspace_refusal_leaves_the_pilot_count(
    db: Session,
    signed_in: Callable[[str], TestClient],
    run_worker_once: Callable[[], bool],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "daily_question_limit", 1)
    octocat = signed_in("octocat")
    repository_id = indexed_repository(octocat, run_worker_once)
    assert ask(octocat, repository_id).status_code == 202

    refused = ask(octocat, repository_id, "What does the README say about setup?")

    assert error(refused)["code"] == "daily_limit_reached"
    assert pilot_counts(db) == (1, 0)
    assert usage(octocat) == (1, 0)


def test_both_allowances_used_up_reports_the_pilot_limit(
    db: Session,
    signed_in: Callable[[str], TestClient],
    run_worker_once: Callable[[], bool],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "daily_question_limit", 1)
    monkeypatch.setattr(settings, "pilot_daily_question_limit", 1)
    octocat = signed_in("octocat")
    repository_id = indexed_repository(octocat, run_worker_once)
    assert ask(octocat, repository_id).status_code == 202

    refused = ask(octocat, repository_id, "What does the README say about setup?")

    assert error(refused)["code"] == "pilot_limit_reached"
    assert pilot_counts(db) == (1, 0)
    assert usage(octocat) == (1, 0)


def test_concurrent_questions_never_exceed_the_pilot_limit(
    db: Session,
    signed_in: Callable[[str], TestClient],
    run_worker_once: Callable[[], bool],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "pilot_daily_question_limit", 5)
    octocat, hubot = signed_in("octocat"), signed_in("hubot")
    repositories = {
        "octocat": indexed_repository(octocat, run_worker_once),
        "hubot": indexed_repository(hubot, run_worker_once),
    }
    # One session per thread, so the requests share no connection or cookie jar.
    submitters = [
        (signed_in(login), repositories[login], f"Question {index} about {login}'s code?")
        for index, login in enumerate(["octocat", "hubot"] * 5)
    ]

    with ThreadPoolExecutor(max_workers=len(submitters)) as pool:
        futures = [
            pool.submit(ask, client, repository_id, question)
            for client, repository_id, question in submitters
        ]
        responses = [future.result(timeout=WAIT_SECONDS) for future in futures]

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [202] * 5 + [429] * 5, [response.text for response in responses]
    codes = {response.json()["error"]["code"] for response in responses if response.is_error}
    assert codes == {"pilot_limit_reached"}
    assert pilot_counts(db) == (5, 0)
    assert usage(octocat)[0] + usage(hubot)[0] == 5


def test_browsing_and_search_continue_after_the_limit(
    signed_in: Callable[[str], TestClient],
    run_worker_once: Callable[[], bool],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "pilot_daily_question_limit", 1)
    octocat = signed_in("octocat")
    repository_id = indexed_repository(octocat, run_worker_once)
    assert ask(octocat, repository_id).status_code == 202
    assert error(ask(octocat, repository_id, "Another question?"))["code"] == (
        "pilot_limit_reached"
    )

    listed = octocat.get("/v1/repositories")
    snapshot_id = octocat.get(f"/v1/repositories/{repository_id}").json()["active_snapshot"]["id"]
    searched = octocat.post(
        "/v1/search", json={"snapshot_id": snapshot_id, "query": "import", "mode": "text"}
    )

    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [repository_id]
    assert searched.status_code == 200, searched.text
    assert searched.json()["results"]


# Reviews -----------------------------------------------------------------------------------


def test_the_pilot_review_limit_counts_every_workspace(
    db: Session,
    signed_in: Callable[[str], TestClient],
    run_worker_once: Callable[[], bool],
    pilot_limits: Settings,
) -> None:
    octocat, hubot = signed_in("octocat"), signed_in("hubot")
    octocat_repository = connect(octocat, run_worker_once)
    hubot_repository = connect(hubot, run_worker_once, REVIEW_APP_ID)

    assert request_review(octocat, octocat_repository, 1).status_code == 202
    refused = request_review(hubot, hubot_repository, 1)

    body = error(refused)
    assert (body["code"], body["retryable"]) == ("pilot_limit_reached", False)
    assert body["message"] == (
        "CodeAtlas has reached today's limit of 1 reviews for all pilot users."
    )
    assert body["details"]["resets_at"].endswith("T00:00:00Z")
    assert (body["details"]["limit"], body["details"]["allowance"]) == (1, "reviews")
    assert usage(hubot) == (0, 0)
    assert pilot_counts(db) == (0, 1)


def test_a_workspace_review_refusal_leaves_the_pilot_count(
    db: Session,
    signed_in: Callable[[str], TestClient],
    run_worker_once: Callable[[], bool],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "daily_review_limit", 1)
    octocat = signed_in("octocat")
    repository_id = connect(octocat, run_worker_once)
    assert request_review(octocat, repository_id, 1).status_code == 202

    refused = request_review(octocat, repository_id, 5)

    assert error(refused)["code"] == "daily_limit_reached"
    assert pilot_counts(db) == (0, 1)
    assert usage(octocat) == (0, 1)


def test_nothing_to_review_refunds_both_counts(
    db: Session,
    signed_in: Callable[[str], TestClient],
    run_worker_once: Callable[[], bool],
    pilot_limits: Settings,
) -> None:
    octocat = signed_in("octocat")
    repository_id = connect(octocat, run_worker_once)
    response = request_review(octocat, repository_id, 4)
    assert response.status_code == 202, response.text
    assert pilot_counts(db) == (0, 1)
    assert usage(octocat) == (0, 1)

    drain(run_worker_once)

    run = octocat.get(f"/v1/analysis-runs/{response.json()['run_id']}").json()
    assert run["quality_state"] == "nothing_to_review"
    assert pilot_counts(db) == (0, 0)
    assert usage(octocat) == (0, 0)
    # The refund makes room for another review within the pilot-wide limit.
    assert request_review(octocat, repository_id, 1).status_code == 202
