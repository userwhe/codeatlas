import json
import logging
from collections.abc import Iterator

import pytest

from codeatlas.logging import JsonFormatter, RedactQueryStrings, configure_logging, log_context

STRUCTURED_LOGGER = "codeatlas.tests.structured"


def access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:5000", "GET", path, "1.1", 302),
        exc_info=None,
    )


def test_query_string_is_redacted() -> None:
    record = access_record("/auth/github/callback?code=one-time-code&state=abc")

    RedactQueryStrings().filter(record)

    message = record.getMessage()
    assert "one-time-code" not in message
    assert "state=abc" not in message
    assert "/auth/github/callback?[redacted]" in message


def test_paths_without_query_are_unchanged() -> None:
    record = access_record("/v1/repositories")

    RedactQueryStrings().filter(record)

    assert record.getMessage() == '127.0.0.1:5000 - "GET /v1/repositories HTTP/1.1" 302'


def test_configure_logging_installs_the_filter_once() -> None:
    configure_logging()
    configure_logging()

    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, RedactQueryStrings) for f in access.filters) == 1


class _JsonLines(logging.Handler):
    """Keeps each record as the JSON line the formatter writes."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self.lines: list[dict[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture
def json_lines() -> Iterator[list[dict[str, object]]]:
    """The JSON lines of INFO records sent to `STRUCTURED_LOGGER`, which keeps them to itself."""
    logger = logging.getLogger(STRUCTURED_LOGGER)
    handler = _JsonLines()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)


def test_structured_fields_are_top_level_keys(json_lines: list[dict[str, object]]) -> None:
    fields = {
        "method": "GET",
        "route": "/v1/repositories/{repository_id}",
        "status": 200,
        "duration_ms": 12.5,
        "kind": "index_repository",
        "outcome": "succeeded",
        "attempt": 2,
        "provider": "gemini",
    }

    with log_context(request_id="req_1"):
        logging.getLogger(STRUCTURED_LOGGER).info("request", extra={"fields": fields})

    (line,) = json_lines
    assert {key: line[key] for key in fields} == fields
    assert (line["message"], line["request_id"]) == ("request", "req_1")
    assert "fields" not in line


def test_operations_fields_are_top_level_keys(json_lines: list[dict[str, object]]) -> None:
    # The worker's status, the release steps, and the backup (004 research R9).
    fields = {
        "queued_jobs": 3,
        "oldest_runnable_job_age_seconds": 42.5,
        "database_revision": "0005",
        "release_revision": "0004",
        "commit": "3f9c2e1d4b5a69788c7d0e1f2a3b4c5d6e7f8091",
        "reported_commit": "9a8b7c6d5e4f30211f2e3d4c5b6a79880f1e2d3c",
        "object_key": "backups/2026-10-12/033000/codeatlas.dump",
        "bytes": 1_048_576,
        "pruned": 2,
    }

    logging.getLogger(STRUCTURED_LOGGER).info(
        "operations", extra={"fields": {**fields, "secret_access_key": "backup-secret-0c4f"}}
    )

    (line,) = json_lines
    assert {key: line[key] for key in fields} == fields
    assert "secret_access_key" not in line
    assert "backup-secret-0c4f" not in json.dumps(line)


def test_fields_outside_the_allow_list_are_dropped(json_lines: list[dict[str, object]]) -> None:
    fields = {
        "method": "POST",
        "body": "def check_access(user, repository):",
        "prompt": "Answer from the evidence only.",
        "message": "overwritten",
    }

    logging.getLogger(STRUCTURED_LOGGER).info("request", extra={"fields": fields})

    (line,) = json_lines
    assert line["method"] == "POST"
    assert "body" not in line
    assert "prompt" not in line
    assert line["message"] == "request"
    assert "check_access" not in json.dumps(line)


def test_lines_without_fields_are_unchanged(json_lines: list[dict[str, object]]) -> None:
    logging.getLogger(STRUCTURED_LOGGER).info("worker started")

    (line,) = json_lines
    assert set(line) == {"time", "level", "logger", "message"}
    assert (line["level"], line["logger"], line["message"]) == (
        "INFO",
        STRUCTURED_LOGGER,
        "worker started",
    )
