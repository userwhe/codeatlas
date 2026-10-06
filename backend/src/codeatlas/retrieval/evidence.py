"""Evidence assembly for a question (research R10): a bounded, deterministic pipeline.

1. Collect candidates: exact symbol and path matches, the top code chunks by full-text rank, and
   the top documentation chunks by hybrid rank.
2. Fuse them with reciprocal rank fusion, symbol matches first.
3. Cap symbol spans at 120 lines, then merge overlapping or adjacent ranges in the same file.
4. Read the exact lines from the snapshot and take items in rank order until about 16,000
   estimated tokens (3.5 characters per token) or 12 items.
5. Label the items `E1` to `En` with a SHA-256 checksum of each excerpt.

Steps 2 to 5 are pure functions, so they can be tested without a database.
"""

import hashlib
import uuid
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.models import File, Snapshot
from codeatlas.providers.embeddings import Embedder
from codeatlas.retrieval.code import (
    Candidate,
    SourceType,
    code_fts_candidates,
    symbol_and_path_candidates,
)
from codeatlas.retrieval.docs import DocHit, search_docs

CHARS_PER_TOKEN = 3.5
EVIDENCE_TOKEN_BUDGET = 16_000
MAX_EVIDENCE_ITEMS = 12
MAX_ITEM_TOKENS = 4_000
MAX_SYMBOL_LINES = 120
RRF_K = 60
DOC_CANDIDATES = 10


@dataclass(frozen=True)
class Evidence:
    label: str
    source_type: SourceType
    path: str
    commit_sha: str
    start_line: int
    end_line: int
    excerpt: str
    excerpt_sha256: bytes
    rank: int


def reciprocal_rank_fusion_with_scores[T](
    ranked_lists: Sequence[Sequence[T]], *, key: Callable[[T], Hashable], k: int = RRF_K
) -> list[tuple[T, float]]:
    """Fuse ranked lists by summing `1 / (k + rank)` per item, with ranks starting at 1.

    Items with equal keys are one item, represented by its first occurrence; a repeat within
    one list is ignored. Ties keep the order of first occurrence, scanning the lists in order,
    so earlier lists win ties.
    """
    first: dict[Hashable, T] = {}
    scores: dict[Hashable, float] = {}
    for ranked in ranked_lists:
        seen: set[Hashable] = set()
        for rank, item in enumerate(ranked, start=1):
            item_key = key(item)
            if item_key in seen:
                continue
            seen.add(item_key)
            first.setdefault(item_key, item)
            scores[item_key] = scores.get(item_key, 0.0) + 1.0 / (k + rank)
    ordered = sorted(first, key=lambda item_key: -scores[item_key])  # stable
    return [(first[item_key], scores[item_key]) for item_key in ordered]


def reciprocal_rank_fusion[T](
    ranked_lists: Sequence[Sequence[T]], *, key: Callable[[T], Hashable], k: int = RRF_K
) -> list[T]:
    """The items of `reciprocal_rank_fusion_with_scores`, best first."""
    return [item for item, _ in reciprocal_rank_fusion_with_scores(ranked_lists, key=key, k=k)]


def fuse_candidates(ranked_lists: Sequence[Sequence[Candidate]]) -> list[Candidate]:
    """RRF over the candidate lists, keyed by file and line range, then symbol matches first.

    Pass the symbol-and-path list first so its items also win ties. Symbol candidates move
    ahead of all others, each group keeping its fused order.
    """
    fused = reciprocal_rank_fusion(
        ranked_lists,
        key=lambda candidate: (candidate.file_id, candidate.start_line, candidate.end_line),
    )
    return sorted(fused, key=lambda candidate: candidate.source_type != "symbol")


def cap_symbol_span(candidate: Candidate, max_lines: int = MAX_SYMBOL_LINES) -> Candidate:
    """Limit a symbol candidate to the first `max_lines` lines of its declaration."""
    if candidate.source_type != "symbol":
        return candidate
    end_line = min(candidate.end_line, candidate.start_line + max_lines - 1)
    return replace(candidate, end_line=end_line)


def merge_overlapping(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Merge candidates whose ranges overlap or touch in the same file, in rank order.

    A merged item covers the union of the ranges and keeps the position and other fields of its
    best-ranked part.
    """
    kept: list[Candidate] = []
    for candidate in candidates:
        start, end = candidate.start_line, candidate.end_line
        absorbed: set[int] = set()
        grew = True
        while grew:  # a merged range can reach items that the new one alone did not
            grew = False
            for index, item in enumerate(kept):
                if (
                    index not in absorbed
                    and item.file_id == candidate.file_id
                    and item.start_line <= end + 1
                    and start <= item.end_line + 1
                ):
                    absorbed.add(index)
                    start, end = min(start, item.start_line), max(end, item.end_line)
                    grew = True
        if not absorbed:
            kept.append(candidate)
            continue
        best = min(absorbed)
        merged = replace(kept[best], start_line=start, end_line=end)
        kept = [
            merged if index == best else item
            for index, item in enumerate(kept)
            if index == best or index not in absorbed
        ]
    return kept


def plan_ranges(ranked_lists: Sequence[Sequence[Candidate]]) -> list[Candidate]:
    """Fuse, cap symbol spans, and merge: the ranges to read, best first."""
    return merge_overlapping([cap_symbol_span(c) for c in fuse_candidates(ranked_lists)])


def file_lines(content: str) -> list[str]:
    """Split file content into lines numbered from 1, as the indexing pipeline does.

    Lines split on `\\n` only; a final line break does not start another line.
    """
    lines = content.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def estimate_tokens(text: str) -> float:
    return len(text) / CHARS_PER_TOKEN


def build_evidence(
    ranges: Sequence[Candidate],
    lines_of: Callable[[uuid.UUID], Sequence[str]],
    commit_sha: str,
    *,
    token_budget: int = EVIDENCE_TOKEN_BUDGET,
    max_items: int = MAX_EVIDENCE_ITEMS,
    max_item_tokens: int = MAX_ITEM_TOKENS,
) -> list[Evidence]:
    """Read each range's exact lines and label them, in rank order, within the budget.

    Items are taken until `max_items` or until the estimated tokens of the excerpts would
    exceed `token_budget`. Each item is also cut to whole lines within `max_item_tokens`, so one
    huge file (for example a large SVG or data file) cannot crowd out the rest. The item that
    reaches the overall budget is cut to the whole lines that still fit and ends the list; an
    item whose first line alone does not fit is skipped. `lines_of` returns a file's lines by
    file ID.
    """
    evidence: list[Evidence] = []
    used = 0.0
    for candidate in ranges:
        if len(evidence) == max_items:
            break
        lines = lines_of(candidate.file_id)
        end = min(candidate.end_line, len(lines))
        if candidate.start_line < 1 or candidate.start_line > end:
            continue
        selected = list(lines[candidate.start_line - 1 : end])
        remaining = token_budget - used
        count = _lines_within(selected, min(remaining, max_item_tokens))
        if count == 0:
            continue
        excerpt = "\n".join(selected[:count])
        used += estimate_tokens(excerpt)
        rank = len(evidence) + 1
        evidence.append(
            Evidence(
                label=f"E{rank}",
                source_type=candidate.source_type,
                path=candidate.path,
                commit_sha=commit_sha,
                start_line=candidate.start_line,
                end_line=candidate.start_line + count - 1,
                excerpt=excerpt,
                excerpt_sha256=hashlib.sha256(excerpt.encode()).digest(),
                rank=rank,
            )
        )
        if count < len(selected) and remaining <= max_item_tokens:
            break
    return evidence


def doc_candidate(hit: DocHit) -> Candidate:
    return Candidate(
        "doc", hit.file_id, hit.path, hit.start_line, hit.end_line, hit.score, hit.heading_path
    )


def assemble_evidence(
    db: Session, snapshot: Snapshot, question: str, *, embedder: Embedder | None = None
) -> tuple[list[Evidence], bool]:
    """Build the labeled evidence for `question` from `snapshot`.

    Returns the evidence and whether documentation search ran in reduced (keyword-only) mode.
    """
    symbols = symbol_and_path_candidates(db, snapshot.id, question)
    code = code_fts_candidates(db, snapshot.id, question)
    docs = search_docs(db, snapshot, question, limit=DOC_CANDIDATES, embedder=embedder)
    ranges = plan_ranges([symbols, code, [doc_candidate(hit) for hit in docs.hits]])

    cache: dict[uuid.UUID, list[str]] = {}

    def lines_of(file_id: uuid.UUID) -> list[str]:
        if file_id not in cache:
            content = db.scalar(
                select(File.content).where(File.snapshot_id == snapshot.id, File.id == file_id)
            )
            cache[file_id] = file_lines(content or "")
        return cache[file_id]

    return build_evidence(ranges, lines_of, snapshot.commit_sha), docs.degraded


def _lines_within(lines: Sequence[str], token_budget: float) -> int:
    """How many leading lines, joined with `\\n`, fit within `token_budget` estimated tokens."""
    chars = 0
    for count, line in enumerate(lines, start=1):
        chars += len(line) + (1 if count > 1 else 0)
        if chars / CHARS_PER_TOKEN > token_budget:
            return count - 1
    return len(lines)
