"""Evidence assembly helpers (research R10): fusion, span caps, merging, budget, labels."""

import hashlib
import uuid
from collections.abc import Sequence

import pytest

from codeatlas.retrieval.code import Candidate, SourceType
from codeatlas.retrieval.evidence import (
    CHARS_PER_TOKEN,
    EVIDENCE_TOKEN_BUDGET,
    MAX_EVIDENCE_ITEMS,
    MAX_ITEM_TOKENS,
    MAX_SYMBOL_LINES,
    RRF_K,
    build_evidence,
    cap_symbol_span,
    estimate_tokens,
    file_lines,
    fuse_candidates,
    merge_overlapping,
    plan_ranges,
    reciprocal_rank_fusion,
    reciprocal_rank_fusion_with_scores,
)

COMMIT = "a" * 40
FILE_A = uuid.UUID(int=1)
FILE_B = uuid.UUID(int=2)


def candidate(
    start: int,
    end: int,
    *,
    source_type: SourceType = "code",
    file_id: uuid.UUID = FILE_A,
    path: str | None = None,
) -> Candidate:
    path = path if path is not None else f"file-{file_id.int}.py"
    return Candidate(source_type, file_id, path, start, end, 1.0)


def ranges(candidates: Sequence[Candidate]) -> list[tuple[int, int, int]]:
    return [(c.file_id.int, c.start_line, c.end_line) for c in candidates]


def numbered(count: int, width: int = 10) -> list[str]:
    return [f"{number:0{width}d}" for number in range(1, count + 1)]


# Reciprocal rank fusion


def test_constants_match_research() -> None:
    assert (RRF_K, MAX_SYMBOL_LINES, MAX_EVIDENCE_ITEMS) == (60, 120, 12)
    assert (EVIDENCE_TOKEN_BUDGET, CHARS_PER_TOKEN) == (16_000, 3.5)


def test_rrf_sums_reciprocal_ranks_with_k_60() -> None:
    fused = reciprocal_rank_fusion_with_scores([["a", "b", "c"], ["c", "a"]], key=str)

    assert [item for item, _ in fused] == ["a", "c", "b"]
    scores = dict(fused)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert scores["b"] == pytest.approx(1 / 62)


def test_rrf_item_in_both_lists_beats_top_of_one() -> None:
    assert reciprocal_rank_fusion([["x", "both"], ["y", "both"]], key=str)[0] == "both"


def test_rrf_ties_keep_list_order() -> None:
    assert reciprocal_rank_fusion([["a1", "a2"], ["b1", "b2"]], key=str) == [
        "a1",
        "b1",
        "a2",
        "b2",
    ]
    assert reciprocal_rank_fusion([["b1"], ["a1"]], key=str) == ["b1", "a1"]


def test_rrf_uses_key_and_ignores_repeats_within_a_list() -> None:
    fused = reciprocal_rank_fusion_with_scores(
        [[("x", 1), ("x", 2), ("y", 3)]], key=lambda item: item[0]
    )
    assert fused == [(("x", 1), pytest.approx(1 / 61)), (("y", 3), pytest.approx(1 / 63))]


def test_rrf_k_changes_scores() -> None:
    fused = reciprocal_rank_fusion_with_scores([["a"]], key=str, k=0)
    assert fused == [("a", 1.0)]


def test_symbol_matches_come_first() -> None:
    symbol = candidate(30, 40, source_type="symbol")
    path_match = candidate(1, 120, file_id=FILE_B)
    code = [candidate(1, 60), candidate(51, 110)]
    docs = [candidate(5, 9, source_type="doc", file_id=FILE_B)]

    fused = fuse_candidates([[path_match, symbol], code, docs])

    # Without the symbol-first rule, the three rank-1 items would lead.
    assert fused[0] == symbol
    assert fused[1:] == [path_match, code[0], docs[0], code[1]]


def test_identical_ranges_fuse_into_the_first_listed_item() -> None:
    symbol = candidate(1, 60, source_type="symbol")
    chunk = candidate(1, 60)
    fused = fuse_candidates([[symbol], [chunk]])
    assert fused == [symbol]


# Symbol spans and merging


def test_symbol_span_is_capped_at_120_lines() -> None:
    long_symbol = candidate(10, 400, source_type="symbol")
    capped = cap_symbol_span(long_symbol)
    assert (capped.start_line, capped.end_line) == (10, 129)
    assert capped.end_line - capped.start_line + 1 == MAX_SYMBOL_LINES


def test_short_symbols_and_other_sources_are_not_capped() -> None:
    short = candidate(10, 129, source_type="symbol")
    chunk = candidate(1, 500)
    assert cap_symbol_span(short) == short
    assert cap_symbol_span(chunk) == chunk


def test_overlapping_ranges_in_one_file_merge_at_the_better_rank() -> None:
    first = candidate(51, 110)
    other_file = candidate(1, 60, file_id=FILE_B)
    second = candidate(1, 60)

    merged = merge_overlapping([first, other_file, second])

    assert ranges(merged) == [(1, 1, 110), (2, 1, 60)]
    assert merged[0].source_type == first.source_type


def test_adjacent_ranges_merge_and_separate_ones_do_not() -> None:
    assert ranges(merge_overlapping([candidate(1, 10), candidate(11, 20)])) == [(1, 1, 20)]
    assert ranges(merge_overlapping([candidate(1, 10), candidate(12, 20)])) == [
        (1, 1, 10),
        (1, 12, 20),
    ]


def test_a_merged_range_absorbs_every_range_it_reaches() -> None:
    symbol = candidate(1, 10, source_type="symbol")
    middle_file = candidate(5, 6, file_id=FILE_B)
    later = candidate(30, 40)
    bridge = candidate(8, 32)

    merged = merge_overlapping([symbol, middle_file, later, bridge])

    assert ranges(merged) == [(1, 1, 40), (2, 5, 6)]
    assert merged[0].source_type == "symbol"


def test_plan_ranges_fuses_caps_and_merges() -> None:
    symbol = candidate(1, 300, source_type="symbol")
    chunk = candidate(101, 160)
    planned = plan_ranges([[symbol], [chunk]])
    assert ranges(planned) == [(1, 1, 160)]


# Excerpts, budget, labels


def test_file_lines_match_the_indexing_line_numbers() -> None:
    assert file_lines("a\nb\n") == ["a", "b"]
    assert file_lines("a\nb") == ["a", "b"]
    assert file_lines("a\r\n\nb\n") == ["a\r", "", "b"]
    assert file_lines("") == []


def test_labels_ranks_and_checksums_of_exact_excerpts() -> None:
    lines = {FILE_A: numbered(50), FILE_B: ["def f():", "    return 1", ""]}
    planned = [
        candidate(3, 5, source_type="symbol"),
        candidate(1, 3, file_id=FILE_B, path="b.py"),
        candidate(40, 60),
    ]

    evidence = build_evidence(planned, lines.__getitem__, COMMIT)

    assert [item.label for item in evidence] == ["E1", "E2", "E3"]
    assert [item.rank for item in evidence] == [1, 2, 3]
    assert [(item.path, item.start_line, item.end_line) for item in evidence] == [
        ("file-1.py", 3, 5),
        ("b.py", 1, 3),
        ("file-1.py", 40, 50),  # clamped to the file's last line
    ]
    assert evidence[0].source_type == "symbol"
    assert evidence[1].excerpt == "def f():\n    return 1\n"
    for item in evidence:
        file_id = FILE_B if item.path == "b.py" else FILE_A
        expected = "\n".join(lines[file_id][item.start_line - 1 : item.end_line])
        assert item.excerpt == expected
        assert item.excerpt_sha256 == hashlib.sha256(expected.encode("utf-8")).digest()
        assert item.commit_sha == COMMIT


def test_at_most_12_items() -> None:
    lines = {FILE_A: numbered(100)}
    planned = [candidate(line, line) for line in range(1, 31, 2)]

    evidence = build_evidence(planned, lines.__getitem__, COMMIT)

    assert len(evidence) == MAX_EVIDENCE_ITEMS
    assert evidence[-1].label == "E12"


def test_budget_stops_near_16000_estimated_tokens() -> None:
    # Each 100-line item is 100 * 69 + 99 = 6,999 characters, about 2,000 tokens.
    width = 69
    files = [uuid.UUID(int=index) for index in range(1, 11)]
    lines = {file_id: numbered(100, width) for file_id in files}
    planned = [candidate(1, 100, file_id=file_id) for file_id in files]

    evidence = build_evidence(planned, lines.__getitem__, COMMIT)

    total = sum(estimate_tokens(item.excerpt) for item in evidence)
    assert len(evidence) == 8
    assert total <= EVIDENCE_TOKEN_BUDGET
    assert total + estimate_tokens(numbered(1, width)[0]) > EVIDENCE_TOKEN_BUDGET


def test_item_reaching_the_budget_is_cut_to_whole_lines_and_ends_the_list() -> None:
    big = {FILE_A: ["x" * 99] * 600, FILE_B: ["short"]}
    planned = [candidate(1, 600), candidate(1, 1, file_id=FILE_B)]

    evidence = build_evidence(planned, big.__getitem__, COMMIT, token_budget=1000)

    # 1,000 tokens are 3,500 characters: 35 lines of 99 characters plus 34 line breaks.
    assert [(item.start_line, item.end_line) for item in evidence] == [(1, 35)]
    assert estimate_tokens(evidence[0].excerpt) <= 1000


def test_item_whose_first_line_exceeds_the_budget_is_skipped() -> None:
    lines = {FILE_A: ["y" * 60_000], FILE_B: ["small"]}
    planned = [candidate(1, 1), candidate(1, 1, file_id=FILE_B, path="b.py")]

    evidence = build_evidence(planned, lines.__getitem__, COMMIT)

    assert [(item.label, item.path) for item in evidence] == [("E1", "b.py")]


def test_ranges_outside_the_file_are_skipped() -> None:
    lines = {FILE_A: numbered(5)}
    evidence = build_evidence([candidate(9, 12), candidate(2, 2)], lines.__getitem__, COMMIT)
    assert [(item.label, item.start_line) for item in evidence] == [("E1", 2)]


def test_one_huge_item_cannot_crowd_out_the_rest() -> None:
    # A file with very long lines (like a large SVG) is cut to the per-item cap, not the budget.
    huge = ["x" * 9_000 for _ in range(60)]
    lines = {FILE_A: huge, FILE_B: ["def handler():", "    return 1"]}
    planned = [candidate(1, 60), candidate(1, 2, file_id=FILE_B, path="b.py")]

    evidence = build_evidence(planned, lines.__getitem__, COMMIT)

    assert [item.path for item in evidence] == ["file-1.py", "b.py"]
    assert estimate_tokens(evidence[0].excerpt) <= MAX_ITEM_TOKENS
    assert evidence[0].end_line < 60
