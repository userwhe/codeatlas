"""Routes: questions and answers (contracts/http-api.md, "Questions").

A question and its answer are stored as an analysis run of kind `repository_qa`.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from codeatlas.api.deps import CurrentUser, CurrentWorkspace, DbSession, RequestId
from codeatlas.api.pagination import PageParams, next_cursor, page_params
from codeatlas.api.routes.jobs import JobError, job_error
from codeatlas.models import AnalysisRun, Job
from codeatlas.qa import runs
from codeatlas.qa.schema import AnswerOutput, Claim
from codeatlas.workspace import repositories as repos
from codeatlas.workspace.idempotency import run_idempotent

router = APIRouter(tags=["questions"])

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
RunStatus = Literal["queued", "running", "retry_wait", "succeeded", "failed", "canceled"]


class TargetIn(BaseModel):
    snapshot_id: uuid.UUID | None = None


class SubmitIn(BaseModel):
    repository_id: uuid.UUID
    kind: Literal["repository_qa"] = "repository_qa"
    target: TargetIn = Field(default_factory=TargetIn)
    question: str


class SubmitOut(BaseModel):
    run_id: uuid.UUID
    job_id: uuid.UUID
    status: RunStatus
    snapshot_id: uuid.UUID
    result_url: str
    events_url: str


class ClaimOut(BaseModel):
    text: str
    kind: Literal["fact", "inference"]
    citations: list[str]


class AnswerOut(BaseModel):
    summary: str
    claims: list[ClaimOut]
    gaps: list[str]


class CitationOut(BaseModel):
    label: str
    source_type: Literal["symbol", "code", "doc"]
    path: str
    commit_sha: str
    start_line: int
    end_line: int
    excerpt: str
    view_url: str


class RunOut(BaseModel):
    id: uuid.UUID
    kind: Literal["repository_qa"]
    status: RunStatus
    quality_state: Literal["answered", "insufficient_evidence"] | None
    repository_id: uuid.UUID
    snapshot_id: uuid.UUID
    commit_sha: str
    question: str
    answer: AnswerOut | None
    citations: list[CitationOut]
    error: JobError | None
    job_id: uuid.UUID | None
    created_at: datetime
    completed_at: datetime | None


class RunSummaryOut(BaseModel):
    id: uuid.UUID
    question: str
    status: RunStatus
    quality_state: Literal["answered", "insufficient_evidence"] | None
    commit_sha: str
    created_at: datetime


class RunPage(BaseModel):
    items: list[RunSummaryOut]
    next_cursor: str | None


def _status(job: Job | None) -> RunStatus:
    status = job.status if job is not None else "queued"
    return status  # type: ignore[return-value]


def _claim_out(claim: Claim) -> ClaimOut:
    return ClaimOut(text=claim.text, kind=claim.kind, citations=claim.evidence_ids)


def run_out(db: Session, run: AnalysisRun) -> RunOut:
    job = db.get(Job, run.job_id) if run.job_id is not None else None
    answer = AnswerOutput.model_validate(run.result) if run.result is not None else None
    return RunOut(
        id=run.id,
        kind="repository_qa",
        status=_status(job),
        quality_state=run.quality_state,
        repository_id=run.repository_id,
        snapshot_id=run.snapshot_id,
        commit_sha=run.commit_sha,
        question=run.question,
        answer=(
            AnswerOut(
                summary=answer.summary,
                claims=[_claim_out(claim) for claim in answer.claims],
                gaps=answer.gaps,
            )
            if answer is not None
            else None
        ),
        citations=[CitationOut(**item) for item in runs.citations(db, run)],
        error=job_error(job) if job is not None else None,
        job_id=run.job_id,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


@router.post("/analysis-runs", status_code=202, response_model=SubmitOut)
def submit_question(
    body: SubmitIn,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    def operation() -> tuple[int, dict[str, Any]]:
        run, job = runs.submit(
            db,
            user=user,
            workspace=workspace,
            repository_id=body.repository_id,
            snapshot_id=body.target.snapshot_id,
            question=body.question,
            request_id=request_id,
        )
        db.flush()
        out = SubmitOut(
            run_id=run.id,
            job_id=job.id,
            status=_status(job),
            snapshot_id=run.snapshot_id,
            result_url=f"/v1/analysis-runs/{run.id}",
            events_url=f"/v1/jobs/{job.id}/events",
        )
        return 202, out.model_dump(mode="json")

    status, payload = run_idempotent(
        db,
        workspace_id=workspace.id,
        route="POST /v1/analysis-runs",
        key=idempotency_key,
        payload=body.model_dump(mode="json"),
        operation=operation,
    )
    db.commit()
    return JSONResponse(status_code=status, content=payload)


@router.get("/analysis-runs")
def list_runs(
    repository_id: Annotated[uuid.UUID, Query()],
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
    page: Annotated[PageParams, Depends(page_params)],
) -> RunPage:
    # Questions and answers are content: 403 while the repository's access is lost (FR-014).
    repository = repos.get_scoped(
        db,
        user=user,
        workspace=workspace,
        repository_id=repository_id,
        request_id=request_id,
        content=True,
    )
    rows = runs.list_for_repository(db, repository.id, offset=page.offset, limit=page.limit + 1)
    window = rows[: page.limit]
    items = []
    for run in window:
        job = db.get(Job, run.job_id) if run.job_id is not None else None
        items.append(
            RunSummaryOut(
                id=run.id,
                question=run.question,
                status=_status(job),
                quality_state=run.quality_state,
                commit_sha=run.commit_sha,
                created_at=run.created_at,
            )
        )
    return RunPage(items=items, next_cursor=next_cursor(page, len(window), len(rows) > page.limit))


@router.get("/analysis-runs/{run_id}")
def get_run(
    run_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
) -> RunOut:
    run = runs.get_scoped(db, user=user, workspace=workspace, run_id=run_id, request_id=request_id)
    return run_out(db, run)
