"""Log hygiene (research R16): logs never contain tokens, source text, prompts, or model output.

Webhook deliveries add their own rule (002 research R2): logs never contain request bodies, so no
commit messages, author data, or file names, nor the webhook secret or the signature.
"""

import json
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from codeatlas.auth.sessions import COOKIE_NAME
from codeatlas.github.fake import ACCESS_PREFIX, REFRESH_PREFIX, SAMPLE_APP_ID, get_fake_github
from codeatlas.logging import JsonFormatter
from codeatlas.qa.prompt import SYSTEM_PROMPT
from tests.conftest import FIXTURE_REPOS_DIR
from tests.webhooks import (
    AUTHOR_EMAIL,
    COMMIT_MESSAGE,
    delivery_headers,
    post_delivery,
    push_payload,
)

pytestmark = pytest.mark.integration

QUESTION = "Where are repository permissions checked?"
SOURCE_FILE = FIXTURE_REPOS_DIR / "sample-app" / "app" / "auth" / "access.py"
# What the fake answer model writes (codeatlas.providers.answer_model.FakeAnswerModel).
MODEL_OUTPUT = [
    "The evidence answers the question.",
    "The first evidence item answers the question.",
    "The second evidence item supports the answer.",
]
SIGNATURE_HEADER = "X-Hub-Signature-256"


class _Capture(logging.Handler):
    """Keeps each record's message and exception text, and its line from the JSON formatter."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.json_formatter = JsonFormatter()
        self.raw: list[str] = []
        self.json_lines: list[dict[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        # Format now: the formatter reads the job and request IDs from context variables.
        self.json_lines.append(json.loads(self.json_formatter.format(record)))
        self.raw.append(record.getMessage())
        if record.exc_info:
            self.raw.append(self.json_formatter.formatException(record.exc_info))
        if record.stack_info:
            self.raw.append(record.stack_info)

    def texts(self) -> list[str]:
        decoded = [str(value) for line in self.json_lines for value in line.values()]
        return self.raw + decoded


@contextmanager
def _capture_logs() -> Iterator[_Capture]:
    root = logging.getLogger()
    handler = _Capture()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


def _drain(run_worker_once: Callable[[], bool]) -> None:
    for _ in range(20):
        if not run_worker_once():
            return
    raise AssertionError("jobs did not finish")


def _lines(text: str, min_length: int = 24) -> list[str]:
    return [line.strip() for line in text.splitlines() if len(line.strip()) >= min_length]


def _leaks(captured: _Capture, forbidden: dict[str, list[str]]) -> list[str]:
    texts = captured.texts()
    return [
        f"{kind}: {needle!r}"
        for kind, needles in forbidden.items()
        for needle in needles
        if any(needle in text for text in texts)
    ]


def test_logs_hold_no_secrets_source_prompts_or_model_output(
    signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    with _capture_logs() as captured:
        client = signed_in("octocat")
        connected = client.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})
        assert connected.status_code == 202, connected.text
        _drain(run_worker_once)
        asked = client.post(
            "/v1/analysis-runs",
            json={"repository_id": connected.json()["repository"]["id"], "question": QUESTION},
        )
        assert asked.status_code == 202, asked.text
        _drain(run_worker_once)
        run = client.get(asked.json()["result_url"]).json()

    # The run did produce the content that must stay out of the logs.
    assert run["status"] == "succeeded"
    assert run["answer"]["summary"] in MODEL_OUTPUT
    excerpts = "\n".join(citation["excerpt"] for citation in run["citations"])
    assert excerpts
    # Logs were captured, with job IDs from the context.
    messages = [str(line["message"]) for line in captured.json_lines]
    assert any(m.startswith("claimed index_repository job") for m in messages), messages
    assert any(m.startswith("claimed answer_question job") for m in messages), messages
    assert {asked.json()["job_id"], connected.json()["job"]["id"]} <= {
        line.get("job_id") for line in captured.json_lines
    }

    session_cookie = client.cookies.get(COOKIE_NAME)
    assert session_cookie
    forbidden = {
        "access token": [f"{ACCESS_PREFIX}octocat"],
        "refresh token": [f"{REFRESH_PREFIX}octocat"],
        "session cookie": [session_cookie],
        "encryption key": [os.environ["TOKEN_ENCRYPTION_KEY"]],
        "file content": _lines(SOURCE_FILE.read_text()) + _lines(excerpts),
        "system prompt": [SYSTEM_PROMPT, *_lines(SYSTEM_PROMPT)],
        "question": [QUESTION],
        "model output": MODEL_OUTPUT,
    }
    leaks = _leaks(captured, forbidden)
    assert leaks == [], "\n".join(leaks)


def test_logs_hold_no_webhook_payload_secret_or_signature(
    signed_in: Callable[[str], TestClient], run_worker_once: Callable[[], bool]
) -> None:
    client = signed_in("octocat")
    connected = client.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})
    assert connected.status_code == 202, connected.text
    _drain(run_worker_once)
    sha = get_fake_github().push(SAMPLE_APP_ID)
    payload = push_payload(SAMPLE_APP_ID, sha)
    body = json.dumps(payload).encode()
    signed = delivery_headers("push", body)
    forged = delivery_headers("push", body, secret="not-the-webhook-secret")

    with _capture_logs() as captured:
        rejected = post_delivery(client, body, forged)
        accepted = post_delivery(client, body, signed)
        _drain(run_worker_once)

    # The forged delivery was rejected, and the signed one was indexed by a push run.
    assert rejected.status_code == 401, rejected.text
    assert accepted.json() == {"outcome": "processed"}, accepted.text
    repository = client.get(f"/v1/repositories/{connected.json()['repository']['id']}").json()
    assert repository["active_snapshot"]["commit_sha"] == sha
    push_job = repository["latest_push"]["job"]
    assert (push_job["status"], push_job["trigger"]) == ("succeeded", "push")
    # Logs were captured, including the push run's, with its job ID from the context.
    claimed = [
        line.get("job_id")
        for line in captured.json_lines
        if str(line["message"]).startswith("claimed index_repository job")
    ]
    assert claimed == [push_job["id"]], captured.json_lines

    commit = payload["head_commit"]
    file_names = sorted({*commit["added"], *commit["removed"], *commit["modified"]})
    assert file_names
    signatures = [signed[SIGNATURE_HEADER], forged[SIGNATURE_HEADER]]
    forbidden = {
        "commit message": [COMMIT_MESSAGE],
        "author": [AUTHOR_EMAIL, commit["author"]["name"]],
        "file name": file_names,
        "webhook secret": [os.environ["GITHUB_WEBHOOK_SECRET"]],
        "signature": signatures + [value.removeprefix("sha256=") for value in signatures],
    }
    leaks = _leaks(captured, forbidden)
    assert leaks == [], "\n".join(leaks)
