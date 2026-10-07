"""Review evidence: labels, sides, and the input budget (research R6).

Labels run `E1` to `En` in this order:

1. Change items, by reviewed file (in review order), then by hunk. A hunk that adds lines gives
   an `after` item over its head range, at the head commit, and a hunk that removes lines gives
   a `before` item over its merge-base range, at the merge base. The after item comes first.
2. Related code (`reference` items, side `after`), best first.
3. Candidate test excerpts (`test` items, side `after`), best first.

Every excerpt is the exact lines of its side, with a SHA-256 checksum. The diff already fits
the diff budget, so change items are never dropped. Related-code and test items fill the rest of
`max_input_tokens`; when they do not fit, test items go first, then related code, lowest rank
first. 80 hunks give at most 160 change items, so with 8 related-code and 5 test items a review
stays within 200 labels.
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from codeatlas.retrieval.evidence import estimate_tokens, file_lines
from codeatlas.review.context import CodeExcerpt
from codeatlas.review.diff import Selection

MAX_LABELS = 200

ReviewSourceType = Literal["change", "reference", "test"]
Side = Literal["before", "after"]


@dataclass(frozen=True)
class ReviewEvidence:
    label: str
    source_type: ReviewSourceType
    side: Side
    path: str  # the path on that side: a renamed file's before item has the old path
    commit_sha: str
    start_line: int  # 1-based, inclusive
    end_line: int
    excerpt: str
    excerpt_sha256: bytes
    rank: int


@dataclass(frozen=True)
class HunkLabels:
    """The labels of one hunk's sides; a side the hunk does not change has none."""

    before: str | None
    after: str | None


@dataclass(frozen=True)
class ReviewEvidenceSet:
    """The labeled items, and each hunk's labels by (reviewed file path, hunk index)."""

    items: tuple[ReviewEvidence, ...]
    hunk_labels: Mapping[tuple[str, int], HunkLabels]

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(item.label for item in self.items)

    @property
    def change_labels(self) -> frozenset[str]:
        return frozenset(item.label for item in self.items if item.source_type == "change")

    @property
    def context_items(self) -> int:
        """Related-code and test excerpts: what was examined beyond the diff."""
        return sum(1 for item in self.items if item.source_type != "change")


@dataclass(frozen=True)
class _Draft:
    source_type: ReviewSourceType
    side: Side
    path: str
    commit_sha: str
    start_line: int
    end_line: int
    excerpt: str


def build_evidence(
    selection: Selection,
    related: Sequence[CodeExcerpt],
    *,
    head_sha: str,
    merge_base_sha: str,
    max_input_tokens: float,
    tests: Sequence[CodeExcerpt] = (),
) -> ReviewEvidenceSet:
    """Label the change hunks, then the related code and candidate tests that fit.

    `tests` are candidate test excerpts (research R5), labeled after related code and dropped
    before it.
    """
    # Change items are never dropped, so their labels are final as soon as they are added.
    drafts: list[_Draft] = []
    hunk_labels: dict[tuple[str, int], HunkLabels] = {}
    for file in selection.reviewed:
        before = file_lines(file.before or "")
        after = file_lines(file.after or "")
        for index, hunk in enumerate(file.hunks):
            after_label = before_label = None
            if hunk.added_lines and hunk.after_start and hunk.after_end:
                after_label = f"E{len(drafts) + 1}"
                drafts.append(
                    _change("after", file.path, head_sha, after, hunk.after_start, hunk.after_end)
                )
            if hunk.removed_lines and hunk.before_start and hunk.before_end:
                before_label = f"E{len(drafts) + 1}"
                drafts.append(
                    _change(
                        "before",
                        file.before_path,
                        merge_base_sha,
                        before,
                        hunk.before_start,
                        hunk.before_end,
                    )
                )
            hunk_labels[(file.path, index)] = HunkLabels(before=before_label, after=after_label)

    context = [
        *(_excerpt_draft("reference", item, head_sha) for item in related),
        *(_excerpt_draft("test", item, head_sha) for item in tests),
    ]
    budget = max_input_tokens - selection.diff_tokens
    used = sum(estimate_tokens(draft.excerpt) for draft in context)
    while context and (used > budget or len(drafts) + len(context) > MAX_LABELS):
        used -= estimate_tokens(context.pop().excerpt)  # the lowest-ranked item
    drafts.extend(context)

    items = tuple(
        ReviewEvidence(
            label=f"E{rank}",
            source_type=draft.source_type,
            side=draft.side,
            path=draft.path,
            commit_sha=draft.commit_sha,
            start_line=draft.start_line,
            end_line=draft.end_line,
            excerpt=draft.excerpt,
            excerpt_sha256=hashlib.sha256(draft.excerpt.encode()).digest(),
            rank=rank,
        )
        for rank, draft in enumerate(drafts, start=1)
    )
    return ReviewEvidenceSet(items=items, hunk_labels=hunk_labels)


def _change(
    side: Side, path: str, commit_sha: str, lines: Sequence[str], start: int, end: int
) -> _Draft:
    excerpt = "\n".join(lines[start - 1 : end])
    return _Draft("change", side, path, commit_sha, start, end, excerpt)


def _excerpt_draft(source_type: ReviewSourceType, item: CodeExcerpt, head_sha: str) -> _Draft:
    return _Draft(
        source_type, "after", item.path, head_sha, item.start_line, item.end_line, item.excerpt
    )
