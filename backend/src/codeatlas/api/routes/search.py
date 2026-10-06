"""Routes: search within one indexed version (contracts/http-api.md, "Search")."""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field

from codeatlas.api.deps import CurrentUser, CurrentWorkspace, DbSession, RequestId
from codeatlas.retrieval.search import SearchMode, search
from codeatlas.workspace.repositories import get_scoped_snapshot

router = APIRouter(tags=["search"])


class SearchRequest(BaseModel):
    snapshot_id: uuid.UUID
    query: str = Field(min_length=1, max_length=200)
    mode: SearchMode
    limit: int = Field(default=20, ge=1, le=50)


class SearchSymbolOut(BaseModel):
    name: str
    kind: str


class SearchResultOut(BaseModel):
    path: str
    start_line: int
    end_line: int
    snippet: str
    symbol: SearchSymbolOut | None = None
    exact: bool


class SearchResponse(BaseModel):
    mode: SearchMode
    degraded: bool
    results: list[SearchResultOut]


@router.post("/search")
def search_snapshot(
    body: SearchRequest,
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    request_id: RequestId,
) -> SearchResponse:
    """Text, path, symbol, or documentation search; exact path and symbol matches come first."""
    snapshot = get_scoped_snapshot(
        db, user=user, workspace=workspace, snapshot_id=body.snapshot_id, request_id=request_id
    )
    result = search(db, snapshot, query=body.query, mode=body.mode, limit=body.limit)
    return SearchResponse(
        mode=result.mode,
        degraded=result.degraded,
        results=[
            SearchResultOut(
                path=hit.path,
                start_line=hit.start_line,
                end_line=hit.end_line,
                snippet=hit.snippet,
                symbol=None
                if hit.symbol is None
                else SearchSymbolOut(name=hit.symbol.name, kind=hit.symbol.kind),
                exact=hit.exact,
            )
            for hit in result.results
        ],
    )
