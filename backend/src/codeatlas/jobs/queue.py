"""PostgreSQL-backed job queue with leases and fencing tokens (research R3).

The `jobs` table is both the state authority and the queue. A worker claims one job at a time
with `FOR UPDATE SKIP LOCKED`, holds a 60-second lease that a heartbeat renews, and proves
ownership with the job's fencing token in every write it makes after the claim.
"""

import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from codeatlas.config import get_settings
from codeatlas.models import ACTIVE_JOB_STATUSES, Job, JobEvent

LEASE_DURATION = timedelta(seconds=60)
BACKOFF_BASE_SECONDS = 5.0
BACKOFF_CAP_SECONDS = 60.0
WAITING_STATUSES = ("queued", "retry_wait")
# Bounds one claim call: unique-index races and jobs failed on sight each use one round.
MAX_CLAIM_ROUNDS = 10


class LeaseLost(Exception):
    """The attempt no longer owns the job: another attempt claimed it, or it left `running`."""


class JobFailure(Exception):
    """A failure with a user-safe code and message.

    `permanent` failures are not retried. `retryable` tells the user whether trying again later
    may help; it defaults to `not permanent`.
    """

    def __init__(
        self, code: str, message: str, *, permanent: bool, retryable: bool | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.permanent = permanent
        self.retryable = (not permanent) if retryable is None else retryable


@dataclass(frozen=True)
class Claim:
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


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _deadline(kind: str) -> timedelta:
    settings = get_settings()
    return settings.question_deadline if kind == "answer_question" else settings.indexing_deadline


def timeout_failure(kind: str) -> JobFailure:
    """The failure recorded when a job passes its deadline (FR-031)."""
    work = "Answering the question" if kind == "answer_question" else "Indexing"
    return JobFailure(
        "timeout", f"{work} did not finish within the time limit.", permanent=True, retryable=True
    )


def append_event(
    db: Session,
    job_id: uuid.UUID,
    *,
    event_type: str,
    stage: str | None = None,
    message: str = "",
    data: dict[str, Any] | None = None,
) -> JobEvent:
    """Add the job's next event; `seq` is contiguous from 1. Callers hold the job row lock."""
    last = db.scalar(select(func.max(JobEvent.seq)).where(JobEvent.job_id == job_id))
    event = JobEvent(
        job_id=job_id,
        seq=(last or 0) + 1,
        event_type=event_type,
        stage=stage,
        message=message,
        data=data or {},
    )
    db.add(event)
    db.flush()
    return event


def _active_with_key(db: Session, workspace_id: uuid.UUID, dedupe_key: str) -> Job | None:
    return db.scalar(
        select(Job).where(
            Job.workspace_id == workspace_id,
            Job.dedupe_key == dedupe_key,
            Job.status.in_(ACTIVE_JOB_STATUSES),
        )
    )


def enqueue(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    kind: str,
    repository_id: uuid.UUID,
    created_by: uuid.UUID | None,
    analysis_run_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
) -> tuple[Job, bool]:
    """Add a queued job, or return the active job with the same dedupe key (FR-029).

    Returns `(job, created)`. Does not commit.
    """
    if dedupe_key is not None:
        existing = _active_with_key(db, workspace_id, dedupe_key)
        if existing is not None:
            return existing, False
    job = Job(
        workspace_id=workspace_id,
        kind=kind,
        repository_id=repository_id,
        analysis_run_id=analysis_run_id,
        payload=payload or {},
        dedupe_key=dedupe_key,
        status="queued",
        run_after=datetime.now(UTC),
        created_by=created_by,
    )
    try:
        with db.begin_nested():
            db.add(job)
    except IntegrityError:
        # A concurrent request inserted the same dedupe key first.
        if dedupe_key is None:
            raise
        existing = _active_with_key(db, workspace_id, dedupe_key)
        if existing is None:
            raise
        return existing, False
    append_event(db, job.id, event_type="queued", message="Waiting to start.")
    return job, True


def _claimable(now: datetime) -> Select[Job]:
    running = aliased(Job)
    other_running = exists().where(
        running.workspace_id == Job.workspace_id,
        running.kind == Job.kind,
        running.status == "running",
        running.id != Job.id,
    )
    return (
        select(Job)
        .where(
            or_(
                and_(Job.status.in_(WAITING_STATUSES), Job.run_after <= now),
                and_(Job.status == "running", Job.lease_expires_at < now),
            ),
            ~other_running,
        )
        .order_by(Job.created_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=Job)
        .execution_options(populate_existing=True)
    )


def _failure_on_claim(job: Job, now: datetime) -> JobFailure | None:
    if job.deadline_at is not None and now >= job.deadline_at:
        return timeout_failure(job.kind)
    if job.status == "running" and job.attempt >= job.max_attempts:
        # The last allowed attempt stopped without reporting (for example, a worker crash).
        return JobFailure(
            "internal_error", "The job stopped unexpectedly.", permanent=True, retryable=True
        )
    return None


def _mark_failed(db: Session, job: Job, failure: JobFailure, now: datetime) -> None:
    job.status = "failed"
    job.error_code = failure.code
    job.error_message = failure.message
    job.error_retryable = failure.retryable
    job.finished_at = now
    job.lease_expires_at = None
    append_event(
        db, job.id, event_type="failed", message=failure.message, data={"code": failure.code}
    )


def claim_next(db: Session, *, now: datetime | None = None) -> Claim | None:
    """Claim one eligible job in its own transaction, or return None.

    Eligible: `queued` or `retry_wait` with `run_after <= now`, or `running` with an expired
    lease, and no other `running` job of the same workspace and kind (FR-027). A job past its
    deadline, or a reclaimed job without attempts left, is failed instead of returned.
    """
    for _ in range(MAX_CLAIM_ROUNDS):
        current = _now(now)
        try:
            job = db.scalar(_claimable(current))
            if job is None:
                db.commit()
                return None
            failure = _failure_on_claim(job, current)
            if failure is not None:
                _mark_failed(db, job, failure, current)
                db.commit()
                continue
            job.attempt += 1
            job.fencing_token += 1
            job.status = "running"
            job.lease_expires_at = current + LEASE_DURATION
            deadline_at = job.deadline_at
            if deadline_at is None:  # first start
                deadline_at = current + _deadline(job.kind)
                job.started_at = current
                job.deadline_at = deadline_at
            db.flush()
            claim = Claim(
                job_id=job.id,
                kind=job.kind,
                attempt=job.attempt,
                fencing_token=job.fencing_token,
                workspace_id=job.workspace_id,
                repository_id=job.repository_id,
                analysis_run_id=job.analysis_run_id,
                payload=dict(job.payload),
                created_by=job.created_by,
                deadline_at=deadline_at,
            )
            db.commit()
            return claim
        except IntegrityError:
            # Another worker started a job of the same workspace and kind first.
            db.rollback()
    return None


def fenced(db: Session, job_id: uuid.UUID, token: int) -> Job:
    """Lock the job row and check that the attempt with `token` still owns it.

    Raises `LeaseLost` otherwise. Does not commit; the caller ends the transaction.
    """
    job = db.scalar(
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None or job.status != "running" or job.fencing_token != token:
        raise LeaseLost(f"job {job_id} is no longer held by fencing token {token}")
    return job


def renew_lease(db: Session, job_id: uuid.UUID, token: int, *, now: datetime | None = None) -> None:
    """Extend the lease by 60 seconds, or raise `LeaseLost`. Commits."""
    try:
        job = fenced(db, job_id, token)
    except LeaseLost:
        db.rollback()
        raise
    job.lease_expires_at = _now(now) + LEASE_DURATION
    db.commit()


def complete(db: Session, job: Job, *, message: str = "", now: datetime | None = None) -> None:
    """Mark a job returned by `fenced` as succeeded. Does not commit."""
    job.status = "succeeded"
    job.finished_at = _now(now)
    job.lease_expires_at = None
    append_event(db, job.id, event_type="succeeded", message=message)


def cancel(db: Session, job: Job, *, message: str = "", now: datetime | None = None) -> None:
    """Mark a locked job as canceled. Does not commit."""
    job.status = "canceled"
    job.finished_at = _now(now)
    job.lease_expires_at = None
    append_event(db, job.id, event_type="canceled", message=message)


def backoff_seconds(attempt: int) -> float:
    """Capped exponential backoff with full jitter: base 5 s, cap 60 s."""
    cap = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
    return random.uniform(0, cap)  # noqa: S311 - retry jitter, not a secret


def fail(
    db: Session,
    job_id: uuid.UUID,
    token: int,
    *,
    code: str,
    message: str,
    permanent: bool,
    retryable: bool | None = None,
    now: datetime | None = None,
) -> None:
    """Record a failed attempt: schedule a retry, or fail the job. Commits.

    The job fails when the failure is permanent, no attempts are left, or the deadline has
    passed (then with code `timeout`). Raises `LeaseLost` if `token` no longer owns the job.
    """
    current = _now(now)
    try:
        job = fenced(db, job_id, token)
    except LeaseLost:
        db.rollback()
        raise
    failure = JobFailure(code, message, permanent=permanent, retryable=retryable)
    if not permanent and job.deadline_at is not None and current >= job.deadline_at:
        failure = timeout_failure(job.kind)
    if failure.permanent or job.attempt >= job.max_attempts:
        _mark_failed(db, job, failure, current)
    else:
        delay = backoff_seconds(job.attempt)
        job.status = "retry_wait"
        job.run_after = current + timedelta(seconds=delay)
        job.lease_expires_at = None
        append_event(
            db,
            job.id,
            event_type="retry_scheduled",
            message=f"Attempt {job.attempt} of {job.max_attempts} failed. Retrying soon.",
            data={"code": code, "attempt": job.attempt, "retry_in_seconds": round(delay, 1)},
        )
    db.commit()


def cancel_for_repository(db: Session, repository_id: uuid.UUID) -> int:
    """Cancel the repository's `queued` and `retry_wait` jobs; returns the count. No commit."""
    jobs = db.scalars(
        select(Job)
        .where(Job.repository_id == repository_id, Job.status.in_(WAITING_STATUSES))
        .order_by(Job.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    for job in jobs:
        cancel(db, job, message="Canceled because the repository was disconnected.")
    return len(jobs)


def queued_behind(db: Session, job: Job) -> int:
    """Count the jobs of the same workspace and kind that will run before `job` (FR-027)."""
    ahead = or_(
        Job.status == "running",
        and_(
            Job.status.in_(WAITING_STATUSES),
            or_(
                Job.created_at < job.created_at,
                and_(Job.created_at == job.created_at, Job.id < job.id),
            ),
        ),
    )
    count = db.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.workspace_id == job.workspace_id,
            Job.kind == job.kind,
            Job.id != job.id,
            ahead,
        )
    )
    return count or 0
