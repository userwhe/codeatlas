"""The Code Review Bench runner's parsing, export, and comparison (no model calls, no network).

The files under `tests/fixtures/code-review-bench/offline/` are small excerpts of the benchmark's
`offline/` directory at commit e616e849755441da38f18bf3adba2c9583b03803 of
withmartian/code-review-benchmark (MIT license): three Sentry items and one Cal.com item of the
golden comments, their `results/benchmark_data.json` entries with one review of one tool, and
two tools' published evaluations by the Claude Opus 4.5 judge.
"""

import copy
import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from codeatlas.config import Settings, get_settings
from codeatlas.providers.answer_model import FakeAnswerModel
from evals import run_bench_eval
from evals.run_bench_eval import (
    DEFAULT_JUDGE,
    JUDGES,
    RAISED_LIMITS,
    TOOL,
    BenchItem,
    Candidate,
    Export,
    GoldenComment,
    ItemResult,
    PullRequest,
    Totals,
    candidate,
    comparisons,
    judge_environment,
    load_golden,
    main,
    model_dir_name,
    parse_golden,
    parse_pr_url,
    raised_settings,
    render_report,
    resolve_pull_request,
    review_archives,
    review_cost,
    select_items,
    step_command,
    tool_totals,
    write_exports,
)
from evals.run_qa_eval import Rate
from evals.run_review_eval import (
    DEFAULT_SET,
    EXIT_SETUP,
    Archive,
    EvalError,
    EvidenceRef,
    RiskRecord,
    apply_overlay,
    fixture_archive,
    load_items,
    read_archive,
    write_archive,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "code-review-bench" / "offline"
GOLDEN_DIR = FIXTURE / "golden_comments"
PUBLISHED = FIXTURE / "results" / "anthropic_claude-opus-4-5-20251101" / "evaluations.json"

GREPTILE_2 = "https://github.com/ai-code-review-evaluation/sentry-greptile/pull/2"
GREPTILE_5 = "https://github.com/ai-code-review-evaluation/sentry-greptile/pull/5"
SENTRY = "https://github.com/getsentry/sentry/pull/80528"
CALCOM = "https://github.com/calcom/cal.com/pull/22345"
ALL_URLS = [GREPTILE_2, GREPTILE_5, SENTRY, CALCOM]
SHA = "a" * 40


def golden_items() -> dict[str, BenchItem]:
    return load_golden(GOLDEN_DIR)


def published() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    return data


def risk(
    risk_id: str = "R1",
    labels: tuple[str, ...] = ("E1",),
    path: str | None = None,
    title: str = "Viewers can write",
) -> RiskRecord:
    return RiskRecord(
        risk_id,
        "high",
        "security",
        "observed",
        title,
        "The role check is gone, so any listed user may write.",
        "Add a test for a viewer.",
        labels,
        "model" if path is None else "rule",
        path,
    )


def ref(label: str = "E1", side: str = "after", start: int = 10, end: int = 12) -> EvidenceRef:
    return EvidenceRef(label, side, "app/auth.py", start, end)


def run_info(*, judged: bool = True) -> run_bench_eval.RunInfo:
    return run_bench_eval.RunInfo(
        started_at="2026-10-08T00:00:00+00:00",
        seconds=12.0,
        bench_commit="e" * 40,
        judge=JUDGES[DEFAULT_JUDGE],
        limit=None,
        exclude_flagged=False,
        model="gemini-3.8-flash",
        thinking_level="medium",
        limits=raised_settings(get_settings()),
        judged=judged,
    )


def offline_copy(tmp_path: Path) -> Path:
    target = tmp_path / "offline"
    shutil.copytree(FIXTURE, target)
    return target


# Golden comments -------------------------------------------------------------------------------


def test_parse_golden_keeps_every_item_keyed_by_its_url() -> None:
    data = json.loads((GOLDEN_DIR / "sentry.json").read_text(encoding="utf-8"))

    items = parse_golden(data, "sentry.json")

    assert list(items) == [GREPTILE_2, GREPTILE_5, SENTRY]
    flagged = items[GREPTILE_2]
    assert (flagged.owner, flagged.repository, flagged.number) == (
        "ai-code-review-evaluation",
        "sentry-greptile",
        2,
    )
    assert flagged.original_url == "https://github.com/getsentry/sentry/pull/92393"
    assert flagged.az_comment == "reviewed commit is not in the repo"
    assert flagged.flagged
    assert flagged.source_file == "sentry.json"
    assert len(flagged.comments) == 4
    assert flagged.comments[0].severity == "Critical"
    assert flagged.comments[0].category == "bug"
    mixed = items[GREPTILE_5]
    assert mixed.original_url is None
    assert mixed.az_comment == "there is no such PR, it is a mix of many PRs"
    plain = items[SENTRY]
    assert (plain.full_name, plain.number) == ("getsentry/sentry", 80528)
    assert (plain.original_url, plain.az_comment, plain.flagged) == (None, None, False)
    assert [c.comment for c in plain.comments] == [c["comment"] for c in data[2]["comments"]]


def test_load_golden_reads_sentry_then_cal_dot_com() -> None:
    items = golden_items()

    assert list(items) == ALL_URLS
    calcom = items[CALCOM]
    assert (calcom.owner, calcom.repository, calcom.number) == ("calcom", "cal.com", 22345)
    assert calcom.source_file == "cal_dot_com.json"
    assert [item.flagged for item in items.values()] == [True, True, False, False]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/getsentry/sentry/pull/80528", ("getsentry", "sentry", 80528)),
        ("https://github.com/calcom/cal.com/pull/8087", ("calcom", "cal.com", 8087)),
        ("https://github.com/getsentry/sentry/pull/0", None),
        ("https://github.com/getsentry/sentry/issues/1", None),
        ("https://github.com/getsentry/pull/1", None),
        ("http://github.com/getsentry/sentry/pull/1", None),
        ("https://github.com/getsentry/sentry/pull/1/files", None),
        (42, None),
    ],
)
def test_parse_pr_url(url: object, expected: tuple[str, str, int] | None) -> None:
    if expected is None:
        with pytest.raises(ValueError, match="pull request URL"):
            parse_pr_url(url)
    else:
        assert parse_pr_url(url) == expected


def golden_record(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "pr_title": "Title",
        "url": SENTRY,
        "comments": [{"comment": "A bug.", "severity": "High", "category": "bug"}],
    }
    return base | changes


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "a list"),
        ([golden_record(url="https://example.com/x")], "pull request URL"),
        ([golden_record(), golden_record()], "duplicate"),
        ([{"url": SENTRY, "comments": []}], "pr_title"),
        ([golden_record(comments={})], "comments"),
        (
            [golden_record(comments=[{"comment": "", "severity": "Low", "category": "x"}])],
            "comment",
        ),
        ([golden_record(comments=[{"comment": "A bug."}])], "severity"),
        ([golden_record(az_comment=3)], "az_comment"),
        ([golden_record(original_url=["x"])], "original_url"),
    ],
)
def test_parse_golden_rejects_invalid_records(data: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_golden(data, "sentry.json")


def test_select_items_limits_and_excludes_flagged_items() -> None:
    items = list(golden_items().values())

    assert [i.url for i in select_items(items, exclude_flagged=False, limit=None)] == ALL_URLS
    assert [i.url for i in select_items(items, exclude_flagged=False, limit=3)] == ALL_URLS[:3]
    assert [i.url for i in select_items(items, exclude_flagged=True, limit=None)] == [
        SENTRY,
        CALCOM,
    ]
    assert [i.url for i in select_items(items, exclude_flagged=True, limit=1)] == [SENTRY]


# Candidates and the export ---------------------------------------------------------------------


def test_candidate_text_names_the_cited_file_and_line_range() -> None:
    refs = {"E1": ref(), "E2": ref("E2", side="before", start=7, end=7)}

    made = candidate(risk(labels=("E1", "E2")), refs)

    assert made.text.startswith("Viewers can write")
    assert "app/auth.py lines 10-12" in made.text
    assert "app/auth.py line 7, before the change" in made.text
    assert "The role check is gone" in made.text
    assert (made.path, made.line) == ("app/auth.py", 10)


def test_candidate_of_a_rule_risk_names_its_file() -> None:
    made = candidate(risk(labels=(), path="config/.env", title="Credential file added"), {})

    assert "config/.env" in made.text
    assert (made.path, made.line) == ("config/.env", None)


def test_candidate_without_any_location_still_has_text() -> None:
    made = candidate(risk(labels=("E9",)), {})

    assert made.text.startswith("Viewers can write: The role check")
    assert (made.path, made.line) == (None, None)


def exports() -> list[Export]:
    first = Candidate("Viewers can write (app/auth.py lines 10-12): Why.", "app/auth.py", 10)
    second = Candidate("Unbounded query (app/db.py line 4): Why.", "app/db.py", 4)
    return [
        Export(SENTRY, "getsentry/sentry", (first, second)),
        Export(CALCOM, "calcom/cal.diy", ()),
    ]


def test_export_writes_one_review_per_url_and_one_candidate_per_risk(tmp_path: Path) -> None:
    offline = offline_copy(tmp_path)
    golden = golden_items()

    write_exports(offline, "claude-opus-4-5-20251101", exports(), golden, "2026-10-08T00:00:00Z")

    data = json.loads((offline / "results" / "benchmark_data.json").read_text(encoding="utf-8"))
    assert list(data) == ALL_URLS
    for url, entry in data.items():
        ours = [review for review in entry["reviews"] if review["tool"] == TOOL]
        others = [review for review in entry["reviews"] if review["tool"] != TOOL]
        assert [review["tool"] for review in others] == ["cubic-v2"]
        assert len(ours) == (1 if url in (SENTRY, CALCOM) else 0)
    review = next(r for r in data[SENTRY]["reviews"] if r["tool"] == TOOL)
    published_review = next(r for r in data[SENTRY]["reviews"] if r["tool"] == "cubic-v2")
    assert set(review) == set(published_review)
    assert (review["repo_name"], review["pr_url"]) == ("getsentry/sentry", SENTRY)
    assert [set(c) for c in review["review_comments"]] == [
        set(published_review["review_comments"][0])
    ] * 2
    assert review["review_comments"][0]["path"] == "app/auth.py"
    assert review["review_comments"][0]["line"] == 10
    assert "app/auth.py lines 10-12" in review["review_comments"][0]["body"]

    candidates_file = offline / "results" / "claude-opus-4-5-20251101" / "candidates.json"
    candidates = json.loads(candidates_file.read_text(encoding="utf-8"))
    assert candidates == {
        SENTRY: {
            TOOL: [
                {
                    "text": "Viewers can write (app/auth.py lines 10-12): Why.",
                    "path": "app/auth.py",
                    "line": 10,
                    "source": "risk",
                },
                {
                    "text": "Unbounded query (app/db.py line 4): Why.",
                    "path": "app/db.py",
                    "line": 4,
                    "source": "risk",
                },
            ]
        },
        CALCOM: {TOOL: []},
    }


def test_export_replaces_earlier_codeatlas_entries_and_keeps_other_tools(tmp_path: Path) -> None:
    offline = offline_copy(tmp_path)
    golden = golden_items()
    model_dir = offline / "results" / "anthropic_claude-opus-4-5-20251101"
    other = {"text": "A real issue.", "path": None, "line": None, "source": "extracted"}
    (model_dir / "candidates.json").write_text(
        json.dumps(
            {
                GREPTILE_2: {"cubic-v2": [other], TOOL: [other]},
                SENTRY: {TOOL: [other, other, other]},
            }
        )
    )
    write_exports(offline, model_dir.name, exports()[:1], golden, "t0")
    write_exports(offline, model_dir.name, exports()[:1], golden, "t1")

    candidates = json.loads((model_dir / "candidates.json").read_text(encoding="utf-8"))
    assert candidates[GREPTILE_2] == {"cubic-v2": [other]}
    assert len(candidates[SENTRY][TOOL]) == 2
    assert CALCOM not in candidates
    data = json.loads((offline / "results" / "benchmark_data.json").read_text(encoding="utf-8"))
    ours = [r for entry in data.values() for r in entry["reviews"] if r["tool"] == TOOL]
    assert len(ours) == 1
    assert ours[0]["review_comments"][0]["created_at"] == "t1"
    # The published evaluations are never touched.
    assert json.loads((model_dir / "evaluations.json").read_text(encoding="utf-8")) == published()


def test_export_refuses_a_url_whose_golden_comments_differ(tmp_path: Path) -> None:
    offline = offline_copy(tmp_path)
    golden = golden_items()
    changed = dataclasses.replace(golden[SENTRY], comments=(GoldenComment("Other.", "Low", "bug"),))

    with pytest.raises(ValueError, match="golden comments"):
        write_exports(offline, "judge", exports()[:1], {SENTRY: changed}, "t0")

    data_file = offline / "results" / "benchmark_data.json"
    data = json.loads(data_file.read_text(encoding="utf-8"))
    del data[SENTRY]
    data_file.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="not in results/benchmark_data.json"):
        write_exports(offline, "judge", exports()[:1], golden, "t0")


@pytest.mark.parametrize(
    ("model", "directory"),
    [
        ("claude-opus-4-5-20251101", "claude-opus-4-5-20251101"),
        ("anthropic/claude-opus-4-5-20251101", "anthropic_claude-opus-4-5-20251101"),
        (" openai/gpt-5.2 ", "openai_gpt-5.2"),
    ],
)
def test_model_dir_name_matches_the_benchmark_steps(model: str, directory: str) -> None:
    assert model_dir_name(model) == directory


# Comparison ------------------------------------------------------------------------------------


def test_tool_totals_sum_the_published_counts_over_the_selected_urls() -> None:
    evaluations = published()

    every = tool_totals(evaluations, ALL_URLS)
    unflagged = tool_totals(evaluations, [SENTRY, CALCOM])

    assert every["cubic-v2"] == Totals("cubic-v2", prs=4, tp=8, fp=5, fn=6)
    assert every["cubic-v2"].precision == Rate(8, 13)
    assert every["cubic-v2"].recall == Rate(8, 14)
    assert every["claude"] == Totals("claude", prs=4, tp=3, fp=5, fn=11)
    assert unflagged["cubic-v2"] == Totals("cubic-v2", prs=2, tp=3, fp=2, fn=1)
    assert unflagged["cubic-v2"].precision.value == pytest.approx(0.6)
    assert unflagged["cubic-v2"].recall.value == pytest.approx(0.75)
    assert unflagged["claude"].precision.value is None  # no candidates at all
    assert unflagged["claude"].recall == Rate(0, 4)


def test_tool_totals_leave_out_skipped_and_unselected_evaluations() -> None:
    evaluations: dict[str, Any] = {
        SENTRY: {"a": {"skipped": True, "reason": "No golden comments"}},
        CALCOM: {"a": {"skipped": False, "tp": 1, "fp": 2, "fn": 3}},
        GREPTILE_2: {"a": {"skipped": False, "tp": 9, "fp": 9, "fn": 9}},
    }

    assert tool_totals(evaluations, [SENTRY, CALCOM]) == {"a": Totals("a", 1, 1, 2, 3)}


def with_older_golden(evaluations: dict[str, Any], url: str) -> dict[str, Any]:
    """`evaluations` plus a tool `older` whose evaluation of `url` was made against golden
    comments that differ from the current ones, as some published evaluations were."""
    older = copy.deepcopy(evaluations[url]["cubic-v2"])
    older["false_negatives"].append({"golden_comment": "A comment the labels no longer have."})
    evaluations[url]["older"] = older
    return evaluations


def test_tool_totals_count_evaluations_made_against_older_golden_comments() -> None:
    golden = golden_items()
    evaluations = with_older_golden(published(), SENTRY)

    totals = tool_totals(evaluations, ALL_URLS, golden)

    assert totals["older"] == Totals("older", prs=1, tp=2, fp=1, fn=0, older=1)
    assert totals["cubic-v2"].older == totals["claude"].older == 0
    assert tool_totals(evaluations, ALL_URLS)["older"].older == 0  # not checked without labels


def test_report_lists_tools_judged_against_older_golden_comments_apart() -> None:
    items = golden_items()
    evaluations = with_older_golden(published(), SENTRY)
    tables = comparisons([items[SENTRY], items[CALCOM]], evaluations, None)
    info = run_info(judged=False)

    report = render_report(info, [ItemResult(items[SENTRY], quality_state="reviewed")], tables)

    main_table, _, older_table = report.partition("Judged against an older version")
    assert "| cubic-v2 | 2 | 3 | 2 | 1 |" in main_table
    assert "| older |" not in main_table
    assert "| older | 1 | 1 | 2 | 1 | 0 | 66.7% (2/3) | 100.0% (2/2) |" in older_table


def test_comparisons_with_and_without_flagged_items() -> None:
    items = list(golden_items().values())
    ours = {
        GREPTILE_2: {TOOL: {"skipped": False, "tp": 1, "fp": 0, "fn": 3}},
        SENTRY: {TOOL: {"skipped": False, "tp": 1, "fp": 1, "fn": 1}},
        CALCOM: {TOOL: {"skipped": False, "tp": 0, "fp": 2, "fn": 2}},
    }

    tables = comparisons([items[0], items[2], items[3]], published(), ours)

    assert list(tables) == ["all", "without_flagged"]
    every, unflagged = tables["all"], tables["without_flagged"]
    assert every[0] == Totals(TOOL, prs=3, tp=2, fp=3, fn=6)
    assert [row.tool for row in every] == [TOOL, "claude", "cubic-v2"]
    assert every[2] == Totals("cubic-v2", prs=3, tp=6, fp=3, fn=2)
    assert unflagged[0] == Totals(TOOL, prs=2, tp=1, fp=3, fn=3)
    assert unflagged[2] == Totals("cubic-v2", prs=2, tp=3, fp=2, fn=1)


def test_comparisons_without_a_judged_run_show_the_published_tools_only() -> None:
    items = list(golden_items().values())

    tables = comparisons(items, published(), None)

    assert [row.tool for row in tables["all"]] == ["claude", "cubic-v2"]
    assert tables["all"][1] == Totals("cubic-v2", prs=4, tp=8, fp=5, fn=6)


# Judge steps -----------------------------------------------------------------------------------


def test_judge_steps_run_for_codeatlas_without_dedup_groups(tmp_path: Path) -> None:
    command = step_command("/usr/bin/uv", tmp_path, "step3_judge_comments")

    assert command[:4] == ["/usr/bin/uv", "run", "--directory", str(tmp_path)]
    assert command[4:7] == ["python", "-m", "code_review_benchmark.step3_judge_comments"]
    assert command[command.index("--tool") + 1] == TOOL
    # Earlier CodeAtlas results in the same directory are judged again, not kept.
    assert "--force" in command
    assert "--dedup-groups" not in command


def test_judge_environment_names_the_judge_and_keeps_other_secrets_out() -> None:
    judge = JUDGES[DEFAULT_JUDGE]
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "UV_CACHE_DIR": "/cache",
        "GEMINI_API_KEY": "gemini-secret",
        "GITHUB_TOKEN": "github-secret",
        "VIRTUAL_ENV": "/backend/.venv",
        "MARTIAN_MODEL": "openai/gpt-4o-mini",
    }

    env = judge_environment(judge, "judge-secret", base)

    assert env["MARTIAN_API_KEY"] == "judge-secret"
    assert env["MARTIAN_MODEL"] == "claude-opus-4-5-20251101"
    assert env["MARTIAN_BASE_URL"] == "https://api.anthropic.com/v1/"
    assert (env["PATH"], env["HOME"], env["UV_CACHE_DIR"]) == ("/usr/bin", "/home/x", "/cache")
    assert not {"GEMINI_API_KEY", "GITHUB_TOKEN", "VIRTUAL_ENV"} & set(env)


def test_judges_read_the_published_results_of_the_same_judge() -> None:
    assert DEFAULT_JUDGE == "claude-opus-4-5-20251101"
    assert JUDGES[DEFAULT_JUDGE].published_dir == "anthropic_claude-opus-4-5-20251101"
    assert JUDGES["gpt-5.2"].published_dir == "openai_gpt-5.2"
    assert JUDGES["gpt-5.2"].base_url == "https://api.openai.com/v1"


# GitHub ----------------------------------------------------------------------------------------


def test_resolve_pull_request_follows_redirects_and_finds_the_merge_base() -> None:
    seen: list[httpx.Request] = []
    base, head, merge_base = "b" * 40, "c" * 40, "d" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/repos/calcom/cal.com/pulls/22345":
            return httpx.Response(301, headers={"Location": "/repositories/350360184/pulls/22345"})
        if path == "/repositories/350360184/pulls/22345":
            return httpx.Response(
                200,
                json={
                    "title": "fix: insights",
                    "body": None,
                    "base": {"sha": base, "repo": {"full_name": "calcom/cal.diy"}},
                    "head": {"sha": head, "repo": None},
                },
            )
        if path == f"/repos/calcom/cal.diy/compare/{base}...{head}":
            return httpx.Response(
                200,
                json={
                    "merge_base_commit": {"sha": merge_base},
                    "files": [
                        {"filename": "b.ts", "status": "renamed", "previous_filename": "a.ts"},
                        {"filename": "c.ts", "status": "modified"},
                    ],
                },
            )
        return httpx.Response(404)

    with run_bench_eval.github_client("read-only-token", httpx.MockTransport(handler)) as client:
        pull = resolve_pull_request(client, golden_items()[CALCOM])

    assert pull == PullRequest(
        "calcom/cal.diy", "fix: insights", "", base, head, merge_base, {"b.ts": "a.ts"}
    )
    assert all(r.headers["Authorization"] == "Bearer read-only-token" for r in seen)


def test_resolve_pull_request_reports_an_unreadable_pull_request() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404))

    with (
        run_bench_eval.github_client("", transport) as client,
        pytest.raises(EvalError, match="HTTP 404"),
    ):
        resolve_pull_request(client, golden_items()[SENTRY])


# Reviews and the report ------------------------------------------------------------------------


def test_raised_settings_change_only_the_repository_limits() -> None:
    settings = get_settings()

    raised = raised_settings(settings)

    for name, value in RAISED_LIMITS.items():
        assert getattr(raised, name) == value > getattr(settings, name)
    assert raised.review_max_files == settings.review_max_files
    assert raised.max_file_bytes == settings.max_file_bytes
    assert settings.max_files_per_snapshot == Settings().max_files_per_snapshot


def fixture_pull() -> tuple[PullRequest, bytes, bytes]:
    """The offline review item `review-app-s01` as a pull request with real archives."""
    item = next(i for i in load_items(DEFAULT_SET) if i.id == "review-app-s01")
    base_bytes = fixture_archive("review-app", item.commit_sha)
    base = read_archive(base_bytes)
    head_bytes = write_archive(Archive(base.top, apply_overlay(base.files, item.overlay)))
    pull = PullRequest(
        "octo-org/review-app", item.title, item.body, SHA, "e" * 40, item.commit_sha, {}
    )
    return pull, head_bytes, base_bytes


def test_review_archives_turns_each_risk_into_a_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "fake_review_model_mode", "ok")
    pull, head_bytes, base_bytes = fixture_pull()

    result = review_archives(
        golden_items()[SENTRY], pull, head_bytes, base_bytes, settings, FakeAnswerModel(settings)
    )

    assert result.failures == []
    assert result.reviewed
    assert len(result.candidates) == len(result.risks) >= 1
    assert "app/auth/permissions.py line" in result.candidates[0].text
    assert result.candidates[0].path == "app/auth/permissions.py"
    assert result.usage["model_calls"] == 1


def test_review_archives_records_an_exceeded_limit_as_a_failure() -> None:
    settings = get_settings().model_copy(update={"max_files_per_snapshot": 1})
    pull, head_bytes, base_bytes = fixture_pull()

    result = review_archives(
        golden_items()[SENTRY], pull, head_bytes, base_bytes, settings, FakeAnswerModel(settings)
    )

    assert not result.reviewed
    assert result.failures == ["limit_exceeded: max_files_per_snapshot (1)"]


def test_review_cost_uses_the_prices_of_the_run_date() -> None:
    usage = {"input_tokens": 1_000_000, "output_tokens": 100_000, "thinking_tokens": 100_000}

    assert review_cost(usage, "2026-10-08") == pytest.approx(0.75 + 0.2 * 3.75)
    assert review_cost(usage, "2027-01-01") == pytest.approx(1.50 + 0.2 * 7.50)


def test_report_shows_denominators_flagged_items_and_limits() -> None:
    items = golden_items()
    results = [
        ItemResult(items[GREPTILE_2], failures=["limit_exceeded: max_files_per_snapshot (1)"]),
        ItemResult(
            items[SENTRY],
            quality_state="reviewed",
            candidates=[Candidate("A (x.py line 1): B.", "x.py", 1)],
            evaluation={"skipped": False, "tp": 1, "fp": 0, "fn": 1},
        ),
    ]
    tables = comparisons([items[SENTRY]], published(), {SENTRY: {TOOL: results[1].evaluation}})
    report = render_report(run_info(), results, tables)

    assert f"| {TOOL} | 1 | 1 | 0 | 1 | 100.0% (1/1) | 50.0% (1/2) |" in report
    assert "| cubic-v2 | 1 | 2 | 1 | 0 | 66.7% (2/3) | 100.0% (2/2) |" in report
    assert "reviewed commit is not in the repo" in report
    assert "max_files_per_snapshot" in report
    assert "limit_exceeded" in report


@pytest.fixture
def quiet_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` reconfigures the root logger for a command-line run; keep the test session's."""
    monkeypatch.setattr(run_bench_eval, "configure_logging", lambda level: None)


@pytest.mark.usefixtures("quiet_main")
def test_fake_externals_stop_the_run_before_any_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("no download expected")

    monkeypatch.setattr(run_bench_eval, "download_archive", no_network)

    assert main(["--bench-commit", "e" * 40, "--out", str(tmp_path), "--skip-judge"]) == (
        EXIT_SETUP
    )
    assert list(tmp_path.iterdir()) == []


def test_bench_commit_must_be_a_full_sha() -> None:
    with pytest.raises(SystemExit) as raised:
        run_bench_eval.parse_args(["--bench-commit", "e616e84"])

    assert raised.value.code == 2
