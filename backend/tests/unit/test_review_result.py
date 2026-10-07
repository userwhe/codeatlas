"""The parts of a review that the server computes (research R8)."""

import hashlib
from typing import Any

from codeatlas.review.context import CandidateTest, ChangedTest, CodeExcerpt
from codeatlas.review.diff import ChangedFile, Hunk, Selection, select_for_review
from codeatlas.review.evidence import ReviewEvidence
from codeatlas.review.result import (
    build_result,
    nothing_to_review_result,
    rule_risks,
    rule_summary_points,
)
from codeatlas.review.schema import (
    ChecklistItem,
    NewTestCase,
    ReviewOutput,
    RiskOut,
    Severity,
    SummaryPoint,
)
from codeatlas.review.validate import drop_invalid

LIMITS = {"max_files": 100, "max_changed_lines": 2000, "max_hunks": 80, "max_diff_tokens": 40_000}
EMPTY_TESTS = {"changed": [], "candidates": [], "new_cases": []}


def _hunk(added: int = 1, removed: int = 0) -> Hunk:
    return Hunk(
        before_start=1 if removed else None,
        before_end=removed if removed else None,
        after_start=1 if added else None,
        after_end=added if added else None,
        lines=(*("-x" for _ in range(removed)), *("+y" for _ in range(added))),
        added_lines=tuple(range(1, added + 1)),
        removed_lines=tuple(range(1, removed + 1)),
    )


def _file(path: str, *, added: int = 1, removed: int = 0, change: str = "modified") -> ChangedFile:
    return ChangedFile(
        path=path,
        previous_path=None,
        change=change,  # type: ignore[arg-type]
        language="python",
        before="",
        after="",
        hunks=(_hunk(added, removed),),
    )


def _skipped(path: str, reason: str, change: str = "added") -> ChangedFile:
    return ChangedFile(path=path, previous_path=None, change=change, reason=reason)  # type: ignore[arg-type]


RENAMED = ChangedFile(
    path="app/strings.py",
    previous_path="app/text.py",
    change="renamed",
    language="python",
    before="",
    after="",
)


def _select(files: list[ChangedFile], **limits: int) -> Selection:
    return select_for_review(files, **{**LIMITS, **limits})


def _item(
    label: str, path: str, source_type: str = "change", side: str = "after"
) -> ReviewEvidence:
    return ReviewEvidence(
        label=label,
        source_type=source_type,  # type: ignore[arg-type]
        side=side,  # type: ignore[arg-type]
        path=path,
        commit_sha="h" * 40,
        start_line=1,
        end_line=1,
        excerpt="x",
        excerpt_sha256=hashlib.sha256(b"x").digest(),
        rank=int(label[1:]),
    )


EVIDENCE = [
    _item("E1", "app/auth/permissions.py"),
    _item("E2", "app/auth/permissions.py", side="before"),
    _item("E3", "backend/src/codeatlas/config.py"),
    _item("E4", "README.md"),
    _item("E5", "app/text.py"),
    _item("E6", "app/repositories.py", "reference"),
]


def _risk(title: str, severity: Severity, *labels: str) -> RiskOut:
    return RiskOut(
        title=title,
        severity=severity,
        category="correctness",
        basis="possible",
        explanation="An explanation.",
        suggested_check="A check.",
        evidence_ids=list(labels) or ["E1"],
    )


def _point(text: str, *labels: str) -> SummaryPoint:
    return SummaryPoint(change="modified", text=text, evidence_ids=list(labels))


def _result(
    output: ReviewOutput, selection: Selection | None = None, **kwargs: Any
) -> dict[str, Any]:
    selection = selection or _select([_file("app/auth/permissions.py")])
    return build_result(
        output,
        evidence=kwargs.get("evidence", EVIDENCE),
        selection=selection,
        rule_risks=kwargs.get("rule_risks", rule_risks(selection)),
        omitted_items=kwargs.get("omitted_items", 0),
        changed_tests=kwargs.get("changed_tests", ()),
        candidate_tests=kwargs.get("candidate_tests", ()),
    )


def _check(
    text: str, *, paths: list[str] | None = None, risks: list[int] | None = None
) -> ChecklistItem:
    return ChecklistItem(text=text, paths=paths or [], risk_indexes=risks or [])


def test_risks_are_ordered_by_severity_and_numbered() -> None:
    output = ReviewOutput(
        overview="o",
        summary_points=[_point("p", "E1")],
        risks=[
            _risk("low one", "low"),
            _risk("high one", "high"),
            _risk("medium one", "medium"),
            _risk("high two", "high"),
        ],
    )
    selection = _select([_file("app/auth/permissions.py"), _skipped(".env", "credential_file")])

    risks = _result(output, selection)["risks"]

    assert [(risk["id"], risk["title"], risk["origin"]) for risk in risks] == [
        ("R1", "high one", "model"),
        ("R2", "high two", "model"),
        ("R3", "Credential file added", "rule"),
        ("R4", "medium one", "model"),
        ("R5", "low one", "model"),
    ]
    assert risks[0] == {
        "id": "R1",
        "title": "high one",
        "severity": "high",
        "category": "correctness",
        "basis": "possible",
        "explanation": "An explanation.",
        "suggested_check": "A check.",
        "evidence_ids": ["E1"],
        "origin": "model",
        "path": None,
    }


def test_overall_risk_is_the_highest_severity_or_none() -> None:
    def level(*severities: Severity) -> str:
        output = ReviewOutput(
            overview="o",
            summary_points=[_point("p", "E1")],
            risks=[_risk(severity, severity) for severity in severities],
        )
        return str(_result(output)["overall_risk"]["level"])

    assert level("low", "medium") == "medium"
    assert level("low") == "low"
    assert level("medium", "high", "low") == "high"
    assert level() == "none"


def test_partial_follows_coverage() -> None:
    output = ReviewOutput(overview="o", summary_points=[_point("p", "E1")])
    files = [_file("app/a.py", added=1500), _file("app/b.py", added=600)]

    partial = _result(output, _select(files))
    whole = _result(output, _select(files, max_changed_lines=5000))

    assert partial["overall_risk"] == {"level": "none", "partial": True}
    assert whole["overall_risk"] == {"level": "none", "partial": False}


def test_summary_points_are_grouped_by_area_of_the_first_cited_path() -> None:
    output = ReviewOutput(
        overview="o",
        summary_points=[
            _point("auth", "E2", "E3"),
            _point("config", "E3"),
            _point("readme", "E4"),
            _point("auth again", "E1"),
            _point("text", "E5"),
        ],
    )

    summary = _result(output)["summary"]

    assert [(group["area"], [p["text"] for p in group["points"]]) for group in summary] == [
        ("app/auth", ["auth", "auth again"]),
        ("backend/src", ["config"]),
        ("(root)", ["readme"]),
        ("app", ["text"]),
    ]
    assert summary[0]["points"][0] == {
        "change": "modified",
        "text": "auth",
        "evidence_ids": ["E2", "E3"],
        "origin": "model",
    }


def test_each_changed_credential_file_gets_a_rule_risk() -> None:
    selection = _select(
        [
            _file("app/main.py"),
            _skipped(".env", "credential_file"),
            _skipped("deploy/server.pem", "credential_file", "modified"),
            _skipped("assets/logo.png", "binary"),
        ]
    )

    risks = rule_risks(selection)

    assert [(risk["path"], risk["title"]) for risk in risks] == [
        (".env", "Credential file added"),
        ("deploy/server.pem", "Credential file changed"),
    ]
    for risk in risks:
        assert (risk["severity"], risk["category"], risk["basis"]) == (
            "high",
            "security",
            "observed",
        )
        assert (risk["origin"], risk["evidence_ids"]) == ("rule", [])
        assert risk["explanation"] and risk["suggested_check"]
        assert "id" not in risk


def test_each_file_renamed_without_changes_gets_a_rule_summary_point() -> None:
    selection = _select([_file("app/main.py"), RENAMED])
    point = {
        "change": "renamed",
        "text": "Renamed `app/text.py` to `app/strings.py` without changes",
        "evidence_ids": [],
        "origin": "rule",
    }

    assert rule_summary_points(selection) == [("app", point)]

    output = ReviewOutput(overview="o", summary_points=[_point("auth", "E1"), _point("text", "E5")])
    summary = _result(output, selection)["summary"]

    assert summary[1] == {"area": "app", "points": [summary[1]["points"][0], point]}

    only_auth = ReviewOutput(overview="o", summary_points=[_point("auth", "E1")])
    summary = _result(only_auth, selection)["summary"]

    assert summary[-1] == {"area": "app", "points": [point]}


def test_a_renamed_file_with_edits_gets_no_rule_point() -> None:
    edited = ChangedFile(
        path="app/strings.py",
        previous_path="app/text.py",
        change="renamed",
        language="python",
        before="",
        after="",
        hunks=(_hunk(1, 1),),
    )

    assert rule_summary_points(_select([edited])) == []


def test_result_shape_and_coverage() -> None:
    selection = _select(
        [
            _file("app/auth/permissions.py", added=3, removed=9),
            _skipped("assets/logo.png", "binary"),
            ChangedFile(
                path="node_modules",
                previous_path=None,
                change="modified",
                entry_type="directory",
                count=3,
                reason="excluded_directory",
            ),
        ]
    )
    output = ReviewOutput(overview="Changes the write check.", summary_points=[_point("p", "E1")])

    result = _result(output, selection, omitted_items=2)

    assert result["overview"] == "Changes the write check."
    assert result["checklist"] == []
    assert result["tests"] == EMPTY_TESTS
    assert result["omitted_items"] == 2
    assert result["coverage"] == {
        "files": [
            {
                "path": "app/auth/permissions.py",
                "previous_path": None,
                "change": "modified",
                "entry_type": "file",
                "count": 1,
                "additions": 3,
                "deletions": 9,
                "reviewed": True,
                "reason": None,
            },
            {
                "path": "assets/logo.png",
                "previous_path": None,
                "change": "added",
                "entry_type": "file",
                "count": 1,
                "additions": None,
                "deletions": None,
                "reviewed": False,
                "reason": "binary",
            },
            {
                "path": "node_modules",
                "previous_path": None,
                "change": "modified",
                "entry_type": "directory",
                "count": 3,
                "additions": None,
                "deletions": None,
                "reviewed": False,
                "reason": "excluded_directory",
            },
        ],
        "changed_files": 5,
        "reviewed_files": 1,
        "changed_lines_reviewed": 12,
        "context_items": 1,
    }


def test_nothing_to_review_has_empty_text_sections_and_only_rule_risks() -> None:
    selection = _select(
        [_skipped(".env", "credential_file"), _skipped("assets/logo.png", "binary")]
    )

    result = nothing_to_review_result(selection, rule_risks(selection))

    assert result["overall_risk"] == {"level": "high", "partial": False}
    assert result["overview"] == ""
    assert result["summary"] == []
    assert result["checklist"] == []
    assert result["tests"] == EMPTY_TESTS
    assert [(risk["id"], risk["origin"], risk["path"]) for risk in result["risks"]] == [
        ("R1", "rule", ".env")
    ]
    assert result["coverage"]["reviewed_files"] == 0
    assert result["coverage"]["changed_files"] == 2
    assert result["coverage"]["context_items"] == 0
    assert result["omitted_items"] == 0

    plain = _select([_skipped("assets/logo.png", "binary")])

    assert nothing_to_review_result(plain, [])["overall_risk"] == {
        "level": "none",
        "partial": False,
    }


def test_checklist_risk_indexes_map_to_risk_ids_after_sorting() -> None:
    selection = _select([_file("app/auth/permissions.py"), _skipped(".env", "credential_file")])
    output = ReviewOutput(
        overview="o",
        summary_points=[_point("p", "E1")],
        risks=[_risk("low one", "low"), _risk("high one", "high")],
        checklist=[
            _check("Check the low one.", risks=[0]),
            _check(
                "Check both.",
                paths=["app/auth/permissions.py", "app/auth/permissions.py"],
                risks=[1, 0, 1],
            ),
            _check("Check the file.", paths=["app/auth/permissions.py", "app/unknown.py"]),
        ],
    )

    result = _result(output, selection)

    # The model's risk 0 ("low one") sorts after its risk 1 and the credential-file rule risk.
    assert [(risk["id"], risk["title"]) for risk in result["risks"]] == [
        ("R1", "high one"),
        ("R2", "Credential file added"),
        ("R3", "low one"),
    ]
    assert result["checklist"] == [
        {"text": "Check the low one.", "paths": [], "risk_ids": ["R3"]},
        {"text": "Check both.", "paths": ["app/auth/permissions.py"], "risk_ids": ["R1", "R3"]},
        {"text": "Check the file.", "paths": ["app/auth/permissions.py"], "risk_ids": []},
    ]


def test_checklist_items_lose_references_to_dropped_risks() -> None:
    output = ReviewOutput(
        overview="o",
        summary_points=[_point("p", "E1")],
        risks=[_risk("unknown label", "high", "E99"), _risk("kept", "medium")],
        checklist=[
            _check("Only the dropped risk.", risks=[0]),
            _check("Both risks.", risks=[0, 1]),
            _check("A file and the dropped risk.", paths=["app/auth/permissions.py"], risks=[0]),
        ],
    )

    cleaned, removed = drop_invalid(
        output,
        labels={item.label for item in EVIDENCE},
        change_labels={item.label for item in EVIDENCE if item.source_type == "change"},
        reviewed_paths={"app/auth/permissions.py"},
    )
    result = _result(cleaned, omitted_items=removed)

    assert [(risk["id"], risk["title"]) for risk in result["risks"]] == [("R1", "kept")]
    assert result["checklist"] == [
        {"text": "Both risks.", "paths": [], "risk_ids": ["R1"]},
        {
            "text": "A file and the dropped risk.",
            "paths": ["app/auth/permissions.py"],
            "risk_ids": [],
        },
    ]
    # The dropped risk, and the item left with no reviewed file and no risk.
    assert result["omitted_items"] == 2


def test_a_checklist_item_left_without_a_valid_reference_is_dropped_and_counted() -> None:
    output = ReviewOutput(
        overview="o",
        summary_points=[_point("p", "E1")],
        checklist=[
            _check("Nothing valid.", paths=["app/unknown.py"], risks=[3]),
            _check("Kept.", paths=["app/auth/permissions.py"]),
        ],
    )

    # Validation rejects such an item first; the result never shows one either way.
    result = _result(output, omitted_items=1)

    assert result["checklist"] == [
        {"text": "Kept.", "paths": ["app/auth/permissions.py"], "risk_ids": []}
    ]
    assert result["omitted_items"] == 2


def test_tests_come_from_the_context_and_new_cases_from_the_model() -> None:
    evidence = [*EVIDENCE, _item("E7", "tests/test_permissions.py", "test")]
    changed = [ChangedTest(path="tests/test_text.py", change="modified")]
    candidates = [
        CandidateTest(
            path="tests/test_permissions.py",
            reason="refers to `can_write`",
            excerpt=CodeExcerpt("tests/test_permissions.py", 1, 1, "x", ("can_write",)),
        ),
        CandidateTest(path="tests/test_other.py", reason="refers to `can_write`", excerpt=None),
    ]
    output = ReviewOutput(
        overview="o",
        summary_points=[_point("p", "E1")],
        new_test_cases=[
            NewTestCase(
                behavior="A viewer is refused on write.",
                location_hint="tests/test_permissions.py",
                evidence_ids=["E2"],
            ),
            NewTestCase(behavior="An owner may write.", location_hint="  ", evidence_ids=["E1"]),
        ],
    )

    result = _result(output, evidence=evidence, changed_tests=changed, candidate_tests=candidates)

    assert result["tests"] == {
        "changed": [{"path": "tests/test_text.py", "change": "modified"}],
        "candidates": [
            {
                "path": "tests/test_permissions.py",
                "reason": "refers to `can_write`",
                "evidence_ids": ["E7"],
            },
            # Below the top 5, or its excerpt did not fit the budget: no citation.
            {"path": "tests/test_other.py", "reason": "refers to `can_write`", "evidence_ids": []},
        ],
        "new_cases": [
            {
                "behavior": "A viewer is refused on write.",
                "location_hint": "tests/test_permissions.py",
                "evidence_ids": ["E2"],
            },
            {"behavior": "An owner may write.", "location_hint": None, "evidence_ids": ["E1"]},
        ],
    }
    assert result["coverage"]["context_items"] == 2


def test_nothing_to_review_still_lists_changed_tests() -> None:
    selection = _select([_skipped("tests/data/logo.png", "binary")])
    changed = [ChangedTest(path="tests/data/logo.png", change="added")]

    result = nothing_to_review_result(selection, [], changed_tests=changed)

    assert result["tests"] == {
        "changed": [{"path": "tests/data/logo.png", "change": "added"}],
        "candidates": [],
        "new_cases": [],
    }
