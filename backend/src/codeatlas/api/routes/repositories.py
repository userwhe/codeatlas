"""Routes: connected repositories (contracts/http-api.md, "Repositories")."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.api.deps import CurrentUser, CurrentWorkspace, DbSession, RequestId
from codeatlas.api.pagination import PageParams, next_cursor, page_params
from codeatlas.api.routes.jobs import JobError, job_error
from codeatlas.models import Job, Repository, Snapshot
from codeatlas.workspace import repositories as repos
from codeatlas.workspace.idempotency import run_idempotent

router = APIRouter(tags=["repositories"])

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]


class ActiveSnapshotOut(BaseModel):
    id: uuid.UUID
    commit_sha: str
    branch: str
    ready_at: datetime | None


class LatestJobOut(BaseModel):
    id: uuid.UUID
    status: str
    error: JobError | None


class RepositoryOut(BaseModel):
    id: uuid.UUID
    full_name: str
    private: bool
    default_branch: str
    state: repos.RepositoryState
    active_snapshot: ActiveSnapshotOut | None
    latest_indexing_job: LatestJobOut | None
    created_at: datetime


class RepositoryPage(BaseModel):
    items: list[RepositoryOut]
    next_cursor: str | None


class JobRefOut(BaseModel):
    id: uuid.UUID
    status: str


class ConnectIn(BaseModel):
    github_repository_id: int
    branch: str | None = Field(default=None, min_length=1, max_length=255)
    accept_external_processing: bool = False


class ConnectOut(BaseModel):
    repository: RepositoryOut
    job: JobRefOut


class IndexIn(BaseModel):
    branch: str | None = Field(default=None, min_length=1, max_length=255)


class IndexOut(BaseModel):
    job: JobRefOut


class SnapshotOut(BaseModel):
    id: uuid.UUID
    commit_sha: str
    branch: str
    status: str
    is_active: bool
    index_version: str
    coverage: dict[str, Any]
    created_at: datetime
    ready_at: datetime | None


class SnapshotPage(BaseModel):
    items: list[SnapshotOut]
    next_cursor: str | None


def repository_out(db: Session, repository: Repository) -> RepositoryOut:
    latest = repos.latest_indexing_job(db, repository.id)
    active = (
        db.get(Snapshot, repository.active_snapshot_id) if repository.active_snapshot_id else None
    )
    return RepositoryOut(
        id=repository.id,
        full_name=repository.full_name,
        private=repository.is_private,
        default_branch=repository.default_branch,
        state=repos.derive_state(repository, latest),
        active_snapshot=(
            ActiveSnapshotOut(
                id=active.id,
                commit_sha=active.commit_sha,
                branch=active.branch,
                ready_at=active.ready_at,
            )
            if active is not None
            else None
        ),
        latest_indexing_job=(
            LatestJobOut(id=latest.id, status=latest.status, error=job_error(latest))
            if latest is not None
            else None
        ),
        created_at=repository.created_at,
    )


def _job_ref(job: Job) -> JobRefOut:
    return JobRefOut(id=job.id, status=job.status)


@router.post("/repositories", status_code=202, response_model=ConnectOut)
def connect_repository(
    body: ConnectIn,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    def operation() -> tuple[int, dict[str, Any]]:
        repository, job = repos.connect(
            db,
            user=user,
            workspace=workspace,
            github_repository_id=body.github_repository_id,
            branch=body.branch,
            accept_external_processing=body.accept_external_processing,
            request_id=request_id,
        )
        db.flush()
        out = ConnectOut(repository=repository_out(db, repository), job=_job_ref(job))
        return 202, out.model_dump(mode="json")

    status, payload = run_idempotent(
        db,
        workspace_id=workspace.id,
        route="POST /v1/repositories",
        key=idempotency_key,
        payload=body.model_dump(mode="json"),
        operation=operation,
    )
    db.commit()
    return JSONResponse(status_code=status, content=payload)


@router.get("/repositories")
def list_repositories(
    db: DbSession,
    workspace: CurrentWorkspace,
    page: Annotated[PageParams, Depends(page_params)],
) -> RepositoryPage:
    rows = db.scalars(
        select(Repository)
        .where(Repository.workspace_id == workspace.id, Repository.deleted_at.is_(None))
        .order_by(Repository.created_at.desc(), Repository.id)
        .offset(page.offset)
        .limit(page.limit + 1)
    ).all()
    window = rows[: page.limit]
    return RepositoryPage(
        items=[repository_out(db, repository) for repository in window],
        next_cursor=next_cursor(page, len(window), len(rows) > page.limit),
    )


@router.get("/repositories/{repository_id}")
def get_repository(
    repository_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
) -> RepositoryOut:
    repository = repos.get_scoped(
        db, user=user, workspace=workspace, repository_id=repository_id, request_id=request_id
    )
    return repository_out(db, repository)


@router.delete("/repositories/{repository_id}", status_code=204)
def disconnect_repository(
    repository_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
) -> Response:
    repos.disconnect(
        db, user=user, workspace=workspace, repository_id=repository_id, request_id=request_id
    )
    db.commit()
    return Response(status_code=204)


@router.post("/repositories/{repository_id}/index", status_code=202, response_model=IndexOut)
def index_repository(
    repository_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
    body: IndexIn | None = None,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    branch = body.branch if body is not None else None

    def operation() -> tuple[int, dict[str, Any]]:
        job = repos.reindex(
            db,
            user=user,
            workspace=workspace,
            repository_id=repository_id,
            branch=branch,
            request_id=request_id,
        )
        db.flush()
        return 202, IndexOut(job=_job_ref(job)).model_dump(mode="json")

    status, payload = run_idempotent(
        db,
        workspace_id=workspace.id,
        route=f"POST /v1/repositories/{repository_id}/index",
        key=idempotency_key,
        payload={"branch": branch},
        operation=operation,
    )
    db.commit()
    return JSONResponse(status_code=status, content=payload)


@router.get("/repositories/{repository_id}/snapshots")
def list_snapshots(
    repository_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
    page: Annotated[PageParams, Depends(page_params)],
) -> SnapshotPage:
    repository = repos.get_scoped(
        db, user=user, workspace=workspace, repository_id=repository_id, request_id=request_id
    )
    rows = db.scalars(
        select(Snapshot)
        .where(Snapshot.repository_id == repository.id, Snapshot.status == "ready")
        .order_by(Snapshot.ready_at.desc(), Snapshot.id)
        .offset(page.offset)
        .limit(page.limit + 1)
    ).all()
    window = rows[: page.limit]
    return SnapshotPage(
        items=[
            SnapshotOut(
                id=snapshot.id,
                commit_sha=snapshot.commit_sha,
                branch=snapshot.branch,
                status=snapshot.status,
                is_active=snapshot.id == repository.active_snapshot_id,
                index_version=snapshot.index_version,
                coverage=snapshot.coverage,
                created_at=snapshot.created_at,
                ready_at=snapshot.ready_at,
            )
            for snapshot in window
        ],
        next_cursor=next_cursor(page, len(window), len(rows) > page.limit),
    )
