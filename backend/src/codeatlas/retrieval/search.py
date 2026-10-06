"""Search within one snapshot by text, path, symbol, or documentation (FR-016 to FR-018).

Every query filters by snapshot in SQL and takes the user's text only as bound parameters.
`text` and `path` use the trigram indexes on `files.content` and `files.path` through `ILIKE`
with an escaped pattern, so `%` and `_` match themselves. `path` and `symbol` rank exact matches
first, then substring matches, then trigram similarity. `docs` delegates to `retrieval.docs`.
"""

import textwrap
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import ColumnElement, SQLColumnExpression, Text, case, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from codeatlas.models import File, Snapshot, Symbol
from codeatlas.retrieval.browse import split_lines
from codeatlas.retrieval.docs import search_docs

SearchMode = Literal["text", "path", "symbol", "docs"]

MAX_SNIPPET_CHARS = 300
MAX_SNIPPET_LINES = 3
# Characters read from the start of a file for a path match's snippet.
PATH_SNIPPET_SOURCE_CHARS = 2000
# Matching files fetched per round trip in `text` mode; content can be up to 1 MiB per file.
TEXT_FILES_PER_BATCH = 8
LIKE_ESCAPE = "\\"


@dataclass(frozen=True)
class SymbolRef:
    name: str
    kind: str


@dataclass(frozen=True)
class SearchHit:
    path: str
    start_line: int
    end_line: int
    snippet: str
    exact: bool
    symbol: SymbolRef | None = None


@dataclass(frozen=True)
class SearchResult:
    mode: SearchMode
    degraded: bool
    results: list[SearchHit]


def search(
    db: Session, snapshot: Snapshot, *, query: str, mode: SearchMode, limit: int
) -> SearchResult:
    """At most `limit` results from `snapshot`; `degraded` is only ever set in `docs` mode."""
    if mode == "text":
        return SearchResult(mode, False, _text_hits(db, snapshot.id, query, limit))
    if mode == "path":
        return SearchResult(mode, False, _path_hits(db, snapshot.id, query, limit))
    if mode == "symbol":
        return SearchResult(mode, False, _symbol_hits(db, snapshot.id, query, limit))
    docs = search_docs(db, snapshot, query, limit=limit)
    hits = [
        SearchHit(hit.path, hit.start_line, hit.end_line, snippet(hit.text), exact=False)
        for hit in docs.hits
    ]
    return SearchResult(mode, docs.degraded, hits)


def escape_like(value: str) -> str:
    """`value` as a literal inside a LIKE pattern that uses `LIKE_ESCAPE`."""
    for special in (LIKE_ESCAPE, "%", "_"):
        value = value.replace(special, LIKE_ESCAPE + special)
    return value


def snippet(text: str) -> str:
    """The first few non-blank lines of `text`, dedented, at most `MAX_SNIPPET_CHARS` long."""
    lines = [line.rstrip() for line in text.split("\n") if line.strip()][:MAX_SNIPPET_LINES]
    return _shorten(textwrap.dedent("\n".join(lines)).strip())


def line_snippet(line: str, position: int) -> str:
    """One line, trimmed; a long line is cut to a window around the match at `position`."""
    text = line.lstrip()
    position -= len(line) - len(text)
    text = text.rstrip()
    if len(text) <= MAX_SNIPPET_CHARS:
        return text
    start = max(0, min(position - MAX_SNIPPET_CHARS // 3, len(text) - MAX_SNIPPET_CHARS))
    end = start + MAX_SNIPPET_CHARS
    window = text[start:end]
    if start > 0:
        window = "…" + window[1:]
    if end < len(text):
        window = window[:-1] + "…"
    return window


def _shorten(text: str) -> str:
    if len(text) <= MAX_SNIPPET_CHARS:
        return text
    return text[: MAX_SNIPPET_CHARS - 1].rstrip() + "…"


def _contains(column: SQLColumnExpression[str], term: str) -> ColumnElement[bool]:
    return column.ilike(f"%{escape_like(term)}%", escape=LIKE_ESCAPE)


def _trigram_match(column: SQLColumnExpression[str], term: str) -> ColumnElement[bool]:
    """pg_trgm similarity (`%`) or word similarity (`<%`), both served by the trigram indexes."""
    return or_(column.op("%")(term), literal(term).op("<%")(column))


def _trigram_score(column: SQLColumnExpression[str], term: str) -> ColumnElement[float]:
    return func.greatest(func.similarity(column, term), func.word_similarity(term, column))


def _text_hits(db: Session, snapshot_id: uuid.UUID, query: str, limit: int) -> list[SearchHit]:
    """Each matching line, ordered by path and line number."""
    needle = query.lower()
    statement = (
        select(File.path, File.content)
        .where(File.snapshot_id == snapshot_id, _contains(File.content, query))
        .order_by(File.path)
        .execution_options(yield_per=TEXT_FILES_PER_BATCH)
    )
    hits: list[SearchHit] = []
    result = db.execute(statement)
    try:
        for path, content in result:
            for number, line in enumerate(split_lines(content), start=1):
                position = line.lower().find(needle)
                if position < 0:
                    continue
                hits.append(SearchHit(path, number, number, line_snippet(line, position), False))
                if len(hits) == limit:
                    return hits
    finally:
        result.close()
    return hits


def _path_hits(db: Session, snapshot_id: uuid.UUID, query: str, limit: int) -> list[SearchHit]:
    """Files whose path matches; the full path or the file name equal to the query is exact."""
    term = query.strip().lstrip("/")
    if not term:
        return []
    basename = func.regexp_replace(File.path, "^.*/", "")
    exact = or_(File.path == term, basename == term)
    substring = _contains(File.path, term)
    rows = db.execute(
        select(
            File.path,
            File.line_count,
            exact.label("exact"),
            func.left(File.content, PATH_SNIPPET_SOURCE_CHARS),
        )
        .where(
            File.snapshot_id == snapshot_id,
            or_(exact, substring, _trigram_match(File.path, term)),
        )
        .order_by(
            case((exact, 0), (substring, 1), else_=2),
            _trigram_score(File.path, term).desc(),
            func.length(File.path),
            File.path,
        )
        .limit(limit)
    ).all()
    return [
        SearchHit(path, 1, max(line_count, 1), snippet(head), bool(is_exact))
        for path, line_count, is_exact, head in rows
    ]


def _symbol_hits(db: Session, snapshot_id: uuid.UUID, query: str, limit: int) -> list[SearchHit]:
    """Declarations whose name or qualified name matches; an equal one is exact."""
    term = query.strip()
    if not term:
        return []
    exact = or_(Symbol.name == term, Symbol.qualified_name == term)
    substring = or_(_contains(Symbol.name, term), _contains(Symbol.qualified_name, term))
    tier = case((exact, 0), (substring, 1), else_=2)
    score = _trigram_score(Symbol.name, term)
    ranked = (
        select(
            Symbol.file_id,
            Symbol.name,
            Symbol.kind,
            Symbol.start_line,
            Symbol.end_line,
            File.path,
            tier.label("tier"),
            score.label("score"),
        )
        .join(File, File.id == Symbol.file_id)
        .where(
            Symbol.snapshot_id == snapshot_id,
            File.snapshot_id == snapshot_id,
            or_(exact, substring, _trigram_match(Symbol.name, term)),
        )
        .order_by(tier, score.desc(), func.length(Symbol.name), File.path, Symbol.start_line)
        .limit(limit)
        .subquery()
    )
    # Slice the declaration's first lines out of the file only for the ranked rows.
    lines = func.string_to_array(File.content, "\n", type_=ARRAY(Text))
    last = func.least(ranked.c.start_line + MAX_SNIPPET_LINES - 1, ranked.c.end_line)
    head = func.array_to_string(lines[ranked.c.start_line : last], "\n")
    rows = db.execute(
        select(
            ranked.c.path,
            ranked.c.start_line,
            ranked.c.end_line,
            ranked.c.name,
            ranked.c.kind,
            ranked.c.tier,
            head,
        )
        .join(File, File.id == ranked.c.file_id)
        .where(File.snapshot_id == snapshot_id)
        .order_by(
            ranked.c.tier,
            ranked.c.score.desc(),
            func.length(ranked.c.name),
            ranked.c.path,
            ranked.c.start_line,
        )
    ).all()
    return [
        SearchHit(path, start, end, snippet(text or ""), tier == 0, SymbolRef(name, kind))
        for path, start, end, name, kind, tier, text in rows
    ]
