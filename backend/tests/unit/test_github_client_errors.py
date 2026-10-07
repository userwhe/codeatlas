"""How the real GitHub client classifies failures (research R4), against `httpx.MockTransport`.

A rejected user authorization means the user must sign in again, and rejected App credentials mean
the App is misconfigured. Neither is an answer about a repository's access.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from codeatlas.config import Settings
from codeatlas.github import client as client_module
from codeatlas.github.client import GitHubClient
from codeatlas.github.gateway import (
    AppCredentialsRejected,
    GitHubAccessDenied,
    GitHubError,
    GitHubNotFound,
    GitHubUnavailable,
    UserAuthorizationInvalid,
)

API = "api.github.com"
SHA = "0123456789abcdef0123456789abcdef01234567"
CODELOAD_PATH = f"codeload.github.com/octo/a/legacy.tar.gz/{SHA}"

INSTALLATIONS = f"GET {API}/user/installations"
INSTALLATION_REPOSITORIES = f"GET {API}/user/installations/1/repositories"
REPOSITORY = f"GET {API}/repositories/10"
USER = f"GET {API}/user"
APP_INSTALLATION = f"GET {API}/repos/octo/a/installation"
TOKEN_MINT = f"POST {API}/app/installations/7/access_tokens"
BRANCH = f"GET {API}/repos/octo/a/branches/main"
TARBALL = f"GET {API}/repos/octo/a/tarball/{SHA}"
TOKEN_REQUEST = "POST github.com/login/oauth/access_token"

PLAIN_403 = {"x-ratelimit-remaining": "4999"}

Route = Callable[[httpx.Request], httpx.Response]
Call = Callable[[GitHubClient], object]


def respond(status: int = 200, **kwargs: object) -> Route:
    return lambda request: httpx.Response(status, **kwargs)  # type: ignore[arg-type]


def healthy_routes() -> dict[str, Route]:
    """A successful answer for every request, so a test can make exactly one of them fail."""
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repository = {"id": 10, "full_name": "octo/a", "default_branch": "main", "private": False}
    return {
        INSTALLATIONS: respond(json={"total_count": 1, "installations": [{"id": 1}]}),
        INSTALLATION_REPOSITORIES: respond(json={"total_count": 1, "repositories": [repository]}),
        REPOSITORY: respond(json=repository),
        USER: respond(json={"id": 1, "login": "octocat"}),
        APP_INSTALLATION: respond(json={"id": 7}),
        TOKEN_MINT: respond(201, json={"token": "ghs_installation", "expires_at": expires_at}),
        BRANCH: respond(json={"name": "main", "commit": {"sha": SHA}}),
        TARBALL: respond(302, headers={"Location": f"https://{CODELOAD_PATH}"}),
        f"GET {CODELOAD_PATH}": respond(200, content=b""),
        TOKEN_REQUEST: respond(json={"access_token": "ghu_new", "refresh_token": "ghr_new"}),
    }


class GitHubRoutes:
    """Answers every request successfully except `failing`, and records the requests."""

    def __init__(self, failing: str, failure: Route):
        self.routes = {**healthy_routes(), failing: failure}
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.host}{request.url.path}"
        self.requests.append(key)
        route = self.routes.get(key)
        if route is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return route(request)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    (tmp_path / "app.pem").write_bytes(pem)
    return Settings(
        _env_file=None,
        app_origin="https://app.example.test",
        github_app_id="123456",
        github_app_client_id="Iv1.testclientid",
        github_app_client_secret="test-client-secret",
        github_app_private_key_path=str(tmp_path / "app.pem"),
    )


@pytest.fixture(autouse=True)
def _clean_token_cache() -> Iterator[None]:
    client_module.clear_installation_token_cache()
    yield
    client_module.clear_installation_token_cache()


def call_failing(settings: Settings, call: Call, failing: str, failure: Route) -> GitHubError:
    """Run `call` with only `failing` answered by `failure`; return the gateway error it raises."""
    routes = GitHubRoutes(failing, failure)
    with pytest.raises(GitHubError) as caught:
        call(GitHubClient(settings, transport=httpx.MockTransport(routes)))
    assert failing in routes.requests
    return caught.value


def enter_tarball(client: GitHubClient) -> None:
    with client.open_tarball(7, "octo/a", SHA):
        pass


def resolve_main(client: GitHubClient) -> object:
    return client.resolve_commit(7, "octo/a", "main")


def get_repository(client: GitHubClient) -> object:
    return client.get_repository("ghu_user", 10)


def refresh(client: GitHubClient) -> object:
    return client.refresh_user_token("ghr_old")


def exchange(client: GitHubClient) -> object:
    return client.exchange_code("stale-code")


# Each call, and the request that fails for it.
USER_TOKEN_CALLS: dict[str, tuple[Call, str]] = {
    "get_repository": (get_repository, REPOSITORY),
    "list_installation_ids": (lambda c: c.list_installation_ids("ghu_user"), INSTALLATIONS),
    "list_accessible_repositories": (
        lambda c: c.list_accessible_repositories("ghu_user"),
        INSTALLATIONS,
    ),
    "list_accessible_repositories_per_installation": (
        lambda c: c.list_accessible_repositories("ghu_user"),
        INSTALLATION_REPOSITORIES,
    ),
    "get_authenticated_user": (lambda c: c.get_authenticated_user("ghu_user"), USER),
}
APP_CREDENTIAL_CALLS: dict[str, tuple[Call, str]] = {
    "get_installation_id": (lambda c: c.get_installation_id("octo/a"), APP_INSTALLATION),
    "resolve_commit_token": (resolve_main, TOKEN_MINT),
    "open_tarball_token": (enter_tarball, TOKEN_MINT),
}
INSTALLATION_TOKEN_CALLS: dict[str, tuple[Call, str]] = {
    "installation_token": (resolve_main, TOKEN_MINT),
    "resolve_commit": (resolve_main, BRANCH),
    "open_tarball": (enter_tarball, TARBALL),
}
ALL_CALLS: dict[str, tuple[Call, str]] = {
    **USER_TOKEN_CALLS,
    **APP_CREDENTIAL_CALLS,
    **INSTALLATION_TOKEN_CALLS,
    "refresh_user_token": (refresh, TOKEN_REQUEST),
}


def test_the_new_errors_fit_the_existing_hierarchy() -> None:
    # Callers that catch `GitHubAccessDenied` keep treating a rejected user token as a denial.
    assert issubclass(UserAuthorizationInvalid, GitHubAccessDenied)
    # Rejected App credentials say nothing about access, so they are not a denial.
    assert issubclass(AppCredentialsRejected, GitHubError)
    assert not issubclass(AppCredentialsRejected, GitHubAccessDenied)


# The user's authorization


@pytest.mark.parametrize(("call", "failing"), USER_TOKEN_CALLS.values(), ids=USER_TOKEN_CALLS)
def test_a_401_on_a_user_token_call_means_the_user_authorization_is_invalid(
    settings: Settings, call: Call, failing: str
) -> None:
    error = call_failing(settings, call, failing, respond(401, json={"message": "Bad credentials"}))

    assert isinstance(error, UserAuthorizationInvalid)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, {"error": "bad_refresh_token", "error_description": "The refresh token is wrong."}),
        (200, {"token_type": "bearer"}),
        (400, {"error": "invalid_request"}),
        (401, {"message": "Bad credentials"}),
    ],
)
def test_a_refused_token_refresh_means_the_user_authorization_is_invalid(
    settings: Settings, status: int, body: dict[str, str]
) -> None:
    error = call_failing(settings, refresh, TOKEN_REQUEST, respond(status, json=body))

    assert isinstance(error, UserAuthorizationInvalid)


def test_a_refused_code_exchange_means_the_user_authorization_is_invalid(
    settings: Settings,
) -> None:
    refused = respond(200, json={"error": "bad_verification_code"})

    assert isinstance(
        call_failing(settings, exchange, TOKEN_REQUEST, refused), UserAuthorizationInvalid
    )


@pytest.mark.parametrize("request_tokens", [exchange, refresh], ids=["exchange", "refresh"])
def test_a_rejected_client_secret_means_the_app_credentials_are_rejected(
    settings: Settings, request_tokens: Call
) -> None:
    rejected = respond(200, json={"error": "incorrect_client_credentials"})

    error = call_failing(settings, request_tokens, TOKEN_REQUEST, rejected)

    # A wrong client secret is the App's problem; it must not send every user back to sign-in.
    assert isinstance(error, AppCredentialsRejected)


# The App's own credentials


@pytest.mark.parametrize(
    ("call", "failing"), APP_CREDENTIAL_CALLS.values(), ids=APP_CREDENTIAL_CALLS
)
def test_a_401_on_an_app_jwt_call_means_the_app_credentials_are_rejected(
    settings: Settings, call: Call, failing: str
) -> None:
    error = call_failing(settings, call, failing, respond(401, json={"message": "Bad credentials"}))

    assert isinstance(error, AppCredentialsRejected)
    assert not isinstance(error, GitHubAccessDenied)


# Answers about access


@pytest.mark.parametrize(
    "failure",
    [respond(404, json={"message": "Not Found"}), respond(403, json={}, headers=PLAIN_403)],
    ids=["404", "plain-403"],
)
def test_get_repository_reports_an_invisible_repository_as_not_found(
    settings: Settings, failure: Route
) -> None:
    assert isinstance(call_failing(settings, get_repository, REPOSITORY, failure), GitHubNotFound)


@pytest.mark.parametrize(
    ("call", "failing"), INSTALLATION_TOKEN_CALLS.values(), ids=INSTALLATION_TOKEN_CALLS
)
def test_a_plain_403_on_an_installation_token_call_is_an_access_denial(
    settings: Settings, call: Call, failing: str
) -> None:
    error = call_failing(settings, call, failing, respond(403, json={}, headers=PLAIN_403))

    assert isinstance(error, GitHubAccessDenied)
    assert not isinstance(error, UserAuthorizationInvalid)


@pytest.mark.parametrize(
    ("call", "failing"),
    [(resolve_main, BRANCH), (enter_tarball, TARBALL)],
    ids=["resolve_commit", "open_tarball"],
)
def test_a_401_with_an_installation_token_is_an_access_denial(
    settings: Settings, call: Call, failing: str
) -> None:
    error = call_failing(settings, call, failing, respond(401, json={"message": "Bad credentials"}))

    # Neither the user nor the App credentials: the installation can no longer read.
    assert type(error) is GitHubAccessDenied


# GitHub is unavailable


@pytest.mark.parametrize(
    "failure",
    [
        respond(429),
        respond(429, headers={"retry-after": "60"}),
        respond(500),
        respond(503),
        respond(403, json={}, headers={"x-ratelimit-remaining": "0"}),
        respond(403, json={}, headers={"retry-after": "60"}),
    ],
    ids=["429", "429-retry-after", "500", "503", "403-primary-limit", "403-secondary-limit"],
)
@pytest.mark.parametrize(("call", "failing"), ALL_CALLS.values(), ids=ALL_CALLS)
def test_rate_limits_and_server_errors_mean_github_is_unavailable(
    settings: Settings, failure: Route, call: Call, failing: str
) -> None:
    assert isinstance(call_failing(settings, call, failing, failure), GitHubUnavailable)


# Cached installation tokens


@pytest.mark.parametrize(
    "failure",
    [respond(401, json={"message": "Bad credentials"}), respond(403, json={}, headers=PLAIN_403)],
    ids=["401", "plain-403"],
)
@pytest.mark.parametrize(
    ("call", "failing"),
    [(resolve_main, BRANCH), (enter_tarball, TARBALL)],
    ids=["resolve_commit", "open_tarball"],
)
def test_a_rejected_installation_token_is_not_reused(
    settings: Settings, call: Call, failing: str, failure: Route
) -> None:
    routes = GitHubRoutes(failing, failure)
    client = GitHubClient(settings, transport=httpx.MockTransport(routes))
    with pytest.raises(GitHubAccessDenied):
        call(client)

    # Access returns; the next call must not reuse the token GitHub rejected.
    routes.routes[failing] = healthy_routes()[failing]
    call(client)

    assert routes.requests.count(TOKEN_MINT) == 2


@pytest.mark.parametrize(
    "failure",
    [respond(404, json={"message": "Not Found"}), respond(429), respond(500)],
    ids=["404", "429", "500"],
)
def test_other_failures_keep_the_cached_installation_token(
    settings: Settings, failure: Route
) -> None:
    routes = GitHubRoutes(BRANCH, failure)
    client = GitHubClient(settings, transport=httpx.MockTransport(routes))
    with pytest.raises(GitHubError):
        resolve_main(client)

    routes.routes[BRANCH] = healthy_routes()[BRANCH]
    resolve_main(client)

    assert routes.requests.count(TOKEN_MINT) == 1
