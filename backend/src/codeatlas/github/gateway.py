"""The GitHub boundary (research R5, ADR 0005). Real and fake gateways follow this protocol."""

from concurrent.futures import ThreadPoolExecutor
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
        """`GET /repositories/{id}` with the user token.

        Raises `GitHubNotFound` unless the user can see it. GitHub shows a private repository
        only through an installation of the App, but shows a public one to every user.
        """
        ...

    def list_installation_ids(self, user_token: str) -> set[int]:
        """IDs of the App installations the user can access (`GET /user/installations`)."""
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


def verify_access(
    gateway: GitHubGateway, user_token: str, github_repository_id: int
) -> tuple[GitHubRepository, int]:
    """The access check (FR-003): the user can see the repository, and an installation of the App
    that the user can access covers it. Returns the repository and that installation's ID.

    Raises `GitHubNotFound` otherwise. GitHub shows every public repository to every user, so the
    covering installation must also be one of the user's. Three GitHub calls; the installation
    listing runs alongside the other two, which keeps connecting under a second (SC-007).
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        user_installations = pool.submit(gateway.list_installation_ids, user_token)
        repository = gateway.get_repository(user_token, github_repository_id)
        installation_id = gateway.get_installation_id(repository.full_name)
        if installation_id not in user_installations.result():
            raise GitHubNotFound(
                f"repository {github_repository_id} is outside the user's installations"
            )
    return repository, installation_id
