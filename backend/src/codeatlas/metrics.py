"""Application metrics as CloudWatch embedded metric format (EMF) lines on standard output.

CloudWatch extracts the metrics from the worker's log stream, so the worker needs no AWS
credentials (specs/004-pilot-deployment, research R10, contracts/operations.md "Metrics"). Lines
are written only when `EMIT_METRICS` is set, which only the pilot does.
"""

import json
import logging
import sys
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import func, select

from codeatlas.config import get_settings
from codeatlas.db import new_session
from codeatlas.models import WAITING_JOB_STATUSES, Job

logger = logging.getLogger(__name__)

NAMESPACE = "CodeAtlas"
DIMENSION = "Environment"
REPORT_INTERVAL_SECONDS = 60.0

# The reporter thread and the job loop both write lines; one write per line keeps them whole.
_write_lock = threading.Lock()


def _default_unit(name: str) -> str:
    return "Seconds" if name.endswith("Seconds") else "Count"


def emit(values: Mapping[str, float], *, units: Mapping[str, str] | None = None) -> None:
    """Write one EMF line with these metric values, if `emit_metrics` is set.

    Units default to `Count`, and to `Seconds` for names ending in `Seconds`.
    """
    settings = get_settings()
    if not settings.emit_metrics:
        return
    units = units or {}
    line = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [[DIMENSION]],
                    "Metrics": [
                        {"Name": name, "Unit": units.get(name, _default_unit(name))}
                        for name in values
                    ],
                }
            ],
        },
        DIMENSION: settings.metrics_environment,
        **values,
    }
    with _write_lock:
        sys.stdout.write(json.dumps(line) + "\n")
        sys.stdout.flush()


def report_once(*, now: datetime | None = None) -> None:
    """Emit the worker's heartbeat, the waiting jobs, and the age of the oldest runnable one.

    A runnable job is one `claim_next` could claim now; a job not due yet, or waiting behind a
    running job of its workspace and kind, does not count. The age is 0 when there is none.
    """
    # Imported here because the queue imports this module.
    from codeatlas.jobs.queue import runnable_waiting

    current = now if now is not None else datetime.now(UTC)
    with new_session() as db:
        queued = db.scalar(
            select(func.count()).select_from(Job).where(Job.status.in_(WAITING_JOB_STATUSES))
        )
        oldest = db.scalar(select(func.min(Job.run_after)).where(runnable_waiting(current)))
    age = max((current - oldest).total_seconds(), 0.0) if oldest is not None else 0.0
    emit({"WorkerHeartbeat": 1, "QueuedJobs": queued or 0, "OldestRunnableJobAgeSeconds": age})


def _report(interval: float, stop: threading.Event) -> None:
    while True:
        try:
            report_once()
        except Exception:
            logger.exception("metrics report failed")
        if stop.wait(interval):
            return


def start_reporter(interval: float = REPORT_INTERVAL_SECONDS) -> threading.Event:
    """Report every `interval` seconds from a daemon thread until the returned event is set.

    The job loop runs one job at a time, and an indexing job may take 15 minutes, so the reports
    cannot wait for it (research R10).
    """
    stop = threading.Event()
    threading.Thread(
        target=_report, args=(interval, stop), name="metrics-reporter", daemon=True
    ).start()
    return stop
