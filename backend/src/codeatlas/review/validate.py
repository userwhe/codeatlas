"""Checks that a review only cites supplied labels, and repair by dropping items (research R7).

The schema already enforces enumerations and size bounds while parsing. These checks cover what
depends on the evidence: every label exists, every summary point and new test case cites changed
lines, every risk cites something, and every checklist item names a reviewed file or one of the
review's own risks. After one repair call, items that still fail are dropped and counted, and
the review is published only if an overview and a summary point remain.

Checklist risk indexes are 0-based positions in the model's original risk list. When risks are
dropped, the indexes are moved to the risks that are left; an index pointing to a dropped risk
is removed, and an item left with neither a reviewed file nor a risk is dropped too.
"""

from collections.abc import Collection

from codeatlas.review.schema import (
    ChecklistItem,
    NewTestCase,
    ReviewOutput,
    RiskOut,
    SummaryPoint,
)


def validate(
    output: ReviewOutput | None,
    *,
    labels: Collection[str],
    change_labels: Collection[str],
    reviewed_paths: Collection[str] = (),
    parse_error: str | None = None,
) -> list[str]:
    """Return the problems with `output`; an empty list means it can be published as is.

    `reviewed_paths` are the paths of the reviewed files, at both sides of a rename.
    """
    if output is None:
        return [f"The output did not match the response schema ({parse_error or 'invalid'})."]
    known, change, paths = set(labels), set(change_labels), set(reviewed_paths)
    errors: list[str] = []
    if not output.overview.strip():
        errors.append("The overview is empty.")
    for index, point in enumerate(output.summary_points, start=1):
        errors.extend(_point_problems(point, index, known, change))
    for index, risk in enumerate(output.risks, start=1):
        errors.extend(_risk_problems(risk, index, known))
    for index, item in enumerate(output.checklist, start=1):
        errors.extend(_checklist_problems(item, index, paths, range(len(output.risks))))
    for index, case in enumerate(output.new_test_cases, start=1):
        errors.extend(_case_problems(case, index, known, change))
    return errors


def drop_invalid(
    output: ReviewOutput,
    *,
    labels: Collection[str],
    change_labels: Collection[str],
    reviewed_paths: Collection[str] = (),
) -> tuple[ReviewOutput, int]:
    """Remove the items that fail validation; return what is left and how many items were
    removed. The overview is kept as it is; `usable` decides about it.

    Checklist risk indexes are renumbered for the risks that are left, so they stay positions in
    the returned output's `risks`.
    """
    known, change, paths = set(labels), set(change_labels), set(reviewed_paths)
    points = [
        point
        for index, point in enumerate(output.summary_points, start=1)
        if not _point_problems(point, index, known, change)
    ]
    kept_risks = [
        position
        for position, risk in enumerate(output.risks)
        if not _risk_problems(risk, position + 1, known)
    ]
    moved = {old: new for new, old in enumerate(kept_risks)}
    checklist = []
    for index, item in enumerate(output.checklist, start=1):
        renumbered = item.model_copy(
            update={
                "risk_indexes": [moved[old] for old in item.risk_indexes if old in moved],
            }
        )
        if not _checklist_problems(renumbered, index, paths, range(len(kept_risks))):
            checklist.append(renumbered)
    cases = [
        case
        for index, case in enumerate(output.new_test_cases, start=1)
        if not _case_problems(case, index, known, change)
    ]
    removed = (
        len(output.summary_points)
        - len(points)
        + len(output.risks)
        - len(kept_risks)
        + len(output.checklist)
        - len(checklist)
        + len(output.new_test_cases)
        - len(cases)
    )
    if not removed:
        return output, 0
    cleaned = output.model_copy(
        update={
            "summary_points": points,
            "risks": [output.risks[position] for position in kept_risks],
            "checklist": checklist,
            "new_test_cases": cases,
        }
    )
    return cleaned, removed


def usable(output: ReviewOutput) -> bool:
    """Whether a review, after `drop_invalid`, still has an overview and a summary point."""
    return bool(output.overview.strip()) and bool(output.summary_points)


def _point_problems(
    point: SummaryPoint, index: int, known: set[str], change: set[str]
) -> list[str]:
    problems = _unknown(f"Summary point {index}", point.evidence_ids, known)
    if not any(label in change for label in point.evidence_ids):
        problems.append(f"Summary point {index} cites no changed lines.")
    return problems


def _risk_problems(risk: RiskOut, index: int, known: set[str]) -> list[str]:
    problems = _unknown(f"Risk {index}", risk.evidence_ids, known)
    if not risk.evidence_ids:
        problems.append(f"Risk {index} cites no evidence.")
    return problems


def _checklist_problems(
    item: ChecklistItem, index: int, paths: set[str], risk_indexes: range
) -> list[str]:
    # A path or index that does not resolve is left out of the result; only an item with no
    # valid reference at all is a problem.
    if any(path in paths for path in item.paths) or any(
        position in risk_indexes for position in item.risk_indexes
    ):
        return []
    return [f"Checklist item {index} names no reviewed file and no risk in the risks list."]


def _case_problems(case: NewTestCase, index: int, known: set[str], change: set[str]) -> list[str]:
    problems = _unknown(f"New test case {index}", case.evidence_ids, known)
    if not any(label in change for label in case.evidence_ids):
        problems.append(f"New test case {index} cites no changed lines.")
    return problems


def _unknown(item: str, cited: list[str], known: set[str]) -> list[str]:
    unknown = [label for label in cited if label not in known]
    return [f"{item} cites unknown evidence ids: {', '.join(unknown)}."] if unknown else []
