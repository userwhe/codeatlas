"""Changed declarations, module names, and related code found by name (research R5)."""

import io
import tarfile
from collections.abc import Mapping
from typing import IO

from codeatlas.config import Settings
from codeatlas.review.context import changed_declarations, module_names, related_code
from codeatlas.review.diff import ChangedFile, Tree, changed_files, read_tree

TOP = "octo-org-review-app-abc1234"

BASE_PERMISSIONS = b"""\
def can_write(user, repository_id):
    return user.role in WRITE_ROLES and repository_id in user.repository_ids


def removed_helper(user):
    return user.id


def untouched_function(user):
    return user.role
"""
HEAD_PERMISSIONS = b"""\
def can_write(user, repository_id):
    allowed = repository_id in user.repository_ids
    return allowed


def untouched_function(user):
    return user.role
"""


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


def _changes(base: Mapping[str, bytes], head: Mapping[str, bytes]) -> list[ChangedFile]:
    return changed_files(_tree(head), _tree(base), {})


def _file(path: str, language: str = "python", previous_path: str | None = None) -> ChangedFile:
    return ChangedFile(
        path=path,
        previous_path=previous_path,
        change="renamed" if previous_path else "modified",
        language=language,  # type: ignore[arg-type]
    )


# changed_declarations ------------------------------------------------------------------------


def test_declarations_containing_added_or_removed_lines_are_changed() -> None:
    files = _changes(
        {"app/auth/permissions.py": BASE_PERMISSIONS},
        {"app/auth/permissions.py": HEAD_PERMISSIONS},
    )

    # `can_write` holds added lines at the head; `removed_helper` holds removed lines at the
    # merge base; `untouched_function` holds neither.
    assert changed_declarations(files) == ["can_write", "removed_helper"]


def test_typescript_declarations_are_found_too() -> None:
    base = b"export function formatCount(count: number): string {\n  return `${count}`;\n}\n"
    head = b"export function formatCount(count: number): string {\n  return `${count} items`;\n}\n"

    files = _changes({"web/src/format.ts": base}, {"web/src/format.ts": head})

    assert changed_declarations(files) == ["formatCount"]


def test_short_and_common_names_are_ignored() -> None:
    added = b"""\
def main():
    pass


def init():
    pass


def get():
    pass


def run():
    pass


def test():
    pass


def abc():
    pass


def fetch_user():
    pass


class Loader:
    def __init__(self):
        pass
"""
    files = _changes({}, {"app/loader.py": added})

    assert changed_declarations(files) == ["fetch_user", "Loader"]


def test_at_most_30_names_are_returned() -> None:
    added = "".join(f"def function_{index:02d}():\n    pass\n\n\n" for index in range(35))
    files = _changes({}, {"app/many.py": added.encode()})

    names = changed_declarations(files)

    assert names == [f"function_{index:02d}" for index in range(30)]


def test_files_without_a_parser_give_no_declarations() -> None:
    files = _changes({"README.md": b"# App\n"}, {"README.md": b"# App\n\ndef helper_name():\n"})

    assert changed_declarations(files) == []


# module_names --------------------------------------------------------------------------------


def test_module_names_are_the_stem_and_the_import_path() -> None:
    files = [_file("app/auth/permissions.py"), _file("web/src/format.ts", "typescript")]

    assert module_names(files) == [
        "permissions",
        "app.auth.permissions",
        "format",
        "web/src/format",
    ]


def test_module_names_of_packages_renames_and_other_files() -> None:
    files = [
        _file("app/auth/__init__.py"),
        _file("app/strings.py", previous_path="app/text.py"),
        _file("README.md", "markdown"),
        _file("main.py"),
    ]

    # A package gives its dotted path; a renamed file also gives the names it had; documentation
    # gives none; short and common stems are ignored, as for declarations.
    assert module_names(files) == ["app.auth", "strings", "app.strings", "text", "app.text"]


# related_code --------------------------------------------------------------------------------


def test_related_code_matches_whole_words_only() -> None:
    head = _tree(
        {
            "app/caller.py": b"from app.auth import can_write\n",
            "app/near_miss.py": b"def can_writer():\n    return my_can_write\n",
        }
    )

    related = related_code(head, [], ["can_write"], [])

    assert [item.path for item in related] == ["app/caller.py"]
    assert related[0].names == ("can_write",)


def test_related_code_skips_changed_files_and_tests() -> None:
    unchanged = {
        "app/repositories.py": b"from app.auth.permissions import can_write\n",
        "tests/test_permissions.py": b"from app.auth.permissions import can_write\n",
    }
    head = {"app/auth/permissions.py": HEAD_PERMISSIONS, **unchanged}
    files = _changes({"app/auth/permissions.py": BASE_PERMISSIONS, **unchanged}, head)
    assert [file.path for file in files] == ["app/auth/permissions.py"]

    related = related_code(_tree(head), files, ["can_write"], ["app.auth.permissions"])

    assert [item.path for item in related] == ["app/repositories.py"]
    assert related[0].names == ("can_write", "app.auth.permissions")


def test_related_code_ranks_by_distinct_names_then_path_and_keeps_the_top_8() -> None:
    names = ["alpha_name", "beta_name", "gamma_name"]
    files = {f"app/one_{index}.py": b"alpha_name()\nalpha_name()\n" for index in range(8)}
    files["app/two.py"] = b"beta_name()\nalpha_name()\n"
    files["app/zz_three.py"] = b"gamma_name()\nbeta_name()\nalpha_name()\n"
    files["app/none.py"] = b"nothing()\n"

    related = related_code(_tree(files), [], names, [])

    assert [item.path for item in related] == [
        "app/zz_three.py",
        "app/two.py",
        *(f"app/one_{index}.py" for index in range(6)),
    ]
    assert related[0].names == ("alpha_name", "beta_name", "gamma_name")
    assert len(related_code(_tree(files), [], names, [], max_files=2)) == 2


def _numbered(count: int, matches: Mapping[int, str]) -> bytes:
    return "".join(f"{matches.get(n, f'line_{n}')}\n" for n in range(1, count + 1)).encode()


def test_excerpt_covers_six_lines_around_the_first_two_matches() -> None:
    content = _numbered(60, {20: "call(can_write)", 24: "call(can_write)", 50: "can_write"})

    [item] = related_code(_tree({"app/caller.py": content}), [], ["can_write"], [])

    assert (item.start_line, item.end_line) == (14, 30)
    lines = content.decode().split("\n")
    assert item.excerpt == "\n".join(lines[13:30])


def test_excerpt_of_distant_matches_is_cut_to_40_lines() -> None:
    content = _numbered(80, {10: "can_write", 60: "can_write"})

    [item] = related_code(_tree({"app/caller.py": content}), [], ["can_write"], [])

    assert (item.start_line, item.end_line) == (4, 43)
    assert item.excerpt.count("\n") == 39


def test_excerpt_stays_within_the_file() -> None:
    content = _numbered(5, {2: "can_write"})

    [item] = related_code(_tree({"app/caller.py": content}), [], ["can_write"], [])

    assert (item.start_line, item.end_line) == (1, 5)


def test_no_names_give_no_related_code() -> None:
    head = _tree({"app/caller.py": b"can_write\n"})

    assert related_code(head, [], [], []) == []
