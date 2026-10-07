"""The parts of a review that the server computes (research R8, FR-006, FR-013, FR-019).

The model writes text; the server decides structure. Risks are ordered by severity, keeping the
model's order within a level, and numbered `R1` to `Rn`. The overall risk level is the highest
severity shown, or `none`, and the review is partial when any changed file hit the review limits.
Summary points are grouped by area, the first two directories of their first cited path.

Two kinds of item come from rules, not the model: a `high` `security` risk per changed
credential file, which names the file but never cites its content, and a summary point per file
renamed without changes, which has no changed lines to cite.

Checklist items keep only the reviewed files they name, and their risk indexes, positions in the
model's risk list, become the numbered risk identifiers. An item left with neither is dropped and
counted in `omitted_items`. The changed and candidate tests come from the change and the head
tree (research R5), never from the model; a candidate cites its test excerpt when it has one.

The result is the JSON stored in `analysis_runs.result` (data-model.md, "Review result shape").
"""

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

from codeatlas.review.context import CandidateTest, ChangedTest
from codeatlas.review.diff import Selection
from codeatlas.review.evidence import ReviewEvidence
from codeatlas.review.schema import ChangeKind, ChecklistItem, NewTestCase, ReviewOutput, RiskOut

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
    changed_tests: Sequence[ChangedTest] = (),
    candidate_tests: Sequence[CandidateTest] = (),
) -> dict[str, Any]:
    """The stored result of a reviewed run. `output` has already been through `drop_invalid`,
    so every label it cites is one of `evidence`.

    `omitted_items` counts the items validation dropped; checklist items dropped here are added.
    """
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
    all_risks = [*(_model_risk(risk) for risk in output.risks), *rule_risks]
    order = _severity_order(all_risks)
    risks = [{"id": f"R{number}", **all_risks[position]} for number, position in order]
    # Checklist risk indexes are positions in the model's risk list, which comes first.
    risk_ids = {
        position: f"R{number}" for number, position in order if position < len(output.risks)
    }
    reviewed = {
        path for file in selection.reviewed for path in (file.path, file.previous_path) if path
    }
    checklist = [
        item
        for item in (_checklist_item(item, reviewed, risk_ids) for item in output.checklist)
        if item["paths"] or item["risk_ids"]
    ]
    context_items = sum(1 for item in evidence if item.source_type != "change")
    excerpts = {item.path: item.label for item in evidence if item.source_type == "test"}
    return {
        "overall_risk": _overall_risk(risks, selection),
        "overview": output.overview,
        "summary": _grouped([*model_points, *rule_summary_points(selection)]),
        "risks": risks,
        "checklist": checklist,
        "tests": {
            "changed": _changed_tests(changed_tests),
            "candidates": [_candidate(candidate, excerpts) for candidate in candidate_tests],
            "new_cases": [_new_case(case) for case in output.new_test_cases],
        },
        "coverage": _coverage(selection, context_items=context_items),
        "omitted_items": omitted_items + len(output.checklist) - len(checklist),
    }


def nothing_to_review_result(
    selection: Selection,
    rule_risks: Sequence[Mapping[str, Any]],
    changed_tests: Sequence[ChangedTest] = (),
) -> dict[str, Any]:
    """The result when no changed file could be reviewed (FR-020): no model text, only rule
    risks, the changed tests, and the coverage."""
    risks = _numbered(rule_risks)
    return {
        "overall_risk": _overall_risk(risks, selection),
        "overview": "",
        "summary": [],
        "risks": risks,
        "checklist": [],
        "tests": {"changed": _changed_tests(changed_tests), "candidates": [], "new_cases": []},
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
    return [{"id": f"R{number}", **risks[position]} for number, position in _severity_order(risks)]


def _severity_order(risks: Sequence[Mapping[str, Any]]) -> list[tuple[int, int]]:
    """(number, position) for each risk, by severity and then position: `R<number>` is the
    identifier of `risks[position]`."""
    positions = sorted(
        range(len(risks)), key=lambda position: SEVERITY_ORDER[risks[position]["severity"]]
    )
    return list(enumerate(positions, start=1))


def _checklist_item(
    item: ChecklistItem, reviewed: set[str], risk_ids: Mapping[int, str]
) -> dict[str, Any]:
    """The item with only the reviewed paths it names and the identifiers of its risks, each
    once, in the model's order."""
    paths = [path for path in dict.fromkeys(item.paths) if path in reviewed]
    ids = dict.fromkeys(risk_ids[index] for index in item.risk_indexes if index in risk_ids)
    return {"text": item.text, "paths": paths, "risk_ids": list(ids)}


def _new_case(case: NewTestCase) -> dict[str, Any]:
    return {
        "behavior": case.behavior,
        "location_hint": case.location_hint.strip() or None,
        "evidence_ids": list(case.evidence_ids),
    }


def _candidate(candidate: CandidateTest, excerpts: Mapping[str, str]) -> dict[str, Any]:
    """A candidate test, citing its test excerpt when the excerpt was kept as evidence."""
    label = excerpts.get(candidate.path)
    return {
        "path": candidate.path,
        "reason": candidate.reason,
        "evidence_ids": [label] if label else [],
    }


def _changed_tests(tests: Sequence[ChangedTest]) -> list[dict[str, Any]]:
    return [{"path": test.path, "change": test.change} for test in tests]


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
