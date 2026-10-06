"""Declaration extraction with tree-sitter (research R8).

Extracted declarations:

- Python: classes, functions (including async and decorated ones), and methods, which are
  functions directly inside a class body. Nested classes are walked, so a method of `Inner`
  declared inside `Outer` has the qualified name `Outer.Inner.method`.
- TypeScript and TSX: classes, function declarations, methods (method definitions in class
  bodies), interfaces, type aliases, enums, and exported `const` declarations whose value is an
  arrow function or function expression (kind `function`).

Only module-level declarations and class members are extracted. Functions nested in function
bodies and declarations inside TypeScript namespaces are not. A declaration's line range
includes its decorators and, in TypeScript, its `export` or `declare` keyword.

Changing these rules requires bumping `PARSER_RULES_VERSION` in `chunking.py`, which changes
the index version.
"""

from dataclasses import dataclass
from typing import Literal

import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

ParseLanguage = Literal["python", "typescript", "tsx"]
DeclarationKind = Literal["class", "function", "method", "interface", "type_alias", "enum"]


@dataclass(frozen=True)
class Declaration:
    name: str
    qualified_name: str
    kind: DeclarationKind
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive


@dataclass(frozen=True)
class ParseResult:
    declarations: list[Declaration]
    has_errors: bool  # the syntax tree has ERROR or MISSING nodes


# Built once per process. A parser is not thread-safe; the worker runs one job at a time.
_PARSERS: dict[ParseLanguage, Parser] = {
    "python": Parser(Language(tree_sitter_python.language())),
    "typescript": Parser(Language(tree_sitter_typescript.language_typescript())),
    "tsx": Parser(Language(tree_sitter_typescript.language_tsx())),
}

_TYPESCRIPT_KINDS: dict[str, DeclarationKind] = {
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "interface_declaration": "interface",
    "type_alias_declaration": "type_alias",
    "enum_declaration": "enum",
}
_TYPESCRIPT_WRAPPERS = frozenset({"export_statement", "ambient_declaration"})
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "generator_function"})


def parse_declarations(content: str, language: ParseLanguage) -> ParseResult:
    """Parse `content` and return its declarations in source order.

    Never raises on malformed input: tree-sitter recovers from syntax errors, and declarations
    it could still recognize are returned with `has_errors=True`.
    """
    tree = _PARSERS[language].parse(content.encode("utf-8", errors="replace"))
    root = tree.root_node
    if language == "python":
        declarations = _python_declarations(root)
    else:
        declarations = _typescript_declarations(root)
    declarations.sort(key=lambda d: (d.start_line, -d.end_line))
    return ParseResult(declarations=declarations, has_errors=root.has_error)


def _text(node: Node) -> str:
    return (node.text or b"").decode("utf-8", errors="replace")


def _declaration(
    name: Node, kind: DeclarationKind, prefix: str, span: Node, first: Node | None = None
) -> Declaration:
    """Build a declaration covering `span`, starting at `first` (a decorator) when given."""
    start_row = (first or span).start_point.row
    end_row = span.end_point.row
    if span.end_point.column == 0 and end_row > start_row:
        end_row -= 1  # the span ends with a line break
    text = _text(name)
    return Declaration(text, prefix + text, kind, start_row + 1, end_row + 1)


def _python_declarations(module: Node) -> list[Declaration]:
    found: list[Declaration] = []
    # (body, qualified-name prefix); an empty prefix means module level. An explicit stack
    # keeps deeply nested classes from hitting the recursion limit.
    scopes: list[tuple[Node, str]] = [(module, "")]
    while scopes:
        body, prefix = scopes.pop()
        for statement in body.named_children:
            node = statement
            if statement.type == "decorated_definition":
                definition = statement.child_by_field_name("definition")
                if definition is None:
                    continue
                node = definition
            name = node.child_by_field_name("name")
            if name is None:
                continue
            if node.type == "class_definition":
                declaration = _declaration(name, "class", prefix, statement)
                found.append(declaration)
                class_body = node.child_by_field_name("body")
                if class_body is not None:
                    scopes.append((class_body, declaration.qualified_name + "."))
            elif node.type == "function_definition":
                kind: DeclarationKind = "method" if prefix else "function"
                found.append(_declaration(name, kind, prefix, statement))
    return found


def _typescript_declarations(program: Node) -> list[Declaration]:
    found: list[Declaration] = []
    for statement in program.named_children:
        wrapped = statement.type in _TYPESCRIPT_WRAPPERS
        for node in statement.named_children if wrapped else [statement]:
            kind = _TYPESCRIPT_KINDS.get(node.type)
            name = node.child_by_field_name("name")
            if kind is not None and name is not None:
                found.append(_declaration(name, kind, "", statement))
                body = node.child_by_field_name("body")
                if kind == "class" and body is not None:
                    found.extend(_typescript_methods(body, _text(name) + "."))
            elif statement.type == "export_statement" and node.type == "lexical_declaration":
                found.extend(_const_functions(node))
    return found


def _typescript_methods(class_body: Node, prefix: str) -> list[Declaration]:
    found: list[Declaration] = []
    first_decorator: Node | None = None
    for member in class_body.named_children:
        if member.type == "decorator":
            first_decorator = first_decorator or member
            continue
        name = member.child_by_field_name("name")
        if member.type == "method_definition" and name is not None:
            found.append(_declaration(name, "method", prefix, member, first_decorator))
        first_decorator = None
    return found


def _const_functions(declaration: Node) -> list[Declaration]:
    """`const name = () => ...` or `const name = function () {...}` declarators."""
    keyword = declaration.child_by_field_name("kind")
    if keyword is None or keyword.type != "const":
        return []
    found: list[Declaration] = []
    for declarator in declaration.named_children:
        name = declarator.child_by_field_name("name")
        value = declarator.child_by_field_name("value")
        if (
            declarator.type == "variable_declarator"
            and name is not None
            and name.type == "identifier"
            and value is not None
            and value.type in _FUNCTION_VALUES
        ):
            found.append(_declaration(name, "function", "", declarator))
    return found
