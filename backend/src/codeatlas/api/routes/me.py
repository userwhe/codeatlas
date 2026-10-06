"""Routes: the signed-in account (contracts/http-api.md, "Account")."""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from codeatlas.api.deps import AppSettings, CurrentUser, CurrentWorkspace

router = APIRouter()


class UserOut(BaseModel):
    id: uuid.UUID
    github_login: str
    name: str | None
    avatar_url: str | None


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str


class MeOut(BaseModel):
    user: UserOut
    workspace: WorkspaceOut
    github_app_install_url: str


@router.get("/me")
def get_me(user: CurrentUser, workspace: CurrentWorkspace, settings: AppSettings) -> MeOut:
    return MeOut(
        user=UserOut(
            id=user.id,
            github_login=user.github_login,
            name=user.name,
            avatar_url=user.avatar_url,
        ),
        workspace=WorkspaceOut(id=workspace.id, name=workspace.name),
        github_app_install_url=(
            f"https://github.com/apps/{settings.github_app_slug}/installations/new"
        ),
    )
