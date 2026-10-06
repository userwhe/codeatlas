"""Routes: indexed versions, their coverage, and browsing (contracts/http-api.md, "Snapshots")."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from codeatlas.api.deps import CurrentUser, CurrentWorkspace, DbSession, RequestId
from codeatlas.api.pagination import PageParams, next_cursor, page_params
from codeatlas.models import CoverageEntry
from codeatlas.workspace.repositories import get_scoped_snapshot

router = APIRouter(tags=["snapshots"])


class SnapshotDetailOut(BaseModel):
    id: uuid.UUID
    repository_id: uuid.UUID
    commit_sha: str
    branch: str
    status: str
    index_version: str
    coverage: dict[str, Any]
    embeddings_available: bool
    created_at: datetime
    ready_at: datetime | None


class CoverageEntryOut(BaseModel):
    path: str
    entry_type: Literal["file", "directory"]
    reason: str
    detail: str | None


class CoveragePage(BaseModel):
    items: list[CoverageEntryOut]
    next_cursor: str | None


@router.get("/snapshots/{snapshot_id}")
def get_snapshot(
    snapshot_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
) -> SnapshotDetailOut:
    snapshot = get_scoped_snapshot(
        db, user=user, workspace=workspace, snapshot_id=snapshot_id, request_id=request_id
    )
    return SnapshotDetailOut(
        id=snapshot.id,
        repository_id=snapshot.repository_id,
        commit_sha=snapshot.commit_sha,
        branch=snapshot.branch,
        status=snapshot.status,
        index_version=snapshot.index_version,
        coverage=snapshot.coverage,
        embeddings_available=bool(snapshot.coverage.get("embeddings_available", True)),
        created_at=snapshot.created_at,
        ready_at=snapshot.ready_at,
    )


@router.get("/snapshots/{snapshot_id}/coverage")
def get_coverage(
    snapshot_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
    page: Annotated[PageParams, Depends(page_params)],
) -> CoveragePage:
    snapshot = get_scoped_snapshot(
        db, user=user, workspace=workspace, snapshot_id=snapshot_id, request_id=request_id
    )
    rows = db.scalars(
        select(CoverageEntry)
        .where(CoverageEntry.snapshot_id == snapshot.id)
        .order_by(CoverageEntry.path, CoverageEntry.id)
        .offset(page.offset)
        .limit(page.limit + 1)
    ).all()
    window = rows[: page.limit]
    return CoveragePage(
        items=[
            CoverageEntryOut(
                path=row.path,
                entry_type="directory" if row.entry_type == "directory" else "file",
                reason=row.reason,
                detail=row.detail,
            )
            for row in window
        ],
        next_cursor=next_cursor(page, len(window), len(rows) > page.limit),
    )
