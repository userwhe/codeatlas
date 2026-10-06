"""The GitHub boundary (research R5, ADR 0005). Real and fake gateways follow this protocol."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import IO, Protocol

from codeatlas.config import Settings, get_settings


class GitHubError(Exception):
    """Base class for gateway errors."""


class GitHubAccessDenied(GitHubError):
    """The token is invalid, revoked, or lacks access."""


class GitHubNotFound(GitHubError):
    """The resource does not exist or is not visible to the caller."""


class BranchNotFound(GitHubError):
    """The requested branch does not exist."""


class RepositoryEmpty(GitHubError):
    """The repository has no branches (no commits)."""


class GitHubUnavailable(GitHubError):
    """GitHub returned 5xx, timed out, or could not be reached."""


@dataclass(frozen=True)
class UserTokens:
    access_token: str
    access_token_expires_at: datetime | None
    refresh_token: str | None
    refresh_token_expires_at: datetime | None


@dataclass(frozen=True)
class GitHubUser:
    id: int
    login: str
    name: str | None
    avatar_url: str | None


@dataclass(frozen=True)
class GitHubRepository:
    id: int
    full_name: str
    default_branch: str
    private: bool
    installation_id: int | None = None


class GitHubGateway(Protocol):
    def authorize_url(self, state: str) -> str:
        """URL of GitHub's App user-authorization page for this state."""
        ...

    def exchange_code(self, code: str) -> UserTokens: ...

    def refresh_user_token(self, refresh_token: str) -> UserTokens: ...

    def get_authenticated_user(self, user_token: str) -> GitHubUser: ...

    def list_accessible_repositories(self, user_token: str) -> list[GitHubRepository]:
        """Repositories reachable through the user's installations of the App (connect dialog).

        Each result carries `installation_id`.
        """
        ...

    def get_repository(self, user_token: str, github_repository_id: int) -> GitHubRepository:
        """`GET /repositories/{id}` with the user token: the access check (FR-003).

        Raises `GitHubNotFound` unless both the user and the App installation can access it.
        """
        ...

    def get_installation_id(self, full_name: str) -> int:
        """`GET /repos/{owner}/{repo}/installation` with the App JWT."""
        ...

    def resolve_commit(self, installation_id: int, full_name: str, branch: str) -> str:
        """Return the branch head SHA. Raises `BranchNotFound` or `RepositoryEmpty`."""
        ...

    def open_tarball(
        self, installation_id: int, full_name: str, sha: str
    ) -> AbstractContextManager[IO[bytes]]:
        """Open a readable gzip tar stream of the repository at `sha`."""
        ...


def get_gateway(settings: Settings | None = None) -> GitHubGateway:
    """Return the fake gateway in fake mode, otherwise the real GitHub client."""
    settings = settings or get_settings()
    if settings.fake_externals:
        from codeatlas.github.fake import get_fake_github

        return get_fake_github()
    from codeatlas.github.client import GitHubClient

    return GitHubClient(settings)
