"""Code candidates for a question: exact symbol and path matches, and code full-text search.

Research R8 and R10. Every query filters by snapshot in SQL. Question text only ever reaches the
database as bound parameters.
"""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import PurePosixPath
from typing import Literal

from sqlalchemy import ColumnElement, func, literal_column, or_, select
from sqlalchemy.orm import Session

from codeatlas.ingestion.chunking import split_identifiers
from codeatlas.models import CodeChunk, File, Symbol

SourceType = Literal["symbol", "code", "doc"]

FTS_LIMIT = 20
# Exact matches kept per question, best first; a common word can match many declarations.
MAX_EXACT_MATCHES = 20
# Rows read per exact-match query before scoring, so a very common name stays cheap.
MAX_EXACT_ROWS = 200
PATH_SPAN_LINES = 120
# Distinct terms in one full-text query.
MAX_QUERY_TERMS = 64

# English function words and question words. They are dropped from plain question words and from
# full-text terms, because the `simple` configuration used for code keeps every word.
STOP_WORDS = frozenset(
    """
    a about above after again against all also am an and any are aren as at be because been
    before being below between both but by can cannot could couldn did didn do does doesn doing
    don done down during each either else etc ever every few for from further get gets got had
    has have having he her here hers herself him himself his how however i if in into is isn it
    its itself just let like may me might more most much must my myself no nor not now of off
    on once only or other our ours out over own please same shall she should shouldn show so
    some such tell than that the their theirs them themselves then there these they this those
    through to too under until up upon us use used uses using very via was wasn we were weren
    what when where whether which while who whom whose why will with within without won would
    wouldn yes yet you your yours
    """.split()
)

# Language keywords that a question uses as plain words ("what does X return", "which
# function"). They occur in almost every chunk, so they are dropped from full-text terms only.
CODE_KEYWORDS = frozenset(
    """
    async await class const def elif export false function import lambda let new none null
    pass return self true var yield
    """.split()
)

# Runs of word characters, dots, slashes, and hyphens: words, identifiers, and paths.
_TOKEN = re.compile(r"[\w./-]+")
_LEADING_JUNK = re.compile(r"^(?:\.?/|-)+")
_CAMEL = re.compile(r"[a-z][A-Z]|[A-Z]{2}[a-z]")
_LEXEME = re.compile(r"[^\W_]+")
# Crude English suffixes. A stripped term becomes a prefix query, so `checked` also matches
# `check` and `checker`, and `permissions` matches `permission`.
_SUFFIXES = ("ing", "ies", "es", "ed", "s")
_MIN_STEM = 4


@dataclass(frozen=True)
class Candidate:
    """A line range proposed as evidence. `label_hint` is for logs and display, never the model."""

    source_type: SourceType
    file_id: uuid.UUID
    path: str
    start_line: int
    end_line: int
    score: float
    label_hint: str | None = None


def identifier_tokens(question: str) -> list[str]:
    """Return identifier-like tokens and meaningful plain words from `question`, in order.

    Identifier-like tokens (snake_case, camelCase, dotted or path-like words, and words with
    digits) are always kept. Plain words are kept when longer than 2 characters and not English
    stop words. Hyphens separate words outside path-like tokens. Tokens keep their original
    case; a token that repeats an earlier one ignoring case is dropped.
    """
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in _TOKEN.findall(question):
        token = _clean(raw)
        parts = [token] if _is_path_like(token) else [_clean(part) for part in token.split("-")]
        for part in parts:
            if not _LEXEME.search(part) or part.lower() in seen:
                continue
            if _is_identifier_like(part) or (
                len(part) > 2 and part.lower() not in STOP_WORDS and _LEXEME.fullmatch(part)
            ):
                seen.add(part.lower())
                tokens.append(part)
    return tokens


def symbol_and_path_candidates(
    db: Session, snapshot_id: uuid.UUID, question: str
) -> list[Candidate]:
    """Declarations and files whose name exactly matches a question token, best first.

    Symbols match on `name` or `qualified_name`. Files match on the whole path, a path suffix
    after a `/`, the basename, or the basename without its extension. Case-sensitive matches
    and identifier-like tokens score higher than case-insensitive matches and plain words.
    Symbol candidates span the declaration; path candidates span the file's first 120 lines.
    """
    tokens = identifier_tokens(question)
    if not tokens:
        return []
    lowered = sorted({token.lower() for token in tokens})
    best: dict[tuple[uuid.UUID, int, int], Candidate] = {}
    order: dict[tuple[uuid.UUID, int, int], tuple[int, str]] = {}

    def keep(candidate: Candidate, token_index: int) -> None:
        key = (candidate.file_id, candidate.start_line, candidate.end_line)
        if key not in best or candidate.score > best[key].score:
            best[key] = candidate
            order[key] = (token_index, candidate.path)

    symbol_rows = db.execute(
        select(
            Symbol.file_id,
            File.path,
            Symbol.name,
            Symbol.qualified_name,
            Symbol.start_line,
            Symbol.end_line,
        )
        .join(File, File.id == Symbol.file_id)
        .where(
            Symbol.snapshot_id == snapshot_id,
            or_(
                func.lower(Symbol.name).in_(lowered),
                func.lower(Symbol.qualified_name).in_(lowered),
            ),
        )
        .order_by(File.path, Symbol.start_line)
        .limit(MAX_EXACT_ROWS)
    )
    for file_id, path, name, qualified_name, start_line, end_line in symbol_rows:
        match = _best_match(tokens, partial(_symbol_forms, name, qualified_name))
        if match is not None:
            score, token_index = match
            keep(
                Candidate("symbol", file_id, path, start_line, end_line, score, qualified_name),
                token_index,
            )

    basename = func.regexp_replace(File.path, "^.*/", "")
    stem = func.regexp_replace(basename, r"^(.+)\.[^.]*$", r"\1")
    path_conditions: list[ColumnElement[bool]] = [
        func.lower(File.path).in_(lowered),
        func.lower(basename).in_(lowered),
        func.lower(stem).in_(lowered),
    ]
    for token in lowered:
        if "/" in token:
            suffix = "/" + token
            path_conditions.append(func.right(func.lower(File.path), len(suffix)) == suffix)
    file_rows = db.execute(
        select(File.id, File.path, File.language, File.line_count)
        .where(File.snapshot_id == snapshot_id, File.line_count > 0, or_(*path_conditions))
        .order_by(File.path)
        .limit(MAX_EXACT_ROWS)
    )
    for file_id, path, language, line_count in file_rows:
        match = _best_match(tokens, partial(_path_forms, path))
        if match is not None:
            score, token_index = match
            source: SourceType = "doc" if language == "markdown" else "code"
            end_line = min(line_count, PATH_SPAN_LINES)
            keep(Candidate(source, file_id, path, 1, end_line, score, path), token_index)

    ranked = sorted(
        best,
        key=lambda key: (-best[key].score, order[key], best[key].start_line),
    )
    return [best[key] for key in ranked[:MAX_EXACT_MATCHES]]


def code_fts_candidates(
    db: Session, snapshot_id: uuid.UUID, question: str, *, limit: int = FTS_LIMIT
) -> list[Candidate]:
    """The top `limit` code chunks for an OR query of the question's terms, by `ts_rank_cd`."""
    query_text = fts_query(question)
    if not query_text:
        return []
    query = func.to_tsquery(literal_column("'simple'::regconfig"), query_text)
    rank = func.ts_rank_cd(CodeChunk.search_vector, query)
    rows = db.execute(
        select(CodeChunk.file_id, File.path, CodeChunk.start_line, CodeChunk.end_line, rank)
        .join(File, File.id == CodeChunk.file_id)
        .where(CodeChunk.snapshot_id == snapshot_id, CodeChunk.search_vector.bool_op("@@")(query))
        .order_by(rank.desc(), File.path, CodeChunk.start_line)
        .limit(limit)
    )
    return [
        Candidate("code", file_id, path, start_line, end_line, float(score))
        for file_id, path, start_line, end_line, score in rows
    ]


def fts_query(question: str) -> str:
    """Build a `to_tsquery('simple', ...)` OR query from the question's identifier-split terms.

    Terms are letters or digits only (anything else is dropped), each one quoted. Stop words,
    language keywords, and single characters are dropped. A term with a common English suffix
    becomes a prefix query on its stem (`checked` gives `'check':*`). Returns "" when no term
    is left.
    """
    parts: list[str] = []
    seen: set[str] = set()
    for term in split_identifiers(question).split():
        if len(term) < 2 or term in STOP_WORDS or term in CODE_KEYWORDS:
            continue
        if not _LEXEME.fullmatch(term):
            continue
        stem = _strip_suffix(term)
        part = f"'{stem}':*" if stem != term else f"'{term}'"
        if part not in seen:
            seen.add(part)
            parts.append(part)
        if len(parts) == MAX_QUERY_TERMS:
            break
    return " | ".join(parts)


def _clean(token: str) -> str:
    """Drop sentence punctuation and a leading `./` or `/` around a token."""
    return _LEADING_JUNK.sub("", token).rstrip("./-")


def _is_path_like(token: str) -> bool:
    return "/" in token or "." in token


def _is_identifier_like(token: str) -> bool:
    return (
        _is_path_like(token)
        or "_" in token
        or bool(_CAMEL.search(token))
        or any(char.isdigit() for char in token)
    )


def _stem(name: str) -> str:
    """`access.py` gives `access`; `.gitattributes` and `Makefile` stay whole."""
    head, dot, _ = name.rpartition(".")
    return head if dot and head else name


def _symbol_forms(name: str, qualified_name: str, token: str) -> list[tuple[str, float]]:
    return [(name, 1.0), (qualified_name, 1.0)]


def _path_forms(path: str, token: str) -> list[tuple[str, float]]:
    """The parts of `path` a token may equal, with weights: the whole path, a suffix after a
    `/` (only for tokens containing `/`), the basename, and the basename without extension."""
    name = PurePosixPath(path).name
    forms = [(path, 1.0), (name, 0.9), (_stem(name), 0.8)]
    if "/" in token and path.lower().endswith("/" + token.lower()):
        forms.append((path[len(path) - len(token) :], 0.95))
    return forms


def _best_match(
    tokens: list[str], forms_for: Callable[[str], list[tuple[str, float]]]
) -> tuple[float, int] | None:
    """The best (score, token index) over tokens equal to one of their weighted forms.

    A case-insensitive match scores 0.8 of a case-sensitive one, and a plain word 0.5 of an
    identifier-like token.
    """
    best: tuple[float, int] | None = None
    for index, token in enumerate(tokens):
        for form, weight in forms_for(token):
            if form == token:
                score = weight
            elif form.lower() == token.lower():
                score = weight * 0.8
            else:
                continue
            if not _is_identifier_like(token):
                score *= 0.5
            if best is None or score > best[0]:
                best = (score, index)
    return best


def _strip_suffix(term: str) -> str:
    """Strip one common suffix. The stem stays a prefix of the term, so its prefix query still
    matches the term itself (`repositories` gives `repositor`, matching `repository` too)."""
    if term.endswith("ss"):
        return term
    for suffix in _SUFFIXES:
        if term.endswith(suffix) and len(term) - len(suffix) >= _MIN_STEM:
            return term[: -len(suffix)]
    return term
