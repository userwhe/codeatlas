"""The real GitHub client's lookup of a user by login (T014, research R13), against
`httpx.MockTransport`. The pilot's operator command resolves a login to its numeric user ID.
"""

from collections.abc import Callable

import httpx
import pytest

from codeatlas.config import Settings
from codeatlas.github.client import GitHubClient
from codeatlas.github.gateway import GitHubNotFound, GitHubUnavailable, GitHubUser

USER = {
    "id": 583231,
    "login": "octocat",
    "name": "The Octocat",
    "avatar_url": "https://avatars.githubusercontent.com/u/583231?v=4",
    "type": "User",
    "site_admin": False,
}

Route = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """Answers every request with `route` and records the requests."""

    def __init__(self, route: Route):
        self.route = route
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.route(request)


def respond(status: int = 200, **kwargs: object) -> Route:
    return lambda request: httpx.Response(status, **kwargs)  # type: ignore[arg-type]


def make_client(recorder: Recorder) -> GitHubClient:
    settings = Settings(_env_file=None, github_app_client_secret="test-client-secret")  # type: ignore[call-arg]
    return GitHubClient(settings, transport=httpx.MockTransport(recorder))


def test_get_user_by_login_reads_public_data_without_credentials() -> None:
    recorder = Recorder(respond(json=USER))

    user = make_client(recorder).get_user_by_login("octocat")

    assert user == GitHubUser(
        id=583231,
        login="octocat",
        name="The Octocat",
        avatar_url="https://avatars.githubusercontent.com/u/583231?v=4",
    )
    (request,) = recorder.requests
    assert (request.method, request.url.host, request.url.path) == (
        "GET",
        "api.github.com",
        "/users/octocat",
    )
    assert "authorization" not in request.headers


def test_a_user_without_a_name_or_avatar() -> None:
    recorder = Recorder(respond(json={"id": 7, "login": "hubot", "name": None}))

    user = make_client(recorder).get_user_by_login("hubot")

    assert user == GitHubUser(id=7, login="hubot", name=None, avatar_url=None)


def test_an_unknown_login_is_not_found() -> None:
    recorder = Recorder(respond(404, json={"message": "Not Found"}))

    with pytest.raises(GitHubNotFound):
        make_client(recorder).get_user_by_login("nobody-by-this-name")


@pytest.mark.parametrize(
    "route",
    [
        respond(502, json={"message": "Bad Gateway"}),
        respond(503),
        respond(429, headers={"retry-after": "30"}),
        respond(403, headers={"x-ratelimit-remaining": "0"}, json={"message": "rate limit"}),
    ],
    ids=["502", "503", "429", "403-rate-limited"],
)
def test_outages_and_rate_limits_are_unavailable(route: Route) -> None:
    with pytest.raises(GitHubUnavailable):
        make_client(Recorder(route)).get_user_by_login("octocat")


def test_an_unreachable_github_is_unavailable() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(GitHubUnavailable):
        make_client(Recorder(unreachable)).get_user_by_login("octocat")
