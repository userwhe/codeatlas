"""The real GitHub client's pull request calls (research R1, R2), against `httpx.MockTransport`."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from codeatlas.config import Settings
from codeatlas.github import client as client_module
from codeatlas.github.client import GitHubClient
from codeatlas.github.gateway import (
    AppCredentialsRejected,
    CommitUnavailable,
    Comparison,
    GitHubAccessDenied,
    GitHubError,
    GitHubNotFound,
    GitHubUnavailable,
    InstallationPermissions,
    NoCommonHistory,
    PullRequest,
    PullRequestPage,
    UserAuthorizationInvalid,
)

API = "api.github.com"
BASE_SHA = "1111111111111111111111111111111111111111"
HEAD_SHA = "2222222222222222222222222222222222222222"
MERGE_BASE_SHA = "3333333333333333333333333333333333333333"

PULLS = f"GET {API}/repos/o/r/pulls"
PULL = f"GET {API}/repos/o/r/pulls/12"
COMPARE = f"GET {API}/repos/o/r/compare/{BASE_SHA}...{HEAD_SHA}"
COMPARE_FORK = f"GET {API}/repos/o/r/compare/o:{BASE_SHA}...hubot:{HEAD_SHA}"
BASE_COMMIT = f"GET {API}/repos/o/r/commits/{BASE_SHA}"
HEAD_COMMIT = f"GET {API}/repos/o/r/commits/{HEAD_SHA}"
APP_INSTALLATION = f"GET {API}/repos/o/r/installation"
TOKEN_MINT = f"POST {API}/app/installations/7/access_tokens"

PLAIN_403 = {"x-ratelimit-remaining": "4999"}
UPDATED_AT = "2026-10-06T08:41:00Z"

Route = Callable[[httpx.Request], httpx.Response]
Call = Callable[[GitHubClient], object]


class FakeGitHub:
    """Routes `"<METHOD> <host><path>"` to a response factory and records every request."""

    def __init__(self, routes: dict[str, Route]):
        self.routes = {TOKEN_MINT: fresh_token_route(), **routes}
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


def fresh_token_route() -> Route:
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return respond(201, json={"token": "ghs_installation", "expires_at": expires_at})


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


def pull_json(
    number: int = 12,
    *,
    head_repository: str | None = "o/r",
    body: str | None = "Caches permissions per request.",
    **fields: Any,
) -> dict[str, Any]:
    """A pull request as "List pull requests" returns it; `fields` adds or overrides keys."""
    return {
        "number": number,
        "state": "open",
        "title": "Cache repository permissions",
        "body": body,
        "user": {"login": "hubot"},
        "draft": False,
        "html_url": f"https://github.com/o/r/pull/{number}",
        "updated_at": UPDATED_AT,
        "merged_at": None,
        "base": {"ref": "main", "sha": BASE_SHA, "repo": {"full_name": "o/r"}},
        "head": {
            "ref": "cache-permissions",
            "sha": HEAD_SHA,
            "repo": None if head_repository is None else {"full_name": head_repository},
        },
        **fields,
    }


def expected_pull(number: int = 12, **fields: Any) -> PullRequest:
    values: dict[str, Any] = {
        "number": number,
        "title": "Cache repository permissions",
        "body": "Caches permissions per request.",
        "author": "hubot",
        "state": "open",
        "draft": False,
        "base_ref": "main",
        "base_sha": BASE_SHA,
        "head_ref": "cache-permissions",
        "head_sha": HEAD_SHA,
        "head_repository": "o/r",
        "is_fork": False,
        "html_url": f"https://github.com/o/r/pull/{number}",
        "updated_at": datetime(2026, 10, 6, 8, 41, tzinfo=UTC),
        "additions": None,
        "deletions": None,
        "changed_files": None,
    }
    return PullRequest(**{**values, **fields})


def compare_json(*files: dict[str, str]) -> dict[str, Any]:
    return {"status": "ahead", "merge_base_commit": {"sha": MERGE_BASE_SHA}, "files": list(files)}


# list_pull_requests


def test_list_pull_requests_reads_one_page_of_open_pull_requests(settings: Settings) -> None:
    next_url = f"https://{API}/repositories/10/pulls?state=open&per_page=30&page=2"
    last_url = f"https://{API}/repositories/10/pulls?state=open&per_page=30&page=4"
    fake = FakeGitHub(
        {
            PULLS: respond(
                json=[
                    pull_json(12),
                    pull_json(11, head_repository="hubot/r", draft=True),
                    pull_json(10, head_repository=None, body=None),
                ],
                headers={"Link": f'<{next_url}>; rel="next", <{last_url}>; rel="last"'},
            )
        }
    )

    page = make_client(settings, fake).list_pull_requests("ghu_user", "o/r", page=1)

    (request,) = fake.requests
    assert request.url.path == "/repos/o/r/pulls"
    assert request.url.query == b"state=open&sort=updated&direction=desc&per_page=30&page=1"
    assert request.headers["authorization"] == "Bearer ghu_user"
    assert page == PullRequestPage(
        items=[
            expected_pull(12),
            expected_pull(11, head_repository="hubot/r", is_fork=True, draft=True),
            expected_pull(10, head_repository=None, is_fork=True, body=""),
        ],
        next_page=2,
    )


@pytest.mark.parametrize(
    "headers",
    [{}, {"Link": f'<https://{API}/repositories/10/pulls?page=1>; rel="prev"'}],
    ids=["no-link", "prev-only"],
)
def test_the_last_page_has_no_next_page(settings: Settings, headers: dict[str, str]) -> None:
    fake = FakeGitHub({PULLS: respond(json=[pull_json(12)], headers=headers)})

    page = make_client(settings, fake).list_pull_requests("ghu_user", "o/r", page=3)

    assert page.next_page is None
    assert fake.requests[0].url.params["page"] == "3"


# get_pull_request


@pytest.mark.parametrize(
    ("fields", "state"),
    [
        ({"state": "open", "merged": False}, "open"),
        ({"state": "closed", "merged": False}, "closed"),
        ({"state": "closed", "merged": True, "merged_at": UPDATED_AT}, "merged"),
    ],
    ids=["open", "closed", "merged"],
)
def test_get_pull_request_maps_the_state(
    settings: Settings, fields: dict[str, Any], state: str
) -> None:
    details = {"additions": 14, "deletions": 3, "changed_files": 2}
    fake = FakeGitHub({PULL: respond(json=pull_json(12, **fields, **details))})

    pull = make_client(settings, fake).get_pull_request("ghu_user", "o/r", 12)

    assert pull == expected_pull(12, state=state, **details)
    (request,) = fake.requests
    assert request.headers["authorization"] == "Bearer ghu_user"


# compare_commits


def test_compare_commits_returns_the_merge_base_and_rename_hints(settings: Settings) -> None:
    fake = FakeGitHub(
        {
            COMPARE: respond(
                json=compare_json(
                    {"filename": "app/b.py", "status": "renamed", "previous_filename": "app/a.py"},
                    {"filename": "README.md", "status": "modified"},
                    {"filename": "web/new.ts", "status": "added"},
                )
            )
        }
    )

    comparison = make_client(settings, fake).compare_commits(
        7, "o/r", BASE_SHA, HEAD_SHA, head_owner=None
    )

    assert comparison == Comparison(
        merge_base_sha=MERGE_BASE_SHA, renamed={"app/b.py": "app/a.py"}, listed_files=3
    )
    (request,) = fake.calls(COMPARE)
    assert request.headers["authorization"] == "Bearer ghs_installation"
    assert fake.calls(TOKEN_MINT)[0].headers["authorization"].startswith("Bearer ey")


def test_a_fork_comparison_is_retried_once_in_the_owner_qualified_form(
    settings: Settings,
) -> None:
    fake = FakeGitHub(
        {
            COMPARE: respond(404, json={"message": "Not Found"}),
            COMPARE_FORK: respond(json=compare_json()),
        }
    )

    comparison = make_client(settings, fake).compare_commits(
        7, "o/r", BASE_SHA, HEAD_SHA, head_owner="hubot"
    )

    assert comparison == Comparison(merge_base_sha=MERGE_BASE_SHA, renamed={}, listed_files=0)
    assert [r.url.raw_path for r in fake.requests[1:]] == [
        f"/repos/o/r/compare/{BASE_SHA}...{HEAD_SHA}".encode(),
        f"/repos/o/r/compare/o:{BASE_SHA}...hubot:{HEAD_SHA}".encode(),
    ]
    assert all(r.headers["authorization"] == "Bearer ghs_installation" for r in fake.requests[1:])


def test_a_fork_comparison_that_works_in_the_plain_form_is_not_retried(
    settings: Settings,
) -> None:
    fake = FakeGitHub({COMPARE: respond(json=compare_json())})

    make_client(settings, fake).compare_commits(7, "o/r", BASE_SHA, HEAD_SHA, head_owner="hubot")

    assert fake.calls(COMPARE_FORK) == []


@pytest.mark.parametrize("head_owner", [None, "hubot"])
def test_a_missing_commit_is_unavailable(settings: Settings, head_owner: str | None) -> None:
    fake = FakeGitHub({})  # Every comparison answers 404 "Not Found".

    with pytest.raises(CommitUnavailable):
        make_client(settings, fake).compare_commits(
            7, "o/r", BASE_SHA, HEAD_SHA, head_owner=head_owner
        )

    compares = [r for r in fake.requests if "/compare/" in r.url.path]
    assert len(compares) == (1 if head_owner is None else 2)


def test_a_comparison_of_existing_commits_that_github_has_not_computed_is_retryable(
    settings: Settings,
) -> None:
    # GitHub answers a plain 404 while it has not computed a comparison of diverged commits, and
    # the same request succeeds later (research R2).
    fake = FakeGitHub(
        {
            COMPARE: respond(404, json={"message": "Not Found"}),
            BASE_COMMIT: respond(json={"sha": BASE_SHA}),
            HEAD_COMMIT: respond(json={"sha": HEAD_SHA}),
        }
    )

    with pytest.raises(GitHubUnavailable):
        make_client(settings, fake).compare_commits(7, "o/r", BASE_SHA, HEAD_SHA, head_owner=None)

    assert len(fake.calls(BASE_COMMIT)) == len(fake.calls(HEAD_COMMIT)) == 1
    assert fake.calls(HEAD_COMMIT)[0].headers["authorization"] == "Bearer ghs_installation"


@pytest.mark.parametrize("missing", ["base", "head"])
def test_a_plain_404_with_a_missing_commit_is_unavailable(settings: Settings, missing: str) -> None:
    present = {"base": HEAD_COMMIT, "head": BASE_COMMIT}[missing]
    fake = FakeGitHub(
        {COMPARE: respond(404, json={"message": "Not Found"}), present: respond(json={})}
    )

    with pytest.raises(CommitUnavailable):
        make_client(settings, fake).compare_commits(7, "o/r", BASE_SHA, HEAD_SHA, head_owner=None)


def test_a_422_comparison_is_unavailable(settings: Settings) -> None:
    fake = FakeGitHub({COMPARE: respond(422, json={"message": "No commit found for SHA"})})

    with pytest.raises(CommitUnavailable):
        make_client(settings, fake).compare_commits(7, "o/r", BASE_SHA, HEAD_SHA, head_owner=None)


@pytest.mark.parametrize("head_owner", [None, "hubot"])
def test_commits_without_a_common_ancestor_have_no_common_history(
    settings: Settings, head_owner: str | None
) -> None:
    message = f"No common ancestor between {BASE_SHA} and {HEAD_SHA}."
    unrelated = respond(404, json={"message": message})
    fake = FakeGitHub({COMPARE: unrelated, COMPARE_FORK: unrelated})

    with pytest.raises(NoCommonHistory):
        make_client(settings, fake).compare_commits(
            7, "o/r", BASE_SHA, HEAD_SHA, head_owner=head_owner
        )


def test_comparison_errors_are_not_access_answers() -> None:
    for error in (CommitUnavailable, NoCommonHistory):
        assert issubclass(error, GitHubError)
        assert not issubclass(error, (GitHubAccessDenied, GitHubNotFound))


@pytest.mark.parametrize(
    "failure",
    [respond(401, json={"message": "Bad credentials"}), respond(403, json={}, headers=PLAIN_403)],
    ids=["401", "plain-403"],
)
def test_a_refused_comparison_is_an_access_denial_and_drops_the_token(
    settings: Settings, failure: Route
) -> None:
    fake = FakeGitHub({COMPARE: failure})
    client = make_client(settings, fake)

    with pytest.raises(GitHubAccessDenied) as caught:
        client.compare_commits(7, "o/r", BASE_SHA, HEAD_SHA, head_owner=None)
    assert type(caught.value) is GitHubAccessDenied

    fake.routes[COMPARE] = respond(json=compare_json())
    client.compare_commits(7, "o/r", BASE_SHA, HEAD_SHA, head_owner=None)
    assert len(fake.calls(TOKEN_MINT)) == 2


# get_installation_permissions


def test_get_installation_permissions_uses_the_app_jwt(settings: Settings) -> None:
    settings_url = "https://github.com/organizations/o/settings/installations/77"
    permissions = {"contents": "read", "metadata": "read", "pull_requests": "read"}
    fake = FakeGitHub(
        {
            APP_INSTALLATION: respond(
                json={"id": 77, "permissions": permissions, "html_url": settings_url}
            )
        }
    )

    result = make_client(settings, fake).get_installation_permissions("o/r")

    assert result == InstallationPermissions(
        installation_id=77, permissions=permissions, html_url=settings_url
    )
    scheme, token = fake.requests[0].headers["authorization"].split(" ")
    assert scheme == "Bearer" and token.count(".") == 2


@pytest.mark.parametrize(
    ("status", "error"), [(404, GitHubNotFound), (401, AppCredentialsRejected)]
)
def test_get_installation_permissions_maps_errors(
    settings: Settings, status: int, error: type[Exception]
) -> None:
    fake = FakeGitHub({APP_INSTALLATION: respond(status, json={"message": "x"})})

    with pytest.raises(error):
        make_client(settings, fake).get_installation_permissions("o/r")


# Errors, classified as the existing calls are


USER_TOKEN_CALLS: dict[str, tuple[Call, str]] = {
    "list_pull_requests": (lambda c: c.list_pull_requests("ghu_user", "o/r", page=1), PULLS),
    "get_pull_request": (lambda c: c.get_pull_request("ghu_user", "o/r", 12), PULL),
}
ALL_CALLS: dict[str, tuple[Call, str]] = {
    **USER_TOKEN_CALLS,
    "compare_commits": (
        lambda c: c.compare_commits(7, "o/r", BASE_SHA, HEAD_SHA, head_owner=None),
        COMPARE,
    ),
    "compare_commits_token": (
        lambda c: c.compare_commits(7, "o/r", BASE_SHA, HEAD_SHA, head_owner=None),
        TOKEN_MINT,
    ),
    "get_installation_permissions": (
        lambda c: c.get_installation_permissions("o/r"),
        APP_INSTALLATION,
    ),
}


def call_failing(settings: Settings, call: Call, failing: str, failure: Route) -> GitHubError:
    """Run `call` with `failing` answered by `failure`; return the gateway error it raises."""
    fake = FakeGitHub({failing: failure})
    with pytest.raises(GitHubError) as caught:
        call(make_client(settings, fake))
    assert fake.calls(failing)
    return caught.value


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (respond(401, json={"message": "Bad credentials"}), UserAuthorizationInvalid),
        (respond(403, json={}, headers=PLAIN_403), GitHubAccessDenied),
        (respond(404, json={"message": "Not Found"}), GitHubNotFound),
    ],
    ids=["401", "plain-403", "404"],
)
@pytest.mark.parametrize(("call", "failing"), USER_TOKEN_CALLS.values(), ids=USER_TOKEN_CALLS)
def test_user_token_calls_classify_refusals(
    settings: Settings, call: Call, failing: str, failure: Route, error: type[GitHubError]
) -> None:
    raised = call_failing(settings, call, failing, failure)

    assert type(raised) is error


@pytest.mark.parametrize(
    "failure",
    [
        respond(429),
        respond(500),
        respond(502),
        respond(403, json={}, headers={"x-ratelimit-remaining": "0"}),
        respond(403, json={}, headers={"retry-after": "60"}),
    ],
    ids=["429", "500", "502", "403-primary-limit", "403-secondary-limit"],
)
@pytest.mark.parametrize(("call", "failing"), ALL_CALLS.values(), ids=ALL_CALLS)
def test_rate_limits_and_server_errors_mean_github_is_unavailable(
    settings: Settings, failure: Route, call: Call, failing: str
) -> None:
    assert isinstance(call_failing(settings, call, failing, failure), GitHubUnavailable)


def timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


@pytest.mark.parametrize(("call", "failing"), ALL_CALLS.values(), ids=ALL_CALLS)
def test_timeouts_mean_github_is_unavailable(settings: Settings, call: Call, failing: str) -> None:
    assert isinstance(call_failing(settings, call, failing, timeout), GitHubUnavailable)
