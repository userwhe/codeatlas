"""Checks that an answer only cites supplied evidence (FR-020, research R11)."""

from collections.abc import Collection

from codeatlas.qa.schema import AnswerOutput


def validate(
    output: AnswerOutput | None, labels: Collection[str], parse_error: str | None = None
) -> list[str]:
    """Return the problems with `output`; an empty list means it can be published."""
    if output is None:
        return [f"The output did not match the response schema ({parse_error or 'invalid'})."]
    errors: list[str] = []
    known = set(labels)
    for index, claim in enumerate(output.claims, start=1):
        unknown = [label for label in claim.evidence_ids if label not in known]
        if unknown:
            errors.append(f"Claim {index} cites unknown evidence ids: {', '.join(unknown)}.")
        if claim.kind == "fact" and not claim.evidence_ids:
            errors.append(f"Claim {index} is a fact but cites no evidence.")
    if not output.summary.strip():
        errors.append("The summary is empty.")
    if output.status == "answered" and not any(c.kind == "fact" for c in output.claims):
        errors.append("An answered response needs at least one fact claim with evidence.")
    if output.status == "insufficient_evidence" and not output.gaps:
        errors.append("An insufficient_evidence response must list the gaps.")
    return errors


def cited_labels(output: AnswerOutput) -> list[str]:
    """Evidence labels cited by the claims, in first-cited order."""
    seen: list[str] = []
    for claim in output.claims:
        for label in claim.evidence_ids:
            if label not in seen:
                seen.append(label)
    return seen
