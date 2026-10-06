"""Declaration extraction with tree-sitter (research R8)."""

from pathlib import Path

import pytest

from codeatlas.ingestion.parse import Declaration, ParseLanguage, parse_declarations

SAMPLE_APP = Path(__file__).resolve().parents[1] / "fixtures" / "repos" / "sample-app"


def fixture_declarations(path: str, language: ParseLanguage) -> list[Declaration]:
    result = parse_declarations((SAMPLE_APP / path).read_text(), language)
    assert result.has_errors is False
    return result.declarations


def summary(declarations: list[Declaration]) -> list[tuple[str, str, int, int]]:
    return [(d.qualified_name, d.kind, d.start_line, d.end_line) for d in declarations]


def test_python_classes_functions_and_methods() -> None:
    declarations = fixture_declarations("app/auth/access.py", "python")

    assert declarations == [
        # The decorated class starts at its `@dataclass` line.
        Declaration("Membership", "Membership", "class", 6, 10),
        Declaration("AccessPolicy", "AccessPolicy", "class", 13, 20),
        Declaration("__init__", "AccessPolicy.__init__", "method", 16, 17),
        Declaration("can_read", "AccessPolicy.can_read", "method", 19, 20),
        Declaration("check_repository_access", "check_repository_access", "function", 26, 33),
    ]


def test_python_async_decorated_and_nested_declarations() -> None:
    source = """\
class Outer:
    @staticmethod
    def build() -> None:
        def helper() -> None:
            pass

    class Inner:
        async def fetch(self) -> None:
            pass


@decorator
async def run() -> None:
    pass
"""
    result = parse_declarations(source, "python")

    assert result.has_errors is False
    # `helper` is local to `build`, so it is not a declaration.
    assert summary(result.declarations) == [
        ("Outer", "class", 1, 9),
        ("Outer.build", "method", 2, 5),
        ("Outer.Inner", "class", 7, 9),
        ("Outer.Inner.fetch", "method", 8, 9),
        ("run", "function", 12, 14),
    ]
    assert [d.name for d in result.declarations] == ["Outer", "build", "Inner", "fetch", "run"]


def test_typescript_enum_interface_and_type_alias() -> None:
    assert summary(fixture_declarations("src/types.ts", "typescript")) == [
        ("Role", "enum", 1, 5),
        ("User", "interface", 7, 11),
        ("UserId", "type_alias", 13, 13),
    ]


def test_typescript_class_and_methods() -> None:
    assert summary(fixture_declarations("src/services/userService.ts", "typescript")) == [
        ("UserService", "class", 3, 17),
        ("UserService.add", "method", 6, 8),
        ("UserService.getUserById", "method", 10, 12),
        ("UserService.isOwner", "method", 14, 16),
    ]


def test_typescript_exported_const_and_function_declaration() -> None:
    assert summary(fixture_declarations("src/utils/format.ts", "typescript")) == [
        ("formatUser", "function", 3, 3),
        ("initials", "function", 5, 11),
    ]


def test_typescript_declaration_variants() -> None:
    source = """\
export abstract class Base {
  abstract name(): string;

  @logged()
  describe(): string {
    return this.name();
  }
}

function* ids() {}
export const handler = async (event: string) => event,
  parse = function (raw: string) {
    return raw;
  },
  limit = 3;
export let mutable = () => 1;
const local = () => 2;
declare enum Ambient { A }
export default function () {}
"""
    result = parse_declarations(source, "typescript")

    assert result.has_errors is False
    # Abstract signatures, `let`, non-exported, and non-function constants are not declarations.
    assert summary(result.declarations) == [
        ("Base", "class", 1, 8),
        ("Base.describe", "method", 4, 7),
        ("ids", "function", 10, 10),
        ("handler", "function", 11, 11),
        ("parse", "function", 12, 14),
        ("Ambient", "enum", 18, 18),
    ]


def test_tsx_component_parses() -> None:
    source = (SAMPLE_APP / "src/components/UserCard.tsx").read_text()

    assert summary(fixture_declarations("src/components/UserCard.tsx", "tsx")) == [
        ("UserCardProps", "interface", 4, 6),
        ("UserCard", "function", 8, 15),
    ]
    # JSX needs the TSX grammar.
    assert parse_declarations(source, "typescript").has_errors is True


def test_python_syntax_error_keeps_valid_declarations() -> None:
    source = "def ok():\n    return 1\n\n\ndef broken(:\n    pass\n"

    result = parse_declarations(source, "python")

    assert result.has_errors is True
    assert ("ok", "function", 1, 2) in summary(result.declarations)


@pytest.mark.parametrize(
    ("source", "language"),
    [
        ("class A { foo( }", "typescript"),
        ("export const x = (", "tsx"),
        ("x = (1,\n", "python"),
        ("\x00\ud800 def", "python"),
    ],
)
def test_malformed_input_reports_errors_without_raising(
    source: str, language: ParseLanguage
) -> None:
    assert parse_declarations(source, language).has_errors is True


def test_empty_file() -> None:
    result = parse_declarations("", "python")

    assert result.declarations == []
    assert result.has_errors is False


def test_large_files_parse_without_corruption() -> None:
    # Regression: tree-sitter 0.26.0 returned corrupt trees and crashed on files of this size.
    python_source = "\n".join(
        f"def function_{i}(value):\n    return value + {i}\n" for i in range(1000)
    )
    typescript_source = "\n".join(
        f"export function fn{i}(value: number): number {{\n  return value + {i};\n}}\n"
        for i in range(1000)
    )

    python = parse_declarations(python_source, "python")
    typescript = parse_declarations(typescript_source, "typescript")

    assert (len(python.declarations), python.has_errors) == (1000, False)
    assert python.declarations[-1].start_line == 2998
    assert (len(typescript.declarations), typescript.has_errors) == (1000, False)
    assert typescript.declarations[-1].end_line == 3999
