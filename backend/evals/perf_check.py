"""Performance check for SC-007 against a running API in fake mode.

Run from `backend/`:

    uv run python -m evals.perf_check --base-url http://localhost:8000 --users 10 --duration 120

Start the API and the worker with the fake GitHub gateway and fake model providers, with daily
question limits high enough for the run (the default of 20 per workspace, or 30 across the pilot,
turns most question submissions into 429 errors), and with rate limiting off:

    export CODEATLAS_ENV=development CODEATLAS_FAKE_EXTERNALS=1 DAILY_QUESTION_LIMIT=100000 \
        RATE_LIMIT_PER_MINUTE=0 PILOT_DAILY_QUESTION_LIMIT=100000 PILOT_DAILY_REVIEW_LIMIT=1000
    export TOKEN_ENCRYPTION_KEY=<a Fernet key>   # see specs/001-repository-qa/quickstart.md
    uv run uvicorn codeatlas.api.app:app --port 8000
    uv run python -m codeatlas.jobs.worker

`--origin` must equal the API's `APP_ORIGIN`. The fake gateway knows two users, `octocat` and
`hubot`, so clients alternate between them: every client signs in with its own session, and the
clients share two workspaces. Each workspace connects the repository given by
`--github-repository-id` (default: the `sample-app` fixture) unless it is already connected, and
the check waits until it has a ready snapshot.

Each client then sends requests back to back until the duration ends, mixing searches (all
modes), views (repository list and detail, file tree, and file lines), question submissions, and
re-index submissions. Latency is measured on the client, from sending a request to receiving its
response. Submissions measure the acknowledgement only; the worker answers them afterwards. A
category passes when it has no errors and its p95 is under the SC-007 limit. The exit status is 0
when every category passes, 1 when one fails, and 2 when setup fails.
"""

import argparse
import asyncio
import math
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

FAKE_LOGINS = ("octocat", "hubot")
SAMPLE_APP_ID = 2001
SESSION_COOKIE = "codeatlas_session"
STATE_COOKIE = "oauth_state"
REQUEST_TIMEOUT = 30.0
READY_TIMEOUT = 300.0
MAX_BROWSE_DIRECTORIES = 25

# p95 limits in seconds (SC-007), and how often each category is picked.
LIMITS = {"search": 1.5, "view": 0.5, "question": 1.0, "reindex": 1.0}
WEIGHTS = {"search": 4, "view": 4, "question": 1, "reindex": 1}
SEARCHES = [
    ("text", "access"),
    ("text", "return"),
    ("path", "utils"),
    ("path", "README.md"),
    ("symbol", "check_repository_access"),
    ("symbol", "User"),
    ("docs", "setup"),
    ("docs", "access control"),
]
VIEWS = ("repositories", "repository", "tree", "file")
QUESTIONS = [
    "Where are repository permissions checked?",
    "How is a user formatted for display?",
    "Which roles can read a repository?",
]


class SetupError(Exception):
    """The check could not sign in or get a ready repository."""


@dataclass(frozen=True)
class Target:
    """A workspace's indexed repository and paths to browse in its active snapshot."""

    repository_id: str
    snapshot_id: str
    directories: list[str]
    files: list[str]


@dataclass(frozen=True)
class Call:
    category: str
    operation: str
    method: str
    url: str
    expected_status: int
    params: dict[str, str] | None = None
    json: dict[str, Any] | None = None


@dataclass
class Results:
    latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    operations: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    errors: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))


def _expect(response: httpx.Response, status: int, action: str) -> dict[str, Any]:
    if response.status_code != status:
        raise SetupError(f"{action}: HTTP {response.status_code} {response.text[:300]}")
    body: dict[str, Any] = response.json() if response.content else {}
    return body


async def sign_in(base_url: str, login: str) -> str:
    """Sign in through the OAuth routes and return the session cookie value.

    The API marks its cookies `Secure`, so httpx keeps them off plain-http requests; the state
    cookie is sent back explicitly instead.
    """
    async with httpx.AsyncClient(base_url=base_url, timeout=REQUEST_TIMEOUT) as client:
        start = await client.get("/auth/github/login")
        if start.status_code != 302 or STATE_COOKIE not in start.cookies:
            raise SetupError(f"sign-in start for {login}: HTTP {start.status_code}")
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        callback = await client.get(
            "/auth/github/callback",
            params={"code": f"fake:{login}", "state": state},
            headers={"Cookie": f"{STATE_COOKIE}={start.cookies[STATE_COOKIE]}"},
        )
        if callback.headers.get("location") != "/repositories" or (
            SESSION_COOKIE not in callback.cookies
        ):
            raise SetupError(
                f"sign-in callback for {login}: HTTP {callback.status_code} to "
                f"{callback.headers.get('location')!r}; is the API running in fake mode?"
            )
        return callback.cookies[SESSION_COOKIE]


async def wait_for_snapshot(client: httpx.AsyncClient, repository_id: str) -> str:
    """Wait until the repository has a ready snapshot and return its ID."""
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        response = await client.get(f"/v1/repositories/{repository_id}")
        repository = _expect(response, 200, "read the repository")
        if repository["active_snapshot"] is not None:
            snapshot_id: str = repository["active_snapshot"]["id"]
            return snapshot_id
        if repository["state"] in ("failed", "rejected"):
            job = repository["latest_indexing_job"] or {}
            raise SetupError(f"indexing {repository['full_name']} failed: {job.get('error')}")
        await asyncio.sleep(1)
    raise SetupError(f"repository {repository_id} had no ready snapshot after {READY_TIMEOUT} s")


async def browse_paths(client: httpx.AsyncClient, snapshot_id: str) -> tuple[list[str], list[str]]:
    """Walk the file tree breadth first and return some directories and files."""
    directories, files, pending = [""], [], [""]
    while pending and len(directories) < MAX_BROWSE_DIRECTORIES:
        path = pending.pop(0)
        response = await client.get(f"/v1/snapshots/{snapshot_id}/tree", params={"path": path})
        for entry in _expect(response, 200, "read the file tree")["entries"]:
            child = f"{path}/{entry['name']}" if path else entry["name"]
            if entry["type"] == "directory":
                directories.append(child)
                pending.append(child)
            else:
                files.append(child)
    if not files:
        raise SetupError("the snapshot has no files to view")
    return directories, files


async def prepare(client: httpx.AsyncClient, github_repository_id: int) -> Target:
    """Connect the repository in this client's workspace if needed and wait until it is ready."""
    response = await client.get("/v1/github/repositories", params={"limit": 100})
    listing = _expect(response, 200, "list connectable repositories")["items"]
    entry = next(
        (item for item in listing if item["github_repository_id"] == github_repository_id), None
    )
    if entry is None:
        raise SetupError(f"GitHub repository {github_repository_id} is not accessible")
    repository_id = entry["connected_repository_id"]
    if repository_id is None:
        response = await client.post(
            "/v1/repositories",
            json={"github_repository_id": github_repository_id, "accept_external_processing": True},
        )
        repository_id = _expect(response, 202, "connect the repository")["repository"]["id"]
    snapshot_id = await wait_for_snapshot(client, repository_id)
    directories, files = await browse_paths(client, snapshot_id)
    return Target(repository_id, snapshot_id, directories, files)


def pick_call(target: Target, rng: random.Random) -> Call:
    category = rng.choices(list(WEIGHTS), weights=list(WEIGHTS.values()))[0]
    if category == "search":
        mode, query = rng.choice(SEARCHES)
        body = {"snapshot_id": target.snapshot_id, "query": query, "mode": mode}
        return Call(category, f"search {mode}", "POST", "/v1/search", 200, json=body)
    if category == "question":
        body = {"repository_id": target.repository_id, "question": rng.choice(QUESTIONS)}
        return Call(category, "question", "POST", "/v1/analysis-runs", 202, json=body)
    if category == "reindex":
        url = f"/v1/repositories/{target.repository_id}/index"
        return Call(category, "reindex", "POST", url, 202, json={})
    view = rng.choice(VIEWS)
    if view == "repositories":
        return Call(category, "view repositories", "GET", "/v1/repositories", 200)
    if view == "repository":
        url = f"/v1/repositories/{target.repository_id}"
        return Call(category, "view repository", "GET", url, 200)
    if view == "tree":
        url = f"/v1/snapshots/{target.snapshot_id}/tree"
        params = {"path": rng.choice(target.directories)}
        return Call(category, "view tree", "GET", url, 200, params=params)
    url = f"/v1/snapshots/{target.snapshot_id}/file"
    return Call(category, "view file", "GET", url, 200, params={"path": rng.choice(target.files)})


async def run_client(
    client: httpx.AsyncClient,
    target: Target,
    deadline: float,
    rng: random.Random,
    results: Results,
) -> None:
    while time.monotonic() < deadline:
        call = pick_call(target, rng)
        started = time.perf_counter()
        try:
            response = await client.request(
                call.method, call.url, params=call.params, json=call.json
            )
        except httpx.HTTPError as exc:
            results.errors[call.category][type(exc).__name__] += 1
            continue
        elapsed = time.perf_counter() - started
        if response.status_code != call.expected_status:
            results.errors[call.category][f"HTTP {response.status_code}"] += 1
            continue
        results.latencies[call.category].append(elapsed)
        results.operations[call.operation].append(elapsed)


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    return ordered[max(1, math.ceil(fraction * len(ordered))) - 1]


def _ms(values: list[float], fraction: float) -> str:
    return f"{percentile(values, fraction) * 1000:.0f}" if values else "-"


def report(results: Results, args: argparse.Namespace, elapsed: float) -> bool:
    count = sum(len(values) for values in results.latencies.values())
    count += sum(sum(errors.values()) for errors in results.errors.values())
    users = min(args.users, len(FAKE_LOGINS))
    print(f"SC-007 check against {args.base_url}: {args.users} clients ({users} fake users)")
    print(f"{count} requests in {elapsed:.0f} s ({count / elapsed:.1f} per second)")
    print()
    print(
        f"{'category':<10} {'count':>7} {'p50 ms':>8} {'p95 ms':>8} {'limit ms':>9} {'errors':>7}"
    )
    passed = True
    for category, limit in LIMITS.items():
        values, errors = results.latencies[category], results.errors[category]
        ok = bool(values) and not errors and percentile(values, 0.95) < limit
        passed = passed and ok
        detail = ", ".join(f"{name} x{n}" for name, n in errors.most_common())
        line = (
            f"{category:<10} {len(values):>7} {_ms(values, 0.5):>8} {_ms(values, 0.95):>8} "
            f"{limit * 1000:>9.0f} {sum(errors.values()):>7}  {'PASS' if ok else 'FAIL'}  {detail}"
        )
        print(line.rstrip())
    print()
    print(f"{'operation':<20} {'count':>7} {'p50 ms':>8} {'p95 ms':>8}")
    for operation, values in sorted(results.operations.items()):
        print(f"{operation:<20} {len(values):>7} {_ms(values, 0.5):>8} {_ms(values, 0.95):>8}")
    print()
    if results.errors["question"]["HTTP 429"]:
        print("Question submissions hit the daily limit: restart the API with a high")
        print("DAILY_QUESTION_LIMIT, for example 100000.")
    if any(errors["HTTP 403"] for errors in results.errors.values()):
        print("Requests were refused with 403: --origin must equal the API's APP_ORIGIN.")
    print(f"Overall: {'PASS' if passed else 'FAIL'}")
    return passed


async def run(args: argparse.Namespace) -> bool:
    logins = [FAKE_LOGINS[index % len(FAKE_LOGINS)] for index in range(args.users)]
    sessions = [await sign_in(args.base_url, login) for login in logins]
    clients = [
        httpx.AsyncClient(
            base_url=args.base_url,
            headers={"Origin": args.origin},
            cookies={SESSION_COOKIE: session},
            timeout=REQUEST_TIMEOUT,
        )
        for session in sessions
    ]
    try:
        first_client = {login: clients[logins.index(login)] for login in dict.fromkeys(logins)}
        prepared = await asyncio.gather(
            *(prepare(client, args.github_repository_id) for client in first_client.values())
        )
        targets = dict(zip(first_client, prepared, strict=True))
        results = Results()
        started = time.monotonic()
        deadline = started + args.duration
        await asyncio.gather(
            *(
                run_client(
                    client, targets[login], deadline, random.Random(args.seed + index), results
                )
                for index, (client, login) in enumerate(zip(clients, logins, strict=True))
            )
        )
        return report(results, args, time.monotonic() - started)
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.perf_check",
        description="Measure search, view, and submission latency against SC-007.",
    )
    parser.add_argument("--base-url", default="http://localhost:8000", help="API base URL")
    parser.add_argument(
        "--origin", default="http://localhost:3000", help="Origin header; the API's APP_ORIGIN"
    )
    parser.add_argument("--users", type=int, default=10, help="concurrent clients")
    parser.add_argument("--duration", type=float, default=120.0, help="seconds of load")
    parser.add_argument(
        "--github-repository-id",
        type=int,
        default=SAMPLE_APP_ID,
        help="repository to connect in each workspace (default: the sample-app fixture)",
    )
    parser.add_argument("--seed", type=int, default=0, help="seed for the request mix")
    args = parser.parse_args(argv)
    if args.users < 1 or args.duration <= 0:
        parser.error("--users must be at least 1 and --duration positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        passed = asyncio.run(run(args))
    except (SetupError, httpx.HTTPError) as exc:
        print(f"perf_check: setup failed: {exc}", file=sys.stderr)
        return 2
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
