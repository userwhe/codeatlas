"""Routes: repositories available to connect (contracts/http-api.md, "Repositories")."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from codeatlas.api.deps import CurrentUser, CurrentWorkspace, DbSession
from codeatlas.api.pagination import PageParams, next_cursor, page_params
from codeatlas.workspace.repositories import list_connectable

router = APIRouter(tags=["repositories"])


class GitHubRepositoryOut(BaseModel):
    github_repository_id: int
    full_name: str
    private: bool
    default_branch: str
    connected_repository_id: str | None


class GitHubRepositoryPage(BaseModel):
    items: list[GitHubRepositoryOut]
    next_cursor: str | None


@router.get("/github/repositories")
def list_github_repositories(
    db: DbSession,
    user: CurrentUser,
    workspace: CurrentWorkspace,
    page: Annotated[PageParams, Depends(page_params)],
) -> GitHubRepositoryPage:
    connectable = sorted(
        list_connectable(db, user, workspace), key=lambda item: item.repository.full_name.lower()
    )
    window = connectable[page.offset : page.offset + page.limit]
    return GitHubRepositoryPage(
        items=[
            GitHubRepositoryOut(
                github_repository_id=item.repository.id,
                full_name=item.repository.full_name,
                private=item.repository.private,
                default_branch=item.repository.default_branch,
                connected_repository_id=(
                    str(item.connected_repository_id) if item.connected_repository_id else None
                ),
            )
            for item in window
        ],
        next_cursor=next_cursor(page, len(window), page.offset + len(window) < len(connectable)),
    )
