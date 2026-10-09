"""Sanity checks for the fake GitHub gateway (no database)."""

import io
import json
import tarfile
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from codeatlas.config import Settings
from codeatlas.github.fake import (
    EMPTY_ID,
    FIXTURE_PULL_REQUESTS_DIR,
    FIXTURE_REPOS_DIR,
    HUBOT_TOOLS_ID,
    PUBLIC_UNINSTALLED_ID,
    REVIEW_APP_ID,
    REVIEW_APP_PRIVATE_ID,
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
    CommitUnavailable,
    Comparison,
    GitHubAccessDenied,
    GitHubGateway,
    GitHubNotFound,
    GitHubUnavailable,
    NoCommonHistory,
    PullRequest,
    RepositoryEmpty,
    UserAuthorizationInvalid,
)

OCTOCAT = "fake-token-octocat"
HUBOT = "fake-token-hubot"
SAMPLE_APP = "octo-org/sample-app"
REVIEW_APP = "octo-org/review-app"
REVIEW_APP_PRIVATE = "octo-org/review-app-private"


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


def test_users_by_login(fake: FakeGitHub) -> None:
    gateway: GitHubGateway = fake

    assert (gateway.get_user_by_login("octocat").id, gateway.get_user_by_login("hubot").id) == (
        1001,
        1002,
    )
    monalisa = gateway.get_user_by_login("monalisa")
    assert (monalisa.id, monalisa.login) == (1003, "monalisa")
    with pytest.raises(GitHubNotFound):
        gateway.get_user_by_login("nobody")


def test_a_user_without_installations_signs_in(fake: FakeGitHub) -> None:
    tokens = fake.exchange_code("fake:monalisa")

    assert fake.get_authenticated_user(tokens.access_token).login == "monalisa"
    assert fake.list_accessible_repositories(tokens.access_token) == []
    assert fake.list_installation_ids(tokens.access_token) == set()
    # As on GitHub, public repositories are visible to every user.
    assert fake.get_repository(tokens.access_token, SOLO_ID).full_name == "octocat/solo"
    with pytest.raises(GitHubNotFound):
        fake.get_repository(tokens.access_token, SAMPLE_APP_PRIVATE_ID)


def test_access_per_user(fake: FakeGitHub) -> None:
    octocat_ids = {repo.id for repo in fake.list_accessible_repositories(OCTOCAT)}
    assert octocat_ids == set(range(2001, 2009)) | {REVIEW_APP_ID, REVIEW_APP_PRIVATE_ID}
    hubot = {repo.id: repo for repo in fake.list_accessible_repositories(HUBOT)}
    assert set(hubot) == {SAMPLE_APP_ID, HUBOT_TOOLS_ID, REVIEW_APP_ID}
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

    assert {repo.id for repo in fake.list_accessible_repositories(HUBOT)} == {
        SAMPLE_APP_ID,
        REVIEW_APP_ID,
    }
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
    "get_user_by_login": lambda fake: fake.get_user_by_login("octocat"),
    "list_accessible_repositories": lambda fake: fake.list_accessible_repositories(OCTOCAT),
    "get_repository": lambda fake: fake.get_repository(OCTOCAT, SAMPLE_APP_ID),
    "list_installation_ids": lambda fake: fake.list_installation_ids(OCTOCAT),
    "get_installation_id": lambda fake: fake.get_installation_id(SAMPLE_APP),
    "resolve_commit": lambda fake: fake.resolve_commit(5001, SAMPLE_APP, "main"),
    "open_tarball": lambda fake: fake.open_tarball(
        5001, SAMPLE_APP, commit_sha(SAMPLE_APP_ID, "initial")
    ),
    "list_pull_requests": lambda fake: fake.list_pull_requests(OCTOCAT, REVIEW_APP, 1),
    "get_pull_request": lambda fake: fake.get_pull_request(OCTOCAT, REVIEW_APP, 1),
    "compare_commits": lambda fake: fake.compare_commits(
        5001,
        REVIEW_APP,
        commit_sha(REVIEW_APP_ID, "initial"),
        commit_sha(REVIEW_APP_ID, "pr-1"),
        head_owner=None,
    ),
    "get_installation_permissions": lambda fake: fake.get_installation_permissions(REVIEW_APP),
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


# Pull requests on the review fixtures


def _tree(fake: FakeGitHub, full_name: str, sha: str) -> dict[str, bytes]:
    """The regular files of a commit, with the top-level directory stripped."""
    files: dict[str, bytes] = {}
    with fake.open_tarball(5001, full_name, sha) as stream:
        with tarfile.open(fileobj=io.BytesIO(stream.read()), mode="r:gz") as tar:
            for member in tar.getmembers():
                extracted = tar.extractfile(member) if member.isfile() else None
                if extracted is not None:
                    files[member.name.split("/", 1)[1]] = extracted.read()
    return files


def _on_disk(root: str) -> dict[str, bytes]:
    directory = FIXTURE_REPOS_DIR.parent / root
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.name != ".DS_Store"
    }


def _overlay(name: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    """An overlay's `pull-request.json` and its stored `files/`."""
    metadata = json.loads((FIXTURE_PULL_REQUESTS_DIR / name / "pull-request.json").read_text())
    files_dir = FIXTURE_PULL_REQUESTS_DIR / name / "files"
    return metadata, (_on_disk(f"pull-requests/{name}/files") if files_dir.is_dir() else {})


def _pull(fake: FakeGitHub, number: int, full_name: str = REVIEW_APP) -> PullRequest:
    return fake.get_pull_request(OCTOCAT, full_name, number)


def test_review_app_repositories(fake: FakeGitHub) -> None:
    initial = commit_sha(REVIEW_APP_ID, "initial")

    assert fake.get_repository(HUBOT, REVIEW_APP_ID).private is False
    assert fake.get_repository(OCTOCAT, REVIEW_APP_PRIVATE_ID).private is True
    with pytest.raises(GitHubNotFound):
        fake.get_repository(HUBOT, REVIEW_APP_PRIVATE_ID)
    assert fake.get_installation_id(REVIEW_APP) == fake.get_installation_id(REVIEW_APP_PRIVATE)
    assert fake.get_installation_id(REVIEW_APP) == 5001
    assert fake.resolve_commit(5001, REVIEW_APP, "main") == initial
    assert fake.resolve_commit(5001, REVIEW_APP, "release") == initial
    assert _tree(fake, REVIEW_APP, initial) == _on_disk("repos/review-app")
    assert _tree(fake, REVIEW_APP_PRIVATE, commit_sha(REVIEW_APP_PRIVATE_ID, "initial")) == (
        _on_disk("repos/review-app")
    )


def test_list_pull_requests(fake: FakeGitHub) -> None:
    page = fake.list_pull_requests(OCTOCAT, REVIEW_APP, 1)

    assert [pull.number for pull in page.items] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert page.next_page is None
    stamps = [pull.updated_at for pull in page.items]
    assert stamps == sorted(stamps, reverse=True) and len(set(stamps)) == len(stamps)
    assert [pull.number for pull in page.items if pull.draft] == [8]
    assert {pull.state for pull in page.items} == {"open"}
    # Only "Get a pull request" reports the size of the change.
    assert {(pull.additions, pull.deletions, pull.changed_files) for pull in page.items} == {
        (None, None, None)
    }
    assert fake.list_pull_requests(OCTOCAT, REVIEW_APP, 2).items == []

    # Public: every user can list them.
    assert [pull.number for pull in fake.list_pull_requests(HUBOT, REVIEW_APP, 1).items] == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
    ]
    private = fake.list_pull_requests(OCTOCAT, REVIEW_APP_PRIVATE, 1).items
    assert [(pull.number, pull.head_sha) for pull in private] == [
        (1, commit_sha(REVIEW_APP_PRIVATE_ID, "pr-1"))
    ]


def test_get_pull_request(fake: FakeGitHub) -> None:
    first = _pull(fake, 1)
    assert first == PullRequest(
        number=1,
        title="Simplify write checks",
        body=first.body,
        author="hubot",
        state="open",
        draft=False,
        base_ref="main",
        base_sha=commit_sha(REVIEW_APP_ID, "initial"),
        head_ref="simplify-write-checks",
        head_sha=commit_sha(REVIEW_APP_ID, "pr-1"),
        head_repository=REVIEW_APP,
        is_fork=False,
        html_url="https://github.com/octo-org/review-app/pull/1",
        updated_at=first.updated_at,
        additions=2,
        deletions=2,
        changed_files=1,
    )
    assert first.body and isinstance(first.updated_at, datetime)
    assert first.updated_at.tzinfo is not None

    fork = _pull(fake, 3)
    assert (fork.is_fork, fork.head_repository, fork.author) == (True, "hubot/review-app", "hubot")
    assert _pull(fake, 2).base_ref == "release"
    assert _pull(fake, 9).state == "closed"
    sizes = {
        n: (p.additions, p.deletions, p.changed_files) for n in (4, 5, 6) if (p := _pull(fake, n))
    }
    assert sizes == {4: (1, 0, 2), 5: (1, 1, 1), 6: (3000, 0, 120)}

    with pytest.raises(GitHubNotFound):
        _pull(fake, 99)
    with pytest.raises(GitHubNotFound):
        _pull(fake, 2, REVIEW_APP_PRIVATE)


def test_compare_commits(fake: FakeGitHub) -> None:
    initial = commit_sha(REVIEW_APP_ID, "initial")

    def compare(number: int) -> Comparison:
        pull = _pull(fake, number)
        owner = "hubot" if pull.is_fork else None
        return fake.compare_commits(
            5001, REVIEW_APP, pull.base_sha, pull.head_sha, head_owner=owner
        )

    assert compare(1) == Comparison(merge_base_sha=initial, renamed={}, listed_files=1)
    assert compare(5) == Comparison(
        merge_base_sha=initial, renamed={"app/strings.py": "app/text.py"}, listed_files=1
    )
    assert compare(3).merge_base_sha == initial
    assert compare(6).listed_files == 120
    with pytest.raises(GitHubAccessDenied):
        fake.compare_commits(
            5003, REVIEW_APP, initial, commit_sha(REVIEW_APP_ID, "pr-1"), head_owner=None
        )


def test_pull_request_archives_are_the_base_plus_the_overlay(fake: FakeGitHub) -> None:
    base = _on_disk("repos/review-app")
    for directory in sorted(path.name for path in FIXTURE_PULL_REQUESTS_DIR.iterdir()):
        if not (FIXTURE_PULL_REQUESTS_DIR / directory).is_dir():
            continue
        metadata, stored = _overlay(directory)
        expected = dict(base)
        for old, new in metadata["rename"].items():
            expected[new] = expected.pop(old)
        expected.update(stored)
        for path in metadata["remove"]:
            del expected[path]

        tree = _tree(fake, REVIEW_APP, _pull(fake, metadata["number"]).head_sha)

        injected = {path: tree[path] for path in tree.keys() - expected.keys()}
        assert {path: tree[path] for path in expected} == expected, directory
        if directory == "credential-and-binary":
            assert injected == {".env": b"API_TOKEN=review-fixture-not-a-secret\n"}
            assert b"\x00" in tree["assets/logo.png"]
        elif directory == "large":
            assert len(injected) == 120
            assert all(path.startswith("data/generated_") for path in injected)
            assert {content.count(b"\n") for content in injected.values()} == {25}
        else:
            assert injected == {}, directory

    renamed = _tree(fake, REVIEW_APP, _pull(fake, 5).head_sha)
    assert "app/text.py" not in renamed and "app/strings.py" in renamed


def test_push_to_pull_request(fake: FakeGitHub) -> None:
    first_head = commit_sha(REVIEW_APP_ID, "pr-1")
    before = _tree(fake, REVIEW_APP, first_head)

    second_head = fake.push_to_pull_request(REVIEW_APP_ID, 1)

    assert second_head == commit_sha(REVIEW_APP_ID, "pr-1-2")
    assert _pull(fake, 1).head_sha == second_head
    listed = fake.list_pull_requests(OCTOCAT, REVIEW_APP, 1).items
    assert (listed[0].number, listed[0].head_sha) == (1, second_head)
    after = _tree(fake, REVIEW_APP, second_head)
    changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    assert changed == {"app/auth/permissions.py"}
    old_lines = before["app/auth/permissions.py"].splitlines()
    new_lines = after["app/auth/permissions.py"].splitlines()
    assert new_lines[:-1] == old_lines and len(new_lines) == len(old_lines) + 1
    # The earlier head is still served, and the merge base is unchanged.
    assert _tree(fake, REVIEW_APP, first_head) == before
    comparison = fake.compare_commits(
        5001, REVIEW_APP, _pull(fake, 1).base_sha, second_head, head_owner=None
    )
    assert comparison.merge_base_sha == commit_sha(REVIEW_APP_ID, "initial")

    # Pushing makes the pull request the most recently updated.
    assert fake.push_to_pull_request(REVIEW_APP_ID, 2) == commit_sha(REVIEW_APP_ID, "pr-2-2")
    assert [p.number for p in fake.list_pull_requests(OCTOCAT, REVIEW_APP, 1).items][:2] == [2, 1]
    assert fake.push_to_pull_request(REVIEW_APP_ID, 1) == commit_sha(REVIEW_APP_ID, "pr-1-3")

    # A push to the default branch does not land on a pull request head.
    assert fake.push(REVIEW_APP_ID) == commit_sha(REVIEW_APP_ID, "push-2")


def test_pull_request_state_switches(fake: FakeGitHub) -> None:
    fake.close_pull_request(REVIEW_APP_ID, 2)
    fake.merge_pull_request(REVIEW_APP_ID, 3)
    fake.set_pull_request_body(REVIEW_APP_ID, 1, "A new description.")

    assert _pull(fake, 2).state == "closed"
    assert _pull(fake, 3).state == "merged"
    listed = fake.list_pull_requests(OCTOCAT, REVIEW_APP, 1).items
    assert [pull.number for pull in listed] == [1, 4, 5, 6, 7, 8]
    assert listed[0].body == _pull(fake, 1).body == "A new description."


def test_withhold_permission(fake: FakeGitHub) -> None:
    granted = fake.get_installation_permissions(REVIEW_APP_PRIVATE)
    assert granted.installation_id == 5001
    assert granted.permissions == {"contents": "read", "metadata": "read", "pull_requests": "read"}
    assert (
        granted.html_url == "https://github.com/organizations/octo-org/settings/installations/5001"
    )

    fake.withhold_permission(REVIEW_APP_PRIVATE_ID, "pull_requests")

    with pytest.raises(GitHubAccessDenied) as caught:
        fake.list_pull_requests(OCTOCAT, REVIEW_APP_PRIVATE, 1)
    assert not isinstance(caught.value, UserAuthorizationInvalid)
    # "Get a pull request" also accepts Contents read.
    assert _pull(fake, 1, REVIEW_APP_PRIVATE).number == 1
    withheld = fake.get_installation_permissions(REVIEW_APP_PRIVATE)
    assert withheld.permissions == {"contents": "read", "metadata": "read"}
    # The permission belongs to the installation; public pull requests stay listable.
    assert fake.get_installation_permissions(REVIEW_APP).permissions == withheld.permissions
    assert fake.list_pull_requests(OCTOCAT, REVIEW_APP, 1).items

    with pytest.raises(GitHubNotFound):
        fake.get_installation_permissions("monalisa/public-lib")
    with pytest.raises(ValueError):
        fake.withhold_permission(REVIEW_APP_ID, "administration")


def test_missing_commits(fake: FakeGitHub) -> None:
    initial = commit_sha(REVIEW_APP_ID, "initial")
    first_head = commit_sha(REVIEW_APP_ID, "pr-1")

    fake.drop_commit(first_head)

    with pytest.raises(CommitUnavailable):
        fake.compare_commits(5001, REVIEW_APP, initial, first_head, head_owner=None)
    with pytest.raises(GitHubNotFound):
        fake.open_tarball(5001, REVIEW_APP, first_head)
    assert _pull(fake, 1).head_sha == first_head

    second = _pull(fake, 2)
    fake.drop_commit(initial)
    with pytest.raises(CommitUnavailable):
        fake.compare_commits(5001, REVIEW_APP, initial, second.head_sha, head_owner=None)

    reset_fake_github()
    fake.unrelated_history(REVIEW_APP_ID, 1)
    with pytest.raises(NoCommonHistory):
        fake.compare_commits(5001, REVIEW_APP, initial, first_head, head_owner=None)
    other = _pull(fake, 2)
    assert fake.compare_commits(5001, REVIEW_APP, initial, other.head_sha, head_owner=None)


def test_pull_request_access(fake: FakeGitHub) -> None:
    with pytest.raises(GitHubNotFound):
        fake.list_pull_requests(HUBOT, REVIEW_APP_PRIVATE, 1)
    with pytest.raises(GitHubNotFound):
        fake.get_pull_request(HUBOT, REVIEW_APP_PRIVATE, 1)
    with pytest.raises(GitHubNotFound):
        fake.list_pull_requests(OCTOCAT, "octo-org/no-such-repo", 1)

    fake.make_private(REVIEW_APP_ID)
    assert fake.list_pull_requests(HUBOT, REVIEW_APP, 1).items
    fake.revoke_access("hubot", REVIEW_APP_ID)
    with pytest.raises(GitHubNotFound):
        fake.list_pull_requests(HUBOT, REVIEW_APP, 1)

    fake.revoke_authorization("octocat")
    with pytest.raises(UserAuthorizationInvalid):
        fake.list_pull_requests(OCTOCAT, REVIEW_APP, 1)
    with pytest.raises(UserAuthorizationInvalid):
        fake.get_pull_request(OCTOCAT, REVIEW_APP, 1)


def test_reset_undoes_the_pull_request_switches(fake: FakeGitHub) -> None:
    initial = commit_sha(REVIEW_APP_ID, "initial")
    first_head = commit_sha(REVIEW_APP_ID, "pr-1")
    fake.push_to_pull_request(REVIEW_APP_ID, 1)
    fake.close_pull_request(REVIEW_APP_ID, 2)
    fake.merge_pull_request(REVIEW_APP_ID, 3)
    fake.set_pull_request_body(REVIEW_APP_ID, 4, "changed")
    fake.withhold_permission(REVIEW_APP_PRIVATE_ID, "pull_requests")
    fake.drop_commit(initial)
    fake.unrelated_history(REVIEW_APP_ID, 1)
    fake.make_private(REVIEW_APP_ID)

    reset_fake_github()

    listed = fake.list_pull_requests(OCTOCAT, REVIEW_APP, 1).items
    assert [pull.number for pull in listed] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert listed[0].head_sha == first_head
    assert listed[3].body == _overlay("credential-and-binary")[0]["body"]
    assert fake.list_pull_requests(OCTOCAT, REVIEW_APP_PRIVATE, 1).items
    assert "pull_requests" in fake.get_installation_permissions(REVIEW_APP).permissions
    assert fake.compare_commits(5001, REVIEW_APP, initial, first_head, head_owner=None)
    assert fake.get_repository(HUBOT, REVIEW_APP_ID).private is False
    with pytest.raises(GitHubNotFound):
        fake.open_tarball(5001, REVIEW_APP, commit_sha(REVIEW_APP_ID, "pr-1-2"))
