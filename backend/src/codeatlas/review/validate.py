"""Checks that a review only cites supplied labels, and repair by dropping items (research R7).

The schema already enforces enumerations and size bounds while parsing. These checks cover what
depends on the evidence: every label exists, every summary point cites changed lines, and every
risk cites something. After one repair call, items that still fail are dropped and counted, and
the review is published only if an overview and a summary point remain.
"""

from collections.abc import Collection

from codeatlas.review.schema import ReviewOutput, RiskOut, SummaryPoint


def validate(
    output: ReviewOutput | None,
    *,
    labels: Collection[str],
    change_labels: Collection[str],
    parse_error: str | None = None,
) -> list[str]:
    """Return the problems with `output`; an empty list means it can be published as is."""
    if output is None:
        return [f"The output did not match the response schema ({parse_error or 'invalid'})."]
    known, change = set(labels), set(change_labels)
    errors: list[str] = []
    if not output.overview.strip():
        errors.append("The overview is empty.")
    for index, point in enumerate(output.summary_points, start=1):
        errors.extend(_point_problems(point, index, known, change))
    for index, risk in enumerate(output.risks, start=1):
        errors.extend(_risk_problems(risk, index, known))
    return errors


def drop_invalid(
    output: ReviewOutput, *, labels: Collection[str], change_labels: Collection[str]
) -> tuple[ReviewOutput, int]:
    """Remove the summary points and risks that fail validation; return what is left and how
    many items were removed. The overview is kept as it is; `usable` decides about it."""
    known, change = set(labels), set(change_labels)
    points = [
        point
        for index, point in enumerate(output.summary_points, start=1)
        if not _point_problems(point, index, known, change)
    ]
    risks = [
        risk
        for index, risk in enumerate(output.risks, start=1)
        if not _risk_problems(risk, index, known)
    ]
    removed = len(output.summary_points) - len(points) + len(output.risks) - len(risks)
    if not removed:
        return output, 0
    return output.model_copy(update={"summary_points": points, "risks": risks}), removed


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


def _unknown(item: str, cited: list[str], known: set[str]) -> list[str]:
    unknown = [label for label in cited if label not in known]
    return [f"{item} cites unknown evidence ids: {', '.join(unknown)}."] if unknown else []
