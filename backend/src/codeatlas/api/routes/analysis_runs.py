"""Routes: questions and answers (contracts/http-api.md, "Questions"), and pull request reviews
(specs/003-pr-review/contracts/http-api.md).

A question and its answer are stored as an analysis run of kind `repository_qa`, and a review as
one of kind `pull_request_review`.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from codeatlas.api.deps import CurrentUser, CurrentWorkspace, DbSession, RequestId
from codeatlas.api.errors import ApiError, not_found
from codeatlas.api.pagination import PageParams, next_cursor, page_params
from codeatlas.api.routes.jobs import JobError, job_error
from codeatlas.github.gateway import get_gateway
from codeatlas.models import AnalysisRun, Job, User, Workspace
from codeatlas.qa import runs
from codeatlas.qa.schema import AnswerOutput, Claim
from codeatlas.review import markdown, pulls
from codeatlas.review import runs as review_runs
from codeatlas.review.schema import ChangeKind, RiskBasis, RiskCategory, Severity
from codeatlas.workspace import repositories as repos
from codeatlas.workspace.idempotency import run_idempotent

router = APIRouter(tags=["questions"])

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
RunStatus = Literal["queued", "running", "retry_wait", "succeeded", "failed", "canceled"]
AnalysisKind = Literal["repository_qa", "pull_request_review"]
QuestionQuality = Literal["answered", "insufficient_evidence"]
ReviewQuality = Literal["reviewed", "nothing_to_review"]


class TargetIn(BaseModel):
    # Questions: the indexed version to ask about; the active one when omitted.
    snapshot_id: uuid.UUID | None = None
    # Reviews: the pull request to review.
    pull_request_number: int | None = Field(default=None, ge=1)


class SubmitIn(BaseModel):
    repository_id: uuid.UUID
    kind: AnalysisKind = "repository_qa"
    target: TargetIn = Field(default_factory=TargetIn)
    # Required for questions; must be absent for reviews.
    question: str | None = None
    # Reviews only: `reuse` (the default when absent) returns an existing review of the same head
    # commit that is waiting, running, or succeeded; `new` always creates one.
    mode: review_runs.SubmitMode | None = None


class SubmitOut(BaseModel):
    run_id: uuid.UUID
    job_id: uuid.UUID
    status: RunStatus
    # Whether an earlier review was returned instead of a new one; always false for questions.
    reused: bool
    # Questions only: the pinned indexed version.
    snapshot_id: uuid.UUID | None
    # Reviews only: the pull request and its pinned head commit.
    pull_request_number: int | None
    head_sha: str | None
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
    quality_state: QuestionQuality | None
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


class PullRequestRefOut(BaseModel):
    """The reviewed pull request, as copied at submission. `title` and `author` are untrusted."""

    number: int
    title: str
    author: str
    draft: bool
    base_ref: str
    head_ref: str
    # Null when the head repository was deleted.
    head_repository: str | None
    is_fork: bool
    html_url: str


class CommitsOut(BaseModel):
    base_sha: str
    head_sha: str
    # Null until the review job resolves it.
    merge_base_sha: str | None


class OverallRiskOut(BaseModel):
    level: Literal["high", "medium", "low", "none"]
    # True when a file was left out for the review limits.
    partial: bool


Origin = Literal["model", "rule"]


class SummaryPointOut(BaseModel):
    change: ChangeKind
    text: str
    citations: list[str]
    origin: Origin


class SummaryAreaOut(BaseModel):
    area: str
    points: list[SummaryPointOut]


class ReviewRiskOut(BaseModel):
    id: str
    title: str
    severity: Severity
    category: RiskCategory
    basis: RiskBasis
    explanation: str
    suggested_check: str
    citations: list[str]
    origin: Origin
    # The credential file a rule risk names; null for model risks.
    path: str | None


class ChecklistItemOut(BaseModel):
    text: str
    paths: list[str]
    risk_ids: list[str]


class ChangedTestOut(BaseModel):
    path: str
    change: ChangeKind


class CandidateTestOut(BaseModel):
    path: str
    reason: str
    citations: list[str]


class NewTestCaseOut(BaseModel):
    behavior: str
    location_hint: str | None
    citations: list[str]


class ReviewTestsOut(BaseModel):
    changed: list[ChangedTestOut]
    candidates: list[CandidateTestOut]
    new_cases: list[NewTestCaseOut]


class ReviewOut(BaseModel):
    overall_risk: OverallRiskOut
    overview: str
    summary: list[SummaryAreaOut]
    risks: list[ReviewRiskOut]
    checklist: list[ChecklistItemOut]
    tests: ReviewTestsOut
    # Model items dropped by validation.
    omitted_items: int


class CoverageFileOut(BaseModel):
    """One changed file, or one excluded directory (`entry_type` `directory`, with a `count`)."""

    path: str
    entry_type: Literal["file", "directory"] = "file"
    previous_path: str | None = None
    change: ChangeKind | None = None
    additions: int | None = None
    deletions: int | None = None
    reviewed: bool
    # Null when reviewed; otherwise a 001 coverage reason or `review_limit`.
    reason: str | None = None
    count: int | None = None


class ReviewCoverageOut(BaseModel):
    files: list[CoverageFileOut]
    changed_files: int
    reviewed_files: int
    changed_lines_reviewed: int
    context_items: int


class ReviewCitationOut(BaseModel):
    label: str
    source_type: Literal["change", "reference", "test"]
    # `before` is the merge base and `after` the head.
    side: Literal["before", "after"]
    path: str
    commit_sha: str
    start_line: int
    end_line: int
    excerpt: str
    github_url: str


class ReviewRunOut(BaseModel):
    id: uuid.UUID
    kind: Literal["pull_request_review"]
    status: RunStatus
    quality_state: ReviewQuality | None
    repository_id: uuid.UUID
    repository_full_name: str
    pull_request: PullRequestRefOut
    commits: CommitsOut
    # Null until the review succeeds.
    review: ReviewOut | None
    coverage: ReviewCoverageOut | None
    citations: list[ReviewCitationOut]
    error: JobError | None
    job_id: uuid.UUID | None
    created_at: datetime
    completed_at: datetime | None


AnyRunOut = Annotated[RunOut | ReviewRunOut, Field(discriminator="kind")]


class ReviewMarkdownOut(BaseModel):
    # Review text in it is neutralized for pasting into GitHub: mentions and references are
    # wrapped in inline code, and raw HTML is escaped.
    markdown: str


class FreshnessOut(BaseModel):
    """A review's pull request as GitHub reports it now."""

    pull_request_state: Literal["open", "closed", "merged"]
    draft: bool
    current_head_sha: str
    # True when `current_head_sha` differs from the head commit the review examined.
    outdated: bool
    checked_at: datetime


class RunSummaryOut(BaseModel):
    id: uuid.UUID
    kind: AnalysisKind
    # Null for reviews.
    question: str | None
    status: RunStatus
    quality_state: QuestionQuality | ReviewQuality | None
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


def _invalid_request(field: str) -> ApiError:
    return ApiError(
        422, "invalid_request", "The request is invalid.", details={"fields": [f"body.{field}"]}
    )


def _submit_question(
    db: Session, body: SubmitIn, *, user: User, workspace: Workspace, request_id: str | None
) -> SubmitOut:
    if body.target.pull_request_number is not None:
        raise _invalid_request("target.pull_request_number")
    if body.question is None:
        raise _invalid_request("question")
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
    return SubmitOut(
        run_id=run.id,
        job_id=job.id,
        status=_status(job),
        reused=False,
        snapshot_id=run.snapshot_id,
        pull_request_number=None,
        head_sha=None,
        result_url=f"/v1/analysis-runs/{run.id}",
        events_url=f"/v1/jobs/{job.id}/events",
    )


def _submit_review(
    db: Session, body: SubmitIn, *, user: User, workspace: Workspace, request_id: str | None
) -> SubmitOut:
    if body.question is not None:
        raise _invalid_request("question")
    if body.target.snapshot_id is not None:
        raise _invalid_request("target.snapshot_id")
    if body.target.pull_request_number is None:
        raise _invalid_request("target.pull_request_number")
    run, job, reused = review_runs.submit(
        db,
        user=user,
        workspace=workspace,
        repository_id=body.repository_id,
        pull_request_number=body.target.pull_request_number,
        mode=body.mode or "reuse",
        request_id=request_id,
        gateway=get_gateway(),
    )
    db.flush()
    return SubmitOut(
        run_id=run.id,
        job_id=job.id,
        status=_status(job),
        reused=reused,
        snapshot_id=None,
        pull_request_number=run.pull_request_number,
        head_sha=run.head_sha,
        result_url=f"/v1/analysis-runs/{run.id}",
        events_url=f"/v1/jobs/{job.id}/events",
    )


@router.post(
    "/analysis-runs",
    status_code=202,
    response_model=SubmitOut,
    responses={200: {"model": SubmitOut, "description": "An earlier review was reused"}},
)
def submit_run(
    body: SubmitIn,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    submit = _submit_review if body.kind == "pull_request_review" else _submit_question

    def operation() -> tuple[int, dict[str, Any]]:
        out = submit(db, body, user=user, workspace=workspace, request_id=request_id)
        # A replay with the same key answers with the same status.
        return 200 if out.reused else 202, out.model_dump(mode="json")

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
    kind: Annotated[AnalysisKind | None, Query()] = None,
) -> RunPage:
    # Questions, answers, and reviews are content: 403 while the repository's access is lost
    # (FR-014).
    repository = repos.get_scoped(
        db,
        user=user,
        workspace=workspace,
        repository_id=repository_id,
        request_id=request_id,
        content=True,
    )
    rows = runs.list_for_repository(
        db, repository.id, offset=page.offset, limit=page.limit + 1, kind=kind
    )
    window = rows[: page.limit]
    items = []
    for run in window:
        job = db.get(Job, run.job_id) if run.job_id is not None else None
        items.append(
            RunSummaryOut(
                id=run.id,
                kind=run.kind,
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
) -> AnyRunOut:
    run = runs.get_scoped(db, user=user, workspace=workspace, run_id=run_id, request_id=request_id)
    if run.kind == review_runs.ANALYSIS_KIND:
        return ReviewRunOut.model_validate(review_runs.review_out(db, run))
    return run_out(db, run)


@router.get("/analysis-runs/{run_id}/markdown")
def get_review_markdown(
    run_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
) -> ReviewMarkdownOut:
    """A succeeded review as Markdown, with citations linking to GitHub (FR-015, research R11).

    404 for an answer to a question, and 409 `review_not_finished` until the review succeeds.
    """
    run = runs.get_scoped(db, user=user, workspace=workspace, run_id=run_id, request_id=request_id)
    if run.kind != review_runs.ANALYSIS_KIND:
        raise not_found()
    job = db.get(Job, run.job_id) if run.job_id is not None else None
    if run.result is None or job is None or job.status != "succeeded":
        raise ApiError(409, "review_not_finished", "The review has not finished.")
    return ReviewMarkdownOut(
        markdown=markdown.render(run, run.result, review_runs.citations(db, run))
    )


@router.get("/analysis-runs/{run_id}/freshness")
def get_review_freshness(
    run_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
) -> FreshnessOut:
    """Whether a review is outdated, from one read of its pull request on GitHub (FR-022,
    research R10).

    404 for an answer to a question. GitHub errors are 401 `github_sign_in_required`, 409
    `github_access_denied`, and 502 `github_unavailable`; the page then shows the freshness as
    unknown.
    """
    freshness = pulls.freshness(
        db,
        user=user,
        workspace=workspace,
        run_id=run_id,
        gateway=get_gateway(),
        request_id=request_id,
    )
    # Keeps a user token that was refreshed for the call.
    db.commit()
    return FreshnessOut(
        pull_request_state=freshness.pull_request_state,
        draft=freshness.draft,
        current_head_sha=freshness.current_head_sha,
        outdated=freshness.outdated,
        checked_at=freshness.checked_at,
    )
