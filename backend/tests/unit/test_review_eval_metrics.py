"""Pure scoring and set-validation functions of the pull request review evaluation (no database,
no network)."""

import hashlib
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest

from codeatlas.config import get_settings
from evals import run_review_eval
from evals.run_qa_eval import Rate, load_questions
from evals.run_review_eval import (
    CATEGORIES,
    DEFAULT_SET,
    EXIT_FAIL,
    EXIT_PASS,
    FIXTURE_REPOS_DIR,
    OVERLAYS_DIRNAME,
    Archive,
    CitationCheck,
    Defect,
    Edit,
    EvidenceRef,
    Item,
    ItemResult,
    Kind,
    Labels,
    Overlay,
    RiskRecord,
    apply_overlay,
    audit_rows,
    audit_sample,
    changed_paths,
    checklist_problem,
    citation_problem,
    cited_labels,
    composition,
    defect_found,
    defect_problem,
    exit_code,
    head_sha,
    level_problem,
    load_items,
    main,
    parse_item,
    parse_labels,
    parse_overlay,
    prepare,
    read_archive,
    select_items,
    set_digest,
    summarize,
    target_checks,
    write_archive,
)

SHA = "a" * 40
QA_SET = Path(__file__).resolve().parents[2] / "evals" / "qa_v1.jsonl"


def defect(**changes: Any) -> Defect:
    values: dict[str, Any] = {
        "path": "app/auth.py",
        "side": "after",
        "start_line": 10,
        "end_line": 12,
        "category": "security",
        "description": "The role check is gone.",
    } | changes
    return Defect(**values)


def item(
    item_id: str = "x-s01", kind: Kind = "seeded", defects: tuple[Defect, ...] | None = None
) -> Item:
    if defects is None:
        defects = () if kind == "safe" else (defect(),)
    labels = Labels(kind, defects, "x-s01" if kind == "injection" else None)
    overlay = Overlay(edits=(Edit("app/auth.py", "a", "b"),))
    return Item(item_id, "o/r", SHA, "Title", "", "x-s01", overlay, labels)


def risk(
    risk_id: str = "R1",
    severity: str = "high",
    labels: tuple[str, ...] = ("E1",),
    origin: str = "model",
) -> RiskRecord:
    return RiskRecord(
        risk_id, severity, "security", "observed", "Title", "Why.", "Check.", labels, origin
    )


def ref(label: str = "E1", side: str = "after", start: int = 8, end: int = 11) -> EvidenceRef:
    return EvidenceRef(label, side, "app/auth.py", start, end)


def record(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "x-s01",
        "repository": "o/r",
        "commit_sha": SHA,
        "title": "Simplify write checks",
        "body": "",
        "overlay": "x-s01",
        "labels": {
            "kind": "seeded",
            "defects": [
                {
                    "path": "app/auth.py",
                    "side": "before",
                    "start_line": 3,
                    "end_line": 3,
                    "category": "security",
                    "description": "The role check is removed.",
                }
            ],
        },
    }
    return base | changes


def write_overlay(directory: Path, name: str, overlay: dict[str, Any]) -> None:
    (directory / name).mkdir(parents=True, exist_ok=True)
    (directory / name / "overlay.json").write_text(json.dumps(overlay))


# The evaluation set -----------------------------------------------------------------------------


def test_parse_item_reads_the_record_and_its_overlay(tmp_path: Path) -> None:
    write_overlay(
        tmp_path,
        "x-s01",
        {
            "edits": [{"path": "app/auth.py", "find": ["a", "b"], "replace": "c"}],
            "add": {"tests/test_auth.py": ["def test_x() -> None:", "    pass"]},
            "remove": ["old.py"],
            "rename": {"a.py": "b.py"},
        },
    )

    parsed = parse_item(record(), "line 1", tmp_path)

    assert parsed.overlay.edits == (Edit("app/auth.py", "a\nb", "c"),)
    assert parsed.overlay.add == {"tests/test_auth.py": "def test_x() -> None:\n    pass\n"}
    assert parsed.overlay.remove == ("old.py",)
    assert parsed.overlay.rename == {"a.py": "b.py"}
    assert parsed.labels.defects[0] == Defect(
        "app/auth.py", "before", 3, 3, "security", "The role check is removed."
    )
    assert not parsed.fixture


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"repository": "repo"}, "repository"),
        ({"repository": "fixture:a/b"}, "repository"),
        ({"commit_sha": "abc"}, "commit_sha"),
        ({"title": " "}, "title"),
        ({"overlay": "../x"}, "overlay"),
        ({"overlay": "missing"}, "has no overlay"),
        ({"extra": 1}, "exactly the keys"),
        ({"labels": {"kind": "risky"}}, "labels.kind"),
        ({"labels": {"kind": "safe", "defects": []}}, "safe labels"),
        ({"labels": {"kind": "seeded", "defects": []}}, "at least one defect"),
        ({"labels": {"kind": "injection", "defects": [{}]}}, "exactly the keys"),
    ],
)
def test_parse_item_rejects_invalid_records(
    tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    write_overlay(tmp_path, "x-s01", {"remove": ["a.py"]})

    with pytest.raises(ValueError, match=message):
        parse_item(record(**changes), "line 1", tmp_path)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"side": "head"}, "side"),
        ({"category": "style"}, "category"),
        ({"start_line": 0}, "start <= end"),
        ({"start_line": 5, "end_line": 4}, "start <= end"),
        ({"path": "/abs.py"}, "relative"),
        ({"path": "a/../b.py"}, "segment"),
        ({"description": ""}, "description"),
    ],
)
def test_parse_labels_rejects_invalid_defects(changes: dict[str, Any], message: str) -> None:
    labels = record()["labels"]
    labels["defects"][0] |= changes

    with pytest.raises(ValueError, match=message):
        parse_labels(labels, "line 1")


def test_injection_labels_name_their_seeded_item() -> None:
    labels = record()["labels"] | {"kind": "injection", "variant_of": "x-s01"}

    assert parse_labels(labels, "line 1").variant_of == "x-s01"
    with pytest.raises(ValueError, match="exactly the keys"):
        parse_labels(record()["labels"] | {"kind": "injection"}, "line 1")


@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        ({}, "changes nothing"),
        ({"patch": "x"}, "keys from"),
        ({"edits": [{"path": "a.py", "find": "", "replace": "x"}]}, "must not be empty"),
        ({"edits": [{"path": "a.py", "find": "x", "replace": "x"}]}, "replace equals find"),
        ({"edits": [{"path": "a.py", "find": 3, "replace": "x"}]}, "list of lines"),
        ({"remove": "a.py"}, "list of paths"),
    ],
)
def test_parse_overlay_rejects_invalid_overlays(overlay: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_overlay(overlay, "overlay.json")


def test_load_items_checks_ids_and_variants(tmp_path: Path) -> None:
    write_overlay(tmp_path / OVERLAYS_DIRNAME, "x-s01", {"remove": ["a.py"]})
    path = tmp_path / "set.jsonl"
    variant = record(id="x-i01") | {
        "labels": record()["labels"] | {"kind": "injection", "variant_of": "x-s01"}
    }
    path.write_text(json.dumps(record()) + "\n\n" + json.dumps(variant) + "\n")

    items = load_items(path)

    assert [i.id for i in items] == ["x-s01", "x-i01"]
    path.write_text(json.dumps(record()) + "\n" + json.dumps(record()) + "\n")
    with pytest.raises(ValueError, match="duplicate id"):
        load_items(path)
    orphan = variant | {"labels": variant["labels"] | {"variant_of": "x-s99"}}
    path.write_text(json.dumps(record()) + "\n" + json.dumps(orphan) + "\n")
    with pytest.raises(ValueError, match="not a seeded item"):
        load_items(path)
    moved = variant | {"commit_sha": "b" * 40}
    path.write_text(json.dumps(record()) + "\n" + json.dumps(moved) + "\n")
    with pytest.raises(ValueError, match="base of x-s01"):
        load_items(path)


def test_select_items_splits_fixture_and_pinned_items() -> None:
    pinned = item("p1")
    fixture = Item("f1", "fixture:review-app", SHA, "T", "", "f1", pinned.overlay, pinned.labels)
    items = [pinned, fixture, item("p2")]

    assert [i.id for i in select_items(items, fixtures=False, limit=None)] == ["p1", "p2"]
    assert [i.id for i in select_items(items, fixtures=False, limit=1)] == ["p1"]
    assert [i.id for i in select_items(items, fixtures=True, limit=None)] == ["f1"]


# Overlays and archives --------------------------------------------------------------------------


def test_apply_overlay_renames_edits_adds_and_removes_in_order() -> None:
    files = {"a.py": b"x = 1\ny = 2\n", "old.py": b"z = 3\n", "gone.py": b""}
    overlay = Overlay(
        edits=(Edit("new.py", "z = 3", "z = 4"), Edit("a.py", "y = 2", "y = 5")),
        add={"tests/test_a.py": "def test_a() -> None:\n    pass\n"},
        remove=("gone.py",),
        rename={"old.py": "new.py"},
    )

    head = apply_overlay(files, overlay)

    assert head == {
        "a.py": b"x = 1\ny = 5\n",
        "new.py": b"z = 4\n",
        "tests/test_a.py": b"def test_a() -> None:\n    pass\n",
    }
    assert files["a.py"] == b"x = 1\ny = 2\n"  # the base is not modified


@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        (Overlay(edits=(Edit("a.py", "y", "z"),)), "matches 0 times"),
        (Overlay(edits=(Edit("a.py", "x", "z"),)), "matches 2 times"),
        (Overlay(edits=(Edit("b.py", "x", "z"),)), "no such regular file"),
        (Overlay(edits=(Edit("bin.dat", "x", "z"),)), "not UTF-8"),
        (Overlay(add={"a.py": "x"}), "already exists"),
        (Overlay(remove=("b.py",)), "not a regular file"),
        (Overlay(rename={"b.py": "c.py"}), "not a regular file"),
        (Overlay(rename={"a.py": "bin.dat"}), "already exists"),
    ],
)
def test_apply_overlay_rejects_edits_that_do_not_apply_exactly(
    overlay: Overlay, message: str
) -> None:
    files = {"a.py": b"x = 1\nx = 2\n", "bin.dat": b"\xff\xfe x"}

    with pytest.raises(ValueError, match=message):
        apply_overlay(files, overlay)


def test_archives_round_trip_with_links_kept() -> None:
    link = tarfile.TarInfo("repo-abc/README.rst")
    link.type, link.linkname = tarfile.SYMTYPE, "docs/index.rst"
    archive = Archive("repo-abc", {"docs/index.rst": b"Docs\n", "a.py": b"x = 1\n"}, (link,))

    restored = read_archive(write_archive(archive))

    assert restored.top == "repo-abc"
    assert restored.files == archive.files
    assert [(m.name, m.linkname) for m in restored.others] == [
        ("repo-abc/README.rst", "docs/index.rst")
    ]


def test_set_digest_covers_the_overlays(tmp_path: Path) -> None:
    write_overlay(tmp_path / OVERLAYS_DIRNAME, "x-s01", {"remove": ["a.py"]})
    path = tmp_path / "set.jsonl"
    path.write_text(json.dumps(record()) + "\n")
    items = load_items(path)
    before = set_digest(path, items)

    write_overlay(tmp_path / OVERLAYS_DIRNAME, "x-s01", {"remove": ["b.py"]})

    assert set_digest(path, items) != before


def test_head_sha_is_stable_and_synthetic() -> None:
    assert head_sha("x-s01") == head_sha("x-s01") != head_sha("x-s02")
    assert len(head_sha("x-s01")) == 40


# Labeled ranges ---------------------------------------------------------------------------------


CONTENT = "".join(f"line {n}\n" for n in range(1, 21))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"changed_lines": {11}}, None),
        ({"changed_lines": {9, 13}}, "holds no line that the change adds"),
        ({"changed_lines": None}, "not among the reviewed files"),
        ({"content": None, "changed_lines": {11}}, "not an eligible text file"),
        ({"content": "a\nb\n", "changed_lines": {11}}, "outside the file's 2 lines"),
    ],
)
def test_defect_problem(kwargs: dict[str, Any], message: str | None) -> None:
    values: dict[str, Any] = {"content": CONTENT} | kwargs
    found = defect_problem(defect(), **values)

    assert found is None if message is None else message in (found or "")


def test_defect_problem_names_the_removed_side() -> None:
    found = defect_problem(defect(side="before"), content=CONTENT, changed_lines={1})

    assert found is not None and "removes" in found


def _tree_archive(files: dict[str, bytes]) -> tuple[Archive, bytes]:
    archive = Archive("o-r-aaaaaaa", files)
    data = write_archive(archive)
    return read_archive(data), data


def test_prepare_builds_both_trees_and_checks_labels() -> None:
    base = {
        "app/auth.py": b"def can_write(user):\n    return user.role == 'owner' and user.listed\n",
        "README.md": b"# App\n",
    }
    archive, data = _tree_archive(base)
    overlay = Overlay(
        edits=(Edit("app/auth.py", "user.role == 'owner' and user.listed", "user.listed"),)
    )
    removed = Defect("app/auth.py", "before", 2, 2, "security", "Role check removed.")
    seeded = Item("x-s01", "o/r", SHA, "T", "", "x-s01", overlay, Labels("seeded", (removed,)))

    prepared = prepare(seeded, archive, data, get_settings())

    assert prepared.head.files["app/auth.py"].content.endswith("return user.listed\n")
    assert "app/auth.py" in prepared.base.files
    assert "README.md" not in prepared.base.files  # unchanged at the head, as in the job
    assert prepared.merge_base_sha == SHA
    assert prepared.head_sha == head_sha("x-s01")

    unreachable = Defect("README.md", "after", 1, 1, "other", "Unchanged file.")
    wrong = Item("x-s02", "o/r", SHA, "T", "", "x-s01", overlay, Labels("seeded", (unreachable,)))
    with pytest.raises(ValueError, match="x-s02: defect after README.md:1-1: README.md is not"):
        prepare(wrong, archive, data, get_settings())
    broken = Item(
        "x-s03",
        "o/r",
        SHA,
        "T",
        "",
        "x-s03",
        Overlay(edits=(Edit("app/auth.py", "missing", "x"),)),
        Labels("safe"),
    )
    with pytest.raises(ValueError, match="x-s03: x-s03: edit 1"):
        prepare(broken, archive, data, get_settings())


# Scoring ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("risks", "found"),
    [
        ([risk(severity="high")], True),
        ([risk(severity="medium")], True),
        ([risk(severity="low")], False),  # low risks do not count
        ([risk(labels=("E2",))], False),  # E2 is on the other side
        ([risk(labels=("E3",))], False),  # E3 is in another file
        ([risk(labels=("E4",))], False),  # E4 ends on the line before the range
        ([risk(labels=("E9",))], False),  # unknown label
        ([risk(severity="low"), risk("R2", "medium", ("E9", "E1"))], True),
    ],
)
def test_defect_found(risks: list[RiskRecord], found: bool) -> None:
    evidence = {
        "E1": ref("E1", "after", 12, 20),
        "E2": ref("E2", "before", 10, 12),
        "E3": EvidenceRef("E3", "after", "app/other.py", 10, 12),
        "E4": ref("E4", "after", 1, 9),
    }

    assert defect_found(defect(), risks, evidence) is found


def test_cited_labels_lists_each_shown_label_once() -> None:
    result = {
        "summary": [{"area": "app", "points": [{"evidence_ids": ["E1", "E2"]}]}],
        "risks": [{"evidence_ids": ["E2", "E3"]}, {"evidence_ids": []}],
        "tests": {
            "candidates": [{"evidence_ids": ["E9"]}],
            "new_cases": [{"evidence_ids": ["E1"]}],
        },
    }

    assert cited_labels(result) == ["E1", "E2", "E3", "E9"]
    assert cited_labels({"summary": [], "risks": [], "checklist": []}) == []


def citation(**changes: Any) -> str | None:
    excerpt = "second\nthird"
    values: dict[str, Any] = {
        "commit_sha": SHA,
        "expected_commit_sha": SHA,
        "content": "first\nsecond\nthird\n",
        "start_line": 2,
        "end_line": 3,
        "excerpt": excerpt,
        "excerpt_sha256": hashlib.sha256(excerpt.encode()).digest(),
    } | changes
    return citation_problem(**values)


def test_citation_matching_its_side_is_valid() -> None:
    assert citation() is None


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"commit_sha": "b" * 40}, "not its side's"),
        ({"content": None}, "not on the cited side"),
        ({"end_line": 4}, "outside the file's 3 lines"),
        ({"excerpt": "second\nTHIRD"}, "differs"),
        ({"excerpt_sha256": b"\0" * 32}, "checksum"),
    ],
)
def test_citation_problems(changes: dict[str, Any], reason: str) -> None:
    found = citation(**changes)

    assert found is not None and reason in found


def test_changed_paths_include_previous_paths() -> None:
    result = {
        "coverage": {
            "files": [
                {"path": "app/new.py", "previous_path": "app/old.py"},
                {"path": "app/b.py", "previous_path": None},
            ]
        }
    }

    assert changed_paths(result) == {"app/new.py", "app/old.py", "app/b.py"}


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ({"text": "Check", "paths": ["app/a.py"], "risk_ids": []}, None),
        ({"text": "Check", "paths": [], "risk_ids": ["R1"]}, None),
        ({"text": "Check", "paths": ["app/other.py"], "risk_ids": ["R1"]}, None),
        ({"text": "Check", "paths": ["app/other.py"], "risk_ids": []}, "refers to no changed"),
        ({"text": "Check"}, "refers to no changed"),
        ({"text": "Check", "paths": ["app/a.py"], "risk_ids": ["R7"]}, "not listed: R7"),
    ],
)
def test_checklist_problem(entry: dict[str, Any], problem: str | None) -> None:
    found = checklist_problem(entry, changed={"app/a.py"}, risk_ids={"R1", "R2"})

    assert found is None if problem is None else problem in (found or "")


def review_result(level: str, severities: list[str], partial: bool = False) -> dict[str, Any]:
    reason = "review_limit" if partial else None
    return {
        "overall_risk": {"level": level, "partial": partial},
        "risks": [{"severity": severity} for severity in severities],
        "coverage": {
            "files": [{"path": "a.py", "reason": None}, {"path": "b.py", "reason": reason}]
        },
    }


@pytest.mark.parametrize(
    ("result", "problem"),
    [
        (review_result("high", ["high", "low"]), None),
        (review_result("medium", ["low", "medium"]), None),
        (review_result("none", []), None),
        (review_result("low", ["low"], partial=True), None),
        (review_result("low", ["high", "low"]), "most severe risk is high"),
        (review_result("low", []), "most severe risk is none"),
        (
            {**review_result("low", ["low"]), "overall_risk": {"level": "low", "partial": True}},
            "partial is True",
        ),
    ],
)
def test_level_problem(result: dict[str, Any], problem: str | None) -> None:
    found = level_problem(result)

    assert found is None if problem is None else problem in (found or "")


def check(label: str = "E1", problem: str | None = None) -> CitationCheck:
    return CitationCheck(ref(label), label, "line 8\nline 9", problem)


def test_summary_denominators_exclude_execution_failures() -> None:
    two = (defect(), defect(start_line=1, end_line=2))
    results = [
        ItemResult(item("s1", defects=two), "reviewed", "high", [risk()], [True, False],
                   [check("E1"), check("E2", "differs")], 2, ["item 2: refers to nothing"]),
        ItemResult(item("s2"), "reviewed", "low", [risk(severity="low")], [False], [check()]),
        ItemResult(item("s3"), failures=["provider_unavailable: down"]),
        ItemResult(item("safe1", "safe"), "reviewed", "high", [risk()], [], [check()], 1,
                   level_problem="overall level low but the most severe risk is high"),
        ItemResult(item("safe2", "safe"), "reviewed", "medium", [risk(severity="medium")]),
        ItemResult(item("i1", "injection"), "reviewed", "high", [risk()], [True]),
    ]  # fmt: skip

    summary = summarize(results)

    assert summary.items == 6
    assert summary.recall == Rate(1, 3)
    assert summary.safe_with_high == 1 and summary.safe_reviews == 2
    assert summary.citation_validity == Rate(3, 4)
    assert summary.checklist_validity == Rate(2, 3)
    assert summary.level_consistency == Rate(4, 5)
    assert summary.injection_recall == Rate(1, 1)
    assert summary.execution_failures == 1
    assert exit_code(summary) == EXIT_FAIL


def test_targets_pass_and_unmeasured_targets_do_not_fail() -> None:
    passing = summarize(
        [
            ItemResult(item("s1"), "reviewed", "high", [risk()], [True], [check()]),
            ItemResult(item("i1", "injection"), "reviewed", "high", [risk()], [True]),
        ]
    )

    verdicts = dict(target_checks(passing))

    assert verdicts["SC-002 seeded-defect recall"] is True
    assert verdicts["SC-003 safe items with a high risk"] is None  # no safe item ran
    assert verdicts["SC-005 checklist references"] is None  # no checklist items
    assert exit_code(passing) == EXIT_PASS


def test_recall_below_the_target_fails() -> None:
    results = [ItemResult(item(f"s{n}"), "reviewed", "high", [risk()], [n < 7]) for n in range(10)]

    assert summarize(results).recall == Rate(7, 10)
    assert exit_code(summarize(results)) == EXIT_PASS
    results[0].found = [False]
    assert exit_code(summarize(results)) == EXIT_FAIL


# Audit sheet ------------------------------------------------------------------------------------


def reviewed_with_risks(count: int, risks_each: int) -> list[ItemResult]:
    return [
        ItemResult(
            item(f"s{n:02d}"),
            "reviewed",
            "high",
            [risk(f"R{r}", "medium") for r in range(1, risks_each + 1)],
        )
        for n in range(count)
    ]


def test_audit_sample_reaches_thirty_risks_from_ten_reviews() -> None:
    results = reviewed_with_risks(12, 5)

    sample = audit_sample(results)

    assert len(sample) == 30
    assert len({id(result) for result, _ in sample}) >= 10
    assert sample == audit_sample(results)  # reproducible
    order = [(results.index(result), int(r.id[1:])) for result, r in sample]
    assert order == sorted(order)


def test_audit_sample_keeps_going_until_ten_reviews_are_covered() -> None:
    results = reviewed_with_risks(3, 20) + reviewed_with_risks(9, 1)

    sample = audit_sample(results)

    assert len({id(result) for result, _ in sample}) >= 10
    assert len(sample) >= 30


def test_audit_sample_takes_every_model_risk_when_there_are_few() -> None:
    results = reviewed_with_risks(3, 2)
    results[0].risks.append(risk("R3", "high", (), origin="rule"))
    results.append(ItemResult(item("failed"), failures=["boom"]))

    sample = audit_sample(results)

    assert len(sample) == 6
    assert all(r.origin == "model" for _, r in sample)


def test_audit_rows_show_the_cited_excerpts_with_empty_judgment_columns() -> None:
    result = ItemResult(
        item(),
        "reviewed",
        "high",
        [risk(labels=("E1", "E2"))],
        [True],
        [check("E1"), CitationCheck(ref("E2", "before", 3, 3), "E2", "x = 1", None)],
    )

    rows = audit_rows([(result, result.risks[0])])

    assert rows[0]["cited_evidence"] == "E1 after app/auth.py:8-11; E2 before app/auth.py:3-3"
    assert rows[0]["cited_excerpts"] == (
        "[E1 after app/auth.py:8-11]\nline 8\nline 9\n\n[E2 before app/auth.py:3-3]\nx = 1"
    )
    assert rows[0]["correctly_explained"] == rows[0]["reviewer_notes"] == ""


# The shipped set --------------------------------------------------------------------------------


def test_shipped_set_meets_the_task_requirements() -> None:
    items = load_items(DEFAULT_SET)
    pinned = [i for i in items if not i.fixture]
    counts = composition(pinned)
    seeded_categories = {d.category for i in pinned if i.labels.kind == "seeded"
                         for d in i.labels.defects}  # fmt: skip
    qa_bases = {(q.repository, q.commit_sha) for q in load_questions(QA_SET)}

    assert counts["seeded"]["items"] >= 20
    assert seeded_categories == set(CATEGORIES)
    assert counts["safe"]["items"] >= 5
    assert counts["injection"]["items"] >= 5
    assert {(i.repository, i.commit_sha) for i in pinned} <= qa_bases
    assert all(i.labels.defects for i in items if i.labels.kind != "safe")


def test_shipped_set_has_offline_fixture_items_and_no_unused_overlays() -> None:
    items = load_items(DEFAULT_SET)
    fixtures = [i for i in items if i.fixture]
    overlays_dir = DEFAULT_SET.parent / OVERLAYS_DIRNAME

    assert len(fixtures) >= 2
    assert {i.labels.kind for i in fixtures} == {"seeded", "safe", "injection"}
    assert all((FIXTURE_REPOS_DIR / i.repository.split(":", 1)[1]).is_dir() for i in fixtures)
    # Overlays stay while any shipped version uses them: version 1 is kept because its numbers
    # were reported, and it still uses an overlay that version 2 dropped.
    versions = sorted(DEFAULT_SET.parent.glob("review_v*.jsonl"))
    used = {i.overlay_name for version in versions for i in load_items(version)}
    assert {p.name for p in overlays_dir.iterdir() if p.is_dir()} == used
    assert {i.overlay_name for i in items} <= used


@pytest.fixture
def quiet_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` reconfigures the root logger for a command-line run; keep the test session's."""
    monkeypatch.setattr(run_review_eval, "configure_logging", lambda level: None)


@pytest.mark.usefixtures("quiet_main")
def test_fixture_smoke_run_passes_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "fake_review_model_mode", "ok")

    code = main(["--fixtures", "--out", str(tmp_path)])

    reports = list(tmp_path.glob("review-eval-*.md"))
    sheets = list(tmp_path.glob("review-eval-*-audit.csv"))
    assert code == EXIT_PASS
    assert len(reports) == 1 and len(sheets) == 1
    assert "| SC-002 seeded defects found by a medium or high risk | 100.0% (1/1)" in (
        reports[0].read_text()
    )


@pytest.mark.usefixtures("quiet_main")
def test_setup_problems_exit_with_2(tmp_path: Path) -> None:
    broken = tmp_path / "broken.jsonl"
    broken.write_text("{not json\n")

    assert main(["--set", str(tmp_path / "missing.jsonl"), "--fixtures"]) == 2
    assert main(["--set", str(broken), "--fixtures"]) == 2


def test_read_archive_rejects_other_data() -> None:
    with pytest.raises(tarfile.ReadError):
        read_archive(b"not an archive")
