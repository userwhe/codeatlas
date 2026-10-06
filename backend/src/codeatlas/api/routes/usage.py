"""Routes: the workspace's daily question allowance (contracts/http-api.md, "Account")."""

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
    resets_at: datetime


@router.get("/usage")
def get_usage(db: DbSession, workspace: CurrentWorkspace) -> UsageOut:
    current = usage(db, workspace.id)
    return UsageOut(
        usage_date=current.usage_date,
        questions_used=current.questions_used,
        questions_limit=current.questions_limit,
        resets_at=current.resets_at,
    )
