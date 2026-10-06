import logging

from codeatlas.logging import RedactQueryStrings, configure_logging


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
