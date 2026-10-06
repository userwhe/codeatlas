"""File tree listing and bounded line reads within one snapshot (FR-015, FR-018).

Lines are split on `\\n` and numbered from 1, exactly as indexing does, so line numbers here
match symbol ranges, chunks, and citations.
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from codeatlas.api.errors import ApiError, not_found
from codeatlas.models import File

MAX_LINES_PER_REQUEST = 1000


@dataclass(frozen=True)
class TreeEntry:
    name: str
    type: Literal["directory", "file"]
    language: str | None = None
    line_count: int | None = None


@dataclass(frozen=True)
class FileLines:
    path: str
    language: str
    line_count: int
    start_line: int
    end_line: int
    lines: list[str]


def invalid_path() -> ApiError:
    return ApiError(422, "invalid_path", "The path must be a repository-relative path.")


def normalize_path(path: str) -> str:
    """A repository-relative POSIX path: no leading `/` or `./`, no empty or `.` segments.

    `..` segments, backslashes, and NUL characters are rejected rather than resolved.
    """
    if "\\" in path or "\x00" in path:
        raise invalid_path()
    segments = [segment for segment in path.split("/") if segment not in ("", ".")]
    if ".." in segments:
        raise invalid_path()
    return "/".join(segments)


def split_lines(content: str) -> list[str]:
    """Lines as indexing numbers them: a final line break does not start another line."""
    lines = content.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def list_tree(db: Session, snapshot_id: uuid.UUID, path: str) -> list[TreeEntry]:
    """Immediate children of directory `path` (`""` is the root): directories, then files.

    Directories are derived from file paths; an unknown or empty directory is a 404.
    """
    directory = normalize_path(path)
    prefix = f"{directory}/" if directory else ""
    rest = func.substr(File.path, len(prefix) + 1)
    name = func.split_part(rest, "/", 1)
    rows = db.execute(
        select(
            name,
            func.bool_or(func.strpos(rest, "/") > 0),
            func.min(File.language),
            func.min(File.line_count),
        )
        .where(File.snapshot_id == snapshot_id, File.path.startswith(prefix, autoescape=True))
        .group_by(name)
    ).all()
    if not rows and directory:
        raise not_found()
    entries = [
        TreeEntry(name=child, type="directory")
        if is_directory
        else TreeEntry(name=child, type="file", language=language, line_count=line_count)
        for child, is_directory, language, line_count in rows
    ]
    return sorted(entries, key=lambda entry: (entry.type != "directory", entry.name))


def read_lines(
    db: Session,
    snapshot_id: uuid.UUID,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> FileLines:
    """Lines `start_line..end_line` (inclusive) of a file, at most 1,000 per request.

    Defaults to lines 1 to 1,000. `end_line` is clamped to the request cap and the file length.
    """
    normalized = normalize_path(path)
    if not normalized:
        raise invalid_path()
    start = 1 if start_line is None else start_line
    end = start + MAX_LINES_PER_REQUEST - 1 if end_line is None else end_line
    if start < 1 or end < start:
        raise ApiError(
            422, "invalid_range", "start_line must be at least 1 and end_line at least start_line."
        )
    row = db.execute(
        select(File.language, File.line_count, File.content).where(
            File.snapshot_id == snapshot_id, File.path == normalized
        )
    ).one_or_none()
    if row is None:
        raise not_found()
    language, line_count, content = row
    if start > max(line_count, 1):
        raise ApiError(
            422,
            "invalid_range",
            f"The file has {line_count} lines.",
            details={"line_count": line_count},
        )
    end = min(end, start + MAX_LINES_PER_REQUEST - 1, line_count)
    return FileLines(
        path=normalized,
        language=language,
        line_count=line_count,
        start_line=start,
        end_line=end,
        lines=split_lines(content)[start - 1 : end],
    )
