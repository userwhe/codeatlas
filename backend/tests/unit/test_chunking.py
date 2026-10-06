"""Code and Markdown chunking, identifier splitting, and the index version (research R8, R9)."""

import re
from pathlib import Path

import pytest

from codeatlas.config import Settings
from codeatlas.ingestion.chunking import (
    CHUNK_OVERLAP,
    CHUNK_WINDOW,
    DOC_MAX_CHARS,
    DocChunkSpec,
    code_chunks,
    index_version,
    markdown_chunks,
    split_identifiers,
)

SAMPLE_APP = Path(__file__).resolve().parents[1] / "fixtures" / "repos" / "sample-app"


def numbered_lines(count: int) -> str:
    return "".join(f"line{number}\n" for number in range(1, count + 1))


def assert_exact_lines(content: str, chunks: list[DocChunkSpec]) -> None:
    lines = content.split("\n")
    for chunk in chunks:
        assert chunk.text == "\n".join(lines[chunk.start_line - 1 : chunk.end_line])


def outline(chunks: list[DocChunkSpec]) -> list[tuple[int, int, str]]:
    return [(chunk.start_line, chunk.end_line, chunk.heading_path) for chunk in chunks]


# Identifier splitting


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("getUserById", "get user by id getuserbyid"),
        ("HTTPServer2", "http server 2 httpserver2"),
        ("user_id", "user id userid"),
        ("MAX_FILE_BYTES", "max file bytes maxfilebytes"),
        ("getHTTPResponse", "get http response gethttpresponse"),
        ("user", "user"),
        ("__init__", "init"),
        ("Größe", "größe"),
        ("return this.users.get(id);", "return this users get id"),
        ("", ""),
    ],
)
def test_split_identifiers(text: str, expected: str) -> None:
    assert split_identifiers(text) == expected


# Code chunks


def test_chunk_constants() -> None:
    assert (CHUNK_WINDOW, CHUNK_OVERLAP, DOC_MAX_CHARS) == (60, 10, 1600)


@pytest.mark.parametrize(
    ("line_count", "expected"),
    [
        (0, []),
        (1, [(1, 1)]),
        (59, [(1, 59)]),
        (60, [(1, 60)]),
        (61, [(1, 60), (51, 61)]),
        (110, [(1, 60), (51, 110)]),
        (111, [(1, 60), (51, 110), (101, 111)]),
        (200, [(1, 60), (51, 110), (101, 160), (151, 200)]),
    ],
)
def test_code_windows_overlap_by_ten_lines(
    line_count: int, expected: list[tuple[int, int]]
) -> None:
    content = numbered_lines(line_count)

    ranges = [(chunk.start_line, chunk.end_line) for chunk in code_chunks(content)]

    assert ranges == expected
    # A missing final line break does not change the line count.
    assert [(c.start_line, c.end_line) for c in code_chunks(content.rstrip("\n"))] == expected


def test_code_chunk_search_text_covers_its_lines() -> None:
    lines = [f"value{number} = 0" for number in range(1, 71)]
    lines[54] = "user = service.getUserById(user_id)"  # line 55, in both windows
    lines[64] = "HTTPServer2.start()"  # line 65, only in the second window

    first, second = code_chunks("\n".join(lines) + "\n")

    assert "get user by id getuserbyid" in first.search_text
    assert "get user by id getuserbyid" in second.search_text
    assert "httpserver2" not in first.search_text
    assert "http server 2 httpserver2" in second.search_text
    assert first.search_text.startswith("value 1 value1 0 ")
    assert second.search_text.startswith("value 51 value51 0 ")


def test_code_chunks_custom_window() -> None:
    ranges = [
        (c.start_line, c.end_line) for c in code_chunks(numbered_lines(10), window=4, overlap=1)
    ]

    assert ranges == [(1, 4), (4, 7), (7, 10)]


@pytest.mark.parametrize(("window", "overlap"), [(10, 10), (10, -1), (0, 0)])
def test_code_chunks_reject_invalid_windows(window: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        code_chunks("x\n", window=window, overlap=overlap)


# Markdown chunks


def test_markdown_chunks_by_heading_with_paths_and_lines() -> None:
    content = (SAMPLE_APP / "README.md").read_text()

    chunks = markdown_chunks(content)

    assert outline(chunks) == [
        (1, 4, "Sample App"),
        (6, 11, "Sample App > Setup"),
        (13, 16, "Sample App > Usage"),
        (18, 22, "Sample App > Project layout"),
    ]
    assert chunks[1].text.startswith("## Setup\n\n1. Install Python 3.13")
    assert_exact_lines(content, chunks)


def test_markdown_heading_only_section_is_skipped() -> None:
    content = (SAMPLE_APP / "docs/architecture.md").read_text()

    chunks = markdown_chunks(content)

    # `# Architecture` has no text of its own; it still appears in the heading paths.
    assert outline(chunks) == [
        (3, 6, "Architecture > Overview"),
        (8, 12, "Architecture > Access control"),
        (14, 18, "Architecture > Frontend"),
        (20, 23, "Architecture > Generated code"),
    ]
    assert_exact_lines(content, chunks)


def test_markdown_heading_levels_and_preamble() -> None:
    content = """\
Intro text before any heading.

# Guide
Guide text.
### Deep
Deep text.
## Setup ##
Setup text.
### Configuration
Configuration text.
# Appendix
#not-a-heading
Appendix text.
"""
    chunks = markdown_chunks(content)

    assert outline(chunks) == [
        (1, 1, ""),
        (3, 4, "Guide"),
        (5, 6, "Guide > Deep"),
        (7, 8, "Guide > Setup"),
        (9, 10, "Guide > Setup > Configuration"),
        (11, 13, "Appendix"),
    ]
    assert_exact_lines(content, chunks)


def test_markdown_headings_inside_code_fences_are_ignored() -> None:
    content = """\
## Install

```bash
# install the dependencies
uv sync
```

~~~~
# still code
```
## still code
~~~~

## Next
Done.
"""
    chunks = markdown_chunks(content)

    assert outline(chunks) == [(1, 12, "Install"), (14, 15, "Next")]
    assert_exact_lines(content, chunks)


def test_long_section_splits_at_paragraph_boundaries() -> None:
    paragraphs = [f"Paragraph {number} " + "word " * 100 for number in range(1, 6)]
    content = "# Long\n\n" + "\n\n".join(paragraphs) + "\n"

    chunks = markdown_chunks(content)

    # Each paragraph is 512 characters: the heading and three paragraphs fit in 1,600, and the
    # fourth starts a new chunk at its own line.
    assert outline(chunks) == [(1, 7, "Long"), (9, 11, "Long")]
    assert all(len(chunk.text) <= 1600 for chunk in chunks)
    assert_exact_lines(content, chunks)


def test_long_paragraph_is_cut_at_line_boundaries() -> None:
    content = "".join(f"{number:02d} " + "x" * 76 + "\n" for number in range(1, 41))

    chunks = markdown_chunks(content, max_chars=800)

    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 10), (11, 20), (21, 30), (31, 40)]
    assert all(len(chunk.text) <= 800 for chunk in chunks)
    assert_exact_lines(content, chunks)


@pytest.mark.parametrize("content", ["", "\n", "   \n\n\t\n", "# Title\n\n\n## Empty\n"])
def test_markdown_without_text_yields_no_chunks(content: str) -> None:
    assert markdown_chunks(content) == []


# Index version


def make_settings(embedding_model: str = "voyage-4", embedding_dimensions: int = 1024) -> Settings:
    return Settings(
        _env_file=None,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
    )


def test_index_version_is_deterministic() -> None:
    version = index_version(make_settings())

    assert re.fullmatch(r"idx-[0-9a-f]{12}", version)
    assert index_version(make_settings()) == version


def test_index_version_changes_with_the_embedding_model() -> None:
    default = index_version(make_settings())

    assert index_version(make_settings(embedding_model="x")) != default
    assert index_version(make_settings(embedding_dimensions=512)) != default
