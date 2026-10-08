"""The GitHub boundary (research R5, ADR 0005). Real and fake gateways follow this protocol."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import IO, Literal, Protocol

from codeatlas.config import Settings, get_settings


class GitHubError(Exception):
    """Base class for gateway errors."""


class GitHubAccessDenied(GitHubError):
    """The token is invalid, revoked, or lacks access."""


class UserAuthorizationInvalid(GitHubAccessDenied):
    """The user's GitHub authorization is missing, expired, revoked, or rejected."""


class AppCredentialsRejected(GitHubError):
    """GitHub rejected the App's own credentials (JWT or installation token request)."""


class GitHubNotFound(GitHubError):
    """The resource does not exist or is not visible to the caller."""


AccessCheckReason = Literal[
    "repository_not_visible", "app_not_installed", "installation_not_accessible"
]


class AccessCheckFailed(GitHubNotFound):
    """GitHub answered the access check definitively: the user cannot reach the repository
    through an installation of the App (research R4). `reason` says which step failed.
    """

    def __init__(self, reason: AccessCheckReason, message: str) -> None:
        super().__init__(message)
        self.reason: AccessCheckReason = reason


class BranchNotFound(GitHubError):
    """The requested branch does not exist."""


class RepositoryEmpty(GitHubError):
    """The repository has no branches (no commits)."""


class GitHubUnavailable(GitHubError):
    """GitHub returned 5xx, timed out, or could not be reached."""


class CommitUnavailable(GitHubError):
    """GitHub no longer serves a pinned commit, for example after a force push and garbage
    collection (specs/003-pr-review, research R2). Raised after the access check passed, so it
    says nothing about access.
    """


class NoCommonHistory(GitHubError):
    """The two commits of a comparison share no ancestor (specs/003-pr-review, research R2)."""


PULL_REQUEST_PAGE_SIZE = 30
"""Pull requests per page of `list_pull_requests`."""


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


PullRequestState = Literal["open", "closed", "merged"]


@dataclass(frozen=True)
class PullRequest:
    """A pull request as GitHub reports it. `title`, `body`, and `author` are untrusted text.

    `body` is empty when GitHub has none. `head_repository` is None when the head repository was
    deleted, and `is_fork` is true when it differs from the base repository. `additions`,
    `deletions`, and `changed_files` come only from `get_pull_request`; listings leave them None.
    """

    number: int
    title: str
    body: str
    author: str
    state: PullRequestState
    draft: bool
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    head_repository: str | None
    is_fork: bool
    html_url: str
    updated_at: datetime
    additions: int | None = None
    deletions: int | None = None
    changed_files: int | None = None


@dataclass(frozen=True)
class PullRequestPage:
    items: list[PullRequest]
    next_page: int | None
    """The next page number for `list_pull_requests`, or None on the last page."""


@dataclass(frozen=True)
class Comparison:
    """The comparison of two pinned commits (specs/003-pr-review, research R2 and R4)."""

    merge_base_sha: str
    renamed: dict[str, str]
    """New path to previous path, for files GitHub reports as renamed (rename hints)."""
    listed_files: int
    """How many files GitHub listed. It lists at most 300, so hints may be incomplete beyond."""


@dataclass(frozen=True)
class InstallationPermissions:
    """The App installation covering a repository, with the permissions it was granted."""

    installation_id: int
    permissions: dict[str, str]
    """Permission name (`contents`, `pull_requests`, ...) to access level (`read`, `write`)."""
    html_url: str
    """The installation's settings page, where the account owner approves new permissions."""


class GitHubGateway(Protocol):
    def authorize_url(self, state: str) -> str:
        """URL of GitHub's App user-authorization page for this state."""
        ...

    def exchange_code(self, code: str) -> UserTokens: ...

    def refresh_user_token(self, refresh_token: str) -> UserTokens: ...

    def get_authenticated_user(self, user_token: str) -> GitHubUser: ...

    def get_user_by_login(self, login: str) -> GitHubUser:
        """`GET /users/{login}`, public data read without credentials (specs/004-pilot-deployment,
        research R13). Raises `GitHubNotFound` for an unknown login.
        """
        ...

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

    def list_pull_requests(self, user_token: str, full_name: str, page: int) -> PullRequestPage:
        """One page of open pull requests, drafts included, most recently updated first, read
        with the user token (`GET /repos/{owner}/{repo}/pulls`). Pages start at 1.

        On a private repository this needs the App's Pull requests permission; without it, GitHub
        refuses (`GitHubAccessDenied` or `GitHubNotFound`).
        """
        ...

    def get_pull_request(self, user_token: str, full_name: str, number: int) -> PullRequest:
        """`GET /repos/{owner}/{repo}/pulls/{number}` with the user token. Raises `GitHubNotFound`
        for an unknown number. It works with either the Contents or the Pull requests permission.
        """
        ...

    def compare_commits(
        self,
        installation_id: int,
        full_name: str,
        base_sha: str,
        head_sha: str,
        *,
        head_owner: str | None,
    ) -> Comparison:
        """Compare two pinned commits with an installation token
        (`GET /repos/{owner}/{repo}/compare/{base}...{head}`).

        `head_owner` is the owner of a fork's head repository, or None. Raises `CommitUnavailable`
        when GitHub no longer serves either commit, and `NoCommonHistory` when they share no
        ancestor.
        """
        ...

    def get_installation_permissions(self, full_name: str) -> InstallationPermissions:
        """`GET /repos/{owner}/{repo}/installation` with the App JWT."""
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

    Raises `AccessCheckFailed` otherwise, a `GitHubNotFound` whose reason names the failed step:
    `repository_not_visible`, `app_not_installed`, or `installation_not_accessible`. GitHub shows
    every public repository to every user, so the covering installation must also be one of the
    user's. Other gateway errors (a rejected credential, an outage) pass through unchanged. Three
    GitHub calls; the installation listing runs alongside the other two, which keeps connecting
    under a second (SC-007).
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        user_installations = pool.submit(gateway.list_installation_ids, user_token)
        try:
            repository = gateway.get_repository(user_token, github_repository_id)
        except GitHubNotFound as exc:
            raise AccessCheckFailed(
                "repository_not_visible",
                f"repository {github_repository_id} is not visible to the user",
            ) from exc
        try:
            installation_id = gateway.get_installation_id(repository.full_name)
        except GitHubNotFound as exc:
            raise AccessCheckFailed(
                "app_not_installed", f"the App is not installed on {repository.full_name}"
            ) from exc
        if installation_id not in user_installations.result():
            raise AccessCheckFailed(
                "installation_not_accessible",
                f"repository {github_repository_id} is outside the user's installations",
            )
    return repository, installation_id
