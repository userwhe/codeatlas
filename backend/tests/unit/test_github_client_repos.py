"""The real GitHub client's repository methods, against `httpx.MockTransport`."""

import io
import random
import tarfile
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
    BranchNotFound,
    GitHubAccessDenied,
    GitHubNotFound,
    GitHubRepository,
    GitHubUnavailable,
    RepositoryEmpty,
)

API = "api.github.com"
SHA = "0123456789abcdef0123456789abcdef01234567"
CODELOAD_URL = f"https://codeload.github.com/octo/repo/legacy.tar.gz/{SHA}?token=archive-secret"

Route = Callable[[httpx.Request], httpx.Response]


class FakeGitHub:
    """Routes `"<METHOD> <host><path>"` to a response factory and records every request."""

    def __init__(self, routes: dict[str, Route]):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(f"{request.method} {request.url.host}{request.url.path}")
        if route is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return route(request)

    def calls(self, key: str) -> list[httpx.Request]:
        return [r for r in self.requests if f"{r.method} {r.url.host}{r.url.path}" == key]


def respond(status: int = 200, **kwargs: object) -> Route:
    return lambda request: httpx.Response(status, **kwargs)  # type: ignore[arg-type]


def token_route(expires_at: datetime) -> Route:
    stamp = expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return respond(201, json={"token": "ghs_installation", "expires_at": stamp})


def fresh_token_route() -> Route:
    return token_route(datetime.now(UTC) + timedelta(hours=1))


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


def make_client(settings: Settings, fake: FakeGitHub) -> GitHubClient:
    return GitHubClient(settings, transport=httpx.MockTransport(fake))


def repo_json(repo_id: int, full_name: str, private: bool = False) -> dict[str, object]:
    return {"id": repo_id, "full_name": full_name, "default_branch": "main", "private": private}


def make_tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(f"octo-repo-{SHA[:7]}/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class ChunkedBody(httpx.SyncByteStream):
    """A streamed body in small chunks that remembers whether it was closed."""

    def __init__(self, data: bytes, chunk_size: int = 997, fail_after: int | None = None):
        self.data = data
        self.chunk_size = chunk_size
        self.fail_after = fail_after
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for index, start in enumerate(range(0, len(self.data), self.chunk_size)):
            if self.fail_after is not None and index >= self.fail_after:
                raise httpx.ReadTimeout("timed out")
            yield self.data[start : start + self.chunk_size]

    def close(self) -> None:
        self.closed = True


# list_accessible_repositories


def test_list_accessible_repositories_paginates_across_installations(settings: Settings) -> None:
    def installations(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200,
                json={
                    "total_count": 3,
                    "installations": [
                        {"id": 2, "suspended_at": None},
                        {"id": 3, "suspended_at": "2026-01-01T00:00:00Z"},
                    ],
                },
            )
        next_url = f"https://{API}/user/installations?per_page=100&page=2"
        return httpx.Response(
            200,
            json={"total_count": 3, "installations": [{"id": 1}]},
            headers={"Link": f'<{next_url}>; rel="next", <{next_url}>; rel="last"'},
        )

    def installation_1_repos(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200, json={"total_count": 3, "repositories": [repo_json(12, "octo/c")]}
            )
        next_url = f"https://{API}/user/installations/1/repositories?per_page=100&page=2"
        return httpx.Response(
            200,
            json={
                "total_count": 3,
                "repositories": [repo_json(10, "octo/a"), repo_json(11, "octo/b", True)],
            },
            headers={"Link": f'<{next_url}>; rel="next"'},
        )

    fake = FakeGitHub(
        {
            f"GET {API}/user/installations": installations,
            f"GET {API}/user/installations/1/repositories": installation_1_repos,
            f"GET {API}/user/installations/2/repositories": respond(
                200, json={"total_count": 1, "repositories": [repo_json(20, "hubot/d")]}
            ),
        }
    )

    repositories = make_client(settings, fake).list_accessible_repositories("ghu_user")

    assert repositories == [
        GitHubRepository(10, "octo/a", "main", False, installation_id=1),
        GitHubRepository(11, "octo/b", "main", True, installation_id=1),
        GitHubRepository(12, "octo/c", "main", False, installation_id=1),
        GitHubRepository(20, "hubot/d", "main", False, installation_id=2),
    ]
    assert len(fake.calls(f"GET {API}/user/installations")) == 2
    assert len(fake.calls(f"GET {API}/user/installations/1/repositories")) == 2
    assert fake.calls(f"GET {API}/user/installations/3/repositories") == []
    assert all(r.headers["authorization"] == "Bearer ghu_user" for r in fake.requests)
    assert fake.requests[0].url.params["per_page"] == "100"


# get_repository and get_installation_id


def test_get_repository_returns_the_repository(settings: Settings) -> None:
    fake = FakeGitHub({f"GET {API}/repositories/10": respond(json=repo_json(10, "octo/a", True))})

    repository = make_client(settings, fake).get_repository("ghu_user", 10)

    assert repository == GitHubRepository(10, "octo/a", "main", True)
    assert fake.requests[0].headers["authorization"] == "Bearer ghu_user"


@pytest.mark.parametrize(
    ("status", "headers", "error"),
    [
        (404, {}, GitHubNotFound),
        (403, {"x-ratelimit-remaining": "4999"}, GitHubNotFound),
        (401, {}, GitHubAccessDenied),
        (403, {"x-ratelimit-remaining": "0"}, GitHubUnavailable),
        (403, {"retry-after": "60"}, GitHubUnavailable),
        (429, {"retry-after": "60"}, GitHubUnavailable),
        (502, {}, GitHubUnavailable),
    ],
)
def test_get_repository_maps_errors(
    settings: Settings, status: int, headers: dict[str, str], error: type[Exception]
) -> None:
    fake = FakeGitHub(
        {f"GET {API}/repositories/10": respond(status, json={"message": "x"}, headers=headers)}
    )

    with pytest.raises(error):
        make_client(settings, fake).get_repository("ghu_user", 10)


def test_get_installation_id_uses_the_app_jwt_and_maps_404(settings: Settings) -> None:
    fake = FakeGitHub({f"GET {API}/repos/octo/a/installation": respond(json={"id": 77})})
    client = make_client(settings, fake)

    assert client.get_installation_id("octo/a") == 77
    scheme, token = fake.requests[0].headers["authorization"].split(" ")
    assert scheme == "Bearer" and token.count(".") == 2

    with pytest.raises(GitHubNotFound):
        client.get_installation_id("octo/not-installed")


# resolve_commit


def test_resolve_commit_returns_the_branch_head_sha(settings: Settings) -> None:
    fake = FakeGitHub(
        {
            f"POST {API}/app/installations/7/access_tokens": fresh_token_route(),
            f"GET {API}/repos/octo/a/branches/feature/x": respond(
                json={"name": "feature/x", "commit": {"sha": SHA}}
            ),
        }
    )

    assert make_client(settings, fake).resolve_commit(7, "octo/a", "feature/x") == SHA
    (branch_request,) = fake.calls(f"GET {API}/repos/octo/a/branches/feature/x")
    assert branch_request.url.raw_path == b"/repos/octo/a/branches/feature%2Fx"
    assert branch_request.headers["authorization"] == "Bearer ghs_installation"
    (token_request,) = fake.calls(f"POST {API}/app/installations/7/access_tokens")
    assert token_request.headers["authorization"].startswith("Bearer ey")


@pytest.mark.parametrize(
    ("branches", "error"), [([{"name": "main"}], BranchNotFound), ([], RepositoryEmpty)]
)
def test_resolve_commit_distinguishes_unknown_branch_and_empty_repository(
    settings: Settings, branches: list[dict[str, str]], error: type[Exception]
) -> None:
    fake = FakeGitHub(
        {
            f"POST {API}/app/installations/7/access_tokens": fresh_token_route(),
            f"GET {API}/repos/octo/a/branches": respond(json=branches),
        }
    )

    with pytest.raises(error):
        make_client(settings, fake).resolve_commit(7, "octo/a", "nope")
    (listing,) = fake.calls(f"GET {API}/repos/octo/a/branches")
    assert listing.url.params["per_page"] == "1"


def test_installation_tokens_are_cached_until_one_minute_before_expiry(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    clock = [start]
    monkeypatch.setattr(client_module, "_utcnow", lambda: clock[0])
    fake = FakeGitHub(
        {
            f"POST {API}/app/installations/7/access_tokens": token_route(
                start + timedelta(hours=1)
            ),
            f"GET {API}/repos/octo/a/branches/main": respond(json={"commit": {"sha": SHA}}),
        }
    )
    mints = f"POST {API}/app/installations/7/access_tokens"

    make_client(settings, fake).resolve_commit(7, "octo/a", "main")
    clock[0] = start + timedelta(minutes=58, seconds=59)
    make_client(settings, fake).resolve_commit(7, "octo/a", "main")
    assert len(fake.calls(mints)) == 1

    clock[0] = start + timedelta(minutes=59, seconds=1)
    make_client(settings, fake).resolve_commit(7, "octo/a", "main")
    assert len(fake.calls(mints)) == 2


def test_installation_tokens_are_cached_per_installation(settings: Settings) -> None:
    fake = FakeGitHub(
        {
            f"POST {API}/app/installations/7/access_tokens": fresh_token_route(),
            f"POST {API}/app/installations/8/access_tokens": fresh_token_route(),
            f"GET {API}/repos/octo/a/branches/main": respond(json={"commit": {"sha": SHA}}),
        }
    )
    client = make_client(settings, fake)

    for installation_id in (7, 8, 7, 8):
        client.resolve_commit(installation_id, "octo/a", "main")

    assert len(fake.calls(f"POST {API}/app/installations/7/access_tokens")) == 1
    assert len(fake.calls(f"POST {API}/app/installations/8/access_tokens")) == 1


# open_tarball


def tarball_routes(location: str, body: httpx.SyncByteStream | None = None) -> dict[str, Route]:
    routes = {
        f"POST {API}/app/installations/7/access_tokens": fresh_token_route(),
        f"GET {API}/repos/octo/repo/tarball/{SHA}": respond(302, headers={"Location": location}),
    }
    if body is not None:
        routes[f"GET codeload.github.com/octo/repo/legacy.tar.gz/{SHA}"] = respond(
            200, stream=body, headers={"Content-Type": "application/x-gzip"}
        )
    return routes


def test_open_tarball_streams_a_tar_gz_readable_by_tarfile(settings: Settings) -> None:
    files = {"README.md": b"# Hello\n", "src/app.py": b"print('hi')\n" * 2000}
    body = ChunkedBody(make_tarball(files))
    fake = FakeGitHub(tarball_routes(CODELOAD_URL, body))

    read: dict[str, bytes] = {}
    with make_client(settings, fake).open_tarball(7, "octo/repo", SHA) as stream:
        with tarfile.open(fileobj=stream, mode="r|gz") as archive:
            for member in archive:
                extracted = archive.extractfile(member)
                assert extracted is not None
                read[member.name.split("/", 1)[1]] = extracted.read()

    assert read == files
    api_request, download = fake.requests[1:]
    assert api_request.headers["authorization"] == "Bearer ghs_installation"
    assert download.url.host == "codeload.github.com"
    assert "authorization" not in download.headers


def test_open_tarball_closes_the_download_when_the_context_exits(settings: Settings) -> None:
    body = ChunkedBody(make_tarball({"README.md": b"# Hello\n" * 5000}))
    fake = FakeGitHub(tarball_routes(CODELOAD_URL, body))

    with make_client(settings, fake).open_tarball(7, "octo/repo", SHA) as stream:
        assert stream.read(2) == b"\x1f\x8b"
        assert not body.closed

    assert body.closed


@pytest.mark.parametrize(
    "location",
    [
        f"https://evil.example.com/octo/repo/legacy.tar.gz/{SHA}",
        f"https://codeload.github.com.evil.example.com/octo/repo/legacy.tar.gz/{SHA}",
        f"http://codeload.github.com/octo/repo/legacy.tar.gz/{SHA}",
        f"/octo/repo/legacy.tar.gz/{SHA}",
    ],
)
def test_open_tarball_rejects_redirects_to_other_hosts(settings: Settings, location: str) -> None:
    fake = FakeGitHub(tarball_routes(location))

    with pytest.raises(GitHubUnavailable, match="codeload.github.com"):
        with make_client(settings, fake).open_tarball(7, "octo/repo", SHA):
            pass

    assert [r.url.host for r in fake.requests] == [API, API]


def test_open_tarball_maps_a_mid_stream_timeout(settings: Settings) -> None:
    body = ChunkedBody(make_tarball({"big.bin": random.Random(0).randbytes(50_000)}), fail_after=2)
    fake = FakeGitHub(tarball_routes(CODELOAD_URL, body))

    with pytest.raises(GitHubUnavailable, match="tarball download failed"):
        with make_client(settings, fake).open_tarball(7, "octo/repo", SHA) as stream:
            with tarfile.open(fileobj=stream, mode="r|gz") as archive:
                for member in archive:
                    extracted = archive.extractfile(member)
                    assert extracted is not None
                    extracted.read()
    assert body.closed


def test_open_tarball_maps_a_failed_download(settings: Settings) -> None:
    routes = tarball_routes(CODELOAD_URL)
    routes[f"GET codeload.github.com/octo/repo/legacy.tar.gz/{SHA}"] = respond(503)
    fake = FakeGitHub(routes)

    with pytest.raises(GitHubUnavailable):
        with make_client(settings, fake).open_tarball(7, "octo/repo", SHA):
            pass


# Server errors and timeouts


def server_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"message": "Server Error"})


def timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


def connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def enter_tarball(client: GitHubClient) -> None:
    with client.open_tarball(7, "octo/repo", SHA):
        pass


# Each call, and the request that fails for it.
CALLS: dict[str, tuple[Callable[[GitHubClient], object], str]] = {
    "list_accessible_repositories": (
        lambda c: c.list_accessible_repositories("ghu_user"),
        f"GET {API}/user/installations",
    ),
    "get_repository": (lambda c: c.get_repository("ghu_user", 10), f"GET {API}/repositories/10"),
    "get_installation_id": (
        lambda c: c.get_installation_id("octo/a"),
        f"GET {API}/repos/octo/a/installation",
    ),
    "resolve_commit": (
        lambda c: c.resolve_commit(7, "octo/a", "main"),
        f"GET {API}/repos/octo/a/branches/main",
    ),
    "installation_token": (
        lambda c: c.resolve_commit(7, "octo/a", "main"),
        f"POST {API}/app/installations/7/access_tokens",
    ),
    "open_tarball": (enter_tarball, f"GET {API}/repos/octo/repo/tarball/{SHA}"),
}


@pytest.mark.parametrize("failure", [server_error, timeout, connect_error])
@pytest.mark.parametrize(("call", "failing"), CALLS.values(), ids=CALLS.keys())
def test_server_errors_and_timeouts_raise_unavailable(
    settings: Settings, failure: Route, call: Callable[[GitHubClient], object], failing: str
) -> None:
    fake = FakeGitHub({f"POST {API}/app/installations/7/access_tokens": fresh_token_route()})
    fake.routes[failing] = failure

    with pytest.raises(GitHubUnavailable):
        call(make_client(settings, fake))
    assert fake.calls(failing)
