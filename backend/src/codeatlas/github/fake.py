"""In-memory GitHub gateway for tests and local development (research R13).

Serves the fixture repositories in `backend/tests/fixtures/repos/` plus a few generated ones, and
the pull requests in `backend/tests/fixtures/pull-requests/` on the review fixtures. Tests change
its state through the switch methods and read `calls`; `reset_fake_github()` undoes all of that.
The switches:

- commits: `push` (also named `advance`), `rename_default_branch`, and `drop_commit`;
- pull requests: `push_to_pull_request`, `close_pull_request`, `merge_pull_request`,
  `set_pull_request_body`, and `unrelated_history`;
- repositories: `rename` and `make_private`;
- installations: `uninstall`, `reinstall`, `suspend`, `remove_from_installation`, and
  `withhold_permission`;
- users: `revoke_access` and `revoke_authorization`;
- GitHub itself: `set_unavailable`.
"""

import difflib
import hashlib
import io
import json
import tarfile
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path, PurePosixPath
from typing import IO, Literal
from urllib.parse import quote

from codeatlas.config import get_settings
from codeatlas.github.gateway import (
    PULL_REQUEST_PAGE_SIZE,
    BranchNotFound,
    CommitUnavailable,
    Comparison,
    GitHubAccessDenied,
    GitHubNotFound,
    GitHubRepository,
    GitHubUnavailable,
    GitHubUser,
    InstallationPermissions,
    NoCommonHistory,
    PullRequest,
    PullRequestPage,
    PullRequestState,
    RepositoryEmpty,
    UserAuthorizationInvalid,
    UserTokens,
)

FIXTURE_REPOS_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "repos"
FIXTURE_PULL_REQUESTS_DIR = FIXTURE_REPOS_DIR.parent / "pull-requests"

SAMPLE_APP_ID = 2001
SAMPLE_APP_PRIVATE_ID = 2002
NO_CODE_ID = 2003
OVERSIZED_ID = 2004
VENDORED_HEAVY_ID = 2005
UNSAFE_PATHS_ID = 2006
EMPTY_ID = 2007
SOLO_ID = 2008
HUBOT_TOOLS_ID = 2009
PUBLIC_UNINSTALLED_ID = 2010
REVIEW_APP_ID = 2011
REVIEW_APP_PRIVATE_ID = 2012

# The second sample-app commit renames one Python file and deletes another.
SAMPLE_APP_RENAMED = ("app/utils/strings.py", "app/utils/text.py")
SAMPLE_APP_DELETED = "app/reports.py"

VENDORED_FILE_COUNT = 6000
# The pull requests on the private review fixture, by overlay directory; the public one has all.
REVIEW_APP_PRIVATE_PULL_REQUESTS = ("seeded-defect",)
LARGE_PULL_REQUEST_FILES = 120
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
    # Has no installation of the App: the user outside the pilot's access list in the tests.
    "monalisa": GitHubUser(
        id=1003,
        login="monalisa",
        name="Mona Lisa",
        avatar_url="https://avatars.githubusercontent.com/u/1003?v=4",
    ),
}
_INITIAL_INSTALLATIONS = {"octo-org": 5001, "octocat": 5002, "hubot": 5003}
_ORGANIZATIONS = frozenset({"octo-org"})
_INITIAL_ACCESS = {
    "octocat": {2001, 2002, 2003, 2004, 2005, 2006, 2007, 2008, 2011, 2012},
    "hubot": {SAMPLE_APP_ID, HUBOT_TOOLS_ID, REVIEW_APP_ID},
    "monalisa": set(),
}
# The App's repository permissions. An installation has them all unless one is withheld.
_PERMISSIONS = ("contents", "metadata", "pull_requests")
_COMPARE_FILE_LIMIT = 300
_COMMENT_PREFIXES = {".py": "# ", ".ts": "// ", ".tsx": "// "}
_FIRST_REINSTALL_ID = 5101
_FIXED_MTIME = 1_700_000_000

ArchiveBuilder = Callable[[str], bytes]
"""Builds the gzip tar bytes of one commit, given the top-level directory name."""


def commit_sha(github_repository_id: int, commit_name: str) -> str:
    """The deterministic SHA of a fake commit: sha1 of `"<repository id>:<commit name>"`."""
    data = f"{github_repository_id}:{commit_name}".encode()
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


# Archive contents ---------------------------------------------------------------------------


def _read_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".DS_Store" and "__pycache__" not in path.parts
    }


def _fixture_tree(name: str) -> dict[str, bytes]:
    return _read_tree(FIXTURE_REPOS_DIR / name)


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


# Pull requests ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Overlay:
    """A pull request fixture: `pull-request.json`, plus `files/` written over the base tree."""

    directory: Path
    number: int
    title: str
    body: str
    author: str
    draft: bool
    state: Literal["open", "closed"]
    base_ref: str
    head_ref: str
    head_owner: str | None
    remove: tuple[str, ...]
    rename: dict[str, str]
    """Old path to new path."""
    updated_at: datetime

    @classmethod
    def load(cls, name: str) -> "_Overlay":
        directory = FIXTURE_PULL_REQUESTS_DIR / name
        data = json.loads((directory / "pull-request.json").read_text())
        if data["state"] not in ("open", "closed"):
            raise ValueError(f"{name}: state must be open or closed")
        return cls(
            directory=directory,
            number=int(data["number"]),
            title=str(data["title"]),
            body=str(data["body"]),
            author=str(data["author"]),
            draft=bool(data["draft"]),
            state=data["state"],
            base_ref=str(data["base_ref"]),
            head_ref=str(data["head_ref"]),
            head_owner=data["head_owner"],
            remove=tuple(data["remove"]),
            rename=dict(data["rename"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    def files(self) -> dict[str, bytes]:
        """What the pull request writes over the base tree: `files/` plus injected files."""
        files_dir = self.directory / "files"
        files = _read_tree(files_dir) if files_dir.is_dir() else {}
        files.update(_injected_overlay_files(self.directory.name))
        return files


def _injected_overlay_files(name: str) -> dict[str, bytes]:
    # Kept out of the fixture directory, as for sample-app: credential names are git-ignored and
    # flagged by secret scanners, and 120 generated files would only add bulk.
    if name == "credential-and-binary":
        return {".env": b"API_TOKEN=review-fixture-not-a-secret\n"}
    if name == "large":
        return {
            f"data/generated_{index:03d}.py": _data_module(index)
            for index in range(1, LARGE_PULL_REQUEST_FILES + 1)
        }
    return {}


def _data_module(index: int) -> bytes:
    """25 lines of Python."""
    lines = [f'"""Data module {index} for the load tests."""', ""]
    lines += [f"VALUE_{line:02d} = {index * 100 + line}" for line in range(23)]
    return "".join(f"{line}\n" for line in lines).encode()


def _pull_request_tree(
    base: Mapping[str, bytes], overlay: _Overlay, revision: int
) -> dict[str, bytes]:
    """The head tree: `rename` applied, the overlay written over it, then `remove` deleted.

    Each revision after the first appends one line to the overlay's first text file.
    """
    files = dict(base)
    for old, new in overlay.rename.items():
        files[new] = files.pop(old)
    written = overlay.files()
    files.update(written)
    for path in overlay.remove:
        del files[path]
    if revision > 1:
        text_paths = [path for path in sorted(written) if b"\0" not in written[path]]
        path = text_paths[0] if text_paths else "README.md"
        content = files.get(path, b"")
        if content and not content.endswith(b"\n"):
            content += b"\n"
        prefix = _COMMENT_PREFIXES.get(PurePosixPath(path).suffix, "")
        lines = "".join(f"{prefix}Revision {number}.\n" for number in range(2, revision + 1))
        files[path] = content + lines.encode()
    return files


def _diff_stats(
    base: Mapping[str, bytes], head: Mapping[str, bytes], renamed: Mapping[str, str]
) -> tuple[int, int, int]:
    """Lines added, lines removed, and files changed, as GitHub counts them: a renamed file
    (`renamed` maps new paths to old ones) counts once, and a binary file adds no lines.
    """
    pairs = [(old, new) for new, old in renamed.items()]
    moved = {path for pair in pairs for path in pair}
    pairs += [
        (path, path)
        for path in sorted(base.keys() | head.keys())
        if path not in moved and base.get(path) != head.get(path)
    ]
    additions = deletions = 0
    for old, new in pairs:
        before, after = base.get(old, b""), head.get(new, b"")
        if b"\0" in before or b"\0" in after:
            continue
        matcher = difflib.SequenceMatcher(
            a=before.splitlines(), b=after.splitlines(), autojunk=False
        )
        for tag, before_start, before_end, after_start, after_end in matcher.get_opcodes():
            if tag != "equal":
                deletions += before_end - before_start
                additions += after_end - after_start
    return additions, deletions, len(pairs)


@dataclass
class _PullRequest:
    overlay: _Overlay
    base_tree: Callable[[], dict[str, bytes]]
    """The tree of `initial`, the commit every pull request branches from."""
    state: PullRequestState
    body: str
    updated_at: datetime
    revision: int = 1
    """1 for the overlay's head commit `pr-<number>`; each push adds `pr-<number>-<revision>`."""
    unrelated: bool = False
    """Set by `unrelated_history`: the head shares no ancestor with the base."""

    @property
    def renamed(self) -> dict[str, str]:
        """The rename hint as GitHub's comparison gives it: new path to previous path."""
        return {new: old for old, new in self.overlay.rename.items()}

    def commit_name(self, revision: int) -> str:
        number = self.overlay.number
        return f"pr-{number}" if revision == 1 else f"pr-{number}-{revision}"

    def tree(self, revision: int) -> dict[str, bytes]:
        return _pull_request_tree(self.base_tree(), self.overlay, revision)


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
    pull_requests: dict[int, _PullRequest] = field(default_factory=dict)
    """By number. Their head commits are kept here, off the branches, so `push` never reaches
    them."""

    @property
    def owner(self) -> str:
        return self.full_name.split("/", 1)[0]

    def archive_builder(self, sha: str) -> ArchiveBuilder | None:
        for name, builder in self.commits:
            if commit_sha(self.id, name) == sha:
                return builder
        found = self.pull_request_commit(sha)
        if found is not None:
            pull, revision = found
            return _from_tree(partial(pull.tree, revision))
        return None

    def pull_request_commit(self, sha: str) -> tuple[_PullRequest, int] | None:
        """The pull request and revision whose head commit is `sha`, if any."""
        for pull in self.pull_requests.values():
            for revision in range(1, pull.revision + 1):
                if commit_sha(self.id, pull.commit_name(revision)) == sha:
                    return pull, revision
        return None

    def touch(self, pull: _PullRequest) -> None:
        """Make the pull request the most recently updated, deterministically."""
        latest = max(other.updated_at for other in self.pull_requests.values())
        pull.updated_at = latest + timedelta(minutes=1)


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


def _review_app(
    repository_id: int, full_name: str, overlays: list[str], *, private: bool = False
) -> _Repository:
    """A review fixture: `review-app/` as `initial`, with `main` and `release` on it, and the
    pull requests of `overlays` (directory names).
    """
    tree = partial(_fixture_tree, "review-app")
    pull_requests: dict[int, _PullRequest] = {}
    for name in overlays:
        overlay = _Overlay.load(name)
        pull_requests[overlay.number] = _PullRequest(
            overlay=overlay,
            base_tree=tree,
            state=overlay.state,
            body=overlay.body,
            updated_at=overlay.updated_at,
        )
    return _Repository(
        id=repository_id,
        full_name=full_name,
        private=private,
        commits=[("initial", _from_tree(tree))],
        branches={"main": "initial", "release": "initial"},
        pull_requests=pull_requests,
    )


def _initial_repositories() -> dict[int, _Repository]:
    no_code = _from_tree(lambda: _fixture_tree("no-code"))
    overlays = sorted(
        path.parent.name for path in FIXTURE_PULL_REQUESTS_DIR.glob("*/pull-request.json")
    )
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
        _single_commit(PUBLIC_UNINSTALLED_ID, "monalisa/public-lib", no_code),
        _review_app(REVIEW_APP_ID, "octo-org/review-app", overlays),
        _review_app(
            REVIEW_APP_PRIVATE_ID,
            "octo-org/review-app-private",
            list(REVIEW_APP_PRIVATE_PULL_REQUESTS),
            private=True,
        ),
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
        self._suspended: set[int] = set()  # installation IDs
        self._removed: set[int] = set()  # repositories taken out of their owner's installation
        self._revoked: set[str] = set()  # logins that revoked their authorization of the App
        self._withheld: set[tuple[int, str]] = set()  # (installation ID, permission)
        self._dropped: set[str] = set()  # commit SHAs that GitHub no longer serves
        self._unavailable = False

    # Test switches -----------------------------------------------------------------------

    def revoke_access(self, login: str, github_repository_id: int) -> None:
        """The user can no longer reach the repository through an installation of the App.

        A public repository stays visible to the user, as on GitHub.
        """
        self._access[login].discard(github_repository_id)

    def uninstall(self, github_repository_id: int) -> None:
        """Uninstall the App from the repository owner's account."""
        del self._installations[self._repositories[github_repository_id].owner]

    def rename(self, github_repository_id: int, new_full_name: str) -> None:
        """Rename or transfer the repository; the old name stops resolving."""
        repository = self._repositories[github_repository_id]
        repository.full_name = new_full_name
        if repository.owner not in self._installations:
            self._installations[repository.owner] = self._allocate_installation_id()

    def reinstall(self, github_repository_id: int) -> int:
        """Reinstall the App on the repository owner's account; returns the new installation ID.

        The new installation is active and covers every repository of the owner.
        """
        owner = self._repositories[github_repository_id].owner
        self._installations[owner] = self._allocate_installation_id()
        self._removed -= {r.id for r in self._repositories.values() if r.owner == owner}
        return self._installations[owner]

    def suspend(self, github_repository_id: int) -> None:
        """Suspend the App's installation on the repository owner's account.

        GitHub still reports the installation for the repository, but leaves it out of the user's
        installations, and the installation can no longer read.
        """
        self._suspended.add(self._installations[self._repositories[github_repository_id].owner])

    def remove_from_installation(self, github_repository_id: int) -> None:
        """Remove the repository from its owner's installation; other repositories stay."""
        self._removed.add(github_repository_id)

    def make_private(self, github_repository_id: int) -> None:
        """Make the repository private; only users with access through the App still see it."""
        self._repositories[github_repository_id].private = True

    def revoke_authorization(self, login: str) -> None:
        """The user revokes their authorization of the App: their tokens stop working.

        Signing in again (`exchange_code`) authorizes the App anew.
        """
        self._revoked.add(login)

    def set_unavailable(self, unavailable: bool) -> None:
        """While set, every method that calls GitHub raises `GitHubUnavailable`.

        `authorize_url` only builds a URL, so it keeps working.
        """
        self._unavailable = unavailable

    def push(self, github_repository_id: int) -> str:
        """Move the default branch to the next commit and return its SHA.

        Past the fixture commits (sample-app has two), each push adds a commit with the same files
        as the head.
        """
        repository = self._repositories[github_repository_id]
        if not repository.branches:
            raise ValueError(f"repository {github_repository_id} has no commits to push onto")
        names = [name for name, _ in repository.commits]
        position = names.index(repository.branches[repository.default_branch])
        if position + 1 == len(names):
            name = f"push-{len(names) + 1}"
            repository.commits.append((name, repository.commits[position][1]))
            names.append(name)
        repository.branches[repository.default_branch] = names[position + 1]
        return commit_sha(github_repository_id, names[position + 1])

    def advance(self, github_repository_id: int) -> str:
        """The earlier name of `push`."""
        return self.push(github_repository_id)

    def rename_default_branch(self, github_repository_id: int, new_name: str) -> None:
        """Rename the default branch; the old name stops resolving."""
        repository = self._repositories[github_repository_id]
        if repository.branches:
            repository.branches[new_name] = repository.branches.pop(repository.default_branch)
        repository.default_branch = new_name

    def drop_commit(self, sha: str) -> None:
        """GitHub no longer serves the commit, as after a force push and garbage collection.

        `compare_commits` raises `CommitUnavailable` and `open_tarball` raises `GitHubNotFound`.
        """
        self._dropped.add(sha)

    def push_to_pull_request(self, github_repository_id: int, number: int) -> str:
        """Push to the pull request's head branch and return the new head SHA.

        The new head, `pr-<number>-2` and so on, appends one line to the overlay's first text
        file. The pull request becomes the most recently updated.
        """
        repository = self._repositories[github_repository_id]
        pull = repository.pull_requests[number]
        pull.revision += 1
        repository.touch(pull)
        return commit_sha(github_repository_id, pull.commit_name(pull.revision))

    def close_pull_request(self, github_repository_id: int, number: int) -> None:
        self._set_pull_request_state(github_repository_id, number, "closed")

    def merge_pull_request(self, github_repository_id: int, number: int) -> None:
        self._set_pull_request_state(github_repository_id, number, "merged")

    def set_pull_request_body(self, github_repository_id: int, number: int, body: str) -> None:
        repository = self._repositories[github_repository_id]
        pull = repository.pull_requests[number]
        pull.body = body
        repository.touch(pull)

    def unrelated_history(self, github_repository_id: int, number: int) -> None:
        """The pull request's head shares no ancestor with its base: `compare_commits` raises
        `NoCommonHistory`.
        """
        self._repositories[github_repository_id].pull_requests[number].unrelated = True

    def withhold_permission(self, github_repository_id: int, permission: str) -> None:
        """The installation covering the repository lacks `permission`, as when the account owner
        has not approved an update of the App's permissions.

        For `pull_requests`: listing the pull requests of a private repository that the
        installation covers raises `GitHubAccessDenied`, while public ones stay listable and
        `get_pull_request` still works. `get_installation_permissions` omits the permission.
        """
        if permission not in _PERMISSIONS:
            raise ValueError(f"unknown permission {permission!r}")
        owner = self._repositories[github_repository_id].owner
        self._withheld.add((self._installations[owner], permission))

    # GitHubGateway -----------------------------------------------------------------------

    def authorize_url(self, state: str) -> str:
        self.calls["authorize_url"] += 1
        return f"/auth/github/callback?code={CODE_PREFIX}octocat&state={quote(state, safe='')}"

    def exchange_code(self, code: str) -> UserTokens:
        self._call("exchange_code")
        login = code.removeprefix(CODE_PREFIX)
        if not code.startswith(CODE_PREFIX) or login not in _USERS:
            raise UserAuthorizationInvalid("unknown authorization code")
        # Signing in again authorizes the App anew, which ends an earlier revocation.
        self._revoked.discard(login)
        return _tokens_for(login)

    def refresh_user_token(self, refresh_token: str) -> UserTokens:
        self._call("refresh_user_token")
        login = refresh_token.removeprefix(REFRESH_PREFIX)
        if not refresh_token.startswith(REFRESH_PREFIX) or login not in _USERS:
            raise UserAuthorizationInvalid("unknown refresh token")
        if login in self._revoked:
            raise UserAuthorizationInvalid("the refresh token was revoked")
        return _tokens_for(login)

    def get_authenticated_user(self, user_token: str) -> GitHubUser:
        self._call("get_authenticated_user")
        return _USERS[self._login_for(user_token)]

    def get_user_by_login(self, login: str) -> GitHubUser:
        self._call("get_user_by_login")
        # As on GitHub, logins match without regard to case.
        user = _USERS.get(login.lower())
        if user is None:
            raise GitHubNotFound(f"user {login} not found")
        return user

    def list_accessible_repositories(self, user_token: str) -> list[GitHubRepository]:
        self._call("list_accessible_repositories")
        return [self._describe(repository) for repository in self._reachable(user_token)]

    def get_repository(self, user_token: str, github_repository_id: int) -> GitHubRepository:
        self._call("get_repository")
        repository = self._repositories.get(github_repository_id)
        login = self._login_for(user_token)
        if repository is None or (
            repository.private and github_repository_id not in self._access[login]
        ):
            raise GitHubNotFound(f"repository {github_repository_id} not found")
        return self._describe(repository)

    def list_installation_ids(self, user_token: str) -> set[int]:
        self._call("list_installation_ids")
        return {self._installations[r.owner] for r in self._reachable(user_token)}

    def get_installation_id(self, full_name: str) -> int:
        self._call("get_installation_id")
        return self._installation_for(full_name)[1]

    def resolve_commit(self, installation_id: int, full_name: str, branch: str) -> str:
        self._call("resolve_commit")
        repository = self._installed(installation_id, full_name)
        if not repository.branches:
            raise RepositoryEmpty(f"{full_name} has no branches")
        if branch not in repository.branches:
            raise BranchNotFound(f"{full_name} has no branch {branch!r}")
        return commit_sha(repository.id, repository.branches[branch])

    def open_tarball(
        self, installation_id: int, full_name: str, sha: str
    ) -> AbstractContextManager[IO[bytes]]:
        self._call("open_tarball")
        repository = self._installed(installation_id, full_name)
        builder = None if sha in self._dropped else repository.archive_builder(sha)
        if builder is None:
            raise GitHubNotFound(f"{full_name} has no commit {sha}")
        owner, name = repository.full_name.split("/", 1)
        return closing(io.BytesIO(builder(f"{owner}-{name}-{sha[:7]}")))

    def list_pull_requests(self, user_token: str, full_name: str, page: int) -> PullRequestPage:
        self._call("list_pull_requests")
        repository = self._visible(user_token, full_name)
        # Public pull requests are readable by every user; private ones need the permission.
        if repository.private and "pull_requests" not in self._granted(repository):
            raise GitHubAccessDenied(f"Resource not accessible by integration ({full_name})")
        pulls = sorted(
            (pull for pull in repository.pull_requests.values() if pull.state == "open"),
            key=lambda pull: pull.updated_at,
            reverse=True,
        )
        start = (max(page, 1) - 1) * PULL_REQUEST_PAGE_SIZE
        end = start + PULL_REQUEST_PAGE_SIZE
        return PullRequestPage(
            items=[self._describe_pull_request(repository, pull) for pull in pulls[start:end]],
            next_page=max(page, 1) + 1 if len(pulls) > end else None,
        )

    def get_pull_request(self, user_token: str, full_name: str, number: int) -> PullRequest:
        self._call("get_pull_request")
        repository = self._visible(user_token, full_name)
        pull = repository.pull_requests.get(number)
        if pull is None:
            raise GitHubNotFound(f"{full_name} has no pull request {number}")
        additions, deletions, changed_files = _diff_stats(
            pull.base_tree(), pull.tree(pull.revision), pull.renamed
        )
        return self._describe_pull_request(
            repository,
            pull,
            additions=additions,
            deletions=deletions,
            changed_files=changed_files,
        )

    def compare_commits(
        self,
        installation_id: int,
        full_name: str,
        base_sha: str,
        head_sha: str,
        *,
        head_owner: str | None,
    ) -> Comparison:
        """Every pull request branches from `initial`, its merge base with any base commit.

        Only pull request heads can be compared. `head_owner` is accepted and not needed: the
        fake serves a fork's head commits from the base repository, as GitHub keeps them.
        """
        self._call("compare_commits")
        repository = self._installed(installation_id, full_name)
        for sha in (base_sha, head_sha):
            if sha in self._dropped or repository.archive_builder(sha) is None:
                raise CommitUnavailable(f"{full_name} has no commit {sha}")
        found = repository.pull_request_commit(head_sha)
        if found is None:
            raise ValueError(f"the fake compares only pull request heads, not {head_sha}")
        pull, revision = found
        if pull.unrelated:
            raise NoCommonHistory(f"no common ancestor between {base_sha} and {head_sha}")
        _, _, changed_files = _diff_stats(pull.base_tree(), pull.tree(revision), pull.renamed)
        return Comparison(
            merge_base_sha=commit_sha(repository.id, "initial"),
            renamed=pull.renamed,
            listed_files=min(changed_files, _COMPARE_FILE_LIMIT),
        )

    def get_installation_permissions(self, full_name: str) -> InstallationPermissions:
        self._call("get_installation_permissions")
        repository, installation_id = self._installation_for(full_name)
        if repository.owner in _ORGANIZATIONS:
            settings_path = f"organizations/{repository.owner}/settings"
        else:
            settings_path = "settings"
        return InstallationPermissions(
            installation_id=installation_id,
            permissions=dict.fromkeys(sorted(self._granted(repository)), "read"),
            html_url=f"https://github.com/{settings_path}/installations/{installation_id}",
        )

    # Helpers -----------------------------------------------------------------------------

    def _call(self, name: str) -> None:
        """Count a call to GitHub; raise `GitHubUnavailable` while GitHub is unavailable."""
        self.calls[name] += 1
        if self._unavailable:
            raise GitHubUnavailable(f"GitHub is unavailable ({name})")

    def _login_for(self, user_token: str) -> str:
        login = user_token.removeprefix(ACCESS_PREFIX)
        if not user_token.startswith(ACCESS_PREFIX) or login not in _USERS:
            raise UserAuthorizationInvalid("invalid user token")
        if login in self._revoked:
            raise UserAuthorizationInvalid("the user token was revoked")
        return login

    def _allocate_installation_id(self) -> int:
        self._next_installation_id += 1
        return self._next_installation_id - 1

    def _reachable(self, user_token: str) -> list[_Repository]:
        """Repositories the user reaches through active installations of the App, by ID."""
        login = self._login_for(user_token)
        repositories = [self._repositories[i] for i in sorted(self._access[login])]
        return [r for r in repositories if self._covering(r) is not None]

    def _covering(self, repository: _Repository) -> int | None:
        """The ID of the active installation that covers the repository, if any."""
        installation_id = self._installations.get(repository.owner)
        if installation_id in self._suspended or repository.id in self._removed:
            return None
        return installation_id

    def _describe(self, repository: _Repository) -> GitHubRepository:
        return GitHubRepository(
            id=repository.id,
            full_name=repository.full_name,
            default_branch=repository.default_branch,
            private=repository.private,
            installation_id=self._installations.get(repository.owner),
        )

    def _by_name(self, full_name: str) -> _Repository:
        for repository in self._repositories.values():
            if repository.full_name.lower() == full_name.lower():
                return repository
        raise GitHubNotFound(f"repository {full_name} not found")

    def _installed(self, installation_id: int, full_name: str) -> _Repository:
        repository = self._by_name(full_name)
        if self._covering(repository) != installation_id:
            raise GitHubAccessDenied(f"installation {installation_id} cannot access {full_name}")
        return repository

    def _installation_for(self, full_name: str) -> tuple[_Repository, int]:
        """The repository and the installation that GitHub reports for it, even if suspended."""
        repository = self._by_name(full_name)
        if repository.owner not in self._installations or repository.id in self._removed:
            raise GitHubNotFound(f"the App is not installed on {full_name}")
        return repository, self._installations[repository.owner]

    def _granted(self, repository: _Repository) -> set[str]:
        """The permissions of the installation on the repository's owner."""
        installation_id = self._installations.get(repository.owner)
        return {name for name in _PERMISSIONS if (installation_id, name) not in self._withheld}

    def _visible(self, user_token: str, full_name: str) -> _Repository:
        """The repository, as `get_repository` shows it to the user: public ones to everyone,
        private ones only with access through the App.
        """
        login = self._login_for(user_token)
        repository = self._by_name(full_name)
        if repository.private and repository.id not in self._access[login]:
            raise GitHubNotFound(f"repository {full_name} not found")
        return repository

    def _set_pull_request_state(
        self, github_repository_id: int, number: int, state: PullRequestState
    ) -> None:
        repository = self._repositories[github_repository_id]
        pull = repository.pull_requests[number]
        pull.state = state
        repository.touch(pull)

    def _describe_pull_request(
        self,
        repository: _Repository,
        pull: _PullRequest,
        *,
        additions: int | None = None,
        deletions: int | None = None,
        changed_files: int | None = None,
    ) -> PullRequest:
        overlay = pull.overlay
        name = repository.full_name.split("/", 1)[1]
        head_owner = overlay.head_owner
        return PullRequest(
            number=overlay.number,
            title=overlay.title,
            body=pull.body,
            author=overlay.author,
            state=pull.state,
            draft=overlay.draft,
            base_ref=overlay.base_ref,
            base_sha=commit_sha(
                repository.id, repository.branches.get(overlay.base_ref, "initial")
            ),
            head_ref=overlay.head_ref,
            head_sha=commit_sha(repository.id, pull.commit_name(pull.revision)),
            head_repository=f"{head_owner}/{name}" if head_owner else repository.full_name,
            is_fork=head_owner is not None,
            html_url=f"https://github.com/{repository.full_name}/pull/{overlay.number}",
            updated_at=pull.updated_at,
            additions=additions,
            deletions=deletions,
            changed_files=changed_files,
        )


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
