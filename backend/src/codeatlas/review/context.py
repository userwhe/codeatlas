"""Related code found by name, without a dependency graph (research R5).

Deterministic and in memory, over the head tree:

1. Changed declarations: 001's parser runs on the reviewed files at both sides. A declaration is
   changed when its line range holds an added line (head side) or a removed line (merge-base
   side), so removed and renamed declarations come from the merge base.
2. Module names: each reviewed file's stem, plus its import path.
3. Related code: unchanged eligible files at the head that contain one of those names as a whole
   word, ranked by how many distinct names they contain, then by path. Each gives one excerpt
   around its first two matches.

Test files are left to candidate tests, which are built separately, so one file never gives two
excerpts. Everything here is a candidate found by name, not a confirmed caller.
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

MIN_NAME_CHARS = 4
STOP_NAMES = frozenset({"main", "init", "__init__", "get", "set", "run", "test"})
MAX_NAMES = 30
MAX_RELATED_FILES = 8
EXCERPT_CONTEXT_LINES = 6
EXCERPT_MATCHES = 2
MAX_EXCERPT_LINES = 40
_WORD = re.compile(r"\w+")
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
    terms = list(dict.fromkeys([*names, *modules]))
    if not terms:
        return []
    patterns = {term: re.compile(rf"(?<!\w){re.escape(term)}(?!\w)") for term in terms}
    # A file can only contain a name as a whole word if it contains each word of the name, so
    # one pass over a file's words rules out most names before any pattern runs.
    parts = {term: frozenset(_WORD.findall(term)) for term in terms}
    changed = {path for file in files for path in (file.path, file.previous_path) if path}

    candidates: list[tuple[str, tuple[str, ...]]] = []
    for path, file in head.files.items():
        if path in changed or is_test_path(path):
            continue
        words = set(_WORD.findall(file.content))
        found = tuple(
            term for term in terms if parts[term] <= words and patterns[term].search(file.content)
        )
        if found:
            candidates.append((path, found))
    candidates.sort(key=lambda candidate: (-len(candidate[1]), candidate[0]))

    excerpts: list[CodeExcerpt] = []
    for path, found in candidates[:max_files]:
        lines = file_lines(head.files[path].content)
        matching = (
            number
            for number, line in enumerate(lines, start=1)
            if any(patterns[term].search(line) for term in found)
        )
        matches = list(islice(matching, EXCERPT_MATCHES))
        if not matches:  # names hold no line breaks, so a found name is always on one line
            continue
        start = max(1, matches[0] - EXCERPT_CONTEXT_LINES)
        end = min(len(lines), matches[-1] + EXCERPT_CONTEXT_LINES, start + MAX_EXCERPT_LINES - 1)
        excerpts.append(
            CodeExcerpt(
                path=path,
                start_line=start,
                end_line=end,
                excerpt="\n".join(lines[start - 1 : end]),
                names=found,
            )
        )
    return excerpts


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
