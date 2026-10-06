"""Job worker: claims one job at a time and runs its handler (research R3).

Run with `python -m codeatlas.jobs.worker`. Handler modules call `register(kind, handler)` at
import time; `load_handlers()` imports them.
"""

import contextvars
import importlib
import logging
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Any

from sqlalchemy.orm import Session

from codeatlas.db import new_session, session_scope
from codeatlas.jobs.queue import (
    Claim,
    JobFailure,
    LeaseLost,
    append_event,
    claim_next,
    complete,
    fail,
    fenced,
    renew_lease,
    timeout_failure,
)
from codeatlas.logging import configure_logging, log_context
from codeatlas.models import Job

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = timedelta(seconds=20)
POLL_INTERVAL = timedelta(seconds=1)
HANDLER_MODULES = (
    "codeatlas.ingestion.pipeline",
    "codeatlas.qa.answer",
    "codeatlas.jobs.maintenance",
)
INTERNAL_ERROR_MESSAGE = "Something went wrong while running this job."


@dataclass
class JobContext:
    """What a handler knows about its attempt, plus fenced ways to report progress and publish."""

    job_id: uuid.UUID
    kind: str
    attempt: int
    fencing_token: int
    workspace_id: uuid.UUID
    repository_id: uuid.UUID
    analysis_run_id: uuid.UUID | None
    payload: dict[str, Any]
    created_by: uuid.UUID | None
    deadline_at: datetime
    # What started the job: `user`, `push`, or `check` (specs/002-push-reindexing, research R3).
    trigger: str = "user"
    lease_lost: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    published: bool = field(default=False, init=False)

    @classmethod
    def from_claim(cls, claim: Claim) -> "JobContext":
        return cls(**asdict(claim))

    def check(self) -> None:
        """Raise `LeaseLost` if the heartbeat lost the lease, or a timeout past the deadline."""
        if self.lease_lost.is_set():
            raise LeaseLost(f"job {self.job_id} lost its lease")
        if datetime.now(UTC) >= self.deadline_at:
            raise timeout_failure(self.kind)

    def event(
        self,
        stage: str,
        message: str,
        data: dict[str, Any] | None = None,
        *,
        event_type: str = "stage_started",
    ) -> None:
        """Append a progress event in its own short transaction, if this attempt owns the job."""
        self.check()
        with session_scope() as db:
            fenced(db, self.job_id, self.fencing_token)
            append_event(
                db,
                self.job_id,
                event_type=event_type,
                stage=stage,
                message=message,
                data=data,
            )

    @contextmanager
    def publish(self) -> Iterator[tuple[Session, Job]]:
        """Yield a session and the locked job; on exit, commit the results with the completion.

        Raises `LeaseLost` if this attempt no longer owns the job. If the body leaves the job in
        another status (for example `queue.cancel`), that status is kept. On an exception,
        everything is rolled back.
        """
        with session_scope() as db:
            job = fenced(db, self.job_id, self.fencing_token)
            yield db, job
            if job.status == "running":
                complete(db, job)
        self.published = True


Handler = Callable[[JobContext], None]


@dataclass
class _Periodic:
    interval: timedelta
    fn: Callable[[], None]
    next_run: float = 0.0


_handlers: dict[str, Handler] = {}
_periodic: dict[str, _Periodic] = {}
_handlers_loaded = False


def register(kind: str, handler: Handler) -> None:
    """Register the handler for a job kind, replacing any previous one."""
    _handlers[kind] = handler


def register_periodic(name: str, interval: timedelta, fn: Callable[[], None]) -> None:
    """Run `fn` from the worker loop every `interval`, starting at the first loop."""
    _periodic[name] = _Periodic(interval=interval, fn=fn)


def load_handlers() -> None:
    """Import the handler modules once; a module that does not exist yet is skipped."""
    global _handlers_loaded
    if _handlers_loaded:
        return
    for name in HANDLER_MODULES:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
    _handlers_loaded = True


def _heartbeat(ctx: JobContext, stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_INTERVAL.total_seconds()):
        if datetime.now(UTC) >= ctx.deadline_at:
            # Let the lease lapse so that the job is reclaimed and failed with a timeout even if
            # the handler never checks in again.
            return
        try:
            with new_session() as db:
                renew_lease(db, ctx.job_id, ctx.fencing_token)
        except LeaseLost:
            logger.warning("lease lost; the attempt will stop at its next check")
            ctx.lease_lost.set()
            return
        except Exception:
            logger.exception("lease renewal failed")


def _record_failure(
    ctx: JobContext, code: str, message: str, *, permanent: bool, retryable: bool | None = None
) -> None:
    try:
        with new_session() as db:
            fail(
                db,
                ctx.job_id,
                ctx.fencing_token,
                code=code,
                message=message,
                permanent=permanent,
                retryable=retryable,
            )
    except LeaseLost:
        logger.warning("lease lost before the failure was recorded")


def _run(claim: Claim) -> None:
    ctx = JobContext.from_claim(claim)
    handler = _handlers.get(claim.kind)
    if handler is None:
        logger.error("no handler registered for job kind %s", claim.kind)
        _record_failure(
            ctx, "unsupported_input", "This kind of job is not supported.", permanent=True
        )
        return
    stop = threading.Event()
    heartbeat = threading.Thread(
        target=contextvars.copy_context().run,
        args=(_heartbeat, ctx, stop),
        name=f"heartbeat-{claim.job_id}",
        daemon=True,
    )
    heartbeat.start()
    try:
        try:
            handler(ctx)
            if not ctx.published:
                with ctx.publish():
                    pass
        finally:
            stop.set()
            heartbeat.join()
    except LeaseLost:
        logger.warning("lease lost; another attempt owns the job")
    except JobFailure as exc:
        logger.info("job attempt failed with %s (permanent=%s)", exc.code, exc.permanent)
        _record_failure(
            ctx, exc.code, exc.message, permanent=exc.permanent, retryable=exc.retryable
        )
    except Exception:
        logger.exception("job handler raised an unexpected error")
        _record_failure(ctx, "internal_error", INTERNAL_ERROR_MESSAGE, permanent=False)
    else:
        logger.info("job attempt %s succeeded", claim.attempt)


def run_once() -> bool:
    """Claim and run one job synchronously. Returns False when no job was claimable."""
    load_handlers()
    with new_session() as db:
        claim = claim_next(db)
    if claim is None:
        return False
    run_id = str(claim.analysis_run_id) if claim.analysis_run_id else None
    with log_context(job_id=str(claim.job_id), run_id=run_id):
        logger.info("claimed %s job, attempt %s", claim.kind, claim.attempt)
        _run(claim)
    return True


def _run_due_periodic(now: float) -> None:
    for name, task in _periodic.items():
        if now < task.next_run:
            continue
        task.next_run = now + task.interval.total_seconds()
        try:
            task.fn()
        except Exception:
            logger.exception("periodic task %s failed", name)


def main() -> None:
    configure_logging()
    load_handlers()
    stopping = threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        logger.info("received signal %s; stopping after the current job", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("worker started")
    while not stopping.is_set():
        try:
            worked = run_once()
        except Exception:
            logger.exception("worker iteration failed")
            worked = False
        _run_due_periodic(time.monotonic())
        if not worked:
            stopping.wait(POLL_INTERVAL.total_seconds())
    logger.info("worker stopped")


if __name__ == "__main__":
    # Run through the importable module so that handlers registered by
    # `from codeatlas.jobs.worker import register` land in the registry the loop reads.
    from codeatlas.jobs import worker

    worker.main()
