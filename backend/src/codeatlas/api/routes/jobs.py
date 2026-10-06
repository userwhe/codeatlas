"""Routes: jobs (see specs/001-repository-qa/contracts/http-api.md)."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from codeatlas.api.deps import CurrentUser, CurrentWorkspace, DbSession, RequestId
from codeatlas.api.errors import not_found
from codeatlas.jobs.queue import queued_behind
from codeatlas.models import Job, JobEvent, Repository, User, Workspace
from codeatlas.workspace.audit import deny

router = APIRouter(tags=["jobs"])

JobKind = Literal["index_repository", "answer_question"]
JobStatus = Literal["queued", "running", "retry_wait", "succeeded", "failed", "canceled"]
JobEventType = Literal[
    "queued",
    "stage_started",
    "stage_completed",
    "retry_scheduled",
    "succeeded",
    "failed",
    "canceled",
]
MAX_EVENTS_PER_PAGE = 200


class JobError(BaseModel):
    code: str
    message: str
    retryable: bool


class JobResponse(BaseModel):
    id: uuid.UUID
    kind: JobKind
    status: JobStatus
    attempt: int
    queued_behind: int | None
    error: JobError | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobEventResponse(BaseModel):
    seq: int
    event_type: JobEventType
    stage: str | None
    message: str
    data: dict[str, Any]
    created_at: datetime


class JobEventsResponse(BaseModel):
    job_status: JobStatus
    items: list[JobEventResponse]
    next_after: int


def job_error(job: Job) -> JobError | None:
    if job.error_code is None:
        return None
    return JobError(
        code=job.error_code,
        message=job.error_message or "",
        retryable=bool(job.error_retryable),
    )


def _get_scoped_job(
    db: DbSession, user: User, workspace: Workspace, job_id: str, request_id: str | None
) -> Job:
    try:
        parsed = uuid.UUID(job_id)
    except ValueError:
        raise not_found() from None
    row = db.execute(
        select(Job, Repository.deleted_at)
        .join(Repository, Repository.id == Job.repository_id)
        .where(Job.id == parsed)
    ).first()
    if row is None:
        raise not_found()
    job, repository_deleted_at = row
    if job.workspace_id != workspace.id or repository_deleted_at is not None:
        raise deny(
            actor_user_id=user.id,
            workspace_id=workspace.id,
            resource_type="job",
            resource_id=str(parsed),
            request_id=request_id,
        )
    return job


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
) -> JobResponse:
    job = _get_scoped_job(db, user, workspace, job_id, request_id)
    return JobResponse(
        id=job.id,
        kind=cast(JobKind, job.kind),
        status=cast(JobStatus, job.status),
        attempt=job.attempt,
        queued_behind=queued_behind(db, job) if job.status == "queued" else None,
        error=job_error(job),
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


@router.get("/jobs/{job_id}/events")
def list_job_events(
    job_id: str,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
    after: Annotated[int, Query(ge=0)] = 0,
) -> JobEventsResponse:
    job = _get_scoped_job(db, user, workspace, job_id, request_id)
    events = db.scalars(
        select(JobEvent)
        .where(JobEvent.job_id == job.id, JobEvent.seq > after)
        .order_by(JobEvent.seq)
        .limit(MAX_EVENTS_PER_PAGE)
    ).all()
    items = [
        JobEventResponse(
            seq=event.seq,
            event_type=cast(JobEventType, event.event_type),
            stage=event.stage,
            message=event.message,
            data=event.data,
            created_at=event.created_at,
        )
        for event in events
    ]
    return JobEventsResponse(
        job_status=cast(JobStatus, job.status),
        items=items,
        next_after=items[-1].seq if items else after,
    )
