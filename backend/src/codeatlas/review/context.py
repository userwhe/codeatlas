"""Related code and tests found by name, without a dependency graph (research R5).

Deterministic and in memory, over the head tree:

1. Changed declarations: 001's parser runs on the reviewed files at both sides. A declaration is
   changed when its line range holds an added line (head side) or a removed line (merge-base
   side), so removed and renamed declarations come from the merge base.
2. Module names: each reviewed file's stem, plus its import path.
3. Related code: unchanged eligible files at the head that contain one of those names as a whole
   word, ranked by how many distinct names they contain, then by path. Each gives one excerpt
   around its first two matches.
4. Candidate tests: unchanged test files at the head that contain one of those names, or that are
   named after a changed file. The top 5 give excerpts like related code.
5. Changed tests: every changed file whose path is a test's, reviewed or not.

Test files are never related code, so one file never gives two excerpts. Everything here is a
candidate found by name, not a confirmed caller, and CodeAtlas runs no tests.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import islice
from pathlib import PurePosixPath

from codeatlas.ingestion.filters import LANGUAGES
from codeatlas.ingestion.parse import Declaration, ParseLanguage, parse_declarations
from codeatlas.retrieval.evidence import file_lines
from codeatlas.review.diff import ChangedFile, Tree, is_test_path
from codeatlas.review.schema import ChangeKind

MIN_NAME_CHARS = 4
STOP_NAMES = frozenset({"main", "init", "__init__", "get", "set", "run", "test"})
MAX_NAMES = 30
MAX_RELATED_FILES = 8
MAX_CANDIDATE_TESTS = 10
MAX_TEST_EXCERPTS = 5
EXCERPT_CONTEXT_LINES = 6
EXCERPT_MATCHES = 2
MAX_EXCERPT_LINES = 40
_WORD = re.compile(r"\w+")
# The subject a test file is named after, by language family: `test_access.py` and
# `access_test.py` test `access.py`; `format.test.ts` and `format.spec.tsx` test `format.ts` or
# `format.tsx`.
_TEST_SUBJECTS = (
    ("python", re.compile(r"^test_(.+)\.py$")),
    ("python", re.compile(r"^(.+)_test\.py$")),
    ("typescript", re.compile(r"^(.+)\.(?:test|spec)\.tsx?$")),
)
_FAMILIES = {".py": "python", ".ts": "typescript", ".tsx": "typescript"}
_PARSED: dict[str, ParseLanguage] = {
    "python": "python",
    "typescript": "typescript",
    "tsx": "tsx",
}


@dataclass(frozen=True)
class CodeExcerpt:
    """Exact lines of one head file, and the names that made it a candidate."""

    path: str
    start_line: int  # 1-based, inclusive
    end_line: int
    excerpt: str
    names: tuple[str, ...]


@dataclass(frozen=True)
class CandidateTest:
    """An unchanged test file that may exercise the change, and why it was chosen. Only the top
    candidates carry an excerpt."""

    path: str
    reason: str  # "refers to `name`" or "named after `path`"
    excerpt: CodeExcerpt | None


@dataclass(frozen=True)
class ChangedTest:
    path: str
    change: ChangeKind


def changed_declarations(files: Iterable[ChangedFile]) -> list[str]:
    """Names of the declarations that the change touches, in file and source order.

    Names shorter than 4 characters and common names such as `main` or `__init__` are ignored,
    and at most 30 names are returned.
    """
    names: list[str] = []
    for file in files:
        language = _PARSED.get(file.language or "")
        if language is None:
            continue
        added = {line for hunk in file.hunks for line in hunk.added_lines}
        removed = {line for hunk in file.hunks for line in hunk.removed_lines}
        if file.after is not None and added:
            names.extend(_touched(parse_declarations(file.after, language).declarations, added))
        if file.before is not None and removed:
            names.extend(_touched(parse_declarations(file.before, language).declarations, removed))
    return _useful(names)[:MAX_NAMES]


def module_names(files: Iterable[ChangedFile]) -> list[str]:
    """Each Python or TypeScript file's stem and import path, at both of a rename's paths.

    `app/auth/permissions.py` gives `permissions` and `app.auth.permissions`; a package's
    `__init__.py` gives the package path. `web/src/format.ts` gives `format` and
    `web/src/format`, which import specifiers end with. Short and common names are ignored, as
    for declarations.
    """
    names: list[str] = []
    for file in files:
        for path in (file.path, file.previous_path):
            if path is None:
                continue
            pure = PurePosixPath(path)
            language = LANGUAGES.get(pure.suffix.lower())
            if language == "python":
                parts = pure.with_suffix("").parts
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                names.extend([pure.stem, ".".join(parts)])
            elif language in ("typescript", "tsx"):
                names.extend([pure.stem, str(pure.with_suffix(""))])
    return _useful(names)


def related_code(
    head: Tree,
    files: Iterable[ChangedFile],
    names: Sequence[str],
    modules: Sequence[str],
    *,
    max_files: int = MAX_RELATED_FILES,
) -> list[CodeExcerpt]:
    """Excerpts of the head files that mention a changed name, best first.

    `files` are every changed file, reviewed or not; none of them is related code. Matches are
    whole words. Files are ranked by the number of distinct names they contain, then by path,
    and the top `max_files` each give one excerpt: from 6 lines before the first match to 6
    lines after the second, at most 40 lines.
    """
    matcher = _Matcher([*names, *modules])
    changed = _changed_paths(files)
    candidates: list[tuple[str, tuple[str, ...]]] = []
    for path, file in head.files.items():
        if path in changed or is_test_path(path):
            continue
        found = matcher.found(file.content)
        if found:
            candidates.append((path, found))
    candidates.sort(key=lambda candidate: (-len(candidate[1]), candidate[0]))

    excerpts: list[CodeExcerpt] = []
    for path, found in candidates[:max_files]:
        excerpt = matcher.excerpt(path, head.files[path].content, found)
        if excerpt is not None:
            excerpts.append(excerpt)
    return excerpts


def candidate_tests(
    head: Tree,
    files: Iterable[ChangedFile],
    names: Sequence[str],
    modules: Sequence[str],
    *,
    max_tests: int = MAX_CANDIDATE_TESTS,
) -> list[CandidateTest]:
    """Unchanged test files at the head that may exercise the change, best first.

    A test file is a candidate when it contains a changed name as a whole word (reason "refers to
    `name`", naming the first one found, declarations before modules), or when it is named after
    a changed file (reason "named after `path`"). References come first, ranked by the number of
    distinct names, then by path; then files named after a changed file, by path. At most
    `max_tests` are returned, and the top 5 carry an excerpt: around their first two matches as
    for related code, or the file's first lines when it contains no name.
    """
    matcher = _Matcher([*names, *modules])
    files = list(files)
    changed = _changed_paths(files)
    subjects = _test_subjects(files)

    references: list[tuple[str, tuple[str, ...]]] = []
    named: list[tuple[str, str]] = []
    for path, file in head.files.items():
        if path in changed or not is_test_path(path):
            continue
        found = matcher.found(file.content)
        if found:
            references.append((path, found))
            continue
        subject = _subject(path, subjects)
        if subject is not None:
            named.append((path, subject))
    references.sort(key=lambda reference: (-len(reference[1]), reference[0]))
    named.sort()

    ranked = [
        *((path, f"refers to `{found[0]}`", found) for path, found in references),
        *((path, f"named after `{subject}`", ()) for path, subject in named),
    ]
    candidates: list[CandidateTest] = []
    for rank, (path, reason, found) in enumerate(ranked[:max_tests]):
        excerpt = None
        if rank < MAX_TEST_EXCERPTS:
            content = head.files[path].content
            excerpt = matcher.excerpt(path, content, found) if found else _opening(path, content)
        candidates.append(CandidateTest(path=path, reason=reason, excerpt=excerpt))
    return candidates


def changed_tests(files: Iterable[ChangedFile]) -> list[ChangedTest]:
    """Every changed file whose path is a test's, reviewed or not, with its change (FR-011).

    An excluded directory is not a file, so it is never listed.
    """
    return [
        ChangedTest(path=file.path, change=file.change)
        for file in files
        if file.entry_type == "file" and is_test_path(file.path)
    ]


class _Matcher:
    """Whole-word matching of a list of names, in their order, without repeats."""

    def __init__(self, terms: Iterable[str]):
        self.terms = list(dict.fromkeys(terms))
        self.patterns = {
            term: re.compile(rf"(?<!\w){re.escape(term)}(?!\w)") for term in self.terms
        }
        # A file can only contain a name as a whole word if it contains each word of the name, so
        # one pass over a file's words rules out most names before any pattern runs.
        self.parts = {term: frozenset(_WORD.findall(term)) for term in self.terms}

    def found(self, content: str) -> tuple[str, ...]:
        if not self.terms:
            return ()
        words = set(_WORD.findall(content))
        return tuple(
            term
            for term in self.terms
            if self.parts[term] <= words and self.patterns[term].search(content)
        )

    def excerpt(self, path: str, content: str, found: Sequence[str]) -> CodeExcerpt | None:
        """From 6 lines before the first match to 6 lines after the second, at most 40 lines."""
        lines = file_lines(content)
        matching = (
            number
            for number, line in enumerate(lines, start=1)
            if any(self.patterns[term].search(line) for term in found)
        )
        matches = list(islice(matching, EXCERPT_MATCHES))
        if not matches:  # names hold no line breaks, so a found name is always on one line
            return None
        start = max(1, matches[0] - EXCERPT_CONTEXT_LINES)
        end = min(len(lines), matches[-1] + EXCERPT_CONTEXT_LINES, start + MAX_EXCERPT_LINES - 1)
        return _lines_excerpt(path, lines, start, end, tuple(found))


def _opening(path: str, content: str) -> CodeExcerpt | None:
    """The first lines of a file, as many as an excerpt around one match on line 1 would hold."""
    lines = file_lines(content)
    if not lines:
        return None
    end = min(len(lines), 1 + 2 * EXCERPT_CONTEXT_LINES)
    return _lines_excerpt(path, lines, 1, end, ())


def _lines_excerpt(
    path: str, lines: Sequence[str], start: int, end: int, names: tuple[str, ...]
) -> CodeExcerpt:
    return CodeExcerpt(
        path=path,
        start_line=start,
        end_line=end,
        excerpt="\n".join(lines[start - 1 : end]),
        names=names,
    )


def _changed_paths(files: Iterable[ChangedFile]) -> set[str]:
    return {path for file in files for path in (file.path, file.previous_path) if path}


def _test_subjects(files: Iterable[ChangedFile]) -> dict[tuple[str, str], str]:
    """(language family, stem) to the changed file with that stem, at either path of a rename.

    The first file in path order wins, so the reason names one file.
    """
    subjects: dict[tuple[str, str], str] = {}
    for file in files:
        if file.entry_type != "file" or is_test_path(file.path):
            continue
        for path in (file.path, file.previous_path):
            if path is None:
                continue
            pure = PurePosixPath(path)
            family = _FAMILIES.get(pure.suffix.lower())
            if family is not None:
                subjects.setdefault((family, pure.stem), path)
    return subjects


def _subject(test_path: str, subjects: dict[tuple[str, str], str]) -> str | None:
    """The changed file that a test file is named after, if any."""
    name = test_path.rsplit("/", 1)[-1]
    for family, pattern in _TEST_SUBJECTS:
        match = pattern.match(name)
        if match is not None and (family, match.group(1)) in subjects:
            return subjects[(family, match.group(1))]
    return None


def _touched(declarations: Sequence[Declaration], lines: set[int]) -> list[str]:
    return [
        declaration.name
        for declaration in declarations
        if any(declaration.start_line <= line <= declaration.end_line for line in lines)
    ]


def _useful(names: Iterable[str]) -> list[str]:
    """Distinct names in first-seen order, without short or common ones."""
    return [
        name
        for name in dict.fromkeys(names)
        if len(name) >= MIN_NAME_CHARS and name.lower() not in STOP_NAMES
    ]
