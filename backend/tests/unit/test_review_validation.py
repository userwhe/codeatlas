"""Checks that a review cites only supplied labels, and repair by dropping items (research R7)."""

from codeatlas.review.schema import ReviewOutput, RiskOut, SummaryPoint
from codeatlas.review.validate import drop_invalid, usable, validate

LABELS = frozenset({"E1", "E2", "E3", "E4"})
CHANGE_LABELS = frozenset({"E1", "E2", "E3"})  # E4 is related code


def _point(*labels: str, text: str = "A point.") -> SummaryPoint:
    return SummaryPoint(change="modified", text=text, evidence_ids=list(labels))


def _risk(*labels: str, title: str = "A risk") -> RiskOut:
    return RiskOut(
        title=title,
        severity="medium",
        category="correctness",
        basis="possible",
        explanation="An explanation.",
        suggested_check="A check.",
        evidence_ids=list(labels),
    )


def _validate(output: ReviewOutput | None, parse_error: str | None = None) -> list[str]:
    return validate(output, labels=LABELS, change_labels=CHANGE_LABELS, parse_error=parse_error)


def test_a_valid_review_has_no_problems() -> None:
    output = ReviewOutput(
        overview="Changes the write check.",
        summary_points=[_point("E1", "E4")],
        risks=[_risk("E4"), _risk("E2")],
    )

    assert _validate(output) == []


def test_unparsed_output_is_reported() -> None:
    assert _validate(None, "bad json") == [
        "The output did not match the response schema (bad json)."
    ]


def test_unknown_labels_are_reported() -> None:
    output = ReviewOutput(
        overview="o", summary_points=[_point("E1", "E9")], risks=[_risk("E2"), _risk("E99")]
    )

    assert _validate(output) == [
        "Summary point 1 cites unknown evidence ids: E9.",
        "Risk 2 cites unknown evidence ids: E99.",
    ]


def test_a_summary_point_must_cite_a_change_label() -> None:
    output = ReviewOutput(overview="o", summary_points=[_point("E1"), _point("E4"), _point()])

    assert _validate(output) == [
        "Summary point 2 cites no changed lines.",
        "Summary point 3 cites no changed lines.",
    ]


def test_a_risk_must_cite_a_label() -> None:
    output = ReviewOutput(overview="o", summary_points=[_point("E1")], risks=[_risk()])

    assert _validate(output) == ["Risk 1 cites no evidence."]


def test_an_empty_overview_is_reported() -> None:
    output = ReviewOutput(overview="   ", summary_points=[_point("E1")])

    assert _validate(output) == ["The overview is empty."]


def test_drop_invalid_removes_only_the_failing_items() -> None:
    output = ReviewOutput(
        overview="o",
        summary_points=[_point("E1", text="kept"), _point("E4"), _point("E2", "E9")],
        risks=[_risk("E9", title="unknown"), _risk("E3", title="kept"), _risk(title="none")],
    )

    cleaned, removed = drop_invalid(output, labels=LABELS, change_labels=CHANGE_LABELS)

    assert removed == 4
    assert [point.text for point in cleaned.summary_points] == ["kept"]
    assert [risk.title for risk in cleaned.risks] == ["kept"]
    assert cleaned.overview == "o"
    assert _validate(cleaned) == []
    assert len(output.summary_points) == 3  # the input is not changed


def test_drop_invalid_on_a_valid_review_removes_nothing() -> None:
    output = ReviewOutput(overview="o", summary_points=[_point("E1")], risks=[_risk("E4")])

    cleaned, removed = drop_invalid(output, labels=LABELS, change_labels=CHANGE_LABELS)

    assert (cleaned, removed) == (output, 0)


def test_usable_needs_an_overview_and_a_valid_summary_point() -> None:
    assert usable(ReviewOutput(overview="o", summary_points=[_point("E1")]))
    assert not usable(ReviewOutput(overview="o", summary_points=[], risks=[_risk("E1")]))
    assert not usable(ReviewOutput(overview="  ", summary_points=[_point("E1")]))

    only_invalid = ReviewOutput(overview="o", summary_points=[_point("E9")], risks=[_risk("E9")])
    cleaned, removed = drop_invalid(only_invalid, labels=LABELS, change_labels=CHANGE_LABELS)

    assert removed == 2
    assert not usable(cleaned)
