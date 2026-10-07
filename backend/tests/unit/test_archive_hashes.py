"""Content hashes of archive members (research R3): every regular file carries the SHA-256 of its
bytes, including files too large to keep, so a review can tell which paths changed.
"""

import dataclasses
import hashlib
import io
import random
import tarfile
from typing import IO

import pytest

from codeatlas.config import Settings
from codeatlas.ingestion.extract import ArchiveMember, LimitExceeded, RejectedMember, iter_archive

TOP = "octo-org-demo-abc1234"
# Larger than any read buffer, so the hash of an unkept member is built from several chunks.
LARGE = random.Random(0).randbytes(3 * 1024 * 1024 + 17)


def _tarball(*members: tarfile.TarInfo | tuple[str, bytes]) -> IO[bytes]:
    """A gzip tarball of `members`; a `(name, data)` pair is a regular file."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=1) as tar:
        for member in members:
            if isinstance(member, tarfile.TarInfo):
                tar.addfile(member)
            else:
                name, data = member
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return buffer


def _link(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = "README.md"
    return info


def _settings(**overrides: int) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def test_every_member_carries_the_sha256_of_its_bytes() -> None:
    files = {
        "README.md": b"# Demo\n",
        "app/main.py": b"print('hi')\n",
        "empty.txt": b"",
        "assets/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR",
    }
    stream = _tarball(*((f"{TOP}/{path}", data) for path, data in files.items()))

    members = list(iter_archive(stream, _settings()))

    assert [member.path for member in members] == list(files)
    for member in members:
        assert isinstance(member, ArchiveMember)
        assert member.sha256 == _sha256(files[member.path])
        assert len(member.sha256) == 32


def test_a_member_over_max_file_bytes_keeps_no_content_but_has_its_hash() -> None:
    stream = _tarball(
        (f"{TOP}/small.txt", b"1234"),
        (f"{TOP}/large.bin", LARGE),
        (f"{TOP}/after.txt", b"ok\n"),
    )

    small, large, after = iter_archive(stream, _settings(max_file_bytes=4))

    assert isinstance(large, ArchiveMember)
    assert (large.size, large.content) == (len(LARGE), None)
    assert large.sha256 == _sha256(LARGE)
    # Reading the large member to hash it leaves the stream at the next member.
    assert isinstance(small, ArchiveMember) and small.sha256 == _sha256(b"1234")
    assert isinstance(after, ArchiveMember)
    assert (after.content, after.sha256) == (b"ok\n", _sha256(b"ok\n"))


def test_identical_content_at_different_paths_has_the_same_hash() -> None:
    stream = _tarball((f"{TOP}/a.py", b"x = 1\n"), (f"{TOP}/b/a.py", b"x = 1\n"))

    first, second = iter_archive(stream, _settings())

    assert isinstance(first, ArchiveMember) and isinstance(second, ArchiveMember)
    assert first.sha256 == second.sha256


def test_rejected_members_have_no_hash() -> None:
    assert "sha256" not in {field.name for field in dataclasses.fields(RejectedMember)}

    stream = _tarball(
        (f"{TOP}/README.md", b"# Demo\n"),
        _link(f"{TOP}/docs-link"),
        (f"{TOP}/../escape.txt", b"outside\n"),
    )
    _, link, escape = iter_archive(stream, _settings())

    assert isinstance(link, RejectedMember) and isinstance(escape, RejectedMember)
    assert not hasattr(link, "sha256")
    assert not hasattr(escape, "sha256")


def test_the_byte_cap_still_counts_unkept_members() -> None:
    stream = _tarball((f"{TOP}/large.bin", LARGE), (f"{TOP}/next.txt", b"x"))

    with pytest.raises(LimitExceeded) as raised:
        list(iter_archive(stream, _settings(max_file_bytes=4, max_archive_bytes=len(LARGE))))

    assert raised.value.limit_name == "max_archive_bytes"


def test_the_member_cap_still_applies() -> None:
    stream = _tarball(*((f"{TOP}/f{index}.txt", b"x\n") for index in range(4)))
    members = iter_archive(stream, _settings(max_archive_members=2))

    for _ in range(2):
        member = next(members)
        assert isinstance(member, ArchiveMember) and member.sha256 == _sha256(b"x\n")
    with pytest.raises(LimitExceeded):
        next(members)
