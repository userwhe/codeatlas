"""Daily question allowance per workspace (FR-028). Days are UTC calendar days."""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from codeatlas.api.errors import ApiError
from codeatlas.config import get_settings
from codeatlas.models import UsageCounter


@dataclass(frozen=True)
class Usage:
    usage_date: date
    questions_used: int
    questions_limit: int
    resets_at: datetime


def next_reset(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(UTC).date() + timedelta(days=1), time(), tzinfo=UTC)


def reserve_question(db: Session, workspace_id: uuid.UUID, *, now: datetime | None = None) -> None:
    """Count one question for today, or raise 429 when the allowance is used up.

    The upsert only increments while the count is below the limit, so concurrent submissions
    cannot exceed it. Runs in the caller's transaction.
    """
    now = now or datetime.now(UTC)
    limit = get_settings().daily_question_limit
    today = now.astimezone(UTC).date()
    if limit > 0:
        statement = (
            insert(UsageCounter)
            .values(workspace_id=workspace_id, usage_date=today, questions_count=1)
            .on_conflict_do_update(
                index_elements=[UsageCounter.workspace_id, UsageCounter.usage_date],
                set_={"questions_count": UsageCounter.questions_count + 1},
                where=UsageCounter.questions_count < limit,
            )
            .returning(UsageCounter.questions_count)
        )
        if db.execute(statement).scalar_one_or_none() is not None:
            return
    resets_at = next_reset(now)
    raise ApiError(
        429,
        "daily_limit_reached",
        f"This workspace has used {limit} of {limit} questions today.",
        details={"resets_at": resets_at.isoformat().replace("+00:00", "Z"), "limit": limit},
    )


def usage(db: Session, workspace_id: uuid.UUID, *, now: datetime | None = None) -> Usage:
    now = now or datetime.now(UTC)
    today = now.astimezone(UTC).date()
    used = db.scalar(
        select(UsageCounter.questions_count).where(
            UsageCounter.workspace_id == workspace_id, UsageCounter.usage_date == today
        )
    )
    return Usage(
        usage_date=today,
        questions_used=used or 0,
        questions_limit=get_settings().daily_question_limit,
        resets_at=next_reset(now),
    )
