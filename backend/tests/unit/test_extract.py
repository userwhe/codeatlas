"""Safe tarball extraction (research R5 and R7): unsafe members, safety caps, large members."""

import io
import tarfile
from typing import IO

import pytest

from codeatlas.config import Settings
from codeatlas.github.fake import UNSAFE_PATHS_ID, commit_sha, get_fake_github
from codeatlas.ingestion.extract import (
    ArchiveMember,
    LimitExceeded,
    RejectedMember,
    iter_archive,
)

TOP = "octo-org-demo-abc1234"


class _ForwardOnly(io.RawIOBase):
    """A stream that cannot seek, like an HTTP response body."""

    def __init__(self, data: bytes) -> None:
        self._data = io.BytesIO(data)

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        return self._data.readinto(buffer)


def _special(name: str, kind: bytes, linkname: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info


def _tarball(*members: tarfile.TarInfo | tuple[str, bytes]) -> IO[bytes]:
    """A gzip tarball of `members`; a `(name, data)` pair is a regular file."""
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer, mode="w:gz", encoding="utf-8", errors="surrogateescape"
    ) as tar:
        for member in members:
            if isinstance(member, tarfile.TarInfo):
                tar.addfile(member)
            else:
                name, data = member
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return io.BufferedReader(_ForwardOnly(buffer.getvalue()))


def _settings(**overrides: int) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _extract(*members: tarfile.TarInfo | tuple[str, bytes], **limits: int) -> list[object]:
    return list(iter_archive(_tarball(*members), _settings(**limits)))


def test_streams_files_with_the_top_level_directory_stripped() -> None:
    result = _extract(
        _special(f"{TOP}/", tarfile.DIRTYPE),
        _special(f"{TOP}/app/", tarfile.DIRTYPE),
        (f"{TOP}/app/main.py", b"print('hi')\n"),
        (f"./{TOP}/README.md", b"# Demo\n"),
        (f"{TOP}//docs/./guide.md", b"# Guide\n"),
        (f"{TOP}/empty.txt", b""),
    )

    assert result == [
        ArchiveMember(path="app/main.py", size=12, content=b"print('hi')\n"),
        ArchiveMember(path="README.md", size=7, content=b"# Demo\n"),
        ArchiveMember(path="docs/guide.md", size=8, content=b"# Guide\n"),
        ArchiveMember(path="empty.txt", size=0, content=b""),
    ]


@pytest.mark.parametrize(
    ("name", "display", "detail"),
    [
        (f"{TOP}/../escape.txt", "../escape.txt", "parent directory segment"),
        (f"{TOP}/app/../../escape.txt", "app/../../escape.txt", "parent directory segment"),
        ("/etc/passwd", "/etc/passwd", "absolute path"),
        (f"/{TOP}/app.py", f"/{TOP}/app.py", "absolute path"),
        ("", "(empty)", "empty path"),
        (".", ".", "empty path"),
        (f"{TOP}/" + "a" * 120 + "\x00.py", "a" * 120 + "�.py", "NUL byte in path"),
        (f"{TOP}/caf\udce9.txt", "caf�.txt", "path is not valid UTF-8"),
        ("other-top/app.py", "other-top/app.py", "outside the top-level directory"),
        ("stray.txt", "stray.txt", "outside the top-level directory"),
    ],
)
def test_unsafe_names_are_rejected(name: str, display: str, detail: str) -> None:
    result = _extract(
        _special(f"{TOP}/", tarfile.DIRTYPE),
        (name, b"data\n"),
        (f"{TOP}/safe.py", b"x = 1\n"),
    )

    assert result == [
        RejectedMember(path=display, reason="unsafe_path", detail=detail),
        ArchiveMember(path="safe.py", size=6, content=b"x = 1\n"),
    ]


def test_links_and_special_files_are_rejected() -> None:
    result = _extract(
        (f"{TOP}/README.md", b"# Demo\n"),
        _special(f"{TOP}/docs-link", tarfile.SYMTYPE, linkname="/etc/passwd"),
        _special(f"{TOP}/readme-copy.md", tarfile.LNKTYPE, linkname=f"{TOP}/README.md"),
        _special(f"{TOP}/tty", tarfile.CHRTYPE),
        _special(f"{TOP}/disk", tarfile.BLKTYPE),
        _special(f"{TOP}/pipe", tarfile.FIFOTYPE),
    )

    assert result == [
        ArchiveMember(path="README.md", size=7, content=b"# Demo\n"),
        RejectedMember(path="docs-link", reason="link", detail="symbolic link"),
        RejectedMember(path="readme-copy.md", reason="link", detail="hard link"),
        RejectedMember(path="tty", reason="unsafe_path", detail="device file"),
        RejectedMember(path="disk", reason="unsafe_path", detail="device file"),
        RejectedMember(path="pipe", reason="unsafe_path", detail="FIFO"),
    ]


def test_a_duplicate_path_is_rejected() -> None:
    result = _extract((f"{TOP}/a.py", b"first\n"), (f"{TOP}/./a.py", b"second\n"))

    assert result == [
        ArchiveMember(path="a.py", size=6, content=b"first\n"),
        RejectedMember(path="a.py", reason="unsafe_path", detail="duplicate path"),
    ]


def test_a_member_over_max_file_bytes_has_no_content() -> None:
    result = _extract(
        (f"{TOP}/small.txt", b"1234"),
        (f"{TOP}/large.txt", b"12345"),
        (f"{TOP}/after.txt", b"ok\n"),
        max_file_bytes=4,
    )

    assert result == [
        ArchiveMember(path="small.txt", size=4, content=b"1234"),
        ArchiveMember(path="large.txt", size=5, content=None),
        ArchiveMember(path="after.txt", size=3, content=b"ok\n"),
    ]


def test_member_cap_counts_every_member() -> None:
    members = (
        _special(f"{TOP}/", tarfile.DIRTYPE),
        (f"{TOP}/a.py", b"a\n"),
        _special(f"{TOP}/link", tarfile.SYMTYPE, linkname="a.py"),
    )
    assert len(_extract(*members, max_archive_members=3)) == 2

    stream = _tarball(*members, (f"{TOP}/b.py", b"b\n"))
    items = iter_archive(stream, _settings(max_archive_members=3))
    with pytest.raises(LimitExceeded) as raised:
        list(items)

    assert raised.value.limit_name == "max_archive_members"
    assert raised.value.limit_value == 3
    assert "max_archive_members" in str(raised.value)


def test_byte_cap_counts_every_regular_file() -> None:
    # Content-less large files and rejected files still count: their bytes are decompressed.
    members = ((f"{TOP}/a.txt", b"12345"), (f"{TOP}/../b.txt", b"12345"))
    assert len(_extract(*members, max_archive_bytes=10, max_file_bytes=4)) == 2

    stream = _tarball(*members, (f"{TOP}/c.txt", b"1"))
    with pytest.raises(LimitExceeded) as raised:
        list(iter_archive(stream, _settings(max_archive_bytes=10)))

    assert raised.value.limit_name == "max_archive_bytes"
    assert raised.value.limit_value == 10


def test_caps_stop_reading_as_soon_as_they_are_crossed() -> None:
    stream = _tarball(*((f"{TOP}/f{index}.txt", b"x\n") for index in range(5)))
    items = iter_archive(stream, _settings(max_archive_members=2))

    assert next(items) == ArchiveMember(path="f0.txt", size=2, content=b"x\n")
    assert next(items) == ArchiveMember(path="f1.txt", size=2, content=b"x\n")
    with pytest.raises(LimitExceeded):
        next(items)


def test_fake_unsafe_paths_archive() -> None:
    fake = get_fake_github()
    full_name = "octo-org/unsafe-paths"
    sha = commit_sha(UNSAFE_PATHS_ID, "initial")
    with fake.open_tarball(fake.get_installation_id(full_name), full_name, sha) as stream:
        result = list(iter_archive(stream, _settings()))

    readme = b"# Unsafe paths\n\nOnly this file and app.py are safe.\n"
    app = b'def main() -> str:\n    return "safe"\n'
    assert result == [
        ArchiveMember(path="README.md", size=len(readme), content=readme),
        ArchiveMember(path="app.py", size=len(app), content=app),
        RejectedMember(
            path="../escape.txt", reason="unsafe_path", detail="parent directory segment"
        ),
        RejectedMember(
            path="/etc/codeatlas-absolute.txt", reason="unsafe_path", detail="absolute path"
        ),
        RejectedMember(path="docs-link", reason="link", detail="symbolic link"),
        RejectedMember(path="readme-hardlink.md", reason="link", detail="hard link"),
    ]
