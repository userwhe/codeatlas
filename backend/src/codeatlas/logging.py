"""JSON logging with request, job, run, and snapshot identifiers taken from context variables.

Log messages must never contain tokens, source text, prompts, or model output (research R16).

Call sites add structured fields with `extra={"fields": {...}}`. Only the keys in
`STRUCTURED_FIELDS` reach the JSON line, as top-level keys; any other key is dropped. The
allow-list keeps source text, prompts, and tokens out of the logs even when a call site passes
them (specs/004-pilot-deployment, research R9).
"""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)
run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)
snapshot_id_var: ContextVar[str | None] = ContextVar("snapshot_id", default=None)

_CONTEXT_VARS = {
    "request_id": request_id_var,
    "job_id": job_id_var,
    "run_id": run_id_var,
    "snapshot_id": snapshot_id_var,
}

STRUCTURED_FIELDS = (
    "method",
    "route",
    "status",
    "duration_ms",
    "kind",
    "outcome",
    "attempt",
    "provider",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name, var in _CONTEXT_VARS.items():
            value = var.get()
            if value is not None:
                entry[name] = value
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for name in STRUCTURED_FIELDS:
                if name in fields:
                    entry[name] = fields[name]
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


class RedactQueryStrings(logging.Filter):
    """Drop query strings from uvicorn access-log lines.

    The OAuth callback URL carries a one-time authorization code and state; access logs must not
    keep them (research R16). Uvicorn passes `(client, method, path, http_version, status)`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            if "?" in path:
                record.args = (*args[:2], path.split("?", 1)[0] + "?[redacted]", *args[3:])
        return True


_REDACT_QUERY_STRINGS = RedactQueryStrings()


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    access = logging.getLogger("uvicorn.access")
    if _REDACT_QUERY_STRINGS not in access.filters:
        access.addFilter(_REDACT_QUERY_STRINGS)


@contextmanager
def log_context(
    *,
    request_id: str | None = None,
    job_id: str | None = None,
    run_id: str | None = None,
    snapshot_id: str | None = None,
) -> Iterator[None]:
    """Set identifiers for log lines emitted inside the block."""
    values = {
        "request_id": request_id,
        "job_id": job_id,
        "run_id": run_id,
        "snapshot_id": snapshot_id,
    }
    tokens = [
        (_CONTEXT_VARS[name], _CONTEXT_VARS[name].set(value))
        for name, value in values.items()
        if value is not None
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
