"""Safe, streaming extraction of a repository tarball (research R5 and R7).

Members are read straight from the gzip stream: nothing is written to disk and nothing is
executed. Unsafe members are reported instead of yielded as files, and only the extraction safety
caps are enforced here; the filters decide every other coverage reason.
"""

import hashlib
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import IO, Literal

from codeatlas.config import Settings

_HEADER_TYPES = frozenset({tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE})
_HASH_CHUNK_BYTES = 1024 * 1024


class LimitExceeded(Exception):
    """A spec limit or an extraction safety cap was exceeded (research R7)."""

    def __init__(self, limit_name: str, limit_value: int) -> None:
        super().__init__(f"limit {limit_name} ({limit_value}) exceeded")
        self.limit_name = limit_name
        self.limit_value = limit_value


@dataclass(frozen=True)
class ArchiveMember:
    """A regular file. `content` is None when the file is over `max_file_bytes`.

    `sha256` is the digest of the file's bytes, kept or not (specs/003-pr-review, research R3). It
    is left out of equality, and members built by hand may omit it.
    """

    path: str
    size: int
    content: bytes | None
    sha256: bytes = field(default=b"", compare=False)


@dataclass(frozen=True)
class RejectedMember:
    """A member that is never read: an unsafe path, a link, or a special file."""

    path: str
    reason: Literal["unsafe_path", "link"]
    detail: str | None


def iter_archive(stream: IO[bytes], settings: Settings) -> Iterator[ArchiveMember | RejectedMember]:
    """Yield the files of a GitHub tarball with the top-level directory stripped.

    Raises `LimitExceeded` as soon as `max_archive_members` or `max_archive_bytes` is crossed.
    """
    member_count = 0
    archive_bytes = 0
    top: str | None = None
    seen: set[str] = set()
    with tarfile.open(
        fileobj=stream, mode="r|gz", encoding="utf-8", errors="surrogateescape"
    ) as tar:
        for info in tar:
            member_count += 1
            if member_count > settings.max_archive_members:
                raise LimitExceeded("max_archive_members", settings.max_archive_members)
            if info.type in _HEADER_TYPES:
                continue
            if info.isreg():
                archive_bytes += info.size
                if archive_bytes > settings.max_archive_bytes:
                    raise LimitExceeded("max_archive_bytes", settings.max_archive_bytes)

            problem = _unsafe_name(info.name)
            parts = [part for part in info.name.split("/") if part not in ("", ".")]
            if problem is None:
                if top is None and (len(parts) > 1 or info.isdir()):
                    top = parts[0]
                if parts[0] != top or (len(parts) == 1 and not info.isdir()):
                    problem = "outside the top-level directory"
            if info.isdir():
                continue
            if problem is not None:
                yield RejectedMember(_display_path(info.name, top), "unsafe_path", problem)
                continue

            path = "/".join(parts[1:])
            rejection = _rejection(info)
            if rejection is None and path in seen:
                rejection = ("unsafe_path", "duplicate path")
            if rejection is not None:
                yield RejectedMember(path, *rejection)
                continue

            seen.add(path)
            fileobj = tar.extractfile(info)
            if fileobj is None:
                raise tarfile.ReadError(f"cannot read member {path}")
            content = None
            if info.size <= settings.max_file_bytes:
                content = fileobj.read()
                digest = hashlib.sha256(content).digest()
            else:
                # Hash a file too large to keep without holding it in memory.
                hasher = hashlib.sha256()
                while chunk := fileobj.read(_HASH_CHUNK_BYTES):
                    hasher.update(chunk)
                digest = hasher.digest()
            yield ArchiveMember(path=path, size=info.size, content=content, sha256=digest)


def _unsafe_name(name: str) -> str | None:
    """Why a member name is unsafe, or None when it is a safe relative path."""
    if "\x00" in name:
        return "NUL byte in path"
    if any("\ud800" <= char <= "\udfff" for char in name):
        return "path is not valid UTF-8"
    if name.startswith("/"):
        return "absolute path"
    segments = name.split("/")
    if ".." in segments:
        return "parent directory segment"
    if all(segment in ("", ".") for segment in segments):
        return "empty path"
    return None


def _display_path(name: str, top: str | None) -> str:
    """A best-effort, storable path for a rejected member."""
    text = "".join("�" if char == "\x00" or "\ud800" <= char <= "\udfff" else char for char in name)
    if top is not None and text.startswith(f"{top}/"):
        text = text[len(top) + 1 :]
    return text or "(empty)"


def _rejection(info: tarfile.TarInfo) -> tuple[Literal["unsafe_path", "link"], str] | None:
    if info.issym():
        return ("link", "symbolic link")
    if info.islnk():
        return ("link", "hard link")
    if info.ischr() or info.isblk():
        return ("unsafe_path", "device file")
    if info.isfifo():
        return ("unsafe_path", "FIFO")
    if not info.isreg():
        return ("unsafe_path", "unsupported member type")
    return None
