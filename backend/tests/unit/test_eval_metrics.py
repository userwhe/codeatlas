"""Pure metric and set-validation functions of the answer-quality evaluation (no database)."""

import json
from pathlib import Path
from typing import Any

import pytest

from evals.run_qa_eval import (
    CitationCheck,
    ClaimRecord,
    LineRange,
    Question,
    QuestionResult,
    Rate,
    audit_rows,
    citation_problem,
    eval_branch,
    load_questions,
    meets,
    parse_question,
    ranges_overlap,
    recall_at_k,
    select_questions,
    summarize,
)

SHA = "a" * 40
SHIPPED_SET = Path(__file__).resolve().parents[2] / "evals" / "qa_v1.jsonl"


def record(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "q-1",
        "repository": "octo-org/sample-app",
        "commit_sha": SHA,
        "question": "Where are repository permissions checked?",
        "answerable": True,
        "split": "heldout",
        "relevant_evidence": [{"path": "app/auth/access.py", "start_line": 26, "end_line": 33}],
    }
    return base | changes


def question(qid: str = "q-1", *, answerable: bool = True, split: str = "heldout") -> Question:
    evidence = (LineRange("a.py", 1, 5),) if answerable else ()
    return Question(qid, "o/r", SHA, "Why?", answerable, split, evidence)


def check(label: str = "E1", problem: str | None = None) -> CitationCheck:
    return CitationCheck(label, "a.py", 1, 2, "line 1\nline 2", problem)


# Recall@5 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (LineRange("a.py", 10, 20), LineRange("a.py", 15, 30), True),
        (LineRange("a.py", 10, 20), LineRange("a.py", 20, 25), True),  # one shared line
        (LineRange("a.py", 10, 20), LineRange("a.py", 1, 9), False),  # adjacent only
        (LineRange("a.py", 10, 20), LineRange("a.py", 12, 13), True),  # contained
        (LineRange("a.py", 10, 20), LineRange("b.py", 10, 20), False),  # other file
    ],
)
def test_ranges_overlap(a: LineRange, b: LineRange, expected: bool) -> None:
    assert ranges_overlap(a, b) is expected
    assert ranges_overlap(b, a) is expected


def test_recall_counts_labeled_ranges_hit_by_the_top_five() -> None:
    labeled = [LineRange("a.py", 10, 20), LineRange("docs/x.md", 1, 4)]
    retrieved = [LineRange("b.py", 1, 50), LineRange("a.py", 18, 40)]

    assert recall_at_k(labeled, retrieved) == 0.5
    assert recall_at_k(labeled, [*retrieved, LineRange("docs/x.md", 4, 9)]) == 1.0


def test_recall_ignores_items_after_the_fifth() -> None:
    labeled = [LineRange("a.py", 10, 20)]
    others = [LineRange(f"other{i}.py", 1, 9) for i in range(5)]

    assert recall_at_k(labeled, [*others, LineRange("a.py", 10, 20)]) == 0.0
    assert recall_at_k(labeled, [*others[:4], LineRange("a.py", 10, 20)]) == 1.0


def test_recall_needs_labeled_evidence() -> None:
    with pytest.raises(ValueError):
        recall_at_k([], [LineRange("a.py", 1, 2)])


# Citation validity ------------------------------------------------------------------------------


CONTENT = "first\nsecond\nthird\n"


def problem(**changes: Any) -> str | None:
    values: dict[str, Any] = {
        "content": CONTENT,
        "commit_sha": SHA,
        "expected_commit_sha": SHA,
        "start_line": 2,
        "end_line": 3,
        "excerpt": "second\nthird",
    } | changes
    return citation_problem(**values)


def test_citation_matching_stored_lines_is_valid() -> None:
    assert problem() is None
    assert problem(start_line=1, end_line=1, excerpt="first") is None


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"excerpt": "second\nTHIRD"}, "differs"),
        ({"end_line": 4, "excerpt": "second\nthird\n"}, "outside"),  # final newline is no line
        ({"start_line": 0}, "outside"),
        ({"content": None}, "not in the indexed version"),
        ({"commit_sha": "b" * 40}, "cites commit"),
    ],
)
def test_citation_problems(changes: dict[str, Any], reason: str) -> None:
    found = problem(**changes)

    assert found is not None and reason in found


# Summary ----------------------------------------------------------------------------------------


def test_summary_denominators_exclude_execution_failures() -> None:
    results = [
        QuestionResult(question("a1"), recall=1.0, answer_status="succeeded",
                       quality_state="answered", citations=[check("E1"), check("E2", "differs")]),
        QuestionResult(question("a2"), recall=0.5, answer_status="succeeded",
                       quality_state="insufficient_evidence"),
        QuestionResult(question("a3"), failures=["retrieval: boom"]),
        QuestionResult(question("u1", answerable=False), answer_status="succeeded",
                       quality_state="insufficient_evidence"),
        QuestionResult(question("u2", answerable=False), answer_status="succeeded",
                       quality_state="answered", citations=[check("E1")]),
        QuestionResult(question("u3", answerable=False), answer_status="failed",
                       failures=["answer failed: provider_unavailable"]),
    ]  # fmt: skip

    summary = summarize(results)

    assert summary.questions == 6
    assert summary.recall == Rate(1.5, 2)
    assert summary.recall.value == 0.75
    assert summary.citation_validity == Rate(2, 3)
    assert summary.abstention == Rate(1, 2)
    assert summary.answered_when_answerable == Rate(1, 2)
    assert summary.execution_failures == 2


def test_retrieval_only_runs_have_no_answer_metrics() -> None:
    summary = summarize([QuestionResult(question(), recall=1.0)])

    assert summary.recall.value == 1.0
    assert summary.citation_validity.value is None
    assert summary.abstention.value is None
    assert meets(summary.abstention, 0.8) is None


def test_meets_targets() -> None:
    assert meets(Rate(8, 10), 0.8) is True
    assert meets(Rate(7, 10), 0.8) is False
    assert meets(Rate(3, 3), 1.0) is True
    assert meets(Rate(0, 0), 0.8) is None


# Audit sheet ------------------------------------------------------------------------------------


def test_audit_rows_list_each_claim_with_its_excerpts_and_empty_review_columns() -> None:
    answered = QuestionResult(
        question("a1"),
        answer_status="succeeded",
        quality_state="answered",
        summary="Checked in access.py.",
        claims=[
            ClaimRecord("Access is checked here.", "fact", ("E1",)),
            ClaimRecord("So reads are limited.", "inference", ("E1", "E2")),
        ],
        citations=[check("E1"), CitationCheck("E2", "b.py", 3, 3, "x = 1", None)],
    )
    abstained = QuestionResult(
        question("u1", answerable=False),
        answer_status="succeeded",
        quality_state="insufficient_evidence",
        gaps=["Nothing about payments."],
    )

    rows = audit_rows([answered, abstained])

    assert [row["claim_number"] for row in rows] == ["1", "2"]
    assert rows[0]["cited_evidence"] == "E1 a.py:1-2"
    assert rows[0]["cited_excerpts"] == "[E1] a.py:1-2\nline 1\nline 2"
    assert "[E2] b.py:3-3\nx = 1" in rows[1]["cited_excerpts"]
    assert rows[0]["answer_summary"] == "Checked in access.py."
    assert rows[0]["supported"] == rows[0]["reviewer_notes"] == ""


# The question set -------------------------------------------------------------------------------


def test_parse_question_accepts_a_valid_record() -> None:
    parsed = parse_question(record(), "line 1")

    assert parsed.relevant_evidence == (LineRange("app/auth/access.py", 26, 33),)
    assert parsed.answerable and parsed.split == "heldout"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"split": "train"}, "split"),
        ({"commit_sha": "abc123"}, "commit_sha"),
        ({"repository": "sample-app"}, "repository"),
        ({"answerable": "yes"}, "answerable"),
        ({"relevant_evidence": []}, "needs labeled evidence"),
        ({"answerable": False}, "must have no labeled evidence"),
        (
            {"relevant_evidence": [{"path": "a.py", "start_line": 9, "end_line": 3}]},
            "start <= end",
        ),
        ({"extra": 1}, "exactly the keys"),
    ],
)
def test_parse_question_rejects_invalid_records(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_question(record(**changes), "line 1")


def test_load_and_select_questions(tmp_path: Path) -> None:
    path = tmp_path / "set.jsonl"
    lines = [
        record(id="t1", split="tuning"),
        record(id="h1"),
        record(id="h2", answerable=False, relevant_evidence=[]),
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n\n")

    questions = load_questions(path)

    assert [q.id for q in select_questions(questions, "heldout", None)] == ["h1", "h2"]
    assert [q.id for q in select_questions(questions, "all", 2)] == ["t1", "h1"]
    path.write_text(json.dumps(record()) + "\n" + json.dumps(record()) + "\n")
    with pytest.raises(ValueError, match="duplicate id"):
        load_questions(path)


def test_eval_branch_names_the_pinned_commit() -> None:
    assert eval_branch("0123456789abcdef" + "0" * 24) == "codeatlas-eval-0123456789ab"


def test_shipped_set_meets_the_task_requirements() -> None:
    questions = load_questions(SHIPPED_SET)
    unanswerable = [q for q in questions if not q.answerable]

    assert len(questions) >= 50
    assert len(unanswerable) >= 10
    assert {q.split for q in unanswerable} == {"tuning", "heldout"}
    assert len({q.repository for q in questions}) in (2, 3)
    assert all(len({q.commit_sha for q in questions if q.repository == r}) == 1
               for r in {q.repository for q in questions})  # fmt: skip
