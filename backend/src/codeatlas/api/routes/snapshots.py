"""Routes: indexed versions, their coverage, and browsing (contracts/http-api.md, "Snapshots")."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from codeatlas.api.deps import CurrentUser, CurrentWorkspace, DbSession, RequestId
from codeatlas.api.pagination import PageParams, next_cursor, page_params
from codeatlas.models import CoverageEntry
from codeatlas.retrieval.browse import list_tree, normalize_path, read_lines
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


class TreeEntryOut(BaseModel):
    name: str
    type: Literal["directory", "file"]
    language: str | None = None
    line_count: int | None = None


class TreeOut(BaseModel):
    path: str
    entries: list[TreeEntryOut]


class FileLinesOut(BaseModel):
    path: str
    commit_sha: str
    language: str
    line_count: int
    start_line: int
    end_line: int
    lines: list[str]


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


@router.get("/snapshots/{snapshot_id}/tree", response_model_exclude_none=True)
def get_tree(
    snapshot_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
    path: Annotated[str, Query(max_length=1024)] = "",
) -> TreeOut:
    """Immediate children of a directory; `path` is empty for the root."""
    snapshot = get_scoped_snapshot(
        db, user=user, workspace=workspace, snapshot_id=snapshot_id, request_id=request_id
    )
    directory = normalize_path(path)
    entries = list_tree(db, snapshot.id, directory)
    return TreeOut(
        path=directory,
        entries=[
            TreeEntryOut(
                name=entry.name,
                type=entry.type,
                language=entry.language,
                line_count=entry.line_count,
            )
            for entry in entries
        ],
    )


@router.get("/snapshots/{snapshot_id}/file")
def get_file(
    snapshot_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
    start_line: Annotated[int | None, Query(ge=1)] = None,
    end_line: Annotated[int | None, Query(ge=1)] = None,
) -> FileLinesOut:
    """At most 1,000 lines of a file; defaults to lines 1 to 1,000."""
    snapshot = get_scoped_snapshot(
        db, user=user, workspace=workspace, snapshot_id=snapshot_id, request_id=request_id
    )
    lines = read_lines(db, snapshot.id, path, start_line, end_line)
    return FileLinesOut(
        path=lines.path,
        commit_sha=snapshot.commit_sha,
        language=lines.language,
        line_count=lines.line_count,
        start_line=lines.start_line,
        end_line=lines.end_line,
        lines=lines.lines,
    )
