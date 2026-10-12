"""The release steps (T008, research R7, contracts/operations.md "Operator commands").

`decide` chooses what `ops migrate` does from plain revisions. `wait_for_api`, the release gate,
runs against `httpx.MockTransport` with a fake clock whose `sleep` moves time forward.
"""

import logging
from collections.abc import Callable

import httpx
import pytest

from codeatlas.config import Settings
from codeatlas.ops import release
from codeatlas.ops.__main__ import main

KNOWN = frozenset({"0001", "0002", "0003"})
HEAD = "0003"
ORIGIN = "http://api.railway.internal:8000"
COMMIT = "3f9c2e1d4b5a69788c7d0e1f2a3b4c5d6e7f8091"
OLDER_COMMIT = "9a8b7c6d5e4f30211f2e3d4c5b6a79880f1e2d3c"

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.mark.parametrize(
    ("database_revision", "decision"),
    [
        (None, "upgrade"),
        ("0001", "upgrade"),
        ("0002", "upgrade"),
        ("0003", "current"),
        # A rollback after a migration: the database is ahead of this release.
        ("0004", "ahead"),
        ("ffffffffffff", "ahead"),
    ],
)
def test_decide(database_revision: str | None, decision: str) -> None:
    assert release.decide(database_revision, KNOWN, HEAD) == decision


class FakeTime:
    """A monotonic clock that only `sleep` moves."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


def version(commit: str) -> httpx.Response:
    return httpx.Response(200, json={"commit": commit})


def replies(*answers: httpx.Response | Exception) -> tuple[Handler, list[httpx.Request]]:
    """A handler giving these answers in turn, the last one again after that, and its requests."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = answers[min(len(seen), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return handler, seen


def wait(handler: Handler, fake_time: FakeTime, commit: str = COMMIT, timeout: float = 60) -> bool:
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return release.wait_for_api(
            ORIGIN,
            commit,
            timeout=timeout,
            interval=5,
            client=client,
            clock=fake_time.clock,
            sleep=fake_time.sleep,
        )


def test_the_gate_passes_as_soon_as_the_api_reports_its_commit(
    fake_time: FakeTime, caplog: pytest.LogCaptureFixture
) -> None:
    handler, seen = replies(
        httpx.Response(503, json={"error": {"code": "not_ready"}}),
        httpx.ConnectError("connection refused"),
        version(OLDER_COMMIT),
        version(COMMIT),
        version(OLDER_COMMIT),
    )

    with caplog.at_level(logging.INFO, logger=release.__name__):
        assert wait(handler, fake_time) is True

    assert [(request.method, str(request.url)) for request in seen] == [
        ("GET", f"{ORIGIN}/version")
    ] * 4
    assert fake_time.sleeps == [5, 5, 5]
    waiting = [record for record in caplog.records if record.getMessage() == "release_gate_waiting"]
    assert [record.fields for record in waiting] == [{"commit": COMMIT, "reported_commit": None}]


def test_the_gate_keeps_waiting_for_another_commit_until_the_deadline(
    fake_time: FakeTime, caplog: pytest.LogCaptureFixture
) -> None:
    handler, seen = replies(version(OLDER_COMMIT))

    with caplog.at_level(logging.INFO, logger=release.__name__):
        assert wait(handler, fake_time, timeout=60) is False

    # One request at the start and one after each of the 12 sleeps of 5 seconds.
    assert len(seen) == 13
    assert fake_time.now == 60
    waiting = [record for record in caplog.records if record.getMessage() == "release_gate_waiting"]
    assert [record.fields for record in waiting] == [
        {"commit": COMMIT, "reported_commit": OLDER_COMMIT}
    ]


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(503),
        httpx.Response(200, json={"status": "ok"}),
        httpx.Response(200, text="not json"),
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
    ],
)
def test_the_gate_keeps_waiting_while_the_api_does_not_answer_its_commit(
    fake_time: FakeTime, answer: httpx.Response | Exception
) -> None:
    handler, seen = replies(answer)

    assert wait(handler, fake_time, timeout=20) is False

    assert len(seen) == 5


def test_the_gate_passes_at_once_without_a_commit(fake_time: FakeTime) -> None:
    handler, seen = replies(version(COMMIT))

    assert wait(handler, fake_time, commit="") is True

    assert seen == []
    assert fake_time.sleeps == []


def test_the_gate_defaults_to_20_minutes_every_5_seconds(fake_time: FakeTime) -> None:
    handler, seen = replies(httpx.ConnectError("connection refused"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        passed = release.wait_for_api(
            ORIGIN, COMMIT, client=client, clock=fake_time.clock, sleep=fake_time.sleep
        )

    assert passed is False
    assert fake_time.now == 20 * 60
    assert set(fake_time.sleeps) == {5}


@pytest.mark.parametrize(("passed", "status"), [(True, 0), (False, 1)])
def test_wait_for_api_command_exits_with_the_gates_result(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    passed: bool,
    status: int,
) -> None:
    monkeypatch.setattr(settings, "api_origin", ORIGIN)
    monkeypatch.setattr(settings, "platform_commit", COMMIT)
    calls: list[tuple[str, str]] = []

    def fake_wait_for_api(origin: str, commit: str) -> bool:
        calls.append((origin, commit))
        return passed

    monkeypatch.setattr(release, "wait_for_api", fake_wait_for_api)

    assert main(["wait-for-api"]) == status

    assert calls == [(ORIGIN, COMMIT)]
    if not passed:
        assert capsys.readouterr().err.count("\n") == 1


def test_wait_for_api_command_ignores_the_images_local_release(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Outside a deployment from GitHub, `release` falls back to the image's CODEATLAS_RELEASE,
    # which no deployed API reports; the gate must not wait for it (research R7).
    monkeypatch.setattr(settings, "release", "development")
    monkeypatch.setattr(settings, "platform_commit", "")
    calls: list[str] = []

    def no_request(*args: object, **kwargs: object) -> httpx.Response:
        calls.append("request")
        return httpx.Response(200, json={"commit": "other"})

    monkeypatch.setattr(httpx.Client, "get", no_request)

    assert main(["wait-for-api"]) == 0

    assert calls == []
    assert "passes without waiting" in capsys.readouterr().out
