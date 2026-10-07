"""Changed files, diff hunks, coverage, and selection under the review limits (research R4).

A review reads two commit archives with 001's extraction and filters: the head, then the merge
base. Changed paths come from comparing member hashes, so a change to a file that is never kept,
such as a credential file, is still detected. Each changed path takes the head's filter outcome,
or the merge base's for a removed file. Eligible text files get hunks with 3 lines of context.

Selection takes whole files, source and configuration first, then tests, then documentation,
while the totals stay within the review limits. Every changed file ends with one coverage entry.

Nothing here touches the database or the network.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import IO, Literal

from codeatlas.config import Settings
from codeatlas.ingestion.extract import ArchiveMember, RejectedMember, iter_archive
from codeatlas.ingestion.filters import (
    EXCLUDED_DIRECTORIES,
    EligibleFile,
    Language,
    SkippedEntry,
    SkipReason,
    filter_members,
)
from codeatlas.retrieval.evidence import CHARS_PER_TOKEN, file_lines
from codeatlas.review.schema import ChangeKind

CONTEXT_LINES = 3
ROOT_ATTRIBUTES = ".gitattributes"
TEST_DIRECTORIES = frozenset({"tests", "test", "__tests__"})
TEST_NAME_PATTERNS = (
    "test_*.py",
    "*_test.py",
    "*.test.ts",
    "*.test.tsx",
    "*.spec.ts",
    "*.spec.tsx",
)
DOCUMENTATION_SUFFIXES = frozenset({".md", ".mdx", ".txt"})
# Identical empty files are not paired as renames: an empty `__init__.py` removed in one place
# and added in another is not a move.
_EMPTY_SHA256 = bytes.fromhex("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")

CoverageReason = SkipReason | Literal["review_limit"]
EntryType = Literal["file", "directory"]
Skip = Callable[[str, bytes], bool]


@dataclass(frozen=True)
class Tree:
    """One commit's members after 001's filters.

    `hashes` holds the SHA-256 of every regular member, including members dropped by `skip`; a
    link or unsafe path, which is never read, maps to None. `files` holds the eligible files and
    `skipped` every other filter outcome, both by path. An excluded directory is one entry under
    the directory's path, as in 001's coverage.
    """

    hashes: Mapping[str, bytes | None]
    files: Mapping[str, EligibleFile]
    skipped: Mapping[str, SkippedEntry]


@dataclass(frozen=True)
class Hunk:
    """One diff hunk with up to 3 lines of context on each side.

    Ranges are 1-based and inclusive. A side with no lines in the hunk, such as the merge-base
    side of an added file, has no range. `lines` are unified diff lines: a space, `-`, or `+`,
    then the line's text. `added_lines` are head line numbers, `removed_lines` merge-base ones.
    """

    before_start: int | None
    before_end: int | None
    after_start: int | None
    after_end: int | None
    lines: tuple[str, ...]
    added_lines: tuple[int, ...]
    removed_lines: tuple[int, ...]

    @property
    def additions(self) -> int:
        return len(self.added_lines)

    @property
    def deletions(self) -> int:
        return len(self.removed_lines)


@dataclass(frozen=True)
class ChangedFile:
    """A changed path, or the changed files under one excluded directory (`count` of them).

    `reason` is None when the file is eligible text, and otherwise the filter's reason. Only an
    eligible file has `language`, its text on each side that has it (`before` at the merge base,
    `after` at the head), and hunks. A file renamed without changes has no hunks.
    """

    path: str
    previous_path: str | None
    change: ChangeKind
    entry_type: EntryType = "file"
    count: int = 1
    reason: SkipReason | None = None
    language: Language | None = None
    before: str | None = None
    after: str | None = None
    hunks: tuple[Hunk, ...] = ()

    @property
    def reviewable(self) -> bool:
        return self.entry_type == "file" and self.reason is None

    @property
    def before_path(self) -> str:
        """The path at the merge base."""
        return self.previous_path or self.path

    @property
    def additions(self) -> int | None:
        """Lines added, or None when the file's lines were not compared."""
        return sum(hunk.additions for hunk in self.hunks) if self.reviewable else None

    @property
    def deletions(self) -> int | None:
        return sum(hunk.deletions for hunk in self.hunks) if self.reviewable else None


@dataclass(frozen=True)
class CoverageEntry:
    """What happened to one changed file. Field order is the stored JSON order."""

    path: str
    previous_path: str | None
    change: ChangeKind
    entry_type: EntryType
    count: int
    additions: int | None
    deletions: int | None
    reviewed: bool
    reason: CoverageReason | None


@dataclass(frozen=True)
class Selection:
    """The files to review, in review order, and a coverage entry per changed file, by path."""

    reviewed: tuple[ChangedFile, ...]
    coverage: tuple[CoverageEntry, ...]

    @property
    def partial(self) -> bool:
        return any(entry.reason == "review_limit" for entry in self.coverage)

    @property
    def changed_lines_reviewed(self) -> int:
        return sum(_changed_lines(file) for file in self.reviewed)

    @property
    def diff_tokens(self) -> float:
        return sum(diff_tokens(file) for file in self.reviewed)


def read_tree(stream: IO[bytes], settings: Settings, *, skip: Skip | None = None) -> Tree:
    """Read and filter one commit archive.

    `skip(path, sha256)` returning True drops a member before `filter_members`, which keeps
    only the changed files of the merge base in memory. The root `.gitattributes` is never
    dropped, because its `linguist-generated` rules classify the files that remain. Raises
    `LimitExceeded` over 001's extraction or snapshot limits.
    """
    hashes: dict[str, bytes | None] = {}

    def members() -> Iterator[ArchiveMember | RejectedMember]:
        for member in iter_archive(stream, settings):
            if isinstance(member, RejectedMember):
                hashes.setdefault(member.path, None)
            else:
                hashes[member.path] = member.sha256
                if (
                    skip is not None
                    and member.path != ROOT_ATTRIBUTES
                    and skip(member.path, member.sha256)
                ):
                    continue
            yield member

    result = filter_members(members(), settings)
    return Tree(
        hashes=hashes,
        files={file.path: file for file in result.files},
        skipped={entry.path: entry for entry in result.skipped},
    )


def is_test_path(path: str) -> bool:
    """Whether `path` is a test: under `tests/`, `test/`, or `__tests__/`, or named like one."""
    *directories, name = path.split("/")
    if any(directory in TEST_DIRECTORIES for directory in directories):
        return True
    return any(fnmatchcase(name, pattern) for pattern in TEST_NAME_PATTERNS)


def changed_files(head: Tree, base: Tree, rename_hints: Mapping[str, str]) -> list[ChangedFile]:
    """Every path that differs between the merge base and the head, by path.

    `rename_hints` maps a new path to its old path, as GitHub's comparison reports renames. A
    hint counts only when the new path was added and the old one removed. Remaining added and
    removed paths with identical content are paired as renames too. Changes under an excluded
    directory become one entry per directory.
    """
    added = sorted(head.hashes.keys() - base.hashes.keys())
    removed = sorted(base.hashes.keys() - head.hashes.keys())
    modified = sorted(
        path
        for path in head.hashes.keys() & base.hashes.keys()
        if _differs(head.hashes[path], base.hashes[path])
    )
    renames = _renames(added, removed, head, base, rename_hints)
    previous = set(renames.values())

    changes: list[tuple[str, str | None, ChangeKind]] = [
        (path, None, "modified") for path in modified
    ]
    for path in added:
        if path in renames:
            changes.append((path, renames[path], "renamed"))
        else:
            changes.append((path, None, "added"))
    changes.extend((path, None, "removed") for path in removed if path not in previous)

    entries: list[ChangedFile] = []
    directories: dict[str, list[ChangeKind]] = {}
    for path, previous_path, change in changes:
        tree = base if change == "removed" else head
        directory = _excluded_directory(tree, path)
        if directory is not None:
            directories.setdefault(directory, []).append(change)
        else:
            entries.append(_changed_file(head, base, path, previous_path, change))
    for directory, kinds in directories.items():
        entries.append(
            ChangedFile(
                path=directory,
                previous_path=None,
                change=kinds[0] if len(set(kinds)) == 1 else "modified",
                entry_type="directory",
                count=len(kinds),
                reason="excluded_directory",
            )
        )
    return sorted(entries, key=lambda entry: entry.path)


def diff_tokens(file: ChangedFile) -> float:
    """Estimated tokens of a file's diff text, at 3.5 characters per token (001 research R10)."""
    chars = sum(len(line) + 1 for hunk in file.hunks for line in hunk.lines)
    return chars / CHARS_PER_TOKEN


def select_for_review(
    files: Sequence[ChangedFile],
    *,
    max_files: int,
    max_changed_lines: int,
    max_hunks: int,
    max_diff_tokens: int,
) -> Selection:
    """Take whole reviewable files, in review order, while every total stays within its limit.

    A file that does not fit gets the reason `review_limit`, and later, smaller files may still
    fit. A file renamed without changes has no lines or hunks, so it always fits while a file
    slot is left.
    """
    candidates = sorted(
        (file for file in files if file.reviewable), key=lambda file: (_group(file.path), file.path)
    )
    reviewed: list[ChangedFile] = []
    lines = hunks = 0
    tokens = 0.0
    for file in candidates:
        if (
            len(reviewed) < max_files
            and lines + _changed_lines(file) <= max_changed_lines
            and hunks + len(file.hunks) <= max_hunks
            and tokens + diff_tokens(file) <= max_diff_tokens
        ):
            reviewed.append(file)
            lines += _changed_lines(file)
            hunks += len(file.hunks)
            tokens += diff_tokens(file)

    taken = {file.path for file in reviewed}
    coverage = tuple(
        CoverageEntry(
            path=file.path,
            previous_path=file.previous_path,
            change=file.change,
            entry_type=file.entry_type,
            count=file.count,
            additions=file.additions,
            deletions=file.deletions,
            reviewed=file.path in taken,
            reason=None if file.path in taken else (file.reason or "review_limit"),
        )
        for file in sorted(files, key=lambda file: file.path)
    )
    return Selection(reviewed=tuple(reviewed), coverage=coverage)


def _differs(head: bytes | None, base: bytes | None) -> bool:
    # A link or unsafe path at both commits has no content to compare; it counts as unchanged.
    if head is None and base is None:
        return False
    return head != base


def _renames(
    added: Sequence[str],
    removed: Sequence[str],
    head: Tree,
    base: Tree,
    hints: Mapping[str, str],
) -> dict[str, str]:
    """New path to old path: valid hints first, then identical content in path order."""
    added_paths, removed_paths = set(added), set(removed)
    renames: dict[str, str] = {}
    used: set[str] = set()
    for new, old in sorted(hints.items()):
        if new in added_paths and old in removed_paths and old not in used:
            renames[new] = old
            used.add(old)

    by_hash: dict[bytes, list[str]] = {}
    for old in removed:
        digest = base.hashes[old]
        if old not in used and digest is not None and digest != _EMPTY_SHA256:
            by_hash.setdefault(digest, []).append(old)
    for new in added:
        digest = head.hashes[new]
        if new in renames or digest is None:
            continue
        candidates = by_hash.get(digest)
        if candidates:
            renames[new] = candidates.pop(0)
    return renames


def _excluded_directory(tree: Tree, path: str) -> str | None:
    """The outermost excluded directory holding `path`, unless the filters gave it its own entry.

    A link or unsafe path is rejected before the directory rule runs, so it keeps its own reason.
    """
    if path in tree.files or path in tree.skipped:
        return None
    parts = path.split("/")
    for index, part in enumerate(parts[:-1]):
        if part in EXCLUDED_DIRECTORIES:
            return "/".join(parts[: index + 1])
    return None


def _changed_file(
    head: Tree, base: Tree, path: str, previous_path: str | None, change: ChangeKind
) -> ChangedFile:
    """Classify one changed file and diff it when it is eligible.

    The outcome is the head's, or the merge base's for a removed file. When the other side has no
    eligible text (it is absent, or was binary or too large there), the diff runs against no
    lines on that side.
    """
    old_path = previous_path or path
    eligible = base.files.get(old_path) if change == "removed" else head.files.get(path)
    if eligible is None:
        tree, outcome_path = (base, old_path) if change == "removed" else (head, path)
        return ChangedFile(
            path=path,
            previous_path=previous_path,
            change=change,
            reason=tree.skipped[outcome_path].reason,
        )
    before_file = None if change == "added" else base.files.get(old_path)
    after_file = None if change == "removed" else head.files.get(path)
    before = before_file.content if before_file is not None else None
    after = after_file.content if after_file is not None else None
    return ChangedFile(
        path=path,
        previous_path=previous_path,
        change=change,
        language=eligible.language,
        before=before,
        after=after,
        hunks=_hunks(file_lines(before or ""), file_lines(after or "")),
    )


def _hunks(before: Sequence[str], after: Sequence[str]) -> tuple[Hunk, ...]:
    hunks: list[Hunk] = []
    matcher = SequenceMatcher(None, before, after)
    for group in matcher.get_grouped_opcodes(CONTEXT_LINES):
        lines: list[str] = []
        added: list[int] = []
        removed: list[int] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines.extend(f" {line}" for line in before[i1:i2])
                continue
            if tag in ("replace", "delete"):
                lines.extend(f"-{line}" for line in before[i1:i2])
                removed.extend(range(i1 + 1, i2 + 1))
            if tag in ("replace", "insert"):
                lines.extend(f"+{line}" for line in after[j1:j2])
                added.extend(range(j1 + 1, j2 + 1))
        i_start, i_end = group[0][1], group[-1][2]
        j_start, j_end = group[0][3], group[-1][4]
        hunks.append(
            Hunk(
                before_start=i_start + 1 if i_end > i_start else None,
                before_end=i_end if i_end > i_start else None,
                after_start=j_start + 1 if j_end > j_start else None,
                after_end=j_end if j_end > j_start else None,
                lines=tuple(lines),
                added_lines=tuple(added),
                removed_lines=tuple(removed),
            )
        )
    return tuple(hunks)


def _group(path: str) -> int:
    """Review order: source and configuration (0), tests (1), then documentation (2)."""
    if PurePosixPath(path).suffix.lower() in DOCUMENTATION_SUFFIXES:
        return 2
    return 1 if is_test_path(path) else 0


def _changed_lines(file: ChangedFile) -> int:
    return (file.additions or 0) + (file.deletions or 0)
