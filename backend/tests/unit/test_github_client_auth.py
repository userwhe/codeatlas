"""The real GitHub client's sign-in methods and App JWT, against `httpx.MockTransport`."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from codeatlas.config import Settings
from codeatlas.github import client as client_module
from codeatlas.github.client import GitHubClient
from codeatlas.github.gateway import GitHubAccessDenied, GitHubUnavailable

APP_ID = "123456"
CLIENT_ID = "Iv1.testclientid"
CLIENT_SECRET = "test-client-secret"
APP_ORIGIN = "https://app.example.test"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def private_key(tmp_path: Path) -> rsa.RSAPrivateKey:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    (tmp_path / "app.pem").write_bytes(pem)
    return key


@pytest.fixture
def settings(tmp_path: Path, private_key: rsa.RSAPrivateKey) -> Settings:
    return Settings(
        _env_file=None,
        app_origin=APP_ORIGIN,
        github_app_id=APP_ID,
        github_app_client_id=CLIENT_ID,
        github_app_client_secret=CLIENT_SECRET,
        github_app_private_key_path=str(tmp_path / "app.pem"),
    )


@pytest.fixture(autouse=True)
def _clean_token_cache() -> Iterator[None]:
    client_module.clear_installation_token_cache()
    yield
    client_module.clear_installation_token_cache()


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(client_module, "_utcnow", lambda: NOW)
    return NOW


def make_client(settings: Settings, handler: Handler) -> GitHubClient:
    return GitHubClient(settings, transport=httpx.MockTransport(handler))


def form(request: httpx.Request) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


def test_authorize_url_carries_client_id_state_and_redirect_uri(settings: Settings) -> None:
    url = make_client(settings, lambda request: httpx.Response(500)).authorize_url("st@te/1")

    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "github.com",
        "/login/oauth/authorize",
    )
    query = parse_qs(parts.query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["state"] == ["st@te/1"]
    assert query["redirect_uri"] == [f"{APP_ORIGIN}/auth/github/callback"]


def test_exchange_code_parses_tokens_and_expiry_times(
    settings: Settings, frozen_now: datetime
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "ghu_access",
                "expires_in": 28800,
                "refresh_token": "ghr_refresh",
                "refresh_token_expires_in": 15897600,
                "token_type": "bearer",
                "scope": "",
            },
        )

    tokens = make_client(settings, handler).exchange_code("the-code")

    assert tokens.access_token == "ghu_access"
    assert tokens.access_token_expires_at == frozen_now + timedelta(seconds=28800)
    assert tokens.refresh_token == "ghr_refresh"
    assert tokens.refresh_token_expires_at == frozen_now + timedelta(seconds=15897600)
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://github.com/login/oauth/access_token"
    assert request.headers["accept"] == "application/json"
    assert form(request) == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": "the-code",
        "redirect_uri": f"{APP_ORIGIN}/auth/github/callback",
    }


def test_refresh_user_token_parses_tokens_and_expiry_times(
    settings: Settings, frozen_now: datetime
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "ghu_new",
                "expires_in": 3600,
                "refresh_token": "ghr_new",
                "refresh_token_expires_in": 7200,
            },
        )

    tokens = make_client(settings, handler).refresh_user_token("ghr_old")

    assert tokens.access_token == "ghu_new"
    assert tokens.access_token_expires_at == frozen_now + timedelta(hours=1)
    assert tokens.refresh_token == "ghr_new"
    assert tokens.refresh_token_expires_at == frozen_now + timedelta(hours=2)
    (request,) = seen
    assert str(request.url) == "https://github.com/login/oauth/access_token"
    assert form(request) == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": "ghr_old",
    }


def test_tokens_without_expiry_have_no_expiry_times(settings: Settings) -> None:
    client = make_client(settings, lambda request: httpx.Response(200, json={"access_token": "t"}))

    tokens = client.exchange_code("code")

    assert tokens.access_token == "t"
    assert tokens.access_token_expires_at is None
    assert tokens.refresh_token is None
    assert tokens.refresh_token_expires_at is None


@pytest.mark.parametrize("method", ["exchange_code", "refresh_user_token"])
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, {"error": "bad_verification_code", "error_description": "The code is wrong."}),
        (200, {"error": "bad_refresh_token"}),
        (400, {"error": "invalid_request"}),
        (401, {"message": "Bad credentials"}),
    ],
)
def test_token_errors_raise_access_denied(
    settings: Settings, method: str, status: int, body: dict[str, str]
) -> None:
    client = make_client(settings, lambda request: httpx.Response(status, json=body))

    with pytest.raises(GitHubAccessDenied):
        getattr(client, method)("value")


@pytest.mark.parametrize("method", ["exchange_code", "refresh_user_token"])
@pytest.mark.parametrize("status", [500, 502, 503])
def test_token_server_errors_raise_unavailable(
    settings: Settings, method: str, status: int
) -> None:
    client = make_client(settings, lambda request: httpx.Response(status, text="oops"))

    with pytest.raises(GitHubUnavailable):
        getattr(client, method)("value")


def test_token_timeout_raises_unavailable(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(GitHubUnavailable):
        make_client(settings, handler).exchange_code("code")


def test_get_authenticated_user(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"id": 583231, "login": "octocat", "name": None, "avatar_url": "https://a/1"},
        )

    user = make_client(settings, handler).get_authenticated_user("ghu_token")

    assert (user.id, user.login, user.name, user.avatar_url) == (
        583231,
        "octocat",
        None,
        "https://a/1",
    )
    (request,) = seen
    assert str(request.url) == "https://api.github.com/user"
    assert request.headers["authorization"] == "Bearer ghu_token"
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == "2022-11-28"
    assert request.headers["user-agent"]


def test_get_authenticated_user_maps_errors(settings: Settings) -> None:
    denied = make_client(settings, lambda request: httpx.Response(401, json={}))
    with pytest.raises(GitHubAccessDenied):
        denied.get_authenticated_user("revoked")

    down = make_client(settings, lambda request: httpx.Response(503))
    with pytest.raises(GitHubUnavailable):
        down.get_authenticated_user("token")


def test_app_jwt_is_rs256_with_app_id_issuer_and_short_expiry(
    settings: Settings, private_key: rsa.RSAPrivateKey
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": 42})

    installation_id = make_client(settings, handler).get_installation_id("octo/repo")

    assert installation_id == 42
    (request,) = seen
    assert str(request.url) == "https://api.github.com/repos/octo/repo/installation"
    scheme, token = request.headers["authorization"].split(" ")
    assert scheme == "Bearer"
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(token, private_key.public_key(), algorithms=["RS256"])
    assert claims["iss"] == APP_ID
    assert claims["exp"] - claims["iat"] <= 10 * 60
    now = datetime.now(UTC).timestamp()
    assert claims["iat"] <= now
    assert now < claims["exp"] <= now + 10 * 60
