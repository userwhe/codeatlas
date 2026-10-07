"""Review evidence: labels, sides, exact excerpts, and the input budget (research R6)."""

import hashlib
import io
import tarfile
from collections.abc import Mapping
from typing import IO

from codeatlas.config import Settings
from codeatlas.review.context import CodeExcerpt, candidate_tests
from codeatlas.review.diff import Selection, Tree, changed_files, read_tree, select_for_review
from codeatlas.review.evidence import MAX_LABELS, HunkLabels, ReviewEvidenceSet, build_evidence

TOP = "octo-org-review-app-abc1234"
HEAD_SHA = "8d3f1c2" + "0" * 33
MERGE_BASE_SHA = "1a9e4c2" + "0" * 33
LIMITS = {"max_files": 100, "max_changed_lines": 2000, "max_hunks": 80, "max_diff_tokens": 40_000}


def _archive(files: Mapping[str, bytes]) -> IO[bytes]:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=1) as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(f"{TOP}/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return buffer


def _tree(files: Mapping[str, bytes]) -> Tree:
    return read_tree(_archive(files), Settings(_env_file=None))  # type: ignore[call-arg]


def _lines(count: int, prefix: str = "line") -> list[str]:
    return [f"{prefix} {number}" for number in range(1, count + 1)]


def _text(lines: list[str]) -> bytes:
    return "".join(f"{line}\n" for line in lines).encode()


BASE_A = _lines(30)
HEAD_A = [*BASE_A[:4], "line five", *BASE_A[5:24], *BASE_A[25:]]  # edits 5, removes 25
HEAD_B = ["b = 1", "b = 2", "b = 3"]
BASE_C = _lines(6, "old")
HEAD_C = ["new 1", *BASE_C[1:]]


def _selection() -> Selection:
    base = {"app/a.py": _text(BASE_A), "app/old_c.py": _text(BASE_C)}
    head = {"app/a.py": _text(HEAD_A), "app/b.py": _text(HEAD_B), "app/c.py": _text(HEAD_C)}
    files = changed_files(_tree(head), _tree(base), {"app/c.py": "app/old_c.py"})
    return select_for_review(files, **LIMITS)


def _excerpt(path: str, text: str = "uses can_write here", start: int = 1) -> CodeExcerpt:
    return CodeExcerpt(
        path=path,
        start_line=start,
        end_line=start + text.count("\n"),
        excerpt=text,
        names=("can_write",),
    )


RELATED = [_excerpt("app/repositories.py"), _excerpt("app/service.py", "can_write(user)\nmore")]


def _build(**overrides: object) -> ReviewEvidenceSet:
    arguments: dict[str, object] = {
        "head_sha": HEAD_SHA,
        "merge_base_sha": MERGE_BASE_SHA,
        "max_input_tokens": 48_000,
    }
    arguments.update(overrides)
    selection = arguments.pop("selection", None) or _selection()
    related = arguments.pop("related", RELATED)
    return build_evidence(selection, related, **arguments)  # type: ignore[arg-type]


def test_labels_run_change_hunks_first_then_related_code() -> None:
    evidence = _build()

    summary = [(item.label, item.source_type, item.side, item.path) for item in evidence.items]
    assert summary == [
        ("E1", "change", "after", "app/a.py"),
        ("E2", "change", "before", "app/a.py"),
        ("E3", "change", "before", "app/a.py"),
        ("E4", "change", "after", "app/b.py"),
        ("E5", "change", "after", "app/c.py"),
        ("E6", "change", "before", "app/old_c.py"),  # the merge base's path of a renamed file
        ("E7", "reference", "after", "app/repositories.py"),
        ("E8", "reference", "after", "app/service.py"),
    ]
    assert [item.rank for item in evidence.items] == list(range(1, 9))


def test_each_hunk_gets_its_labels() -> None:
    evidence = _build()

    assert evidence.hunk_labels == {
        ("app/a.py", 0): HunkLabels(before="E2", after="E1"),
        ("app/a.py", 1): HunkLabels(before="E3", after=None),  # removes a line, adds none
        ("app/b.py", 0): HunkLabels(before=None, after="E4"),
        ("app/c.py", 0): HunkLabels(before="E6", after="E5"),
    }
    assert evidence.change_labels == frozenset({"E1", "E2", "E3", "E4", "E5", "E6"})
    assert evidence.labels == frozenset(f"E{n}" for n in range(1, 9))
    assert evidence.context_items == 2


def test_sides_name_their_commit_and_excerpts_are_the_exact_lines() -> None:
    items = {item.label: item for item in _build().items}

    assert {items[label].commit_sha for label in ("E1", "E4", "E5", "E7")} == {HEAD_SHA}
    assert {items[label].commit_sha for label in ("E2", "E3", "E6")} == {MERGE_BASE_SHA}

    expected = {
        "E1": (2, 8, HEAD_A),
        "E2": (2, 8, BASE_A),
        "E3": (22, 28, BASE_A),
        "E4": (1, 3, HEAD_B),
        "E5": (1, 4, HEAD_C),
        "E6": (1, 4, BASE_C),
    }
    for label, (start, end, lines) in expected.items():
        item = items[label]
        assert (item.start_line, item.end_line) == (start, end), label
        assert item.excerpt == "\n".join(lines[start - 1 : end]), label
        assert item.excerpt_sha256 == hashlib.sha256(item.excerpt.encode()).digest()

    reference = items["E8"]
    assert (reference.start_line, reference.end_line) == (1, 2)
    assert reference.excerpt == "can_write(user)\nmore"
    assert reference.excerpt_sha256 == hashlib.sha256(b"can_write(user)\nmore").digest()


def test_related_code_is_dropped_lowest_rank_first_and_change_items_never() -> None:
    selection = _selection()
    first_only = selection.diff_tokens + len(RELATED[0].excerpt) / 3.5 + 1

    kept = _build(selection=selection, max_input_tokens=first_only)

    assert [item.path for item in kept.items if item.source_type == "reference"] == [
        "app/repositories.py"
    ]
    assert kept.items[-1].label == "E7"

    none = _build(selection=selection, max_input_tokens=0)

    assert [item.source_type for item in none.items] == ["change"] * 6
    assert none.context_items == 0


def test_test_items_are_labeled_after_related_code_and_dropped_first() -> None:
    selection = _selection()
    tests = [_excerpt("tests/test_permissions.py", "assert can_write(owner, 7)")]

    evidence = _build(selection=selection, tests=tests)

    assert [(item.label, item.source_type, item.side) for item in evidence.items[-3:]] == [
        ("E7", "reference", "after"),
        ("E8", "reference", "after"),
        ("E9", "test", "after"),
    ]

    related_only = selection.diff_tokens + sum(len(item.excerpt) for item in RELATED) / 3.5 + 1
    trimmed = _build(selection=selection, tests=tests, max_input_tokens=related_only)

    assert [item.source_type for item in trimmed.items[-2:]] == ["reference", "reference"]
    assert len(trimmed.items) == 8


def test_label_count_with_full_hunks_related_code_and_tests() -> None:
    base = _lines(800)
    head = [f"changed {n}" if n % 10 == 5 else line for n, line in enumerate(base, start=1)]
    files = changed_files(
        _tree({"app/big.py": _text(head)}), _tree({"app/big.py": _text(base)}), {}
    )
    selection = select_for_review(files, **LIMITS)
    assert sum(len(file.hunks) for file in selection.reviewed) == 80

    related = [_excerpt(f"app/related_{index}.py") for index in range(8)]
    tests = [_excerpt(f"tests/test_{index}.py") for index in range(5)]
    evidence = _build(selection=selection, related=related, tests=tests)

    assert len(evidence.items) == 173
    assert len(evidence.items) <= MAX_LABELS
    assert [item.label for item in evidence.items] == [f"E{n}" for n in range(1, 174)]


def test_a_file_renamed_without_changes_has_no_change_items() -> None:
    content = _text(BASE_C)
    files = changed_files(_tree({"app/c.py": content}), _tree({"app/old_c.py": content}), {})
    selection = select_for_review(files, **LIMITS)

    evidence = _build(selection=selection, related=[])

    assert selection.reviewed[0].change == "renamed"
    assert evidence.items == ()
    assert evidence.hunk_labels == {}


def test_the_top_5_candidate_tests_give_test_items() -> None:
    head = _tree({f"tests/test_{index}.py": b"assert can_write(owner, 7)\n" for index in range(7)})
    candidates = candidate_tests(head, [], ["can_write"], [])
    assert len(candidates) == 7

    evidence = _build(
        tests=[candidate.excerpt for candidate in candidates if candidate.excerpt is not None]
    )

    tests = [item for item in evidence.items if item.source_type == "test"]
    assert [item.path for item in tests] == [f"tests/test_{index}.py" for index in range(5)]
    assert {(item.side, item.commit_sha) for item in tests} == {("after", HEAD_SHA)}
    assert [item.label for item in tests] == ["E9", "E10", "E11", "E12", "E13"]
    assert tests[0].excerpt == "assert can_write(owner, 7)"
    assert evidence.context_items == 7  # 2 related-code items and 5 test items
