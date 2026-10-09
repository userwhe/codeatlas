"""Routes: the workspace's daily allowances (contracts/http-api.md, "Account").

Reviews have their own allowance next to questions (specs/003-pr-review/contracts/http-api.md).
"""

from datetime import date, datetime

from fastapi import APIRouter
from pydantic import BaseModel

from codeatlas.api.deps import CurrentWorkspace, DbSession
from codeatlas.workspace.quotas import usage

router = APIRouter(tags=["account"])


class UsageOut(BaseModel):
    usage_date: date
    questions_used: int
    questions_limit: int
    reviews_used: int
    reviews_limit: int
    resets_at: datetime


@router.get("/usage")
def get_usage(db: DbSession, workspace: CurrentWorkspace) -> UsageOut:
    current = usage(db, workspace.id)
    return UsageOut(
        usage_date=current.usage_date,
        questions_used=current.questions_used,
        questions_limit=current.questions_limit,
        reviews_used=current.reviews_used,
        reviews_limit=current.reviews_limit,
        resets_at=current.resets_at,
    )
