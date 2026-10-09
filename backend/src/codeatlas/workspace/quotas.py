"""Daily question and review allowances per workspace (FR-028, and specs/003-pr-review FR-027).

Days are UTC calendar days.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import InstrumentedAttribute, Session

from codeatlas.api.errors import ApiError
from codeatlas.config import get_settings
from codeatlas.models import UsageCounter


@dataclass(frozen=True)
class Usage:
    usage_date: date
    questions_used: int
    questions_limit: int
    reviews_used: int
    reviews_limit: int
    resets_at: datetime


def next_reset(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(UTC).date() + timedelta(days=1), time(), tzinfo=UTC)


def _reserve(
    db: Session,
    workspace_id: uuid.UUID,
    now: datetime | None,
    *,
    counter: InstrumentedAttribute[int],
    limit: int,
    allowance: str,
    noun: str,
) -> None:
    """Count one use of an allowance for today, or raise 429 when it is used up.

    The upsert only increments while the count is below the limit, so concurrent submissions
    cannot exceed it. Runs in the caller's transaction.
    """
    now = now or datetime.now(UTC)
    today = now.astimezone(UTC).date()
    if limit > 0:
        statement = (
            insert(UsageCounter)
            .values({"workspace_id": workspace_id, "usage_date": today, counter.key: 1})
            .on_conflict_do_update(
                index_elements=[UsageCounter.workspace_id, UsageCounter.usage_date],
                set_={counter.key: counter + 1},
                where=counter < limit,
            )
            .returning(counter)
        )
        if db.execute(statement).scalar_one_or_none() is not None:
            return
    resets_at = next_reset(now)
    raise ApiError(
        429,
        "daily_limit_reached",
        f"This workspace has used {limit} of {limit} {noun} today.",
        details={
            "resets_at": resets_at.isoformat().replace("+00:00", "Z"),
            "limit": limit,
            "allowance": allowance,
        },
    )


def reserve_question(db: Session, workspace_id: uuid.UUID, *, now: datetime | None = None) -> None:
    """Count one question for today, or raise 429 when the allowance is used up."""
    _reserve(
        db,
        workspace_id,
        now,
        counter=UsageCounter.questions_count,
        limit=get_settings().daily_question_limit,
        allowance="questions",
        noun="questions",
    )


def reserve_review(db: Session, workspace_id: uuid.UUID, *, now: datetime | None = None) -> None:
    """Count one review for today, or raise 429 when the allowance is used up."""
    _reserve(
        db,
        workspace_id,
        now,
        counter=UsageCounter.reviews_count,
        limit=get_settings().daily_review_limit,
        allowance="reviews",
        noun="reviews",
    )


def refund_review(db: Session, workspace_id: uuid.UUID, usage_date: date) -> None:
    """Give back one review counted on `usage_date`, never going below zero.

    A review that ends `nothing_to_review` does not count (specs/003-pr-review FR-020). Runs in
    the caller's transaction.
    """
    db.execute(
        update(UsageCounter)
        .where(UsageCounter.workspace_id == workspace_id, UsageCounter.usage_date == usage_date)
        .values(reviews_count=func.greatest(UsageCounter.reviews_count - 1, 0))
    )


def usage(db: Session, workspace_id: uuid.UUID, *, now: datetime | None = None) -> Usage:
    now = now or datetime.now(UTC)
    today = now.astimezone(UTC).date()
    counts = db.execute(
        select(UsageCounter.questions_count, UsageCounter.reviews_count).where(
            UsageCounter.workspace_id == workspace_id, UsageCounter.usage_date == today
        )
    ).one_or_none()
    questions_used, reviews_used = counts if counts is not None else (0, 0)
    settings = get_settings()
    return Usage(
        usage_date=today,
        questions_used=questions_used,
        questions_limit=settings.daily_question_limit,
        reviews_used=reviews_used,
        reviews_limit=settings.daily_review_limit,
        resets_at=next_reset(now),
    )
