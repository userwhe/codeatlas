"""The Markdown copy of a finished review (research R11, FR-015)."""

import copy
from typing import Any

from codeatlas.models import AnalysisRun
from codeatlas.review.markdown import render

HEAD_SHA = "8d3f1c2" + "0" * 33
MERGE_BASE_SHA = "1a9e4c2" + "0" * 33
BLOB = "https://github.com/octo-org/review-app/blob"


def _run(quality_state: str = "reviewed") -> AnalysisRun:
    return AnalysisRun(
        kind="pull_request_review",
        pull_request_number=12,
        base_sha="4f2c" + "0" * 36,
        head_sha=HEAD_SHA,
        merge_base_sha=MERGE_BASE_SHA,
        commit_sha=HEAD_SHA,
        pull_request={"title": "Simplify write checks", "author": "hubot"},
        quality_state=quality_state,
    )


def _citation(label: str, source_type: str, side: str, path: str, lines: tuple[int, int]) -> Any:
    sha = MERGE_BASE_SHA if side == "before" else HEAD_SHA
    start, end = lines
    return {
        "label": label,
        "source_type": source_type,
        "side": side,
        "path": path,
        "commit_sha": sha,
        "start_line": start,
        "end_line": end,
        "excerpt": "x",
        "github_url": f"{BLOB}/{sha}/{path}#L{start}-L{end}",
    }


CITATIONS = [
    _citation("E1", "change", "after", "app/auth/permissions.py", (14, 17)),
    _citation("E2", "change", "before", "app/auth/permissions.py", (14, 17)),
    _citation("E3", "reference", "after", "app/repositories.py", (1, 10)),
    _citation("E4", "test", "after", "tests/test_permissions.py", (1, 16)),
]


def _coverage(**overrides: Any) -> dict[str, Any]:
    coverage: dict[str, Any] = {
        "files": [
            {
                "path": "app/auth/permissions.py",
                "previous_path": None,
                "change": "modified",
                "entry_type": "file",
                "count": 1,
                "additions": 1,
                "deletions": 1,
                "reviewed": True,
                "reason": None,
            }
        ],
        "changed_files": 1,
        "reviewed_files": 1,
        "changed_lines_reviewed": 2,
        "context_items": 2,
    }
    coverage.update(overrides)
    return coverage


RESULT: dict[str, Any] = {
    "overall_risk": {"level": "high", "partial": False},
    "overview": "Drops the role check from `can_write`.",
    "summary": [
        {
            "area": "app/auth",
            "points": [
                {
                    "change": "modified",
                    "text": "`can_write` no longer checks the role.",
                    "evidence_ids": ["E1", "E2"],
                    "origin": "model",
                }
            ],
        }
    ],
    "risks": [
        {
            "id": "R1",
            "title": "Viewers may write",
            "severity": "high",
            "category": "security",
            "basis": "observed",
            "explanation": "Anyone listed on the repository may now write.",
            "suggested_check": "Confirm that viewers are still refused.",
            "evidence_ids": ["E2", "E3"],
            "origin": "model",
            "path": None,
        }
    ],
    "checklist": [
        {
            "text": "Confirm that viewers cannot write.",
            "paths": ["app/auth/permissions.py"],
            "risk_ids": ["R1"],
        }
    ],
    "tests": {
        "changed": [{"path": "tests/test_text.py", "change": "modified"}],
        "candidates": [
            {
                "path": "tests/test_permissions.py",
                "reason": "refers to `can_write`",
                "evidence_ids": ["E4"],
            }
        ],
        "new_cases": [
            {
                "behavior": "A viewer is refused on write.",
                "location_hint": "tests/test_permissions.py",
                "evidence_ids": ["E1"],
            }
        ],
    },
    "coverage": _coverage(),
    "omitted_items": 0,
}


def _result(**overrides: Any) -> dict[str, Any]:
    result = copy.deepcopy(RESULT)
    result.update(overrides)
    return result


def test_heading_names_the_pull_request_and_the_short_head_sha() -> None:
    markdown = render(_run(), RESULT, CITATIONS)

    assert markdown.startswith("## CodeAtlas review of #12 at 8d3f1c2\n\n**Overall risk: high**\n")
    assert markdown.endswith("\n") and not markdown.endswith("\n\n")


def test_sections_come_in_order() -> None:
    markdown = render(_run(), RESULT, CITATIONS)

    headings = [
        "**Overall risk: high**",
        "### Summary",
        "### Risks",
        "### Checklist",
        "### Tests",
        "### Coverage",
        "### Citations",
    ]
    positions = [markdown.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "Drops the role check from `can_write`." in markdown
    assert "**`app/auth`**" in markdown
    assert "#### R1: Viewers may write" in markdown
    assert "Confirm that viewers are still refused." in markdown
    assert "- [ ] Confirm that viewers cannot write. (`app/auth/permissions.py`; R1)" in markdown


def test_tests_section_lists_changed_candidate_and_new_tests() -> None:
    markdown = render(_run(), RESULT, CITATIONS)
    tests = markdown[markdown.index("### Tests") : markdown.index("### Coverage")]

    positions = [
        tests.index("**Changed in this pull request**"),
        tests.index("- `tests/test_text.py` (modified)"),
        tests.index("**Candidate tests (not run by CodeAtlas)**"),
        tests.index(f"- `tests/test_permissions.py`: refers to `can_write` ([E4]({BLOB}/"),
        tests.index("**Suggested new tests**"),
        tests.index("- A viewer is refused on write. Where: tests/test_permissions.py ([E1]("),
    ]
    assert positions == sorted(positions)


def test_empty_test_groups_say_so() -> None:
    result = _result(tests={"changed": [], "candidates": [], "new_cases": []})

    markdown = render(_run(), result, CITATIONS)

    assert "The pull request changes no tests." in markdown
    assert "No candidate tests were found." in markdown
    assert "No new tests are suggested." in markdown


def test_citations_are_links_to_github_at_the_cited_commit() -> None:
    markdown = render(_run(), RESULT, CITATIONS)

    for citation in CITATIONS:
        assert f"[{citation['label']}]({citation['github_url']})" in markdown
    assert f"([E1]({BLOB}/{HEAD_SHA}/app/auth/permissions.py#L14-L17), [E2](" in markdown
    citations = markdown[markdown.index("### Citations") :]
    assert (
        f"- [E2]({BLOB}/{MERGE_BASE_SHA}/app/auth/permissions.py#L14-L17): "
        "`app/auth/permissions.py` lines 14-17, before (merge base `1a9e4c2`)"
    ) in citations
    assert "- [E1](" in citations and "after (head `8d3f1c2`)" in citations


def test_related_code_and_test_citations_are_labeled_candidates() -> None:
    citations = render(_run(), RESULT, CITATIONS).split("### Citations")[1]

    [related] = [line for line in citations.splitlines() if line.startswith("- [E3]")]
    [test] = [line for line in citations.splitlines() if line.startswith("- [E4]")]
    assert related.endswith("related code (candidate)")
    assert test.endswith("candidate test (not run by CodeAtlas)")
    assert "candidate" not in citations.split("- [E2]")[1].split("\n")[0]


def test_mentions_references_and_html_are_neutralized() -> None:
    result = _result(
        overview="Thanks @octocat and @octo-org/reviewers, see #12 and owner/repo#3. "
        "<script>alert(1)</script> Mail me@example.com, and keep `@decorator #4` as code.",
    )
    result["risks"][0]["title"] = "Fixes #7 <b>now</b>"
    result["checklist"][0]["text"] = "Ask @hubot about GH-5."

    markdown = render(_run(), result, CITATIONS)

    assert "Thanks `@octocat` and `@octo-org/reviewers`, see `#12` and owner/repo`#3`." in markdown
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in markdown
    assert "<script>" not in markdown and "<b>" not in markdown
    assert "me@example.com" in markdown and "`@example`" not in markdown
    assert "keep `@decorator #4` as code" in markdown
    assert "#### R1: Fixes `#7` &lt;b&gt;now&lt;/b&gt;" in markdown
    assert "Ask `@hubot` about `GH-5`." in markdown


def test_stray_backticks_cannot_unwrap_a_mention() -> None:
    result = _result(overview="A ` stray backtick before @octocat and a \\ backslash.")

    markdown = render(_run(), result, CITATIONS)

    assert "A \\` stray backtick before `@octocat` and a \\\\ backslash." in markdown


def test_text_that_would_start_a_block_stays_a_paragraph() -> None:
    markdown = render(_run(), _result(overview="# Not a heading\n\n> nor a quote"), CITATIONS)

    assert "\n\\# Not a heading &gt; nor a quote\n" in markdown


def test_nothing_to_review_names_rule_risks_and_coverage() -> None:
    files = [
        {
            "path": ".env",
            "previous_path": None,
            "change": "added",
            "entry_type": "file",
            "count": 1,
            "additions": None,
            "deletions": None,
            "reviewed": False,
            "reason": "credential_file",
        },
        {
            "path": "assets/logo.png",
            "previous_path": None,
            "change": "added",
            "entry_type": "file",
            "count": 1,
            "additions": None,
            "deletions": None,
            "reviewed": False,
            "reason": "binary",
        },
    ]
    rule_risk = {
        "id": "R1",
        "title": "Credential file added",
        "severity": "high",
        "category": "security",
        "basis": "observed",
        "explanation": "The file's name marks it as holding credentials.",
        "suggested_check": "Confirm that the file holds no real secret.",
        "evidence_ids": [],
        "origin": "rule",
        "path": ".env",
    }
    result = _result(
        overview="",
        summary=[],
        risks=[rule_risk],
        checklist=[],
        tests={"changed": [], "candidates": [], "new_cases": []},
        coverage=_coverage(
            files=files,
            changed_files=2,
            reviewed_files=0,
            changed_lines_reviewed=0,
            context_items=0,
        ),
    )

    markdown = render(_run("nothing_to_review"), result, [])

    assert "Nothing to review: no changed file could be reviewed." in markdown
    assert "#### R1: Credential file added" in markdown
    assert "File: `.env` (found by a rule)" in markdown
    assert "- `.env`: possible credentials" in markdown
    assert "- `assets/logo.png`: binary file" in markdown
    assert "Reviewed 0 of 2 changed files" in markdown
    for absent in ("### Summary", "### Checklist", "### Tests", "### Citations"):
        assert absent not in markdown


def test_a_partial_review_says_so_under_the_overall_risk() -> None:
    files = [
        {
            "path": f"data/generated_{index:03d}.py",
            "previous_path": None,
            "change": "added",
            "entry_type": "file",
            "count": 1,
            "additions": 25,
            "deletions": 0,
            "reviewed": index <= 100,
            "reason": None if index <= 100 else "review_limit",
        }
        for index in range(1, 121)
    ]
    result = _result(
        overall_risk={"level": "low", "partial": True},
        coverage=_coverage(
            files=files, changed_files=120, reviewed_files=100, changed_lines_reviewed=2500
        ),
    )

    markdown = render(_run(), result, CITATIONS)

    assert (
        "**Overall risk: low**\n\nPartial review: the risk level covers only the reviewed files. "
        "20 of 120 changed files were not reviewed.\n"
    ) in markdown
    coverage = markdown[markdown.index("### Coverage") : markdown.index("### Citations")]
    assert "- `data/generated_101.py`: over the review size limits" in coverage
    # A long list is cut short.
    assert coverage.count("over the review size limits") == 10
    assert "- and 10 more files" in coverage


def test_a_review_without_risks_says_what_was_examined() -> None:
    result = _result(overall_risk={"level": "none", "partial": False}, risks=[])
    result["checklist"][0]["risk_ids"] = []

    markdown = render(_run(), result, CITATIONS)

    assert "**Overall risk: none**" in markdown
    risks = markdown[markdown.index("### Risks") : markdown.index("### Checklist")]
    assert (
        "No risks found. Examined 1 of 1 changed file (2 changed lines), with 2 related code and "
        "test excerpts as context."
    ) in risks


def test_omitted_items_are_counted() -> None:
    markdown = render(_run(), _result(omitted_items=2), CITATIONS)

    assert "2 items were removed because their citations could not be verified." in markdown
