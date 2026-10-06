"""Documentation hybrid search: English full text plus exact cosine similarity (research R9).

The keyword side is an OR query over the query's words, `to_tsquery('english', 'w1 | w2 ...')`,
ranked by `ts_rank_cd`. OR is used instead of `websearch_to_tsquery`, whose implicit AND would
require every word of a natural-language question to appear in one section; the rank still
favors sections that contain more of the words. The english configuration stems the words and
drops stop words. Both sides are filtered by snapshot in SQL and fused with reciprocal rank
fusion. Without query embeddings the result is keyword-only and marked `degraded`.
"""

import logging
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace

from sqlalchemy import Row, func, literal_column, select
from sqlalchemy.orm import Session

from codeatlas.models import DocChunk, File, Snapshot
from codeatlas.providers.embeddings import Embedder, get_embedder
from codeatlas.providers.errors import ProviderUnavailable

logger = logging.getLogger(__name__)

# Hits taken from each side before fusion; a larger `limit` widens both.
CANDIDATE_POOL = 20
DEFAULT_LIMIT = 10
# Distinct words in one full-text query.
MAX_QUERY_WORDS = 64

_WORD = re.compile(r"[^\W_]+")


@dataclass(frozen=True)
class DocHit:
    file_id: uuid.UUID
    path: str
    start_line: int
    end_line: int
    heading_path: str
    text: str
    score: float


@dataclass(frozen=True)
class DocSearchResult:
    hits: list[DocHit]
    degraded: bool


@dataclass(frozen=True)
class _Ranked:
    chunk_id: int
    hit: DocHit


def search_docs(
    db: Session,
    snapshot: Snapshot,
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    embedder: Embedder | None = None,
) -> DocSearchResult:
    """The top `limit` documentation chunks of `snapshot` for `query`, best first.

    `degraded` is True when the snapshot was indexed without embeddings or the query cannot be
    embedded; the hits are then keyword matches only. `score` is the fused RRF score.
    """
    pool = max(CANDIDATE_POOL, limit)
    keyword = _keyword_hits(db, snapshot.id, query, pool)
    if snapshot.coverage.get("embeddings_available") is False:
        return DocSearchResult(_fuse([keyword], limit), degraded=True)
    if snapshot.coverage.get("doc_chunks") == 0:
        return DocSearchResult([], degraded=False)
    try:
        vector = (embedder or get_embedder()).embed_query(query)
    except ProviderUnavailable:
        logger.warning("query embedding unavailable; documentation search uses keywords only")
        return DocSearchResult(_fuse([keyword], limit), degraded=True)
    semantic = _vector_hits(db, snapshot.id, vector, pool)
    return DocSearchResult(_fuse([keyword, semantic], limit), degraded=False)


def docs_query(query: str) -> str:
    """The `to_tsquery('english', ...)` OR query for `query`: its words (letters and digits
    only, so no tsquery operator survives), each quoted. Returns "" when there is no word."""
    words: list[str] = []
    for word in _WORD.findall(query.lower()):
        if word not in words:
            words.append(word)
        if len(words) == MAX_QUERY_WORDS:
            break
    return " | ".join(f"'{word}'" for word in words)


_COLUMNS = (
    DocChunk.id,
    DocChunk.file_id,
    File.path,
    DocChunk.start_line,
    DocChunk.end_line,
    DocChunk.heading_path,
    DocChunk.text,
)


def _ranked(rows: Iterable[Row[int, uuid.UUID, str, int, int, str, str]]) -> list[_Ranked]:
    return [
        _Ranked(chunk_id, DocHit(file_id, path, start, end, heading_path, text, 0.0))
        for chunk_id, file_id, path, start, end, heading_path, text in rows
    ]


def _keyword_hits(db: Session, snapshot_id: uuid.UUID, query: str, pool: int) -> list[_Ranked]:
    query_text = docs_query(query)
    if not query_text:
        return []
    tsquery = func.to_tsquery(literal_column("'english'::regconfig"), query_text)
    rank = func.ts_rank_cd(DocChunk.search_vector, tsquery)
    rows = db.execute(
        select(*_COLUMNS)
        .join(File, File.id == DocChunk.file_id)
        .where(DocChunk.snapshot_id == snapshot_id, DocChunk.search_vector.bool_op("@@")(tsquery))
        .order_by(rank.desc(), File.path, DocChunk.start_line)
        .limit(pool)
    )
    return _ranked(rows)


def _vector_hits(
    db: Session, snapshot_id: uuid.UUID, vector: list[float], pool: int
) -> list[_Ranked]:
    # No approximate index exists on `embedding`, so this is an exact nearest-neighbor scan.
    distance = DocChunk.embedding.cosine_distance(vector)
    rows = db.execute(
        select(*_COLUMNS)
        .join(File, File.id == DocChunk.file_id)
        .where(DocChunk.snapshot_id == snapshot_id, DocChunk.embedding.is_not(None))
        .order_by(distance, DocChunk.id)
        .limit(pool)
    )
    return _ranked(rows)


def _fuse(ranked_lists: list[list[_Ranked]], limit: int) -> list[DocHit]:
    """RRF over the lists by chunk, then drop hits overlapping a better hit in the same file."""
    # Imported here because evidence.py imports this module.
    from codeatlas.retrieval.evidence import reciprocal_rank_fusion_with_scores

    fused = reciprocal_rank_fusion_with_scores(ranked_lists, key=lambda item: item.chunk_id)
    hits: list[DocHit] = []
    for item, score in fused:
        hit = item.hit
        if any(
            kept.file_id == hit.file_id
            and kept.start_line <= hit.end_line
            and hit.start_line <= kept.end_line
            for kept in hits
        ):
            continue
        hits.append(replace(hit, score=score))
        if len(hits) == limit:
            break
    return hits
