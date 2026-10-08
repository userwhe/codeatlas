"""The load check's `--json` summary and the load test table (no API, no database)."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals import perf_check
from evals.load_report import highest_passing, load_levels, render_table
from evals.load_report import main as report_main
from evals.perf_check import LIMITS, Results, SetupError, read_commit, summarize

BASE_URL = "https://loadtest.example.dev"
COMMIT = "a" * 40
FAST = [0.010, 0.020, 0.040]
SUMMARY_KEYS = {"base_url", "commit", "users", "duration", "categories", "passed"}
CATEGORY_KEYS = {"count", "errors", "error_kinds", "p50", "p95", "limit", "passed"}


def make_results(**latencies: list[float]) -> Results:
    """Fast successful requests in every category, unless a category's latencies are given."""
    results = Results()
    for category in LIMITS:
        results.latencies[category].extend(latencies.get(category, FAST))
    return results


def summary(
    results: Results | None = None,
    *,
    users: int = 10,
    commit: str = COMMIT,
    duration: float = 120.0,
) -> dict[str, Any]:
    return summarize(
        make_results() if results is None else results,
        base_url=BASE_URL,
        commit=commit,
        users=users,
        duration=duration,
    )


def write_level(tmp_path: Path, users: int, *, passed: bool = True, **changes: Any) -> Path:
    """One level's `--json` file; a failing level's views miss their 500 ms limit."""
    results = make_results() if passed else make_results(view=[0.600] * 20)
    path = tmp_path / f"level-{users}.json"
    path.write_text(json.dumps(summary(results, users=users) | changes), encoding="utf-8")
    return path


# The --json summary -----------------------------------------------------------------------------


def test_summary_has_the_run_and_every_category() -> None:
    results = make_results(search=[index / 1000 for index in range(20, 0, -1)])

    data = summary(results, users=20, duration=60.0)

    assert set(data) == SUMMARY_KEYS
    assert (data["base_url"], data["commit"], data["users"], data["duration"]) == (
        BASE_URL,
        COMMIT,
        20,
        60.0,
    )
    assert list(data["categories"]) == list(LIMITS)
    assert all(set(category) == CATEGORY_KEYS for category in data["categories"].values())
    assert data["categories"]["search"] == {
        "count": 20,
        "errors": 0,
        "error_kinds": {},
        "p50": 0.010,
        "p95": 0.019,
        "limit": LIMITS["search"],
        "passed": True,
    }
    assert data["passed"] is True
    assert json.loads(json.dumps(data)) == data


def test_errors_count_as_requests_and_fail_their_category() -> None:
    results = make_results()
    results.errors["search"]["HTTP 500"] += 2
    results.errors["search"]["ReadTimeout"] += 1

    data = summary(results)

    search = data["categories"]["search"]
    assert (search["count"], search["errors"]) == (len(FAST) + 3, 3)
    assert search["error_kinds"] == {"HTTP 500": 2, "ReadTimeout": 1}
    assert search["p95"] == 0.040
    assert search["passed"] is False
    assert data["categories"]["view"]["passed"] is True
    assert data["passed"] is False


def test_a_p95_at_the_limit_fails() -> None:
    data = summary(make_results(view=[LIMITS["view"]] * 20))

    assert data["categories"]["view"]["p95"] == LIMITS["view"]
    assert data["categories"]["view"]["passed"] is False
    assert data["passed"] is False


def test_a_category_without_requests_fails() -> None:
    data = summary(make_results(reindex=[]))

    reindex = data["categories"]["reindex"]
    assert (reindex["count"], reindex["p50"], reindex["p95"]) == (0, None, None)
    assert reindex["passed"] is False
    assert data["passed"] is False


def test_read_commit_reads_the_running_version() -> None:
    def routes(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json={"commit": COMMIT})
        return httpx.Response(404)

    async def read(base_url: str) -> str:
        transport = httpx.MockTransport(routes)
        async with httpx.AsyncClient(base_url=base_url, transport=transport) as client:
            return await read_commit(client)

    assert asyncio.run(read(BASE_URL)) == COMMIT
    with pytest.raises(SetupError, match="HTTP 404"):
        asyncio.run(read(f"{BASE_URL}/elsewhere"))


@pytest.mark.parametrize(("passed", "code"), [(True, 0), (False, 1)])
def test_json_option_writes_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passed: bool, code: int
) -> None:
    data = summary(make_results() if passed else make_results(view=[0.600] * 20))

    async def fake_run(args: Any) -> dict[str, Any]:
        assert (args.base_url, args.users, args.duration) == (BASE_URL, 10, 30.0)
        return data

    monkeypatch.setattr(perf_check, "run", fake_run)
    path = tmp_path / "out" / "level-10.json"

    argv = ["--base-url", BASE_URL, "--origin", BASE_URL, "--duration", "30", "--json", str(path)]
    assert perf_check.main(argv) == code
    assert json.loads(path.read_text(encoding="utf-8")) == data


def test_setup_failure_exits_2_and_writes_no_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run(args: Any) -> dict[str, Any]:
        raise SetupError("sign-in start for octocat: HTTP 403")

    monkeypatch.setattr(perf_check, "run", fake_run)
    path = tmp_path / "level-10.json"

    assert perf_check.main(["--json", str(path)]) == 2
    assert not path.exists()


# The level table --------------------------------------------------------------------------------


def test_levels_render_in_level_order(tmp_path: Path) -> None:
    paths = [
        write_level(tmp_path, 40, passed=False),
        write_level(tmp_path, 10),
        write_level(tmp_path, 20),
    ]

    levels = load_levels(paths)
    table = render_table(levels)

    assert [level.users for level in levels] == [10, 20, 40]
    rows = [line for line in table.splitlines() if line.startswith("| ") and line[2].isdigit()]
    assert [row.split(" | ")[0] for row in rows] == ["| 10", "| 20", "| 40"]


def test_table_shows_each_category_against_its_limit_with_denominators(tmp_path: Path) -> None:
    passing = write_level(tmp_path, 10)
    failing = write_level(tmp_path, 20, passed=False)

    table = render_table(load_levels([passing, failing]))

    assert f"`{COMMIT}`" in table and BASE_URL in table and "120 s per level" in table
    header = (
        "| Users | search p95 (limit 1500 ms) | view p95 (limit 500 ms) "
        "| question p95 (limit 1000 ms) | reindex p95 (limit 1000 ms) | Result |"
    )
    assert header in table
    assert "| 10 | 40 ms; 0/3 errors | 40 ms; 0/3 errors |" in table
    assert "| FAIL: 600 ms; 0/20 errors |" in table
    assert table.count("| PASS") == 1 and table.count("| FAIL |") == 1


def test_error_rates_are_shown_when_requests_fail(tmp_path: Path) -> None:
    results = make_results(search=[0.010] * 9)
    results.errors["search"]["HTTP 500"] += 1
    path = tmp_path / "level-10.json"
    path.write_text(json.dumps(summary(results)), encoding="utf-8")

    table = render_table(load_levels([path]))

    assert "| FAIL: 10 ms; 1/10 errors (10.0%) |" in table


def test_the_highest_passing_level_is_marked(tmp_path: Path) -> None:
    paths = [
        write_level(tmp_path, 10),
        write_level(tmp_path, 20),
        write_level(tmp_path, 40, passed=False),
    ]

    levels = load_levels(paths)
    table = render_table(levels)

    assert highest_passing(levels) == 20
    marked = [line for line in table.splitlines() if "highest passing" in line]
    assert len(marked) == 1 and marked[0].startswith("| 20 |")
    assert "Highest level meeting every target: 20 users (40 users missed a target)." in table


def test_when_every_level_passes_the_result_is_at_least_the_highest(tmp_path: Path) -> None:
    levels = load_levels([write_level(tmp_path, users) for users in (10, 20, 40, 80)])
    table = render_table(levels)

    assert highest_passing(levels) == 80
    assert "Highest level meeting every target: at least 80 users" in table
    marked = [line for line in table.splitlines() if "highest passing" in line]
    assert len(marked) == 1 and marked[0].startswith("| 80 |")


def test_when_the_first_level_fails_no_level_passes(tmp_path: Path) -> None:
    levels = load_levels([write_level(tmp_path, 10, passed=False)])
    table = render_table(levels)

    assert highest_passing(levels) is None
    assert "Highest level meeting every target: none (10 users missed a target)." in table
    assert "highest passing" not in table


def test_a_pass_above_a_failing_level_does_not_count(tmp_path: Path) -> None:
    levels = load_levels(
        [
            write_level(tmp_path, 10),
            write_level(tmp_path, 20, passed=False),
            write_level(tmp_path, 40),
        ]
    )

    assert highest_passing(levels) == 10
    assert "Highest level meeting every target: 10 users (20 users missed a target)." in (
        render_table(levels)
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"commit": "b" * 40}, "different runs"),
        ({"base_url": "https://other.example.dev"}, "different runs"),
        ({"duration": 60.0}, "different runs"),
        ({"users": 10}, "level 10 appears twice"),
        ({"users": 0}, "users must be a positive integer"),
        ({"passed": "yes"}, "passed must be true or false"),
        ({"categories": {}}, "categories must be a non-empty object"),
    ],
)
def test_load_levels_rejects_files_that_do_not_fit(
    tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    first = write_level(tmp_path, 10)
    second = tmp_path / "other.json"
    second.write_text(json.dumps(summary(users=20) | changes), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_levels([first, second])


def test_load_levels_names_the_broken_file(tmp_path: Path) -> None:
    broken = tmp_path / "level-20.json"
    broken.write_text("{not json", encoding="utf-8")
    partial = tmp_path / "level-40.json"
    partial.write_text(json.dumps({"users": 40}), encoding="utf-8")

    with pytest.raises(ValueError, match="level-20.json: invalid JSON"):
        load_levels([broken])
    with pytest.raises(ValueError, match="level-40.json: missing base_url"):
        load_levels([partial])


def test_main_prints_the_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = [write_level(tmp_path, 20), write_level(tmp_path, 10)]

    assert report_main([str(path) for path in paths]) == 0
    assert capsys.readouterr().out == render_table(load_levels(paths))

    assert report_main([str(tmp_path / "missing.json")]) == 1
    assert "missing.json" in capsys.readouterr().err
