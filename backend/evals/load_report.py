"""Render the levels of a load test as one Markdown table (specs/004-pilot-deployment, R15).

Run from `backend/`:

    uv run python -m evals.load_report evals/out/load-test/level-*.json > load-test.md

Each file is the `--json` summary of one `evals/perf_check.py` run at one concurrency level (its
`users`), and every file must come from the same base URL, commit, and duration. The table has
one row per level, in increasing order, with each category's p95 latency and its failed
requests out of all its requests, and marks the highest level that meets every target.

The load test workflow runs the levels in increasing order and stops at the first one that misses
a target, so the highest level that meets every target is the last passing level below the first
failing one. When every level passes, the limit was not reached, and the result is "at least" the
highest level tested.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

LEVEL_KEYS = ("base_url", "commit", "users", "duration", "passed", "categories")
CATEGORY_KEYS = ("count", "errors", "p95", "limit", "passed")
MARK = "highest passing"


@dataclass(frozen=True)
class Category:
    name: str
    count: int
    errors: int
    p95: float | None
    limit: float
    passed: bool


@dataclass(frozen=True)
class Level:
    base_url: str
    commit: str
    users: int
    duration: float
    passed: bool
    categories: tuple[Category, ...]


def _is_count(value: object) -> bool:
    return type(value) is int and value >= 0


def _is_seconds(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value >= 0


def _missing(record: dict[str, object], keys: Sequence[str]) -> str | None:
    missing = [key for key in keys if key not in record]
    return ", ".join(missing) if missing else None


def _category(name: str, record: object, where: str) -> Category:
    where = f"{where}: category {name}"
    if not isinstance(record, dict):
        raise ValueError(f"{where} must be an object")
    if missing := _missing(record, CATEGORY_KEYS):
        raise ValueError(f"{where}: missing {missing}")
    count, errors, p95, limit, passed = (record[key] for key in CATEGORY_KEYS)
    if not _is_count(count) or not _is_count(errors) or errors > count:
        raise ValueError(f"{where}: count and errors must be counts, with errors <= count")
    if not (p95 is None or _is_seconds(p95)) or not _is_seconds(limit):
        raise ValueError(f"{where}: p95 and limit must be seconds")
    if not isinstance(passed, bool):
        raise ValueError(f"{where}: passed must be true or false")
    return Category(name, count, errors, p95, limit, passed)


def parse_level(record: object, where: str) -> Level:
    """Validate one level's summary. `where` names it in error messages."""
    if not isinstance(record, dict):
        raise ValueError(f"{where}: a level must be a JSON object")
    if missing := _missing(record, LEVEL_KEYS):
        raise ValueError(f"{where}: missing {missing}")
    base_url, commit, users, duration, passed, categories = (record[key] for key in LEVEL_KEYS)
    if not isinstance(base_url, str) or not base_url:
        raise ValueError(f"{where}: base_url must be a non-empty string")
    if not isinstance(commit, str) or not commit:
        raise ValueError(f"{where}: commit must be a non-empty string")
    if type(users) is not int or users < 1:
        raise ValueError(f"{where}: users must be a positive integer")
    if not _is_seconds(duration) or not duration:
        raise ValueError(f"{where}: duration must be a positive number of seconds")
    if not isinstance(passed, bool):
        raise ValueError(f"{where}: passed must be true or false")
    if not isinstance(categories, dict) or not categories:
        raise ValueError(f"{where}: categories must be a non-empty object")
    parsed = tuple(_category(name, value, where) for name, value in categories.items())
    return Level(base_url, commit, users, float(duration), passed, parsed)


def _run(level: Level) -> tuple[object, ...]:
    limits = tuple((category.name, category.limit) for category in level.categories)
    return (level.base_url, level.commit, level.duration, limits)


def load_levels(paths: Sequence[Path]) -> list[Level]:
    """Read and validate the level files of one load test run, in increasing level order."""
    levels: list[Level] = []
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name}: invalid JSON ({exc.msg})") from exc
        levels.append(parse_level(record, path.name))
    if not levels:
        raise ValueError("no level files")
    levels.sort(key=lambda level: level.users)
    for previous, level in pairwise(levels):
        if level.users == previous.users:
            raise ValueError(f"level {level.users} appears twice")
    first = levels[0]
    for level in levels[1:]:
        if _run(level) != _run(first):
            raise ValueError(
                f"levels {first.users} and {level.users} come from different runs (their base "
                "URL, commit, duration, or category limits differ)"
            )
    return levels


def highest_passing(levels: Sequence[Level]) -> int | None:
    """The last passing level below the first failing one, in increasing level order."""
    highest = None
    for level in sorted(levels, key=lambda level: level.users):
        if not level.passed:
            break
        highest = level.users
    return highest


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms"


def _cell(category: Category) -> str:
    text = f"{'-' if category.p95 is None else _ms(category.p95)}; "
    text += f"{category.errors}/{category.count} errors"
    if category.errors:
        text += f" ({category.errors / category.count:.1%})"
    return text if category.passed else f"FAIL: {text}"


def render_table(levels: Sequence[Level]) -> str:
    """The levels as one Markdown table, followed by the highest level meeting every target."""
    levels = sorted(levels, key=lambda level: level.users)
    first, highest = levels[0], highest_passing(levels)
    header = [f"{category.name} p95 (limit {_ms(category.limit)})" for category in first.categories]
    lines = [
        f"`perf_check` against {first.base_url} at commit `{first.commit}`, "
        f"{first.duration:g} s per level. Each cell is the category's p95 latency over its "
        "successful requests, then its failed requests out of all its requests.",
        "",
        "| Users | " + " | ".join(header) + " | Result |",
        "| ---: | " + " | ".join("---" for _ in header) + " | --- |",
    ]
    for level in levels:
        result = "PASS" if level.passed else "FAIL"
        if level.users == highest:
            result += f" ({MARK})"
        cells = [str(level.users), *(_cell(category) for category in level.categories), result]
        lines.append("| " + " | ".join(cells) + " |")
    failing = next((level.users for level in levels if not level.passed), None)
    if failing is None:
        verdict = f"at least {highest} users (every level passed)"
    else:
        reached = "none" if highest is None else f"{highest} users"
        verdict = f"{reached} ({failing} users missed a target)"
    lines += ["", f"Highest level meeting every target: {verdict}."]
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.load_report",
        description="Render load test levels (perf_check --json files) as one Markdown table.",
    )
    parser.add_argument("levels", nargs="+", type=Path, help="one perf_check --json file per level")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        levels = load_levels(args.levels)
    except (OSError, ValueError) as exc:
        print(f"load_report: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(render_table(levels))
    return 0


if __name__ == "__main__":
    sys.exit(main())
