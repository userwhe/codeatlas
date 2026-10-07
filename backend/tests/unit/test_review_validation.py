"""Checks that a review cites only supplied labels, and repair by dropping items (research R7)."""

import pytest
from pydantic import ValidationError

from codeatlas.review.schema import ChecklistItem, NewTestCase, ReviewOutput, RiskOut, SummaryPoint
from codeatlas.review.validate import drop_invalid, usable, validate

LABELS = frozenset({"E1", "E2", "E3", "E4"})
CHANGE_LABELS = frozenset({"E1", "E2", "E3"})  # E4 is related code
REVIEWED_PATHS = frozenset({"app/auth/permissions.py", "app/text.py"})


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


def _item(
    text: str = "Check it.", *, paths: list[str] | None = None, risks: list[int] | None = None
) -> ChecklistItem:
    return ChecklistItem(text=text, paths=paths or [], risk_indexes=risks or [])


def _case(*labels: str, behavior: str = "A viewer is refused.") -> NewTestCase:
    return NewTestCase(behavior=behavior, location_hint="", evidence_ids=list(labels))


def _validate(output: ReviewOutput | None, parse_error: str | None = None) -> list[str]:
    return validate(
        output,
        labels=LABELS,
        change_labels=CHANGE_LABELS,
        reviewed_paths=REVIEWED_PATHS,
        parse_error=parse_error,
    )


def _drop(output: ReviewOutput) -> tuple[ReviewOutput, int]:
    return drop_invalid(
        output, labels=LABELS, change_labels=CHANGE_LABELS, reviewed_paths=REVIEWED_PATHS
    )


def test_a_valid_review_has_no_problems() -> None:
    output = ReviewOutput(
        overview="Changes the write check.",
        summary_points=[_point("E1", "E4")],
        risks=[_risk("E4"), _risk("E2")],
        checklist=[_item(paths=["app/auth/permissions.py"]), _item(risks=[1])],
        new_test_cases=[_case("E1", "E4")],
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

    cleaned, removed = _drop(output)

    assert removed == 4
    assert [point.text for point in cleaned.summary_points] == ["kept"]
    assert [risk.title for risk in cleaned.risks] == ["kept"]
    assert cleaned.overview == "o"
    assert _validate(cleaned) == []
    assert len(output.summary_points) == 3  # the input is not changed


def test_drop_invalid_on_a_valid_review_removes_nothing() -> None:
    output = ReviewOutput(
        overview="o",
        summary_points=[_point("E1")],
        risks=[_risk("E4")],
        checklist=[_item(paths=["app/text.py"], risks=[0])],
        new_test_cases=[_case("E2")],
    )

    cleaned, removed = _drop(output)

    assert (cleaned, removed) == (output, 0)


def test_usable_needs_an_overview_and_a_valid_summary_point() -> None:
    assert usable(ReviewOutput(overview="o", summary_points=[_point("E1")]))
    assert not usable(ReviewOutput(overview="o", summary_points=[], risks=[_risk("E1")]))
    assert not usable(ReviewOutput(overview="  ", summary_points=[_point("E1")]))

    only_invalid = ReviewOutput(overview="o", summary_points=[_point("E9")], risks=[_risk("E9")])
    cleaned, removed = _drop(only_invalid)

    assert removed == 2
    assert not usable(cleaned)


def test_a_checklist_item_needs_a_reviewed_path_or_a_risk_in_the_list() -> None:
    output = ReviewOutput(
        overview="o",
        summary_points=[_point("E1")],
        risks=[_risk("E1"), _risk("E2")],
        checklist=[
            _item(paths=["app/text.py"]),
            _item(risks=[1]),
            _item(paths=["app/unknown.py", "app/auth/permissions.py"], risks=[7]),
            _item(paths=["app/unknown.py"], risks=[2, -1]),
            _item(),
        ],
    )

    # Risk indexes count from 0 in the model's own risk list. A path or index that does not
    # resolve is left out of the result, but an item with one valid reference is still valid.
    assert _validate(output) == [
        "Checklist item 4 names no reviewed file and no risk in the risks list.",
        "Checklist item 5 names no reviewed file and no risk in the risks list.",
    ]


def test_a_new_test_case_must_cite_a_change_label() -> None:
    output = ReviewOutput(
        overview="o",
        summary_points=[_point("E1")],
        new_test_cases=[_case("E1"), _case("E4"), _case("E9"), _case()],
    )

    assert _validate(output) == [
        "New test case 2 cites no changed lines.",
        "New test case 3 cites unknown evidence ids: E9.",
        "New test case 3 cites no changed lines.",
        "New test case 4 cites no changed lines.",
    ]


def test_checklist_and_new_test_case_bounds_fail_parsing() -> None:
    ReviewOutput(overview="o", checklist=[_item()] * 12, new_test_cases=[_case("E1")] * 8)

    with pytest.raises(ValidationError):
        ReviewOutput(overview="o", checklist=[_item()] * 13)
    with pytest.raises(ValidationError):
        ReviewOutput(overview="o", new_test_cases=[_case("E1")] * 9)
    with pytest.raises(ValidationError):
        ChecklistItem(text="", paths=["app/text.py"], risk_indexes=[])
    with pytest.raises(ValidationError):
        ChecklistItem(text="x" * 201, paths=["app/text.py"], risk_indexes=[])
    with pytest.raises(ValidationError):
        NewTestCase(behavior="", location_hint="", evidence_ids=["E1"])
    with pytest.raises(ValidationError):
        NewTestCase(behavior="x" * 301, location_hint="", evidence_ids=["E1"])
    with pytest.raises(ValidationError):
        NewTestCase(behavior="b", location_hint="x" * 201, evidence_ids=["E1"])


def test_drop_invalid_removes_checklist_references_to_dropped_risks() -> None:
    output = ReviewOutput(
        overview="o",
        summary_points=[_point("E1")],
        risks=[_risk("E9", title="dropped"), _risk("E1", title="kept"), _risk("E2", title="also")],
        checklist=[
            _item("only the dropped risk", risks=[0]),
            _item("both risks", risks=[0, 2]),
            _item("a path and the dropped risk", paths=["app/text.py"], risks=[0]),
            _item("no valid reference", paths=["app/unknown.py"]),
        ],
        new_test_cases=[_case("E1", behavior="kept"), _case("E4", behavior="related only")],
    )

    cleaned, removed = _drop(output)

    # Indexes refer to the original risk list. After "dropped" goes, index 2 ("also") becomes 1;
    # an item left with neither a reviewed path nor a risk goes too, and is counted.
    assert [risk.title for risk in cleaned.risks] == ["kept", "also"]
    assert [(item.text, item.paths, item.risk_indexes) for item in cleaned.checklist] == [
        ("both risks", [], [1]),
        ("a path and the dropped risk", ["app/text.py"], []),
    ]
    assert [case.behavior for case in cleaned.new_test_cases] == ["kept"]
    assert removed == 1 + 2 + 1  # the risk, two checklist items, and one test case
    assert _validate(cleaned) == []
