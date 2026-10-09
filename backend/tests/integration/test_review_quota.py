"""Integration tests for the daily review allowance (T009, FR-027, data-model.md, research R9)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.api.errors import ApiError
from codeatlas.config import Settings
from codeatlas.models import Membership, UsageCounter, User, Workspace
from codeatlas.workspace import quotas

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 6, 9, 30, tzinfo=UTC)
TODAY = NOW.date()
YESTERDAY = TODAY - timedelta(days=1)


@pytest.fixture
def workspace(db: Session) -> Workspace:
    workspace = Workspace(name="octocat")
    db.add(workspace)
    db.commit()
    return workspace


def counts(db: Session, workspace: Workspace) -> dict[str, tuple[int, int]]:
    """Each day's `(questions_count, reviews_count)`, keyed by ISO date."""
    rows = db.execute(
        select(UsageCounter.usage_date, UsageCounter.questions_count, UsageCounter.reviews_count)
        .where(UsageCounter.workspace_id == workspace.id)
        .order_by(UsageCounter.usage_date)
    )
    return {day.isoformat(): (questions, reviews) for day, questions, reviews in rows}


def test_reserve_review_counts_up_to_the_daily_limit(
    db: Session, workspace: Workspace, settings: Settings
) -> None:
    limit = settings.daily_review_limit
    for _ in range(limit):
        quotas.reserve_review(db, workspace.id, now=NOW)
    db.commit()

    with pytest.raises(ApiError) as refused:
        quotas.reserve_review(db, workspace.id, now=NOW)
    db.rollback()

    error = refused.value
    assert (error.status, error.code) == (429, "daily_limit_reached")
    assert error.message == f"This workspace has used {limit} of {limit} reviews today."
    assert error.details == {
        "allowance": "reviews",
        "limit": limit,
        "resets_at": "2026-10-07T00:00:00Z",
    }
    assert counts(db, workspace) == {"2026-10-06": (0, limit)}


def test_a_zero_limit_refuses_every_review(
    db: Session, workspace: Workspace, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "daily_review_limit", 0)

    with pytest.raises(ApiError) as refused:
        quotas.reserve_review(db, workspace.id, now=NOW)

    assert refused.value.details["allowance"] == "reviews"
    assert counts(db, workspace) == {}


def test_questions_and_reviews_have_separate_allowances(
    db: Session, workspace: Workspace, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "daily_question_limit", 1)
    monkeypatch.setattr(settings, "daily_review_limit", 1)

    quotas.reserve_question(db, workspace.id, now=NOW)
    quotas.reserve_review(db, workspace.id, now=NOW)
    db.commit()
    assert counts(db, workspace) == {"2026-10-06": (1, 1)}

    with pytest.raises(ApiError) as questions_refused:
        quotas.reserve_question(db, workspace.id, now=NOW)
    db.rollback()
    with pytest.raises(ApiError) as reviews_refused:
        quotas.reserve_review(db, workspace.id, now=NOW)
    db.rollback()

    assert questions_refused.value.details["allowance"] == "questions"
    assert reviews_refused.value.details["allowance"] == "reviews"
    assert counts(db, workspace) == {"2026-10-06": (1, 1)}


def test_reserve_question_refusal_names_the_question_allowance(
    db: Session, workspace: Workspace, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "daily_question_limit", 2)
    for _ in range(2):
        quotas.reserve_question(db, workspace.id, now=NOW)
    db.commit()

    with pytest.raises(ApiError) as refused:
        quotas.reserve_question(db, workspace.id, now=NOW)

    error = refused.value
    assert (error.status, error.code) == (429, "daily_limit_reached")
    assert error.message == "This workspace has used 2 of 2 questions today."
    assert error.details == {
        "allowance": "questions",
        "limit": 2,
        "resets_at": "2026-10-07T00:00:00Z",
    }


def test_refund_review_decrements_that_day_and_never_below_zero(
    db: Session, workspace: Workspace
) -> None:
    quotas.reserve_review(db, workspace.id, now=NOW - timedelta(days=1))
    for _ in range(2):
        quotas.reserve_review(db, workspace.id, now=NOW)
    quotas.reserve_question(db, workspace.id, now=NOW)
    db.commit()

    quotas.refund_review(db, workspace.id, YESTERDAY)
    db.commit()
    assert counts(db, workspace) == {"2026-10-05": (0, 0), "2026-10-06": (1, 2)}

    quotas.refund_review(db, workspace.id, YESTERDAY)
    quotas.refund_review(db, workspace.id, TODAY - timedelta(days=2))
    db.commit()
    assert counts(db, workspace) == {"2026-10-05": (0, 0), "2026-10-06": (1, 2)}

    for _ in range(2):
        quotas.refund_review(db, workspace.id, TODAY)
    db.commit()
    assert counts(db, workspace) == {"2026-10-05": (0, 0), "2026-10-06": (1, 0)}


def test_usage_reports_both_allowances(
    db: Session, signed_in: Callable[[str], TestClient], settings: Settings
) -> None:
    octocat = signed_in("octocat")
    workspace_id = db.scalars(
        select(Membership.workspace_id)
        .join(User, User.id == Membership.user_id)
        .where(User.github_login == "octocat")
    ).one()
    quotas.reserve_review(db, workspace_id)
    quotas.reserve_review(db, workspace_id)
    quotas.reserve_question(db, workspace_id)
    db.commit()

    response = octocat.get("/v1/usage")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "usage_date": body["usage_date"],
        "questions_used": 1,
        "questions_limit": settings.daily_question_limit,
        "reviews_used": 2,
        "reviews_limit": settings.daily_review_limit,
        "resets_at": body["resets_at"],
    }
    assert body["resets_at"].endswith("T00:00:00Z")
