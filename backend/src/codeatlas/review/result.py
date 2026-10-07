"""The parts of a review that the server computes (research R8, FR-006, FR-013, FR-019).

The model writes text; the server decides structure. Risks are ordered by severity, keeping the
model's order within a level, and numbered `R1` to `Rn`. The overall risk level is the highest
severity shown, or `none`, and the review is partial when any changed file hit the review limits.
Summary points are grouped by area, the first two directories of their first cited path.

Two kinds of item come from rules, not the model: a `high` `security` risk per changed
credential file, which names the file but never cites its content, and a summary point per file
renamed without changes, which has no changed lines to cite.

The result is the JSON stored in `analysis_runs.result` (data-model.md, "Review result shape").
"""

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

from codeatlas.review.diff import Selection
from codeatlas.review.evidence import ReviewEvidence
from codeatlas.review.schema import ChangeKind, ReviewOutput, RiskOut

ROOT_AREA = "(root)"
AREA_DIRECTORIES = 2
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
CREDENTIAL_TITLES: dict[ChangeKind, str] = {
    "added": "Credential file added",
    "modified": "Credential file changed",
    "renamed": "Credential file renamed",
    "removed": "Credential file removed",
}
CREDENTIAL_EXPLANATION = (
    "The pull request changes a file whose name marks it as holding credentials. Its content "
    "was not read, sent to the model, or shown, so this review cannot tell what it holds. A "
    "secret committed to a repository stays in its history even after the file is removed."
)
CREDENTIAL_CHECK = (
    "Confirm that the file holds no real secret and should be in the repository. Rotate any "
    "secret it holds or has held."
)

AreaPoint = tuple[str, dict[str, Any]]


def area(path: str) -> str:
    """The first two directories of `path`, such as `backend/src`, or `(root)`."""
    directories = path.split("/")[:-1]
    return "/".join(directories[:AREA_DIRECTORIES]) if directories else ROOT_AREA


def rule_risks(selection: Selection) -> list[dict[str, Any]]:
    """One risk per changed credential file (FR-019), without an `id` until numbered."""
    return [
        {
            "title": CREDENTIAL_TITLES[entry.change],
            "severity": "high",
            "category": "security",
            "basis": "observed",
            "explanation": CREDENTIAL_EXPLANATION,
            "suggested_check": CREDENTIAL_CHECK,
            "evidence_ids": [],
            "origin": "rule",
            "path": entry.path,
        }
        for entry in selection.coverage
        if entry.entry_type == "file" and entry.reason == "credential_file"
    ]


def rule_summary_points(selection: Selection) -> list[AreaPoint]:
    """A point, with its area, per reviewed file renamed without changes (FR-006)."""
    return [
        (
            area(file.path),
            {
                "change": "renamed",
                "text": f"Renamed `{file.previous_path}` to `{file.path}` without changes",
                "evidence_ids": [],
                "origin": "rule",
            },
        )
        for file in selection.reviewed
        if file.change == "renamed" and not file.hunks
    ]


def build_result(
    output: ReviewOutput,
    *,
    evidence: Sequence[ReviewEvidence],
    selection: Selection,
    rule_risks: Sequence[Mapping[str, Any]],
    omitted_items: int,
) -> dict[str, Any]:
    """The stored result of a reviewed run. `output` has already been through `drop_invalid`,
    so every label it cites is one of `evidence`."""
    paths = {item.label: item.path for item in evidence}
    model_points: list[AreaPoint] = [
        (
            _first_cited_area(point.evidence_ids, paths),
            {
                "change": point.change,
                "text": point.text,
                "evidence_ids": list(point.evidence_ids),
                "origin": "model",
            },
        )
        for point in output.summary_points
    ]
    risks = _numbered([*(_model_risk(risk) for risk in output.risks), *rule_risks])
    context_items = sum(1 for item in evidence if item.source_type != "change")
    return {
        "overall_risk": _overall_risk(risks, selection),
        "overview": output.overview,
        "summary": _grouped([*model_points, *rule_summary_points(selection)]),
        "risks": risks,
        "checklist": [],
        "tests": _empty_tests(),
        "coverage": _coverage(selection, context_items=context_items),
        "omitted_items": omitted_items,
    }


def nothing_to_review_result(
    selection: Selection, rule_risks: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """The result when no changed file could be reviewed (FR-020): no model text, only rule
    risks and the coverage."""
    risks = _numbered(rule_risks)
    return {
        "overall_risk": _overall_risk(risks, selection),
        "overview": "",
        "summary": [],
        "risks": risks,
        "checklist": [],
        "tests": _empty_tests(),
        "coverage": _coverage(selection, context_items=0),
        "omitted_items": 0,
    }


def _model_risk(risk: RiskOut) -> dict[str, Any]:
    return {
        "title": risk.title,
        "severity": risk.severity,
        "category": risk.category,
        "basis": risk.basis,
        "explanation": risk.explanation,
        "suggested_check": risk.suggested_check,
        "evidence_ids": list(risk.evidence_ids),
        "origin": "model",
        "path": None,
    }


def _numbered(risks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(risks, key=lambda risk: SEVERITY_ORDER[risk["severity"]])  # stable
    return [{"id": f"R{number}", **risk} for number, risk in enumerate(ordered, start=1)]


def _overall_risk(risks: Sequence[Mapping[str, Any]], selection: Selection) -> dict[str, Any]:
    level = risks[0]["severity"] if risks else "none"  # `risks` is ordered by severity
    return {"level": level, "partial": selection.partial}


def _first_cited_area(labels: Sequence[str], paths: Mapping[str, str]) -> str:
    for label in labels:
        if label in paths:
            return area(paths[label])
    return ROOT_AREA


def _grouped(points: Sequence[AreaPoint]) -> list[dict[str, Any]]:
    """Points grouped by area, areas in order of their first point."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for point_area, point in points:
        groups.setdefault(point_area, []).append(point)
    return [{"area": point_area, "points": group} for point_area, group in groups.items()]


def _coverage(selection: Selection, *, context_items: int) -> dict[str, Any]:
    return {
        "files": [dataclasses.asdict(entry) for entry in selection.coverage],
        "changed_files": sum(entry.count for entry in selection.coverage),
        "reviewed_files": len(selection.reviewed),
        "changed_lines_reviewed": selection.changed_lines_reviewed,
        "context_items": context_items,
    }


def _empty_tests() -> dict[str, list[dict[str, Any]]]:
    # Changed and candidate tests and new test cases are filled in by User Story 2.
    return {"changed": [], "candidates": [], "new_cases": []}
