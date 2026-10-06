import pytest
from pydantic import ValidationError

from codeatlas.qa.schema import MAX_CLAIM_CHARS, MAX_CLAIMS, AnswerOutput, Claim
from codeatlas.qa.validate import cited_labels, validate

LABELS = ["E1", "E2"]


def answered(*claims: Claim) -> AnswerOutput:
    return AnswerOutput(status="answered", summary="Checked in access.py.", claims=list(claims))


def test_valid_answer_passes() -> None:
    output = answered(
        Claim(text="Access is checked in access.py.", kind="fact", evidence_ids=["E1"]),
        Claim(text="Search probably reuses it.", kind="inference", evidence_ids=["E2"]),
    )
    assert validate(output, LABELS) == []


def test_unknown_evidence_ids_are_rejected() -> None:
    output = answered(Claim(text="x", kind="fact", evidence_ids=["E1", "E99"]))
    errors = validate(output, LABELS)
    assert any("E99" in error for error in errors)


def test_fact_without_evidence_is_rejected() -> None:
    output = answered(
        Claim(text="x", kind="fact", evidence_ids=[]),
        Claim(text="y", kind="fact", evidence_ids=["E1"]),
    )
    assert any("cites no evidence" in error for error in validate(output, LABELS))


def test_inference_without_evidence_is_allowed() -> None:
    output = answered(
        Claim(text="x", kind="fact", evidence_ids=["E1"]),
        Claim(text="y", kind="inference", evidence_ids=[]),
    )
    assert validate(output, LABELS) == []


def test_empty_answers_are_rejected() -> None:
    assert validate(AnswerOutput(status="answered", summary=" ", claims=[]), LABELS)
    assert validate(None, LABELS, "not JSON") == [
        "The output did not match the response schema (not JSON)."
    ]


def test_insufficient_evidence_requires_gaps() -> None:
    without_gaps = AnswerOutput(status="insufficient_evidence", summary="Unknown.")
    with_gaps = AnswerOutput(status="insufficient_evidence", summary="Unknown.", gaps=["payments"])
    assert validate(without_gaps, LABELS)
    assert validate(with_gaps, LABELS) == []


def test_schema_bounds() -> None:
    claim = Claim(text="ok", kind="fact", evidence_ids=["E1"])
    with pytest.raises(ValidationError):
        AnswerOutput(status="answered", summary="s", claims=[claim] * (MAX_CLAIMS + 1))
    with pytest.raises(ValidationError):
        Claim(text="x" * (MAX_CLAIM_CHARS + 1), kind="fact", evidence_ids=["E1"])
    with pytest.raises(ValidationError):
        Claim(text="x", kind="opinion", evidence_ids=[])  # type: ignore[arg-type]


def test_cited_labels_in_first_cited_order() -> None:
    output = answered(
        Claim(text="a", kind="fact", evidence_ids=["E2", "E1"]),
        Claim(text="b", kind="fact", evidence_ids=["E1"]),
    )
    assert cited_labels(output) == ["E2", "E1"]
