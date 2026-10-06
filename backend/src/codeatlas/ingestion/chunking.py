"""Code and Markdown chunking, identifier splitting, and the index version (research R8, R9).

Lines are split on `\\n` only and numbered from 1, matching tree-sitter rows and editors. A
final line break does not start another line, and a trailing `\\r` stays part of its line.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import version
from itertools import accumulate, pairwise

from codeatlas.config import Settings

CHUNK_WINDOW = 60
CHUNK_OVERLAP = 10
DOC_MAX_CHARS = 1600
# Bump when declaration extraction (`parse.py`), identifier splitting, or chunk boundary rules
# change, so that existing snapshots are rebuilt.
PARSER_RULES_VERSION = 1

_PARSER_PACKAGES = ("tree-sitter", "tree-sitter-python", "tree-sitter-typescript")

_WORD = re.compile(r"\w+")
# Subwords of one word: a run of A-Z not followed by a lowercase letter (`HTTP` in
# `HTTPServer`), an optionally capitalized lowercase run, or a digit run. Case boundaries are
# ASCII only: every letter other than A-Z counts as lowercase, so non-ASCII words stay whole.
_SUBWORD = re.compile(r"[A-Z]+(?![^\W\d_A-Z])|[A-Z]?[^\W\d_A-Z]+|\d+")

_FENCE_OPEN = re.compile(r" {0,3}(`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(r" {0,3}(`{3,}|~{3,})[ \t]*")
_HEADING = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?")


@dataclass(frozen=True)
class CodeChunkSpec:
    start_line: int
    end_line: int
    search_text: str


@dataclass(frozen=True)
class DocChunkSpec:
    start_line: int
    end_line: int
    heading_path: str
    text: str


def split_identifiers(text: str) -> str:
    """Return lowercase, space-separated terms for the `simple` full-text configuration.

    Each word (a run of letters, digits, and underscores) contributes, in source order, its
    subwords split on underscores, camelCase and PascalCase boundaries, and digit runs, followed
    by the subwords joined together when there is more than one. Everything else separates
    words. For example `getUserById` gives `get user by id getuserbyid`, `HTTPServer2` gives
    `http server 2 httpserver2`, and `user_id` gives `user id userid`.
    """
    terms: list[str] = []
    for word in _WORD.findall(text):
        parts = [part.lower() for part in _SUBWORD.findall(word)]
        terms.extend(parts)
        if len(parts) > 1:
            terms.append("".join(parts))
    return " ".join(terms)


def code_chunks(
    content: str, *, window: int = CHUNK_WINDOW, overlap: int = CHUNK_OVERLAP
) -> list[CodeChunkSpec]:
    """Split code into `window`-line chunks that overlap by `overlap` lines.

    Windows start at lines 1, 51, 101, ... with the defaults. The last window ends at the last
    line, and no window is emitted once one has reached it.
    """
    if not 0 <= overlap < window:
        raise ValueError("overlap must be at least 0 and smaller than window")
    lines = _split_lines(content)
    chunks: list[CodeChunkSpec] = []
    start = 1
    while start <= len(lines):
        end = min(start + window - 1, len(lines))
        text = "\n".join(lines[start - 1 : end])
        chunks.append(CodeChunkSpec(start, end, split_identifiers(text)))
        if end == len(lines):
            break
        start += window - overlap
    return chunks


def markdown_chunks(content: str, *, max_chars: int = DOC_MAX_CHARS) -> list[DocChunkSpec]:
    """Split Markdown into heading sections of at most about `max_chars` characters.

    Sections start at ATX headings (`#` to `######`) outside fenced code blocks. A section
    longer than `max_chars` is split between paragraphs (blank lines outside fences), and a
    paragraph longer than `max_chars` is cut between lines, so only a single line longer than
    `max_chars` yields a longer chunk. Each chunk's `text` is exactly its lines, without
    leading or trailing blank lines. Text before the first heading has an empty heading path.
    Sections with no text besides their heading are skipped.
    """
    lines = _split_lines(content)
    in_fence = [False] * len(lines)
    # (first line index, first body line index, heading path)
    sections: list[tuple[int, int, str]] = [(0, 0, "")]
    headings: list[tuple[int, str]] = []  # (level, title) of the enclosing headings
    fence = ""
    for index, line in enumerate(lines):
        stripped = line.rstrip()
        if fence:
            in_fence[index] = True
            close = _FENCE_CLOSE.fullmatch(stripped)
            if close and close[1][0] == fence[0] and len(close[1]) >= len(fence):
                fence = ""
        elif opening := _FENCE_OPEN.match(stripped):
            in_fence[index] = True
            fence = opening[1]
        elif heading := _HEADING.fullmatch(stripped):
            level = len(heading[1])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, (heading[2] or "").strip()))
            path = " > ".join(title for _, title in headings if title)
            sections.append((index, index + 1, path))
    sections.append((len(lines), len(lines), ""))

    # Prefix sums give the length of `"\n".join(lines[first : last + 1])` in constant time.
    offsets = [0, *accumulate(len(line) for line in lines)]

    def length(first: int, last: int) -> int:
        return offsets[last + 1] - offsets[first] + (last - first)

    chunks: list[DocChunkSpec] = []
    for (first, body, path), (stop, _, _) in pairwise(sections):
        if not any(lines[index].strip() for index in range(body, stop)):
            continue
        for start, end in _pack(_paragraphs(lines, in_fence, first, stop), length, max_chars):
            text = "\n".join(lines[start : end + 1])
            if text.strip():  # a cut piece of a fenced block can hold only blank lines
                chunks.append(DocChunkSpec(start + 1, end + 1, path, text))
    return chunks


def index_version(settings: Settings) -> str:
    """Return a short hash of everything that shapes a snapshot's index.

    It covers the parser package versions, the chunking parameters, the extraction rules
    version, and the embedding model and dimension, so a change to any of them makes the next
    indexing job build a new snapshot instead of reusing a ready one.
    """
    parts = [f"{package}={version(package)}" for package in _PARSER_PACKAGES]
    parts += [
        f"chunk_window={CHUNK_WINDOW}",
        f"chunk_overlap={CHUNK_OVERLAP}",
        f"doc_max_chars={DOC_MAX_CHARS}",
        f"parser_rules={PARSER_RULES_VERSION}",
        f"embedding={settings.embedding_model}:{settings.embedding_dimensions}",
    ]
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
    return f"idx-{digest[:12]}"


def _split_lines(content: str) -> list[str]:
    lines = content.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def _paragraphs(
    lines: list[str], in_fence: list[bool], first: int, stop: int
) -> list[tuple[int, int]]:
    """Inclusive line index ranges of the paragraphs in `lines[first:stop]`.

    Paragraphs are separated by blank lines outside fenced code blocks.
    """
    paragraphs: list[tuple[int, int]] = []
    start: int | None = None
    for index in range(first, stop):
        separator = not lines[index].strip() and not in_fence[index]
        if separator and start is not None:
            paragraphs.append((start, index - 1))
            start = None
        elif not separator and start is None:
            start = index
    if start is not None:
        paragraphs.append((start, stop - 1))
    return paragraphs


def _pack(
    paragraphs: list[tuple[int, int]], length: Callable[[int, int], int], max_chars: int
) -> list[tuple[int, int]]:
    """Greedily merge consecutive paragraphs into line ranges of at most `max_chars` characters.

    A paragraph longer than `max_chars` is cut between lines instead.
    """
    ranges: list[tuple[int, int]] = []
    current: tuple[int, int] | None = None
    for start, end in paragraphs:
        if current is not None and length(current[0], end) <= max_chars:
            current = (current[0], end)
            continue
        if current is not None:
            ranges.append(current)
            current = None
        if length(start, end) <= max_chars:
            current = (start, end)
            continue
        piece = start
        for index in range(start + 1, end + 1):
            if length(piece, index) > max_chars:
                ranges.append((piece, index - 1))
                piece = index
        ranges.append((piece, end))
    if current is not None:
        ranges.append(current)
    return ranges
