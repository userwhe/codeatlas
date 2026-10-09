"""Changed files, hunks, coverage, and selection under the review limits (research R4)."""

import hashlib
import io
import tarfile
from collections.abc import Callable, Mapping
from typing import IO

import pytest

from codeatlas.config import Settings
from codeatlas.ingestion.extract import LimitExceeded
from codeatlas.review.diff import (
    ChangedFile,
    Hunk,
    Tree,
    changed_files,
    is_test_path,
    read_tree,
    select_for_review,
)

TOP = "octo-org-review-app-abc1234"
LIMITS = {"max_files": 100, "max_changed_lines": 2000, "max_hunks": 80, "max_diff_tokens": 40_000}
TEXT = b'"""Text helpers."""\n\n\ndef slug(name: str) -> str:\n    return name.lower()\n'
SECRET = b"API_TOKEN=review-diff-not-a-secret\n"


def _settings(**overrides: int) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _archive(files: Mapping[str, bytes]) -> IO[bytes]:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=1) as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(f"{TOP}/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return buffer


def _tree(
    files: Mapping[str, bytes],
    *,
    skip: Callable[[str, bytes], bool] | None = None,
    settings: Settings | None = None,
) -> Tree:
    return read_tree(_archive(files), settings or _settings(), skip=skip)


def _diff(
    base: Mapping[str, bytes], head: Mapping[str, bytes], hints: Mapping[str, str] | None = None
) -> dict[str, ChangedFile]:
    head_tree = _tree(head)
    base_tree = _tree(base, skip=lambda path, digest: head_tree.hashes.get(path) == digest)
    return {file.path: file for file in changed_files(head_tree, base_tree, hints or {})}


def _lines(count: int, prefix: str = "line") -> bytes:
    return "".join(f"{prefix} {number}\n" for number in range(1, count + 1)).encode()


# read_tree -----------------------------------------------------------------------------------


def test_read_tree_keeps_every_hash_and_filter_outcome() -> None:
    files = {
        "app/main.py": b"print('hi')\n",
        ".env": b"API_TOKEN=not-a-secret\n",
        "assets/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00",
        "node_modules/left-pad/index.js": b"module.exports = 1;\n",
    }

    tree = _tree(files)

    assert tree.hashes == {path: hashlib.sha256(data).digest() for path, data in files.items()}
    assert set(tree.files) == {"app/main.py"}
    assert tree.files["app/main.py"].content == "print('hi')\n"
    assert tree.skipped[".env"].reason == "credential_file"
    assert tree.skipped["assets/logo.png"].reason == "binary"
    assert tree.skipped["node_modules"].entry_type == "directory"


def test_skip_drops_unchanged_members_before_filtering_but_keeps_root_gitattributes() -> None:
    attributes = b"old_data.py linguist-generated\n"
    head = _tree({".gitattributes": attributes, "app/main.py": b"x = 1\n"})
    seen: list[str] = []

    def skip(path: str, digest: bytes) -> bool:
        seen.append(path)
        return head.hashes.get(path) == digest

    base = _tree(
        {".gitattributes": attributes, "app/main.py": b"x = 1\n", "old_data.py": b"y = 2\n"},
        skip=skip,
    )

    assert set(base.hashes) == {".gitattributes", "app/main.py", "old_data.py"}
    assert "app/main.py" not in base.files  # dropped before filtering
    assert ".gitattributes" in base.files  # never dropped, though unchanged
    assert ".gitattributes" not in seen
    assert base.skipped["old_data.py"].reason == "generated"

    removed = changed_files(head, base, {})
    assert [(file.path, file.change, file.reason) for file in removed] == [
        ("old_data.py", "removed", "generated")
    ]


def test_read_tree_raises_over_the_snapshot_limits() -> None:
    files = {f"app/module_{index}.py": b"x = 1\n" for index in range(3)}

    with pytest.raises(LimitExceeded) as raised:
        _tree(files, settings=_settings(max_files_per_snapshot=2))

    assert raised.value.limit_name == "max_files_per_snapshot"


# changed_files -------------------------------------------------------------------------------


def test_added_modified_and_removed_paths() -> None:
    base = {"app/keep.py": b"x = 1\n", "app/edit.py": b"a = 1\n", "app/gone.py": b"g = 1\n"}
    head = {"app/keep.py": b"x = 1\n", "app/edit.py": b"a = 2\n", "app/new.py": b"n = 1\n"}

    changes = _diff(base, head)

    assert {path: file.change for path, file in changes.items()} == {
        "app/edit.py": "modified",
        "app/gone.py": "removed",
        "app/new.py": "added",
    }
    assert list(changes) == sorted(changes)
    assert changes["app/gone.py"].after is None
    assert changes["app/gone.py"].deletions == 1
    assert changes["app/new.py"].before is None
    assert changes["app/new.py"].additions == 1


def test_identical_content_move_is_a_rename_without_hunks() -> None:
    changes = _diff({"app/text.py": TEXT}, {"app/strings.py": TEXT})

    assert list(changes) == ["app/strings.py"]
    renamed = changes["app/strings.py"]
    assert (renamed.change, renamed.previous_path) == ("renamed", "app/text.py")
    assert renamed.hunks == ()
    assert (renamed.additions, renamed.deletions) == (0, 0)


def test_rename_hint_pairs_a_renamed_and_edited_file_with_a_one_line_diff() -> None:
    edited = TEXT.replace(b"Text helpers", b"String helpers")
    hints = {"app/strings.py": "app/text.py"}

    changes = _diff({"app/text.py": TEXT}, {"app/strings.py": edited}, hints)

    renamed = changes["app/strings.py"]
    assert (renamed.change, renamed.previous_path) == ("renamed", "app/text.py")
    assert (renamed.additions, renamed.deletions) == (1, 1)
    assert len(renamed.hunks) == 1
    assert renamed.hunks[0].lines[:2] == ('-"""Text helpers."""', '+"""String helpers."""')


def test_hint_whose_paths_do_not_match_the_trees_is_ignored() -> None:
    edited = TEXT.replace(b"Text helpers", b"String helpers")
    hints = {"app/other.py": "app/text.py", "app/strings.py": "app/missing.py"}

    changes = _diff({"app/text.py": TEXT}, {"app/strings.py": edited}, hints)

    assert {path: file.change for path, file in changes.items()} == {
        "app/strings.py": "added",
        "app/text.py": "removed",
    }


@pytest.mark.parametrize(
    ("base", "head", "hints"),
    [
        ({".env": SECRET}, {"notes.txt": SECRET}, {}),
        ({".env": SECRET}, {"notes.txt": SECRET + b"MORE=1\n"}, {"notes.txt": ".env"}),
        ({"notes.txt": SECRET}, {".env": SECRET}, {}),
        ({"notes.txt": SECRET}, {".env": SECRET + b"MORE=1\n"}, {".env": "notes.txt"}),
    ],
    ids=["moved-away", "renamed-away-and-edited", "moved-in", "renamed-in-and-edited"],
)
def test_a_credential_file_is_never_paired_as_a_rename(
    base: dict[str, bytes], head: dict[str, bytes], hints: dict[str, str]
) -> None:
    changes = _diff(base, head, hints)

    # A removal and an addition, so the credential file keeps its own coverage entry. The file
    # with the ordinary name holds the credential file's content, so it is withheld too.
    assert {path: (file.change, file.previous_path) for path, file in changes.items()} == {
        path: ("removed" if path in base else "added", None) for path in (".env", "notes.txt")
    }
    for file in changes.values():
        assert (file.reason, file.reviewable) == ("credential_file", False)
        assert (file.before, file.after, file.hunks) == (None, None, ())
    selection = select_for_review(list(changes.values()), **LIMITS)
    assert selection.reviewed == ()
    assert [(entry.path, entry.reason) for entry in selection.coverage] == [
        (".env", "credential_file"),
        ("notes.txt", "credential_file"),
    ]


# Classification ------------------------------------------------------------------------------


def test_changed_credential_and_binary_files_take_their_filter_reason() -> None:
    base = {"app/main.py": b"x = 1\n", "assets/logo.png": b"\x89PNG\x00old"}
    head = {
        "app/main.py": b"x = 1\n",
        "assets/logo.png": b"\x89PNG\x00new",
        ".env": b"API_TOKEN=not-a-secret\n",
    }

    changes = _diff(base, head)

    assert changes[".env"].reason == "credential_file"
    assert changes[".env"].change == "added"
    assert changes["assets/logo.png"].reason == "binary"
    for file in changes.values():
        assert not file.reviewable
        assert file.before is None and file.after is None
        assert (file.additions, file.deletions) == (None, None)


def test_changes_under_an_excluded_directory_become_one_entry() -> None:
    base = {"node_modules/a/index.js": b"a\n", "node_modules/b/index.js": b"b\n"}
    head = {
        "node_modules/a/index.js": b"a2\n",
        "node_modules/b/index.js": b"b2\n",
        "node_modules/c/index.js": b"c\n",
    }

    changes = _diff(base, head)

    assert list(changes) == ["node_modules"]
    entry = changes["node_modules"]
    assert (entry.entry_type, entry.count, entry.reason) == ("directory", 3, "excluded_directory")
    assert entry.change == "modified"  # mixed kinds


def test_removed_file_takes_the_merge_base_outcome() -> None:
    readme = {"README.md": b"# App\n"}
    base = {".env.local": b"SECRET=1\n", "app/old.py": b"def old():\n    return 1\n", **readme}

    changes = _diff(base, readme)

    assert set(changes) == {".env.local", "app/old.py"}
    assert changes[".env.local"].change == "removed"
    assert changes[".env.local"].reason == "credential_file"
    old = changes["app/old.py"]
    assert (old.change, old.reason, old.language) == ("removed", None, "python")
    assert old.before == "def old():\n    return 1\n"
    assert (old.additions, old.deletions) == (0, 2)


# Hunks ---------------------------------------------------------------------------------------


def test_hunks_have_three_lines_of_context_and_one_based_ranges() -> None:
    base = _lines(20)
    head = base.replace(b"line 10\n", b"line ten\n")

    hunk = _diff({"notes.py": base}, {"notes.py": head})["notes.py"].hunks[0]

    assert (hunk.before_start, hunk.before_end) == (7, 13)
    assert (hunk.after_start, hunk.after_end) == (7, 13)
    assert hunk.lines == (
        " line 7",
        " line 8",
        " line 9",
        "-line 10",
        "+line ten",
        " line 11",
        " line 12",
        " line 13",
    )
    assert (hunk.added_lines, hunk.removed_lines) == ((10,), (10,))
    assert (hunk.additions, hunk.deletions) == (1, 1)


def test_distant_changes_give_separate_hunks() -> None:
    base = _lines(40)
    head = base.replace(b"line 5\n", b"line five\n").replace(b"line 30\n", b"")

    hunks = _diff({"notes.py": base}, {"notes.py": head})["notes.py"].hunks

    assert [(h.before_start, h.before_end, h.after_start, h.after_end) for h in hunks] == [
        (2, 8, 2, 8),
        (27, 33, 27, 32),
    ]
    assert hunks[1].removed_lines == (30,)
    assert hunks[1].added_lines == ()


def test_pure_addition_has_no_before_range() -> None:
    hunk = _diff({}, {"app/new.py": _lines(4)})["app/new.py"].hunks[0]

    assert (hunk.before_start, hunk.before_end) == (None, None)
    assert (hunk.after_start, hunk.after_end) == (1, 4)
    assert hunk.added_lines == (1, 2, 3, 4)


def test_pure_removal_has_no_after_range() -> None:
    hunk = _diff({"app/old.py": _lines(3)}, {})["app/old.py"].hunks[0]

    assert (hunk.before_start, hunk.before_end) == (1, 3)
    assert (hunk.after_start, hunk.after_end) == (None, None)


def test_an_empty_file_and_a_final_newline_change_are_reviewable_without_hunks() -> None:
    base = {"app/main.py": b"x = 1", "app/legacy.py": b""}
    head = {"app/main.py": b"x = 1\n", "pkg/__init__.py": b""}

    changes = _diff(base, head)

    assert {path: (file.change, file.before, file.after) for path, file in changes.items()} == {
        "app/legacy.py": ("removed", "", None),
        "app/main.py": ("modified", "x = 1", "x = 1\n"),
        "pkg/__init__.py": ("added", None, ""),
    }
    for file in changes.values():
        assert file.reviewable and file.hunks == ()
    selection = select_for_review(list(changes.values()), **LIMITS)
    assert [file.path for file in selection.reviewed] == sorted(changes)


# Test paths ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_permissions.py", True),
        ("backend/test/helpers.py", True),
        ("web/src/__tests__/format.ts", True),
        ("app/test_text.py", True),
        ("app/text_test.py", True),
        ("web/src/format.test.ts", True),
        ("web/src/Button.test.tsx", True),
        ("web/src/format.spec.ts", True),
        ("web/src/Button.spec.tsx", True),
        ("app/text.py", False),
        ("app/testing.py", False),
        ("web/src/format.ts", False),
        ("contest/entry.py", False),
    ],
)
def test_is_test_path(path: str, expected: bool) -> None:
    assert is_test_path(path) is expected


# Selection -----------------------------------------------------------------------------------


def _changed(
    path: str, *, hunks: int = 1, added: int = 1, text: str = "x = 1", change: str = "modified"
) -> ChangedFile:
    """A reviewable file whose hunks each add `added` lines of `text`."""
    return ChangedFile(
        path=path,
        previous_path=None,
        change=change,  # type: ignore[arg-type]
        language="python",
        before="",
        after="",
        hunks=tuple(
            Hunk(
                before_start=None,
                before_end=None,
                after_start=index * 100 + 1,
                after_end=index * 100 + added,
                lines=tuple(f"+{text}" for _ in range(added)),
                added_lines=tuple(range(index * 100 + 1, index * 100 + added + 1)),
                removed_lines=(),
            )
            for index in range(hunks)
        ),
    )


def _excluded(path: str, reason: str = "binary") -> ChangedFile:
    return ChangedFile(path=path, previous_path=None, change="added", reason=reason)  # type: ignore[arg-type]


def test_selection_orders_source_then_tests_then_documentation() -> None:
    files = [
        _changed("README.md"),
        _changed("tests/test_app.py"),
        _changed("pyproject.toml"),
        _changed("app/main.py"),
        _changed("docs/notes.txt"),
        _changed("web/src/format.test.ts"),
    ]

    selection = select_for_review(files, **LIMITS)

    assert [file.path for file in selection.reviewed] == [
        "app/main.py",
        "pyproject.toml",
        "tests/test_app.py",
        "web/src/format.test.ts",
        "README.md",
        "docs/notes.txt",
    ]
    assert not selection.partial
    assert [entry.path for entry in selection.coverage] == sorted(file.path for file in files)
    assert all(entry.reviewed and entry.reason is None for entry in selection.coverage)


def test_selection_stops_at_the_file_limit() -> None:
    files = [_changed(f"app/module_{index:03d}.py") for index in range(105)]

    selection = select_for_review(files, **{**LIMITS, "max_hunks": 1000})

    assert len(selection.reviewed) == 100
    limited = [entry.path for entry in selection.coverage if entry.reason == "review_limit"]
    assert limited == [f"app/module_{index:03d}.py" for index in range(100, 105)]
    assert selection.partial


def test_selection_stops_at_the_changed_line_limit() -> None:
    files = [_changed(f"data/generated_{index:03d}.py", added=25) for index in range(1, 121)]

    selection = select_for_review(files, **LIMITS)

    assert len(selection.reviewed) == 80
    assert selection.changed_lines_reviewed == 2000
    assert sum(1 for entry in selection.coverage if entry.reason == "review_limit") == 40


def test_selection_stops_at_the_hunk_limit() -> None:
    files = [_changed(f"app/module_{index:03d}.py", hunks=3) for index in range(100)]

    selection = select_for_review(files, **LIMITS)

    assert len(selection.reviewed) == 26
    assert sum(len(file.hunks) for file in selection.reviewed) == 78
    limited = [entry for entry in selection.coverage if entry.reason == "review_limit"]
    assert len(limited) == 74
    assert all(not entry.reviewed for entry in limited)
    assert selection.partial


def test_selection_stops_at_the_diff_token_limit() -> None:
    # Each diff line is 348 characters plus its prefix and newline: 100 estimated tokens.
    files = [_changed(f"app/module_{index}.py", text="x" * 348) for index in range(5)]

    selection = select_for_review(files, **{**LIMITS, "max_diff_tokens": 300})

    assert [file.path for file in selection.reviewed] == [
        "app/module_0.py",
        "app/module_1.py",
        "app/module_2.py",
    ]
    assert selection.diff_tokens == pytest.approx(300)


def test_a_file_that_does_not_fit_is_limited_and_a_later_smaller_file_still_fits() -> None:
    files = [
        _changed("app/a.py", added=1500),
        _changed("app/b.py", added=600),
        _changed("app/c.py", added=400),
    ]

    selection = select_for_review(files, **LIMITS)

    assert [file.path for file in selection.reviewed] == ["app/a.py", "app/c.py"]
    entries = {entry.path: entry for entry in selection.coverage}
    assert (entries["app/b.py"].reviewed, entries["app/b.py"].reason) == (False, "review_limit")
    assert (entries["app/b.py"].additions, entries["app/b.py"].deletions) == (600, 0)
    assert selection.partial


def test_a_file_renamed_without_changes_is_reviewed_with_no_hunks() -> None:
    changes = _diff({"app/text.py": TEXT}, {"app/strings.py": TEXT})

    selection = select_for_review(list(changes.values()), **{**LIMITS, "max_hunks": 0})

    assert [file.path for file in selection.reviewed] == ["app/strings.py"]
    entry = selection.coverage[0]
    assert (entry.path, entry.previous_path, entry.change) == (
        "app/strings.py",
        "app/text.py",
        "renamed",
    )
    assert (entry.reviewed, entry.reason, entry.additions, entry.deletions) == (True, None, 0, 0)


def test_a_directory_entry_is_not_reviewed_with_a_file_of_the_same_path() -> None:
    # The merge base has a file under the excluded `build/`; the head has a file named `build`.
    head = _tree({"build": b"make all\n"})
    base = _tree(
        {"build/out.js": b"x = 1;\n"}, skip=lambda path, digest: head.hashes.get(path) == digest
    )

    selection = select_for_review(changed_files(head, base, {}), **LIMITS)

    assert [(file.path, file.entry_type) for file in selection.reviewed] == [("build", "file")]
    assert [(e.path, e.entry_type, e.reviewed, e.reason) for e in selection.coverage] == [
        ("build", "file", True, None),
        ("build", "directory", False, "excluded_directory"),
    ]


def test_coverage_lists_excluded_files_with_their_reasons() -> None:
    files = [
        _changed("app/main.py"),
        _excluded(".env", "credential_file"),
        _excluded("assets/logo.png"),
        ChangedFile(
            path="node_modules",
            previous_path=None,
            change="added",
            entry_type="directory",
            count=3,
            reason="excluded_directory",
        ),
    ]

    selection = select_for_review(files, **LIMITS)

    assert [file.path for file in selection.reviewed] == ["app/main.py"]
    assert [(e.path, e.reviewed, e.reason, e.entry_type, e.count) for e in selection.coverage] == [
        (".env", False, "credential_file", "file", 1),
        ("app/main.py", True, None, "file", 1),
        ("assets/logo.png", False, "binary", "file", 1),
        ("node_modules", False, "excluded_directory", "directory", 3),
    ]
    assert not selection.partial


def test_nothing_is_selected_without_an_eligible_change() -> None:
    files = [_excluded(".env", "credential_file"), _excluded("assets/logo.png")]

    selection = select_for_review(files, **LIMITS)

    assert selection.reviewed == ()
    assert len(selection.coverage) == 2
    assert not selection.partial
    assert selection.changed_lines_reviewed == 0
