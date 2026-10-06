"""In-memory GitHub gateway for tests and local development (research R13).

Serves the fixture repositories in `backend/tests/fixtures/repos/` plus a few generated ones.
Tests change its state through the switch methods (`revoke_access`, `rename`, `reinstall`,
`advance`) and read `calls`; `reset_fake_github()` undoes all of that.
"""

import hashlib
import io
import tarfile
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO
from urllib.parse import quote

from codeatlas.config import get_settings
from codeatlas.github.gateway import (
    BranchNotFound,
    GitHubAccessDenied,
    GitHubNotFound,
    GitHubRepository,
    GitHubUser,
    RepositoryEmpty,
    UserTokens,
)

FIXTURE_REPOS_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "repos"

SAMPLE_APP_ID = 2001
SAMPLE_APP_PRIVATE_ID = 2002
NO_CODE_ID = 2003
OVERSIZED_ID = 2004
VENDORED_HEAVY_ID = 2005
UNSAFE_PATHS_ID = 2006
EMPTY_ID = 2007
SOLO_ID = 2008
HUBOT_TOOLS_ID = 2009

# The second sample-app commit renames one Python file and deletes another.
SAMPLE_APP_RENAMED = ("app/utils/strings.py", "app/utils/text.py")
SAMPLE_APP_DELETED = "app/reports.py"

VENDORED_FILE_COUNT = 6000
ACCESS_PREFIX = "fake-token-"
REFRESH_PREFIX = "fake-refresh-"
CODE_PREFIX = "fake:"

_USERS = {
    "octocat": GitHubUser(
        id=1001,
        login="octocat",
        name="The Octocat",
        avatar_url="https://avatars.githubusercontent.com/u/1001?v=4",
    ),
    "hubot": GitHubUser(
        id=1002,
        login="hubot",
        name="Hubot",
        avatar_url="https://avatars.githubusercontent.com/u/1002?v=4",
    ),
}
_INITIAL_INSTALLATIONS = {"octo-org": 5001, "octocat": 5002, "hubot": 5003}
_INITIAL_ACCESS = {
    "octocat": {2001, 2002, 2003, 2004, 2005, 2006, 2007, 2008},
    "hubot": {SAMPLE_APP_ID, HUBOT_TOOLS_ID},
}
_FIRST_REINSTALL_ID = 5101
_FIXED_MTIME = 1_700_000_000

ArchiveBuilder = Callable[[str], bytes]
"""Builds the gzip tar bytes of one commit, given the top-level directory name."""


def commit_sha(github_repository_id: int, commit_name: str) -> str:
    """The deterministic SHA of a fake commit: sha1 of `"<repository id>:<commit name>"`."""
    data = f"{github_repository_id}:{commit_name}".encode()
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


# Archive contents ---------------------------------------------------------------------------


def _fixture_tree(name: str) -> dict[str, bytes]:
    root = FIXTURE_REPOS_DIR / name
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".DS_Store" and "__pycache__" not in path.parts
    }


def _sample_app_initial() -> dict[str, bytes]:
    # Kept out of the fixture directory: credential names are git-ignored and flagged by secret
    # scanners, `node_modules/` is git-ignored, and an unparsable .py file would fail the linter.
    files = _fixture_tree("sample-app")
    files.update(
        {
            ".env": b"API_TOKEN=not-a-real-token\n",
            "id_rsa": b"not a real private key\n",
            "node_modules/left-pad/package.json": b'{"name": "left-pad", "version": "1.3.0"}\n',
            "node_modules/left-pad/index.js": b"module.exports = (s, n) => `${s}`.padStart(n);\n",
            "app/legacy.py": b"def broken(:\n    return 1\n",
            "data/large.txt": b"x" * (get_settings().max_file_bytes + 1),
        }
    )
    return files


def _sample_app_second() -> dict[str, bytes]:
    files = _sample_app_initial()
    old_path, new_path = SAMPLE_APP_RENAMED
    files[new_path] = files.pop(old_path)
    del files[SAMPLE_APP_DELETED]
    return files


def _oversized_tree() -> dict[str, bytes]:
    count = get_settings().max_files_per_snapshot + 1
    return {f"files/file-{index:05d}.txt": f"line {index}\n".encode() for index in range(count)}


def _vendored_heavy_tree() -> dict[str, bytes]:
    files = {
        f"node_modules/pkg-{index // 100:02d}/lib/file-{index:04d}.js": (
            f"module.exports = {index};\n".encode()
        )
        for index in range(VENDORED_FILE_COUNT)
    }
    files.update(
        {
            "README.md": b"# Vendored heavy\n\n## Usage\n\nRun `python -m app.server`.\n",
            "package.json": b'{"name": "vendored-heavy", "private": true}\n',
            "app/__init__.py": b"",
            "app/server.py": b'def health() -> str:\n    return "ok"\n',
            "src/index.ts": b"export const greet = (name: string): string => `Hello ${name}`;\n",
            "docs/usage.md": b"# Usage\n\nCall `health` to check the server.\n",
        }
    )
    return files


def _member(name: str, kind: bytes = tarfile.REGTYPE, size: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = size
    info.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
    info.mtime = _FIXED_MTIME
    return info


def _add_file(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    tar.addfile(_member(name, size=len(data)), io.BytesIO(data))


def _tar_gz(top: str, files: Mapping[str, bytes]) -> bytes:
    """A GitHub-style tarball: every member sits under `top/`, directories included."""
    directories = {
        path.rsplit("/", depth)[0] for path in files for depth in range(1, path.count("/") + 1)
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=1) as tar:
        tar.addfile(_member(f"{top}/", tarfile.DIRTYPE))
        for path in sorted(directories | files.keys()):
            if path in directories:
                tar.addfile(_member(f"{top}/{path}/", tarfile.DIRTYPE))
            else:
                _add_file(tar, f"{top}/{path}", files[path])
    return buffer.getvalue()


def _from_tree(tree: Callable[[], Mapping[str, bytes]]) -> ArchiveBuilder:
    return lambda top: _tar_gz(top, tree())


def _unsafe_paths_archive(top: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.addfile(_member(f"{top}/", tarfile.DIRTYPE))
        _add_file(
            tar, f"{top}/README.md", b"# Unsafe paths\n\nOnly this file and app.py are safe.\n"
        )
        _add_file(tar, f"{top}/app.py", b'def main() -> str:\n    return "safe"\n')
        _add_file(tar, f"{top}/../escape.txt", b"written outside the extraction root\n")
        _add_file(tar, "/etc/codeatlas-absolute.txt", b"absolute path\n")
        symlink = _member(f"{top}/docs-link", tarfile.SYMTYPE)
        symlink.linkname = "/etc/passwd"
        tar.addfile(symlink)
        hardlink = _member(f"{top}/readme-hardlink.md", tarfile.LNKTYPE)
        hardlink.linkname = f"{top}/README.md"
        tar.addfile(hardlink)
    return buffer.getvalue()


# Repository state ---------------------------------------------------------------------------


@dataclass
class _Repository:
    id: int
    full_name: str
    private: bool
    commits: list[tuple[str, ArchiveBuilder]]
    """(commit name, archive builder), oldest first."""
    branches: dict[str, str] = field(default_factory=dict)
    """Branch name to commit name. Empty means the repository has no commits."""
    default_branch: str = "main"

    @property
    def owner(self) -> str:
        return self.full_name.split("/", 1)[0]

    def archive_builder(self, sha: str) -> ArchiveBuilder | None:
        for name, builder in self.commits:
            if commit_sha(self.id, name) == sha:
                return builder
        return None


def _single_commit(
    repository_id: int, full_name: str, builder: ArchiveBuilder, *, private: bool = False
) -> _Repository:
    return _Repository(
        id=repository_id,
        full_name=full_name,
        private=private,
        commits=[("initial", builder)],
        branches={"main": "initial"},
    )


def _initial_repositories() -> dict[int, _Repository]:
    no_code = _from_tree(lambda: _fixture_tree("no-code"))
    repositories = [
        _Repository(
            id=SAMPLE_APP_ID,
            full_name="octo-org/sample-app",
            private=False,
            commits=[
                ("initial", _from_tree(_sample_app_initial)),
                ("second", _from_tree(_sample_app_second)),
            ],
            branches={"main": "initial"},
        ),
        _single_commit(
            SAMPLE_APP_PRIVATE_ID,
            "octo-org/sample-app-private",
            _from_tree(_sample_app_initial),
            private=True,
        ),
        _single_commit(NO_CODE_ID, "octo-org/no-code", no_code),
        _single_commit(OVERSIZED_ID, "octo-org/oversized", _from_tree(_oversized_tree)),
        _single_commit(
            VENDORED_HEAVY_ID, "octo-org/vendored-heavy", _from_tree(_vendored_heavy_tree)
        ),
        _single_commit(UNSAFE_PATHS_ID, "octo-org/unsafe-paths", _unsafe_paths_archive),
        _Repository(id=EMPTY_ID, full_name="octo-org/empty", private=False, commits=[]),
        _single_commit(SOLO_ID, "octocat/solo", no_code),
        _single_commit(HUBOT_TOOLS_ID, "hubot/tools", no_code),
    ]
    return {repository.id: repository for repository in repositories}


class FakeGitHub:
    """A `GitHubGateway` backed by fixture repositories, with switches for tests."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.reset()

    def reset(self) -> None:
        """Restore the initial users, repositories, access, and installations."""
        self.calls.clear()
        self._repositories = _initial_repositories()
        self._installations = dict(_INITIAL_INSTALLATIONS)
        self._access = {login: set(ids) for login, ids in _INITIAL_ACCESS.items()}
        self._next_installation_id = _FIRST_REINSTALL_ID

    # Test switches -----------------------------------------------------------------------

    def revoke_access(self, login: str, github_repository_id: int) -> None:
        """The user (or the App installation) can no longer reach the repository."""
        self._access[login].discard(github_repository_id)

    def rename(self, github_repository_id: int, new_full_name: str) -> None:
        """Rename or transfer the repository; the old name stops resolving."""
        repository = self._repositories[github_repository_id]
        repository.full_name = new_full_name
        if repository.owner not in self._installations:
            self._installations[repository.owner] = self._allocate_installation_id()

    def reinstall(self, github_repository_id: int) -> int:
        """Reinstall the App on the repository owner's account; returns the new installation ID."""
        owner = self._repositories[github_repository_id].owner
        self._installations[owner] = self._allocate_installation_id()
        return self._installations[owner]

    def advance(self, github_repository_id: int) -> str:
        """Move `main` to the next commit and return its SHA (only sample-app has one)."""
        repository = self._repositories[github_repository_id]
        names = [name for name, _ in repository.commits]
        position = names.index(repository.branches["main"]) if repository.branches else -1
        if position + 1 >= len(names):
            raise ValueError(f"repository {github_repository_id} has no commit to advance to")
        repository.branches["main"] = names[position + 1]
        return commit_sha(github_repository_id, names[position + 1])

    # GitHubGateway -----------------------------------------------------------------------

    def authorize_url(self, state: str) -> str:
        self.calls["authorize_url"] += 1
        return f"/auth/github/callback?code={CODE_PREFIX}octocat&state={quote(state, safe='')}"

    def exchange_code(self, code: str) -> UserTokens:
        self.calls["exchange_code"] += 1
        login = code.removeprefix(CODE_PREFIX)
        if not code.startswith(CODE_PREFIX) or login not in _USERS:
            raise GitHubAccessDenied("unknown authorization code")
        return _tokens_for(login)

    def refresh_user_token(self, refresh_token: str) -> UserTokens:
        self.calls["refresh_user_token"] += 1
        login = refresh_token.removeprefix(REFRESH_PREFIX)
        if not refresh_token.startswith(REFRESH_PREFIX) or login not in _USERS:
            raise GitHubAccessDenied("unknown refresh token")
        return _tokens_for(login)

    def get_authenticated_user(self, user_token: str) -> GitHubUser:
        self.calls["get_authenticated_user"] += 1
        return _USERS[_login_for(user_token)]

    def list_accessible_repositories(self, user_token: str) -> list[GitHubRepository]:
        self.calls["list_accessible_repositories"] += 1
        ids = sorted(self._access[_login_for(user_token)])
        return [self._describe(self._repositories[repository_id]) for repository_id in ids]

    def get_repository(self, user_token: str, github_repository_id: int) -> GitHubRepository:
        self.calls["get_repository"] += 1
        if github_repository_id not in self._access[_login_for(user_token)]:
            raise GitHubNotFound(f"repository {github_repository_id} not found")
        return self._describe(self._repositories[github_repository_id])

    def get_installation_id(self, full_name: str) -> int:
        self.calls["get_installation_id"] += 1
        return self._installations[self._by_name(full_name).owner]

    def resolve_commit(self, installation_id: int, full_name: str, branch: str) -> str:
        self.calls["resolve_commit"] += 1
        repository = self._installed(installation_id, full_name)
        if not repository.branches:
            raise RepositoryEmpty(f"{full_name} has no branches")
        if branch not in repository.branches:
            raise BranchNotFound(f"{full_name} has no branch {branch!r}")
        return commit_sha(repository.id, repository.branches[branch])

    def open_tarball(
        self, installation_id: int, full_name: str, sha: str
    ) -> AbstractContextManager[IO[bytes]]:
        self.calls["open_tarball"] += 1
        repository = self._installed(installation_id, full_name)
        builder = repository.archive_builder(sha)
        if builder is None:
            raise GitHubNotFound(f"{full_name} has no commit {sha}")
        owner, name = repository.full_name.split("/", 1)
        return closing(io.BytesIO(builder(f"{owner}-{name}-{sha[:7]}")))

    # Helpers -----------------------------------------------------------------------------

    def _allocate_installation_id(self) -> int:
        self._next_installation_id += 1
        return self._next_installation_id - 1

    def _describe(self, repository: _Repository) -> GitHubRepository:
        return GitHubRepository(
            id=repository.id,
            full_name=repository.full_name,
            default_branch=repository.default_branch,
            private=repository.private,
            installation_id=self._installations[repository.owner],
        )

    def _by_name(self, full_name: str) -> _Repository:
        for repository in self._repositories.values():
            if repository.full_name.lower() == full_name.lower():
                return repository
        raise GitHubNotFound(f"repository {full_name} not found")

    def _installed(self, installation_id: int, full_name: str) -> _Repository:
        repository = self._by_name(full_name)
        if self._installations[repository.owner] != installation_id:
            raise GitHubAccessDenied(f"installation {installation_id} cannot access {full_name}")
        return repository


def _login_for(user_token: str) -> str:
    login = user_token.removeprefix(ACCESS_PREFIX)
    if not user_token.startswith(ACCESS_PREFIX) or login not in _USERS:
        raise GitHubAccessDenied("invalid user token")
    return login


def _tokens_for(login: str) -> UserTokens:
    now = datetime.now(UTC)
    return UserTokens(
        access_token=f"{ACCESS_PREFIX}{login}",
        access_token_expires_at=now + timedelta(hours=8),
        refresh_token=f"{REFRESH_PREFIX}{login}",
        refresh_token_expires_at=now + timedelta(days=180),
    )


_fake: FakeGitHub | None = None


def get_fake_github() -> FakeGitHub:
    """The process-wide fake, so tests and the app share one state."""
    global _fake
    if _fake is None:
        _fake = FakeGitHub()
    return _fake


def reset_fake_github() -> None:
    """Undo every switch and clear the call counter (the instance itself is kept)."""
    get_fake_github().reset()
