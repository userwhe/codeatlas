"""Source filtering and spec limits (research R7, FR-009, FR-010).

Rules run in this order, and the first match decides the coverage reason: path safety, excluded
directories, credential files, generated files, binary and encoding checks, then size.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Literal

from codeatlas.config import Settings
from codeatlas.ingestion.extract import ArchiveMember, LimitExceeded, RejectedMember

Language = Literal["python", "typescript", "tsx", "markdown", "text"]
SkipReason = Literal[
    "excluded_directory",
    "credential_file",
    "generated",
    "binary",
    "unsupported_encoding",
    "too_large",
    "link",
    "unsafe_path",
]

EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        "node_modules",
        "vendor",
        ".venv",
        "venv",
        "site-packages",
        "__pycache__",
        "dist",
        "build",
        ".next",
        "coverage",
    }
)
# Matched against the lowercased file name.
CREDENTIAL_PATTERNS = (
    ".env*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials*.json",
    ".netrc",
    ".npmrc",
    ".pypirc",
)
CREDENTIAL_EXCEPTIONS = frozenset({".env.example"})
LOCKFILES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "pdm.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
        "go.sum",
        "mix.lock",
        "pubspec.lock",
        "Podfile.lock",
        "flake.lock",
    }
)
# Matched against the lowercased file name.
GENERATED_PATTERNS = ("*.min.js", "*.min.css", "*.map")
GENERATED_MARKERS = (b"@generated", b"DO NOT EDIT")
GENERATED_MARKER_LINES = 5
LANGUAGES: dict[str, Language] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".md": "markdown",
    ".mdx": "markdown",
}


@dataclass(frozen=True)
class EligibleFile:
    path: str
    language: Language
    content: str
    size_bytes: int
    line_count: int
    content_sha256: bytes


@dataclass(frozen=True)
class SkippedEntry:
    path: str
    entry_type: Literal["file", "directory"]
    reason: SkipReason
    detail: str | None


@dataclass(frozen=True)
class FilterResult:
    files: list[EligibleFile]
    skipped: list[SkippedEntry]
    source_lines: int
    eligible_bytes: int


@dataclass(frozen=True)
class _AttributeRule:
    pattern: str
    generated: bool

    def matches(self, path: str) -> bool:
        if "/" not in self.pattern:
            return fnmatchcase(path.rsplit("/", 1)[-1], self.pattern)
        return _match_segments(self.pattern.strip("/").split("/"), path.split("/"))


def filter_members(
    members: Iterable[ArchiveMember | RejectedMember], settings: Settings
) -> FilterResult:
    """Classify every member, then enforce the spec limits over the eligible files.

    Raises `LimitExceeded` naming `max_files_per_snapshot`, `max_expanded_bytes`, or
    `max_source_lines_per_snapshot`.
    """
    skipped: list[SkippedEntry] = []
    excluded: set[str] = set()
    # Outcomes that a root `.gitattributes` may still turn into `generated`. The file can appear
    # anywhere in the archive, so it is applied after every member has been read.
    pending: list[EligibleFile | SkippedEntry] = []
    attributes: str | None = None

    for member in members:
        if isinstance(member, RejectedMember):
            skipped.append(SkippedEntry(member.path, "file", member.reason, member.detail))
            continue
        directory = _excluded_directory(member.path)
        if directory is not None:
            if directory not in excluded:
                excluded.add(directory)
                skipped.append(SkippedEntry(directory, "directory", "excluded_directory", None))
            continue
        name = member.path.rsplit("/", 1)[-1]
        if _is_credential(name):
            skipped.append(SkippedEntry(member.path, "file", "credential_file", None))
            continue
        generated = _generated_reason(name, member.content)
        if generated is not None:
            skipped.append(SkippedEntry(member.path, "file", "generated", generated))
            continue
        outcome = _classify_content(member)
        if member.path == ".gitattributes" and isinstance(outcome, EligibleFile):
            attributes = outcome.content
        pending.append(outcome)

    rules = _parse_attributes(attributes) if attributes is not None else []
    files: list[EligibleFile] = []
    for outcome in pending:
        if _linguist_generated(outcome.path, rules):
            skipped.append(SkippedEntry(outcome.path, "file", "generated", "linguist-generated"))
        elif isinstance(outcome, EligibleFile):
            files.append(outcome)
        else:
            skipped.append(outcome)

    eligible_bytes = sum(file.size_bytes for file in files)
    source_lines = sum(file.line_count for file in files)
    if len(files) > settings.max_files_per_snapshot:
        raise LimitExceeded("max_files_per_snapshot", settings.max_files_per_snapshot)
    if eligible_bytes > settings.max_expanded_bytes:
        raise LimitExceeded("max_expanded_bytes", settings.max_expanded_bytes)
    if source_lines > settings.max_source_lines_per_snapshot:
        raise LimitExceeded("max_source_lines_per_snapshot", settings.max_source_lines_per_snapshot)

    files.sort(key=lambda file: file.path)
    skipped.sort(key=lambda entry: entry.path)
    return FilterResult(
        files=files, skipped=skipped, source_lines=source_lines, eligible_bytes=eligible_bytes
    )


def _language(path: str) -> Language:
    return LANGUAGES.get(PurePosixPath(path).suffix.lower(), "text")


def _count_lines(text: str) -> int:
    """Lines in `text`; a last line without a trailing newline still counts."""
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def _excluded_directory(path: str) -> str | None:
    """The outermost excluded directory containing `path`, if any."""
    parts = path.split("/")
    for index, part in enumerate(parts[:-1]):
        if part in EXCLUDED_DIRECTORIES:
            return "/".join(parts[: index + 1])
    return None


def _is_credential(name: str) -> bool:
    lowered = name.lower()
    if lowered in CREDENTIAL_EXCEPTIONS:
        return False
    return any(fnmatchcase(lowered, pattern) for pattern in CREDENTIAL_PATTERNS)


def _generated_reason(name: str, content: bytes | None) -> str | None:
    if name in LOCKFILES:
        return "lockfile"
    lowered = name.lower()
    for pattern in GENERATED_PATTERNS:
        if fnmatchcase(lowered, pattern):
            return pattern
    if content is not None:
        head = b"\n".join(content.split(b"\n", GENERATED_MARKER_LINES)[:GENERATED_MARKER_LINES])
        for marker in GENERATED_MARKERS:
            if marker in head:
                return f"{marker.decode()} marker"
    return None


def _classify_content(member: ArchiveMember) -> EligibleFile | SkippedEntry:
    """Binary and encoding checks, then size (the last rules of research R7)."""
    content = member.content
    if content is None:
        return SkippedEntry(member.path, "file", "too_large", f"{member.size} bytes")
    # Research R7 sniffs the first 8 KiB for a NUL byte. Any NUL counts here, because PostgreSQL
    # text cannot store one.
    if b"\x00" in content:
        return SkippedEntry(member.path, "file", "binary", None)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return SkippedEntry(member.path, "file", "unsupported_encoding", "not valid UTF-8")
    return EligibleFile(
        path=member.path,
        language=_language(member.path),
        content=text,
        size_bytes=len(content),
        line_count=_count_lines(text),
        content_sha256=hashlib.sha256(content).digest(),
    )


def _parse_attributes(text: str) -> list[_AttributeRule]:
    """`linguist-generated` rules from a root `.gitattributes`, in file order."""
    rules: list[_AttributeRule] = []
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0].startswith("#") or fields[0].startswith("[attr]"):
            continue
        pattern, *attrs = fields
        if pattern.endswith("/"):
            continue  # Directory patterns never apply to the files inside (gitattributes(5)).
        for attr in attrs:
            if attr in ("linguist-generated", "linguist-generated=true"):
                rules.append(_AttributeRule(pattern, generated=True))
            elif attr in ("-linguist-generated", "!linguist-generated") or attr.startswith(
                "linguist-generated="
            ):
                rules.append(_AttributeRule(pattern, generated=False))
    return rules


def _linguist_generated(path: str, rules: list[_AttributeRule]) -> bool:
    """The last matching rule wins, as in git."""
    generated = False
    for rule in rules:
        if rule.matches(path):
            generated = rule.generated
    return generated


def _match_segments(pattern: list[str], path: list[str]) -> bool:
    """Match an anchored gitattributes pattern segment by segment; `**` spans any segments."""
    if not pattern:
        return not path
    if pattern[0] == "**":
        if len(pattern) == 1:
            return bool(path)
        return any(_match_segments(pattern[1:], path[index:]) for index in range(len(path) + 1))
    return (
        bool(path) and fnmatchcase(path[0], pattern[0]) and _match_segments(pattern[1:], path[1:])
    )
