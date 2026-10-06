"""The real GitHub gateway over httpx (research R5, ADR 0005).

- Sign-in uses the App's user authorization (OAuth web flow) with expiring user tokens.
- Repository reads use short-lived installation tokens, cached in memory and never stored.
- Every failure maps to a gateway error from `codeatlas.github.gateway`.
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
    BranchNotFound,
    GitHubAccessDenied,
    GitHubError,
    GitHubNotFound,
    GitHubRepository,
    GitHubUnavailable,
    GitHubUser,
    RepositoryEmpty,
    UserTokens,
)

GITHUB_WEB = "https://github.com"
GITHUB_API = "https://api.github.com"
TARBALL_HOST = "codeload.github.com"
USER_AGENT = "codeatlas"
TIMEOUT = httpx.Timeout(10.0)
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
        _check(response)
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
        # A user token only reaches what both the user and the installation can access, so a
        # plain 403 means the same as 404 here (FR-003).
        _check(response, forbidden=GitHubNotFound)
        return _repository(_json(response))

    def get_installation_id(self, full_name: str) -> int:
        with self._client() as http:
            response = self._api(
                http, "GET", f"{_repo_path(full_name)}/installation", self._app_jwt()
            )
        _check(response)
        return int(_json(response)["id"])

    def resolve_commit(self, installation_id: int, full_name: str, branch: str) -> str:
        repo = _repo_path(full_name)
        with self._client() as http:
            token = self._installation_token(http, installation_id)
            response = self._api(http, "GET", f"{repo}/branches/{quote(branch, safe='')}", token)
            if response.status_code == 404:
                branches = self._api(http, "GET", f"{repo}/branches", token, params={"per_page": 1})
                _check(branches)
                if not _json(branches):
                    raise RepositoryEmpty(f"{full_name} has no branches")
                raise BranchNotFound(f"branch {branch!r} not found in {full_name}")
        _check(response)
        return str(_json(response)["commit"]["sha"])

    @contextmanager
    def open_tarball(self, installation_id: int, full_name: str, sha: str) -> Iterator[IO[bytes]]:
        with self._client() as http:
            token = self._installation_token(http, installation_id)
            path = f"{_repo_path(full_name)}/tarball/{quote(sha, safe='')}"
            response = self._api(http, "GET", path, token, follow_redirects=False)
            if not response.is_redirect:
                _check(response)
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
                    _check(download)
                    raise GitHubUnavailable(f"tarball download returned {download.status_code}")
                yield io.BufferedReader(_ResponseStream(download.iter_bytes()))
            finally:
                download.close()

    # Helpers

    def _client(self) -> httpx.Client:
        return httpx.Client(
            transport=self._transport,
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    def _redirect_uri(self) -> str:
        return f"{self._settings.app_origin.rstrip('/')}/auth/github/callback"

    def _api(
        self,
        http: httpx.Client,
        method: str,
        path: str,
        token: str,
        *,
        params: Mapping[str, int] | None = None,
        follow_redirects: bool = True,
    ) -> httpx.Response:
        url = path if path.startswith("https://") else f"{GITHUB_API}{path}"
        headers = {**API_HEADERS, "Authorization": f"Bearer {token}"}
        request = http.build_request(method, url, headers=headers, params=params)
        return _send(http, request, follow_redirects=follow_redirects)

    def _paginate(
        self, http: httpx.Client, path: str, key: str, token: str
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        url: str | None = path
        params: Mapping[str, int] | None = {"per_page": PAGE_SIZE}
        while url is not None:
            response = self._api(http, "GET", url, token, params=params)
            _check(response)
            items.extend(_json(response)[key])
            url = response.links.get("next", {}).get("url")
            params = None  # The next-page URL already carries the query.
        return items

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
            raise GitHubAccessDenied(f"GitHub rejected the token request ({response.status_code})")
        body = _json(response)
        if not isinstance(body, dict) or "error" in body or not body.get("access_token"):
            error = body.get("error") if isinstance(body, dict) else None
            raise GitHubAccessDenied(f"GitHub rejected the token request ({error or 'no token'})")
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
        _check(response)
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


def _check(response: httpx.Response, *, forbidden: type[GitHubError] = GitHubAccessDenied) -> None:
    """Raise the gateway error for an error response; do nothing for success."""
    status = response.status_code
    if status < 400:
        return
    where = f"{response.request.method} {response.request.url.path}"
    if _unavailable(response):
        raise GitHubUnavailable(f"GitHub returned {status} for {where}")
    if status == 401:
        raise GitHubAccessDenied(f"GitHub rejected the credentials for {where}")
    if status == 403:
        raise forbidden(f"GitHub denied {where}")
    if status == 404:
        raise GitHubNotFound(f"GitHub found nothing for {where}")
    raise GitHubError(f"GitHub returned {status} for {where}")


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


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _expiry(now: datetime, seconds: Any) -> datetime | None:
    if seconds is None or seconds == "":
        return None
    return now + timedelta(seconds=int(seconds))
