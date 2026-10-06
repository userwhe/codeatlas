"""The real GitHub gateway over httpx (research R5, ADR 0005).

- Sign-in uses the App's user authorization (OAuth web flow) with expiring user tokens.
- Repository reads use short-lived installation tokens, cached in memory and never stored.
- Every failure maps to a gateway error from `codeatlas.github.gateway`.
"""

import threading
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any
from urllib.parse import quote, urlencode

import httpx
import jwt

from codeatlas.config import Settings
from codeatlas.github.gateway import (
    GitHubAccessDenied,
    GitHubError,
    GitHubNotFound,
    GitHubRepository,
    GitHubUnavailable,
    GitHubUser,
    UserTokens,
)

GITHUB_WEB = "https://github.com"
GITHUB_API = "https://api.github.com"
USER_AGENT = "codeatlas"
TIMEOUT = httpx.Timeout(10.0)
API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
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

    def get_installation_id(self, full_name: str) -> int:
        with self._client() as http:
            response = self._api(
                http, "GET", f"{_repo_path(full_name)}/installation", self._app_jwt()
            )
        _check(response)
        return int(_json(response)["id"])

    # Helpers

    # Repository access (listing, access checks, commits, tarballs) comes with the indexing work.
    def list_accessible_repositories(self, user_token: str) -> list[GitHubRepository]:
        raise NotImplementedError

    def get_repository(self, user_token: str, github_repository_id: int) -> GitHubRepository:
        raise NotImplementedError

    def resolve_commit(self, installation_id: int, full_name: str, branch: str) -> str:
        raise NotImplementedError

    def open_tarball(
        self, installation_id: int, full_name: str, sha: str
    ) -> AbstractContextManager[IO[bytes]]:
        raise NotImplementedError

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


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _expiry(now: datetime, seconds: Any) -> datetime | None:
    if seconds is None or seconds == "":
        return None
    return now + timedelta(seconds=int(seconds))
