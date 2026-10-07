"""Sanity checks for the fake GitHub gateway (no database)."""

import io
import tarfile
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import pytest

from codeatlas.config import Settings
from codeatlas.github.fake import (
    EMPTY_ID,
    HUBOT_TOOLS_ID,
    PUBLIC_UNINSTALLED_ID,
    SAMPLE_APP_DELETED,
    SAMPLE_APP_ID,
    SAMPLE_APP_PRIVATE_ID,
    SAMPLE_APP_RENAMED,
    SOLO_ID,
    FakeGitHub,
    commit_sha,
    get_fake_github,
    reset_fake_github,
)
from codeatlas.github.gateway import (
    BranchNotFound,
    GitHubAccessDenied,
    GitHubGateway,
    GitHubNotFound,
    GitHubUnavailable,
    RepositoryEmpty,
    UserAuthorizationInvalid,
)

OCTOCAT = "fake-token-octocat"
HUBOT = "fake-token-hubot"
SAMPLE_APP = "octo-org/sample-app"


@pytest.fixture
def fake() -> FakeGitHub:
    return get_fake_github()


def _members(fake: FakeGitHub, full_name: str, branch: str = "main") -> list[tarfile.TarInfo]:
    installation_id = fake.get_installation_id(full_name)
    sha = fake.resolve_commit(installation_id, full_name, branch)
    with fake.open_tarball(installation_id, full_name, sha) as stream:
        with tarfile.open(fileobj=io.BytesIO(stream.read()), mode="r:gz") as tar:
            return tar.getmembers()


def _file_paths(members: list[tarfile.TarInfo]) -> set[str]:
    """Regular-file paths with the top-level directory stripped."""
    return {member.name.split("/", 1)[1] for member in members if member.isfile()}


def test_sign_in_flow(fake: FakeGitHub) -> None:
    gateway: GitHubGateway = fake
    url = urlparse(gateway.authorize_url("a b/c"))
    assert url.path == "/auth/github/callback"
    assert parse_qs(url.query) == {"code": ["fake:octocat"], "state": ["a b/c"]}

    tokens = gateway.exchange_code("fake:hubot")
    assert tokens.access_token == HUBOT
    assert tokens.refresh_token is not None
    user = gateway.get_authenticated_user(tokens.access_token)
    assert (user.login, user.id) == ("hubot", 1002)
    assert gateway.refresh_user_token(tokens.refresh_token).access_token == HUBOT

    with pytest.raises(GitHubAccessDenied):
        gateway.exchange_code("fake:nobody")
    with pytest.raises(GitHubAccessDenied):
        gateway.get_authenticated_user("not-a-token")


def test_access_per_user(fake: FakeGitHub) -> None:
    octocat_ids = {repo.id for repo in fake.list_accessible_repositories(OCTOCAT)}
    assert octocat_ids == set(range(2001, 2009))
    hubot = {repo.id: repo for repo in fake.list_accessible_repositories(HUBOT)}
    assert set(hubot) == {SAMPLE_APP_ID, HUBOT_TOOLS_ID}
    assert hubot[SAMPLE_APP_ID].installation_id == 5001
    assert hubot[HUBOT_TOOLS_ID].installation_id == 5003

    assert fake.list_installation_ids(OCTOCAT) == {5001, 5002}
    assert fake.list_installation_ids(HUBOT) == {5001, 5003}

    assert fake.get_repository(OCTOCAT, SAMPLE_APP_PRIVATE_ID).private is True
    with pytest.raises(GitHubNotFound):
        fake.get_repository(HUBOT, SAMPLE_APP_PRIVATE_ID)
    with pytest.raises(GitHubNotFound):
        fake.get_repository(OCTOCAT, 9999)
    # As on GitHub, public repositories are visible to every user.
    assert fake.get_repository(HUBOT, SOLO_ID).full_name == "octocat/solo"
    assert fake.get_repository(OCTOCAT, PUBLIC_UNINSTALLED_ID).installation_id is None
    with pytest.raises(GitHubNotFound):
        fake.get_installation_id("monalisa/public-lib")

    fake.revoke_access("octocat", SAMPLE_APP_PRIVATE_ID)
    with pytest.raises(GitHubNotFound):
        fake.get_repository(OCTOCAT, SAMPLE_APP_PRIVATE_ID)


def test_uninstall(fake: FakeGitHub) -> None:
    fake.uninstall(HUBOT_TOOLS_ID)

    assert {repo.id for repo in fake.list_accessible_repositories(HUBOT)} == {SAMPLE_APP_ID}
    assert fake.list_installation_ids(HUBOT) == {5001}
    with pytest.raises(GitHubNotFound):
        fake.get_installation_id("hubot/tools")
    with pytest.raises(GitHubAccessDenied):
        fake.resolve_commit(5003, "hubot/tools", "main")


def test_sample_app_commits(fake: FakeGitHub) -> None:
    members = _members(fake, "octo-org/sample-app")
    first_sha = commit_sha(SAMPLE_APP_ID, "initial")
    assert {member.name.split("/", 1)[0] for member in members} == {
        f"octo-org-sample-app-{first_sha[:7]}"
    }
    initial = _file_paths(members)
    assert {"app/auth/access.py", ".env", "id_rsa", "node_modules/left-pad/index.js"} <= initial
    assert {SAMPLE_APP_RENAMED[0], SAMPLE_APP_DELETED} <= initial

    assert fake.advance(SAMPLE_APP_ID) == commit_sha(SAMPLE_APP_ID, "second")
    second = _file_paths(_members(fake, "octo-org/sample-app"))
    assert second == initial - {SAMPLE_APP_RENAMED[0], SAMPLE_APP_DELETED} | {SAMPLE_APP_RENAMED[1]}


def test_branch_errors(fake: FakeGitHub) -> None:
    with pytest.raises(BranchNotFound):
        fake.resolve_commit(5001, "octo-org/sample-app", "nope")
    with pytest.raises(RepositoryEmpty):
        fake.resolve_commit(5001, "octo-org/empty", "main")
    assert fake.get_repository(OCTOCAT, EMPTY_ID).default_branch == "main"
    with pytest.raises(GitHubNotFound):
        fake.open_tarball(5001, "octo-org/sample-app", "0" * 40)


def test_rename_and_reinstall(fake: FakeGitHub) -> None:
    fake.rename(SAMPLE_APP_ID, "octo-org/renamed-app")
    assert fake.get_repository(OCTOCAT, SAMPLE_APP_ID).full_name == "octo-org/renamed-app"
    with pytest.raises(GitHubNotFound):
        fake.get_installation_id("octo-org/sample-app")

    new_id = fake.reinstall(SAMPLE_APP_ID)
    assert new_id != 5001
    with pytest.raises(GitHubAccessDenied):
        fake.resolve_commit(5001, "octo-org/renamed-app", "main")
    assert fake.get_installation_id("octo-org/renamed-app") == new_id
    assert fake.resolve_commit(new_id, "octo-org/renamed-app", "main")


def test_generated_archives(
    fake: FakeGitHub, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "max_files_per_snapshot", 10)
    assert len(_file_paths(_members(fake, "octo-org/oversized"))) == 11

    vendored = _file_paths(_members(fake, "octo-org/vendored-heavy"))
    in_node_modules = {path for path in vendored if path.startswith("node_modules/")}
    assert len(in_node_modules) == 6000
    assert 0 < len(vendored - in_node_modules) < 50

    unsafe = _members(fake, "octo-org/unsafe-paths")
    assert any(".." in member.name.split("/") for member in unsafe)
    assert any(member.name.startswith("/") for member in unsafe)
    assert any(member.issym() for member in unsafe)
    assert any(member.islnk() for member in unsafe)


def test_call_counter_and_reset(fake: FakeGitHub) -> None:
    fake.get_repository(OCTOCAT, SAMPLE_APP_ID)
    fake.get_installation_id("octo-org/sample-app")
    assert fake.calls == {"get_repository": 1, "get_installation_id": 1}

    fake.rename(SAMPLE_APP_ID, "octo-org/renamed-app")
    fake.revoke_access("hubot", SAMPLE_APP_ID)
    fake.advance(SAMPLE_APP_ID)
    reset_fake_github()

    assert get_fake_github() is fake
    assert not fake.calls
    assert fake.get_repository(HUBOT, SAMPLE_APP_ID).full_name == "octo-org/sample-app"
    assert fake.resolve_commit(5001, "octo-org/sample-app", "main") == commit_sha(
        SAMPLE_APP_ID, "initial"
    )


# Switches for push re-indexing and access changes


def test_push_moves_the_default_branch_to_the_next_commit(fake: FakeGitHub) -> None:
    second = commit_sha(SAMPLE_APP_ID, "second")

    assert fake.push(SAMPLE_APP_ID) == second
    assert fake.resolve_commit(5001, SAMPLE_APP, "main") == second
    second_files = _file_paths(_members(fake, SAMPLE_APP))

    # Past the fixture commits, each push adds a commit with the same files as the head.
    third = fake.push(SAMPLE_APP_ID)
    assert third not in {commit_sha(SAMPLE_APP_ID, "initial"), second}
    assert fake.resolve_commit(5001, SAMPLE_APP, "main") == third
    assert _file_paths(_members(fake, SAMPLE_APP)) == second_files
    assert fake.push(SAMPLE_APP_ID) not in {second, third}

    with pytest.raises(ValueError):
        fake.push(EMPTY_ID)


def test_rename_default_branch(fake: FakeGitHub) -> None:
    initial = commit_sha(SAMPLE_APP_ID, "initial")

    fake.rename_default_branch(SAMPLE_APP_ID, "trunk")

    assert fake.get_repository(OCTOCAT, SAMPLE_APP_ID).default_branch == "trunk"
    assert fake.resolve_commit(5001, SAMPLE_APP, "trunk") == initial
    with pytest.raises(BranchNotFound):
        fake.resolve_commit(5001, SAMPLE_APP, "main")

    second = fake.push(SAMPLE_APP_ID)
    assert second == commit_sha(SAMPLE_APP_ID, "second")
    assert fake.resolve_commit(5001, SAMPLE_APP, "trunk") == second


def test_suspend(fake: FakeGitHub) -> None:
    fake.suspend(SAMPLE_APP_ID)

    # The real client skips suspended installations in both listings.
    assert fake.list_installation_ids(OCTOCAT) == {5002}
    assert fake.list_installation_ids(HUBOT) == {5003}
    assert {repo.id for repo in fake.list_accessible_repositories(HUBOT)} == {HUBOT_TOOLS_ID}
    # GitHub still reports the suspended installation for the repository.
    assert fake.get_installation_id(SAMPLE_APP) == 5001
    with pytest.raises(GitHubAccessDenied) as caught:
        fake.resolve_commit(5001, SAMPLE_APP, "main")
    assert not isinstance(caught.value, UserAuthorizationInvalid)
    with pytest.raises(GitHubAccessDenied):
        fake.open_tarball(5001, SAMPLE_APP, commit_sha(SAMPLE_APP_ID, "initial"))
    assert fake.resolve_commit(5003, "hubot/tools", "main")


def test_remove_from_installation(fake: FakeGitHub) -> None:
    fake.remove_from_installation(SAMPLE_APP_ID)

    with pytest.raises(GitHubNotFound):
        fake.get_installation_id(SAMPLE_APP)
    assert SAMPLE_APP_ID not in {repo.id for repo in fake.list_accessible_repositories(OCTOCAT)}
    with pytest.raises(GitHubAccessDenied):
        fake.resolve_commit(5001, SAMPLE_APP, "main")
    # Other repositories of the same owner stay in the installation.
    assert fake.get_installation_id("octo-org/no-code") == 5001
    assert fake.resolve_commit(5001, "octo-org/no-code", "main")
    assert fake.list_installation_ids(OCTOCAT) == {5001, 5002}

    # Reinstalling the App on the owner's account covers the repository again.
    new_id = fake.reinstall(SAMPLE_APP_ID)
    assert fake.get_installation_id(SAMPLE_APP) == new_id
    assert fake.resolve_commit(new_id, SAMPLE_APP, "main")


USER_TOKEN_CALLS: dict[str, Callable[[FakeGitHub, str], object]] = {
    "get_authenticated_user": lambda fake, token: fake.get_authenticated_user(token),
    "list_accessible_repositories": lambda fake, token: fake.list_accessible_repositories(token),
    "get_repository": lambda fake, token: fake.get_repository(token, SAMPLE_APP_ID),
    "list_installation_ids": lambda fake, token: fake.list_installation_ids(token),
}


def test_revoke_authorization(fake: FakeGitHub) -> None:
    fake.revoke_authorization("octocat")

    for name, call in USER_TOKEN_CALLS.items():
        with pytest.raises(UserAuthorizationInvalid):
            call(fake, OCTOCAT)
        assert call(fake, HUBOT), name
    with pytest.raises(UserAuthorizationInvalid):
        fake.refresh_user_token("fake-refresh-octocat")
    assert fake.refresh_user_token("fake-refresh-hubot").access_token == HUBOT

    # Signing in again authorizes the App anew.
    tokens = fake.exchange_code("fake:octocat")
    assert fake.get_authenticated_user(tokens.access_token).login == "octocat"


def test_make_private(fake: FakeGitHub) -> None:
    fake.make_private(SAMPLE_APP_ID)

    # octocat and hubot both reach sample-app through the octo-org installation.
    assert fake.get_repository(OCTOCAT, SAMPLE_APP_ID).private is True
    listed = {repo.id: repo for repo in fake.list_accessible_repositories(HUBOT)}
    assert listed[SAMPLE_APP_ID].private is True

    fake.revoke_access("hubot", SAMPLE_APP_ID)
    with pytest.raises(GitHubNotFound):
        fake.get_repository(HUBOT, SAMPLE_APP_ID)


# Every `GitHubGateway` method that calls GitHub. `authorize_url` only builds a URL.
GATEWAY_CALLS: dict[str, Callable[[FakeGitHub], object]] = {
    "exchange_code": lambda fake: fake.exchange_code("fake:octocat"),
    "refresh_user_token": lambda fake: fake.refresh_user_token("fake-refresh-octocat"),
    "get_authenticated_user": lambda fake: fake.get_authenticated_user(OCTOCAT),
    "list_accessible_repositories": lambda fake: fake.list_accessible_repositories(OCTOCAT),
    "get_repository": lambda fake: fake.get_repository(OCTOCAT, SAMPLE_APP_ID),
    "list_installation_ids": lambda fake: fake.list_installation_ids(OCTOCAT),
    "get_installation_id": lambda fake: fake.get_installation_id(SAMPLE_APP),
    "resolve_commit": lambda fake: fake.resolve_commit(5001, SAMPLE_APP, "main"),
    "open_tarball": lambda fake: fake.open_tarball(
        5001, SAMPLE_APP, commit_sha(SAMPLE_APP_ID, "initial")
    ),
}


def test_gateway_calls_cover_the_protocol() -> None:
    protocol_methods = {name for name in vars(GitHubGateway) if not name.startswith("_")}
    assert set(GATEWAY_CALLS) == protocol_methods - {"authorize_url"}


def test_set_unavailable(fake: FakeGitHub) -> None:
    fake.set_unavailable(True)

    for name, call in GATEWAY_CALLS.items():
        with pytest.raises(GitHubUnavailable):
            call(fake)
        assert fake.calls[name] == 1
    assert fake.authorize_url("state")

    fake.set_unavailable(False)
    for call in GATEWAY_CALLS.values():
        assert call(fake)


def test_reset_undoes_every_switch(fake: FakeGitHub) -> None:
    fake.push(SAMPLE_APP_ID)
    fake.rename_default_branch(SAMPLE_APP_ID, "trunk")
    fake.make_private(SAMPLE_APP_ID)
    fake.suspend(HUBOT_TOOLS_ID)
    fake.remove_from_installation(SOLO_ID)
    fake.revoke_authorization("octocat")
    fake.set_unavailable(True)

    reset_fake_github()

    repository = fake.get_repository(OCTOCAT, SAMPLE_APP_ID)
    assert (repository.default_branch, repository.private) == ("main", False)
    assert fake.resolve_commit(5001, SAMPLE_APP, "main") == commit_sha(SAMPLE_APP_ID, "initial")
    assert fake.list_installation_ids(HUBOT) == {5001, 5003}
    assert fake.get_installation_id("octocat/solo") == 5002
    assert fake.refresh_user_token("fake-refresh-octocat").access_token == OCTOCAT
