"""The real GitHub gateway over httpx (research R5, ADR 0005).

- Sign-in uses the App's user authorization (OAuth web flow) with expiring user tokens.
- Repository reads use short-lived installation tokens, cached in memory and never stored. A
  token that GitHub refuses is dropped from the cache.
- Pull requests are listed and read with the user token; the review job compares commits with an
  installation token (specs/003-pr-review, research R1 and R2).
- Calls share one pooled HTTP client per process, so they reuse TLS connections (SC-007).
- Every failure maps to a gateway error from `codeatlas.github.gateway`. A rejected credential
  maps by whose it is: the user's or the App's (research R4).
"""

import io
import threading
from collections.abc import Buffer, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any
from urllib.parse import quote, urlencode

import httpx
import jwt

from codeatlas.config import Settings
from codeatlas.github.gateway import (
    PULL_REQUEST_PAGE_SIZE,
    AppCredentialsRejected,
    BranchNotFound,
    CommitUnavailable,
    Comparison,
    GitHubAccessDenied,
    GitHubError,
    GitHubNotFound,
    GitHubRepository,
    GitHubUnavailable,
    GitHubUser,
    InstallationPermissions,
    NoCommonHistory,
    PullRequest,
    PullRequestPage,
    PullRequestState,
    RepositoryEmpty,
    UserAuthorizationInvalid,
    UserTokens,
)

GITHUB_WEB = "https://github.com"
GITHUB_API = "https://api.github.com"
TARBALL_HOST = "codeload.github.com"
USER_AGENT = "codeatlas"
TIMEOUT = httpx.Timeout(10.0)
KEEPALIVE_EXPIRY = 60.0
API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
PAGE_SIZE = 100
APP_JWT_BACKDATE = timedelta(seconds=60)
APP_JWT_LIFETIME = timedelta(minutes=9)
TOKEN_REFRESH_MARGIN = timedelta(minutes=1)


@dataclass(frozen=True)
class _CachedToken:
    token: str
    expires_at: datetime


# Installation tokens, keyed by installation ID. Process memory only; never persisted.
_installation_tokens: dict[int, _CachedToken] = {}
_installation_tokens_lock = threading.Lock()


def clear_installation_token_cache() -> None:
    """Forget every cached installation token."""
    with _installation_tokens_lock:
        _installation_tokens.clear()


def _forget_installation_token(installation_id: int, token: str) -> None:
    """Drop a cached installation token that GitHub refused, unless a newer one replaced it."""
    with _installation_tokens_lock:
        cached = _installation_tokens.get(installation_id)
        if cached is not None and cached.token == token:
            del _installation_tokens[installation_id]


def _new_http_client(transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        timeout=TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        limits=httpx.Limits(keepalive_expiry=KEEPALIVE_EXPIRY),
    )


_pooled_http_client: httpx.Client | None = None
_pooled_http_client_lock = threading.Lock()


def _pooled_client() -> httpx.Client:
    """The process-wide client. A new TLS connection to GitHub costs about one extra call."""
    global _pooled_http_client
    with _pooled_http_client_lock:
        if _pooled_http_client is None:
            _pooled_http_client = _new_http_client(None)
        return _pooled_http_client


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GitHubClient:
    """The `GitHubGateway` implementation that talks to github.com."""

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        self._settings = settings
        self._transport = transport

    # Sign-in

    def authorize_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self._settings.github_app_client_id,
                "redirect_uri": self._redirect_uri(),
                "state": state,
            }
        )
        return f"{GITHUB_WEB}/login/oauth/authorize?{query}"

    def exchange_code(self, code: str) -> UserTokens:
        return self._request_user_tokens({"code": code, "redirect_uri": self._redirect_uri()})

    def refresh_user_token(self, refresh_token: str) -> UserTokens:
        return self._request_user_tokens(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    def get_authenticated_user(self, user_token: str) -> GitHubUser:
        with self._client() as http:
            response = self._api(http, "GET", "/user", user_token)
        _check(response, unauthorized=UserAuthorizationInvalid)
        data = _json(response)
        return GitHubUser(
            id=int(data["id"]),
            login=str(data["login"]),
            name=_optional_str(data.get("name")),
            avatar_url=_optional_str(data.get("avatar_url")),
        )

    # Repositories

    def list_accessible_repositories(self, user_token: str) -> list[GitHubRepository]:
        repositories: list[GitHubRepository] = []
        with self._client() as http:
            installations = self._paginate(http, "/user/installations", "installations", user_token)
            for installation in installations:
                if installation.get("suspended_at"):
                    continue
                installation_id = int(installation["id"])
                path = f"/user/installations/{installation_id}/repositories"
                for data in self._paginate(http, path, "repositories", user_token):
                    repositories.append(_repository(data, installation_id))
        return repositories

    def get_repository(self, user_token: str, github_repository_id: int) -> GitHubRepository:
        with self._client() as http:
            response = self._api(http, "GET", f"/repositories/{github_repository_id}", user_token)
        # A plain 403 means the same as 404 here (FR-003).
        _check(response, unauthorized=UserAuthorizationInvalid, forbidden=GitHubNotFound)
        return _repository(_json(response))

    def list_installation_ids(self, user_token: str) -> set[int]:
        with self._client() as http:
            installations = self._paginate(http, "/user/installations", "installations", user_token)
        return {
            int(installation["id"])
            for installation in installations
            if not installation.get("suspended_at")
        }

    def get_installation_id(self, full_name: str) -> int:
        return int(self._installation(full_name)["id"])

    def resolve_commit(self, installation_id: int, full_name: str, branch: str) -> str:
        repo = _repo_path(full_name)
        with self._client() as http:
            token = self._installation_token(http, installation_id)
            response = self._api(http, "GET", f"{repo}/branches/{quote(branch, safe='')}", token)
            if response.status_code == 404:
                branches = self._api(http, "GET", f"{repo}/branches", token, params={"per_page": 1})
                _check_installation_read(branches, installation_id, token)
                if not _json(branches):
                    raise RepositoryEmpty(f"{full_name} has no branches")
                raise BranchNotFound(f"branch {branch!r} not found in {full_name}")
        _check_installation_read(response, installation_id, token)
        return str(_json(response)["commit"]["sha"])

    @contextmanager
    def open_tarball(self, installation_id: int, full_name: str, sha: str) -> Iterator[IO[bytes]]:
        with self._client() as http:
            token = self._installation_token(http, installation_id)
            path = f"{_repo_path(full_name)}/tarball/{quote(sha, safe='')}"
            response = self._api(http, "GET", path, token, follow_redirects=False)
            if not response.is_redirect:
                _check_installation_read(response, installation_id, token)
                raise GitHubUnavailable(f"GitHub did not redirect the tarball request for {sha}")
            # The redirect URL carries its own short-lived token, so no Authorization header is
            # sent, and only GitHub's archive host is trusted.
            location = httpx.URL(response.headers["location"])
            if location.scheme != "https" or location.host != TARBALL_HOST:
                target = (
                    f"{location.scheme}://{location.host}" if location.host else "a relative URL"
                )
                raise GitHubUnavailable(
                    f"refusing tarball redirect to {target}; only https://{TARBALL_HOST} is allowed"
                )
            download = _send(
                http, http.build_request("GET", location), stream=True, follow_redirects=False
            )
            try:
                if download.status_code != 200:
                    _check(download, unauthorized=GitHubAccessDenied)
                    raise GitHubUnavailable(f"tarball download returned {download.status_code}")
                yield io.BufferedReader(_ResponseStream(download.iter_bytes()))
            finally:
                download.close()

    # Pull requests

    def list_pull_requests(self, user_token: str, full_name: str, page: int) -> PullRequestPage:
        params: dict[str, str | int] = {
            "state": "open",
            "sort": "updated",
            "direction": "desc",
            "per_page": PULL_REQUEST_PAGE_SIZE,
            "page": page,
        }
        with self._client() as http:
            response = self._api(
                http, "GET", f"{_repo_path(full_name)}/pulls", user_token, params=params
            )
        _check(response, unauthorized=UserAuthorizationInvalid)
        return PullRequestPage(
            items=[_pull_request(data) for data in _json(response)],
            next_page=_next_page(response),
        )

    def get_pull_request(self, user_token: str, full_name: str, number: int) -> PullRequest:
        with self._client() as http:
            path = f"{_repo_path(full_name)}/pulls/{int(number)}"
            response = self._api(http, "GET", path, user_token)
        _check(response, unauthorized=UserAuthorizationInvalid)
        return _pull_request(_json(response))

    def compare_commits(
        self,
        installation_id: int,
        full_name: str,
        base_sha: str,
        head_sha: str,
        *,
        head_owner: str | None,
    ) -> Comparison:
        compare = f"{_repo_path(full_name)}/compare"
        with self._client() as http:
            token = self._installation_token(http, installation_id)
            response = self._api(
                http, "GET", f"{compare}/{_ref(base_sha)}...{_ref(head_sha)}", token
            )
            if response.status_code == 404 and head_owner is not None:
                # GitHub documents an owner-qualified form for commits in another repository of
                # the same network; it is tried once for a fork's head (research R2).
                base = f"{_ref(full_name.partition('/')[0])}:{_ref(base_sha)}"
                head = f"{_ref(head_owner)}:{_ref(head_sha)}"
                response = self._api(http, "GET", f"{compare}/{base}...{head}", token)
            # GitHub also answers a plain 404 for a comparison of existing commits that it has not
            # computed yet; only a missing commit makes the comparison unavailable (research R2).
            commits_exist = (
                response.status_code == 404
                and not _no_common_ancestor(response)
                and all(
                    self._commit_exists(http, installation_id, token, full_name, sha)
                    for sha in (base_sha, head_sha)
                )
            )
        _check_comparison(response, installation_id, token, commits_exist=commits_exist)
        data = _json(response)
        files = data.get("files") or []
        return Comparison(
            merge_base_sha=str(data["merge_base_commit"]["sha"]),
            renamed={
                str(file["filename"]): str(file["previous_filename"])
                for file in files
                if file.get("status") == "renamed" and file.get("previous_filename")
            },
            listed_files=len(files),
        )

    def _commit_exists(
        self, http: httpx.Client, installation_id: int, token: str, full_name: str, sha: str
    ) -> bool:
        response = self._api(http, "GET", f"{_repo_path(full_name)}/commits/{_ref(sha)}", token)
        if response.status_code in (404, 422):
            return False
        _check_installation_read(response, installation_id, token)
        return True

    def get_installation_permissions(self, full_name: str) -> InstallationPermissions:
        data = self._installation(full_name)
        return InstallationPermissions(
            installation_id=int(data["id"]),
            permissions={str(name): str(level) for name, level in data["permissions"].items()},
            html_url=str(data["html_url"]),
        )

    # Helpers

    @contextmanager
    def _client(self) -> Iterator[httpx.Client]:
        """The pooled client, or a short-lived one around an injected (test) transport."""
        if self._transport is None:
            yield _pooled_client()
            return
        with _new_http_client(self._transport) as http:
            yield http

    def _redirect_uri(self) -> str:
        return f"{self._settings.app_origin.rstrip('/')}/auth/github/callback"

    def _api(
        self,
        http: httpx.Client,
        method: str,
        path: str,
        token: str,
        *,
        params: Mapping[str, str | int] | None = None,
        follow_redirects: bool = True,
    ) -> httpx.Response:
        url = path if path.startswith("https://") else f"{GITHUB_API}{path}"
        headers = {**API_HEADERS, "Authorization": f"Bearer {token}"}
        request = http.build_request(method, url, headers=headers, params=params)
        return _send(http, request, follow_redirects=follow_redirects)

    def _paginate(
        self, http: httpx.Client, path: str, key: str, user_token: str
    ) -> list[dict[str, Any]]:
        """Every item of a listing read with the user token."""
        items: list[dict[str, Any]] = []
        url: str | None = path
        params: Mapping[str, int] | None = {"per_page": PAGE_SIZE}
        while url is not None:
            response = self._api(http, "GET", url, user_token, params=params)
            _check(response, unauthorized=UserAuthorizationInvalid)
            items.extend(_json(response)[key])
            url = response.links.get("next", {}).get("url")
            params = None  # The next-page URL already carries the query.
        return items

    def _installation(self, full_name: str) -> Any:
        """The installation covering the repository, read with the App JWT."""
        with self._client() as http:
            response = self._api(
                http, "GET", f"{_repo_path(full_name)}/installation", self._app_jwt()
            )
        _check(response, unauthorized=AppCredentialsRejected)
        return _json(response)

    def _request_user_tokens(self, fields: dict[str, str]) -> UserTokens:
        form = {
            "client_id": self._settings.github_app_client_id,
            "client_secret": self._settings.github_app_client_secret,
            **fields,
        }
        with self._client() as http:
            request = http.build_request(
                "POST",
                f"{GITHUB_WEB}/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data=form,
            )
            response = _send(http, request)
        if _unavailable(response):
            raise GitHubUnavailable(f"GitHub returned {response.status_code} for the token request")
        if response.is_error:
            raise UserAuthorizationInvalid(
                f"GitHub rejected the token request ({response.status_code})"
            )
        body = _json(response)
        error = body.get("error") if isinstance(body, dict) else None
        if error == "incorrect_client_credentials":
            # The App's client ID or secret is wrong; the user's authorization is not at fault.
            raise AppCredentialsRejected("GitHub rejected the App's client credentials")
        if not isinstance(body, dict) or "error" in body or not body.get("access_token"):
            raise UserAuthorizationInvalid(
                f"GitHub rejected the token request ({error or 'no token'})"
            )
        now = _utcnow()
        return UserTokens(
            access_token=str(body["access_token"]),
            access_token_expires_at=_expiry(now, body.get("expires_in")),
            refresh_token=_optional_str(body.get("refresh_token")) or None,
            refresh_token_expires_at=_expiry(now, body.get("refresh_token_expires_in")),
        )

    def _app_jwt(self) -> str:
        key_path = self._settings.github_app_private_key_path
        if not key_path:
            raise RuntimeError("GITHUB_APP_PRIVATE_KEY_PATH is not set")
        now = _utcnow()
        claims = {
            "iss": self._settings.github_app_id,
            "iat": int((now - APP_JWT_BACKDATE).timestamp()),
            "exp": int((now + APP_JWT_LIFETIME).timestamp()),
        }
        return jwt.encode(claims, Path(key_path).read_text(), algorithm="RS256")

    def _installation_token(self, http: httpx.Client, installation_id: int) -> str:
        with _installation_tokens_lock:
            cached = _installation_tokens.get(installation_id)
        if cached is not None and _utcnow() < cached.expires_at - TOKEN_REFRESH_MARGIN:
            return cached.token
        response = self._api(
            http, "POST", f"/app/installations/{installation_id}/access_tokens", self._app_jwt()
        )
        _check(response, unauthorized=AppCredentialsRejected)
        data = _json(response)
        fresh = _CachedToken(
            token=str(data["token"]), expires_at=datetime.fromisoformat(data["expires_at"])
        )
        with _installation_tokens_lock:
            _installation_tokens[installation_id] = fresh
        return fresh.token


class _ResponseStream(io.RawIOBase):
    """A raw, read-only file over a streaming response body, for `tarfile` in `r|gz` mode."""

    def __init__(self, chunks: Iterator[bytes]):
        self._chunks = chunks
        self._chunk = b""
        self._offset = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer, /) -> int:
        while self._offset >= len(self._chunk):
            try:
                self._chunk = next(self._chunks)
            except StopIteration:
                return 0
            except httpx.RequestError as exc:
                raise GitHubUnavailable(f"tarball download failed: {type(exc).__name__}") from exc
            self._offset = 0
        view = memoryview(buffer).cast("B")
        size = min(len(view), len(self._chunk) - self._offset)
        view[:size] = self._chunk[self._offset : self._offset + size]
        self._offset += size
        return size


def _send(
    http: httpx.Client,
    request: httpx.Request,
    *,
    stream: bool = False,
    follow_redirects: bool = True,
) -> httpx.Response:
    try:
        return http.send(request, stream=stream, follow_redirects=follow_redirects)
    except httpx.RequestError as exc:
        # Never echo the full URL: a tarball redirect URL carries a token in its query.
        raise GitHubUnavailable(
            f"could not reach GitHub for {request.method} {request.url.host}{request.url.path}: "
            f"{type(exc).__name__}"
        ) from exc


def _unavailable(response: httpx.Response) -> bool:
    """5xx, or 403/429 rate limiting (primary or secondary limits)."""
    status = response.status_code
    rate_limited = (
        response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers
    )
    return status >= 500 or status == 429 or (status == 403 and rate_limited)


def _check(
    response: httpx.Response,
    *,
    unauthorized: type[GitHubError],
    forbidden: type[GitHubError] = GitHubAccessDenied,
) -> None:
    """Raise the gateway error for an error response; do nothing for success.

    A 401 raises `unauthorized`, chosen by the credential the request carried (research R4):
    `UserAuthorizationInvalid` for the user token, `AppCredentialsRejected` for the App JWT, and
    `GitHubAccessDenied` for an installation token (the installation can no longer read).
    """
    status = response.status_code
    if status < 400:
        return
    where = f"{response.request.method} {response.request.url.path}"
    if _unavailable(response):
        raise GitHubUnavailable(f"GitHub returned {status} for {where}")
    if status == 401:
        raise unauthorized(f"GitHub rejected the credentials for {where}")
    if status == 403:
        raise forbidden(f"GitHub denied {where}")
    if status == 404:
        raise GitHubNotFound(f"GitHub found nothing for {where}")
    raise GitHubError(f"GitHub returned {status} for {where}")


def _check_installation_read(response: httpx.Response, installation_id: int, token: str) -> None:
    """`_check` for a read made with an installation token.

    A 401 or a plain 403 means the installation can no longer read (`GitHubAccessDenied`). The
    cached token is dropped first, so a later access check mints a fresh one instead of reusing
    a token that GitHub refused.
    """
    try:
        _check(response, unauthorized=GitHubAccessDenied)
    except GitHubAccessDenied:
        _forget_installation_token(installation_id, token)
        raise


def _no_common_ancestor(response: httpx.Response) -> bool:
    return "no common ancestor" in _message(response).lower()


def _check_comparison(
    response: httpx.Response, installation_id: int, token: str, *, commits_exist: bool
) -> None:
    """`_check_installation_read` for a comparison. It runs after the access check passed, so a
    404 or 422 means that a pinned commit is gone, or that the commits share no ancestor
    (research R2). A plain 404 for commits that both exist means GitHub has not computed the
    comparison yet, which a later attempt can get.
    """
    status = response.status_code
    if status in (404, 422):
        where = f"{response.request.method} {response.request.url.path}"
        if status == 404 and _no_common_ancestor(response):
            raise NoCommonHistory(f"GitHub found no common ancestor for {where}")
        if status == 404 and commits_exist:
            raise GitHubUnavailable(f"GitHub has not computed the comparison for {where} yet")
        raise CommitUnavailable(f"GitHub returned {status} for {where}")
    _check_installation_read(response, installation_id, token)


def _message(response: httpx.Response) -> str:
    """The `message` of a GitHub error body, or an empty string."""
    try:
        body = response.json()
    except ValueError:
        return ""
    message = body.get("message") if isinstance(body, dict) else None
    return message if isinstance(message, str) else ""


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubUnavailable(
            f"GitHub returned invalid JSON for {response.request.url.path}"
        ) from exc


def _repo_path(full_name: str) -> str:
    owner, _, name = full_name.partition("/")
    if not owner or not name or "/" in name:
        raise ValueError(f"invalid repository full name: {full_name!r}")
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"


def _repository(data: Mapping[str, Any], installation_id: int | None = None) -> GitHubRepository:
    return GitHubRepository(
        id=int(data["id"]),
        full_name=str(data["full_name"]),
        default_branch=str(data["default_branch"]),
        private=bool(data["private"]),
        installation_id=installation_id,
    )


def _pull_request(data: Mapping[str, Any]) -> PullRequest:
    base, head = data["base"], data["head"]
    # A deleted head repository (usually a fork) is reported as null.
    head_repository = str(head["repo"]["full_name"]) if head.get("repo") else None
    state: PullRequestState = "open"
    if data["state"] != "open":
        # Listings carry only `merged_at`; "Get a pull request" also carries `merged`.
        state = "merged" if data.get("merged") or data.get("merged_at") else "closed"
    return PullRequest(
        number=int(data["number"]),
        title=str(data["title"]),
        body=_optional_str(data.get("body")) or "",
        # GitHub reports a deleted account as the user "ghost".
        author=str((data.get("user") or {}).get("login") or "ghost"),
        state=state,
        draft=bool(data.get("draft")),
        base_ref=str(base["ref"]),
        base_sha=str(base["sha"]),
        head_ref=str(head["ref"]),
        head_sha=str(head["sha"]),
        head_repository=head_repository,
        is_fork=head_repository != str(base["repo"]["full_name"]),
        html_url=str(data["html_url"]),
        updated_at=datetime.fromisoformat(str(data["updated_at"])),
        additions=_optional_int(data.get("additions")),
        deletions=_optional_int(data.get("deletions")),
        changed_files=_optional_int(data.get("changed_files")),
    )


def _next_page(response: httpx.Response) -> int | None:
    """The page number of the `Link` header's `rel="next"` URL, or None on the last page."""
    url = response.links.get("next", {}).get("url")
    page = httpx.URL(url).params.get("page") if url else None
    return int(page) if page is not None and page.isdigit() else None


def _ref(value: str) -> str:
    return quote(value, safe="")


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _expiry(now: datetime, seconds: Any) -> datetime | None:
    if seconds is None or seconds == "":
        return None
    return now + timedelta(seconds=int(seconds))
