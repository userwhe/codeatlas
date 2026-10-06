"""Sanity checks for the fake GitHub gateway (no database)."""

import io
import tarfile
from urllib.parse import parse_qs, urlparse

import pytest

from codeatlas.config import Settings
from codeatlas.github.fake import (
    EMPTY_ID,
    HUBOT_TOOLS_ID,
    SAMPLE_APP_DELETED,
    SAMPLE_APP_ID,
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
    RepositoryEmpty,
)

OCTOCAT = "fake-token-octocat"
HUBOT = "fake-token-hubot"


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

    assert fake.get_repository(OCTOCAT, 2002).private is True
    with pytest.raises(GitHubNotFound):
        fake.get_repository(HUBOT, SOLO_ID)
    with pytest.raises(GitHubNotFound):
        fake.get_repository(OCTOCAT, 9999)

    fake.revoke_access("octocat", SAMPLE_APP_ID)
    with pytest.raises(GitHubNotFound):
        fake.get_repository(OCTOCAT, SAMPLE_APP_ID)


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
