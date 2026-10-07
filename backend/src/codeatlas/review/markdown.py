"""The Markdown copy of a finished review (research R11, FR-015).

A pure function of the stored result and the cited evidence, so the copy is the same wherever it
is pasted. Sections come in this order: the overall risk level, the summary, the risks, the
checklist (`- [ ]` items), and the tests, then the coverage and the citations. Every citation is
a link to the cited lines on GitHub at the cited commit; related code and test excerpts are
labeled as candidates (FR-012).

Review text can repeat words from an untrusted pull request, and the copy is meant to be pasted
into GitHub by hand, so text that would act there is neutralized: `@name` mentions and `#123`
references are wrapped in inline code, and raw HTML is escaped. Inline code in the text is kept
as it is, since GitHub shows it literally. Each text becomes one line, so it cannot start a new
block such as a heading.
"""

import html
import re
from collections.abc import Mapping, Sequence
from typing import Any

from codeatlas.models import AnalysisRun

SHORT_SHA = 7
MAX_NOT_REVIEWED_LISTED = 10
NOTHING_TO_REVIEW = "Nothing to review: no changed file could be reviewed."
REASON_LABELS = {
    "excluded_directory": "excluded directory",
    "credential_file": "possible credentials",
    "generated": "generated or lock file",
    "binary": "binary file",
    "unsupported_encoding": "not UTF-8 text",
    "too_large": "over the file size limit",
    "link": "link (not followed)",
    "unsafe_path": "unsafe or unsupported entry",
    "review_limit": "over the review size limits",
}
SOURCE_LABELS = {
    "reference": "related code (candidate)",
    "test": "candidate test (not run by CodeAtlas)",
}

# An inline code span: a run of backticks, then text, then a run of the same length.
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
# What GitHub turns into a notification or a cross-reference: `@user` and `@org/team` mentions
# (not the `@` of an email address), and `#123` and `GH-123` references, also after `owner/repo`.
_ACTIVE = re.compile(r"(?<![\w@])@[A-Za-z0-9][\w-]*(?:/[\w.-]+)?|#\d+\b|\bGH-\d+\b", re.IGNORECASE)
# Text that would open a block if it began a line: a heading, quote, list item, rule, or table.
_BLOCK_START = re.compile(r"^(?:[#>+*=|-]|\d+[.)])")


def render(
    run: AnalysisRun, result: Mapping[str, Any], citations: Sequence[Mapping[str, Any]]
) -> str:
    """The review of `run`, whose stored result is `result`, as Markdown.

    `citations` are the cited evidence items with their `github_url`, as the API returns them; a
    cited label without one is shown as plain text.
    """
    links = {citation["label"]: citation for citation in citations}
    coverage = result["coverage"]
    head = (run.head_sha or "")[:SHORT_SHA]
    blocks = [
        f"## CodeAtlas review of #{run.pull_request_number} at {head}",
        *_overall_risk(result["overall_risk"], coverage),
    ]
    if run.quality_state == "nothing_to_review":
        blocks.append(NOTHING_TO_REVIEW)
        if result["risks"]:
            blocks.extend(_risks(result["risks"], coverage, links))
    else:
        blocks.extend(_summary(result, links))
        blocks.extend(_risks(result["risks"], coverage, links))
        blocks.extend(_checklist(result["checklist"]))
        blocks.extend(_tests(result["tests"], links))
    blocks.extend(_coverage(coverage, int(result.get("omitted_items") or 0)))
    if citations:
        blocks.extend(_citations(citations))
    return "\n\n".join(blocks) + "\n"


# Sections ---------------------------------------------------------------------------------------


def _overall_risk(overall: Mapping[str, Any], coverage: Mapping[str, Any]) -> list[str]:
    blocks = [f"**Overall risk: {overall['level']}**"]
    if overall.get("partial"):
        missing = coverage["changed_files"] - coverage["reviewed_files"]
        verb = "was" if missing == 1 else "were"
        blocks.append(
            "Partial review: the risk level covers only the reviewed files. "
            f"{missing} of {_count(coverage['changed_files'], 'changed file')} {verb} not "
            "reviewed."
        )
    return blocks


def _summary(result: Mapping[str, Any], links: Mapping[str, Mapping[str, Any]]) -> list[str]:
    blocks = ["### Summary"]
    if result["overview"]:
        blocks.append(_leading(result["overview"]))
    for group in result["summary"]:
        blocks.append(f"**{_code(group['area'])}**")
        blocks.append(
            "\n".join(
                f"- {point['change'].capitalize()}: {_text(point['text'])}"
                f"{_refs(point['evidence_ids'], links)}"
                for point in group["points"]
            )
        )
    return blocks


def _risks(
    risks: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Any],
    links: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    blocks = ["### Risks"]
    if not risks:
        blocks.append(f"No risks found. {_examined('Examined', coverage)}")
        return blocks
    for risk in risks:
        blocks.append(f"#### {risk['id']}: {_text(risk['title'])}")
        blocks.append(
            f"Severity: {risk['severity']} · Category: {risk['category'].replace('_', ' ')} · "
            f"Basis: {risk['basis']}"
        )
        if risk.get("path"):
            blocks.append(f"File: {_code(risk['path'])} (found by a rule)")
        blocks.append(_leading(risk["explanation"]))
        blocks.append(f"Suggested check: {_text(risk['suggested_check'])}")
        if risk["evidence_ids"]:
            blocks.append(f"Evidence: {_links(risk['evidence_ids'], links)}")
    return blocks


def _checklist(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["### Checklist", "No checklist items."]
    lines = []
    for item in items:
        references = [*(_code(path) for path in item["paths"]), *item["risk_ids"]]
        lines.append(f"- [ ] {_text(item['text'])} ({'; '.join(references)})")
    return ["### Checklist", "\n".join(lines)]


def _tests(tests: Mapping[str, Any], links: Mapping[str, Mapping[str, Any]]) -> list[str]:
    changed = [f"- {_code(test['path'])} ({test['change']})" for test in tests["changed"]]
    candidates = [
        f"- {_code(test['path'])}: {_text(test['reason'])}{_refs(test['evidence_ids'], links)}"
        for test in tests["candidates"]
    ]
    new_cases = []
    for case in tests["new_cases"]:
        where = f" Where: {_text(case['location_hint'])}" if case.get("location_hint") else ""
        new_cases.append(
            f"- {_leading(case['behavior'])}{where}{_refs(case['evidence_ids'], links)}"
        )
    return [
        "### Tests",
        "**Changed in this pull request**",
        "\n".join(changed) or "The pull request changes no tests.",
        "**Candidate tests (not run by CodeAtlas)**",
        "\n".join(candidates) or "No candidate tests were found.",
        "**Suggested new tests**",
        "\n".join(new_cases) or "No new tests are suggested.",
    ]


def _coverage(coverage: Mapping[str, Any], omitted_items: int) -> list[str]:
    blocks = ["### Coverage", _examined("Reviewed", coverage)]
    left_out = [entry for entry in coverage["files"] if not entry["reviewed"]]
    if left_out:
        lines = [_not_reviewed(entry) for entry in left_out[:MAX_NOT_REVIEWED_LISTED]]
        more = sum(int(entry.get("count") or 1) for entry in left_out[MAX_NOT_REVIEWED_LISTED:])
        if more:
            lines.append(f"- and {_count(more, 'more file')}")
        blocks.extend(["Not reviewed:", "\n".join(lines)])
    if omitted_items:
        blocks.append(
            "1 item was removed because its citations could not be verified."
            if omitted_items == 1
            else f"{omitted_items} items were removed because their citations could not be "
            "verified."
        )
    return blocks


def _citations(citations: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = []
    for citation in citations:
        start, end = citation["start_line"], citation["end_line"]
        lines_text = f"line {start}" if start == end else f"lines {start}-{end}"
        sha = _code(str(citation["commit_sha"])[:SHORT_SHA])
        side = (
            f"before (merge base {sha})" if citation["side"] == "before" else f"after (head {sha})"
        )
        source = SOURCE_LABELS.get(citation["source_type"])
        lines.append(
            f"- [{citation['label']}]({citation['github_url']}): {_code(citation['path'])} "
            f"{lines_text}, {side}{f', {source}' if source else ''}"
        )
    return ["### Citations", "\n".join(lines)]


# Pieces -----------------------------------------------------------------------------------------


def _examined(verb: str, coverage: Mapping[str, Any]) -> str:
    """What a review examined (FR-009)."""
    return (
        f"{verb} {coverage['reviewed_files']} of "
        f"{_count(coverage['changed_files'], 'changed file')} "
        f"({_count(coverage['changed_lines_reviewed'], 'changed line')}), with "
        f"{_context(coverage['context_items'])} as context."
    )


def _not_reviewed(entry: Mapping[str, Any]) -> str:
    reason = entry.get("reason") or "not reviewed"
    label = REASON_LABELS.get(reason, reason.replace("_", " "))
    if entry.get("entry_type") == "directory":
        return f"- {_code(entry['path'])} ({_count(int(entry.get('count') or 0), 'file')}): {label}"
    return f"- {_code(entry['path'])}: {label}"


def _refs(labels: Sequence[str], links: Mapping[str, Mapping[str, Any]]) -> str:
    """` ([E1](url), [E2](url))` after an item, or nothing when it cites nothing."""
    return f" ({_links(labels, links)})" if labels else ""


def _links(labels: Sequence[str], links: Mapping[str, Mapping[str, Any]]) -> str:
    return ", ".join(
        f"[{label}]({links[label]['github_url']})" if label in links else label for label in labels
    )


def _count(count: int, one: str) -> str:
    return f"{count} {one if count == 1 else one + 's'}"


def _context(count: int) -> str:
    if count == 1:
        return "1 related code or test excerpt"
    return f"{count} related code and test excerpts"


def _leading(value: str) -> str:
    """Review text that starts a paragraph or a list item, where it must not open another kind
    of block: its first marker is escaped."""
    text = _text(value)
    marker = _BLOCK_START.match(text)
    if marker is None:
        return text
    if text[0].isdigit():  # `1.` starts a list, `1\.` does not
        end = marker.end() - 1
        return f"{text[:end]}\\{text[end:]}"
    return f"\\{text}"


def _text(value: str) -> str:
    """Review text as one line of Markdown that cannot act on GitHub when pasted."""
    flat = " ".join(value.split())
    parts: list[str] = []
    position = 0
    for match in _CODE_SPAN.finditer(flat):
        parts.append(_plain(flat[position : match.start()]))
        parts.append(match.group(0))
        position = match.end()
    parts.append(_plain(flat[position:]))
    return "".join(parts)


def _plain(text: str) -> str:
    """Text outside inline code: HTML escaped, backslashes and stray backticks escaped so they
    cannot open or close a code span, then mentions and references wrapped in inline code."""
    escaped = html.escape(text, quote=False).replace("\\", "\\\\").replace("`", "\\`")
    return _ACTIVE.sub(lambda match: f"`{match.group(0)}`", escaped)


def _code(value: str) -> str:
    """`value` as inline code, fenced by more backticks than any run inside it."""
    flat = " ".join(value.splitlines())
    longest = max((len(run) for run in re.findall(r"`+", flat)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if flat.startswith("`") or flat.endswith("`") else ""
    return f"{fence}{pad}{flat}{pad}{fence}"
