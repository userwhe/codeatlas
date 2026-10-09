"""PostgreSQL-backed job queue with leases and fencing tokens (research R3).

The `jobs` table is both the state authority and the queue. A worker claims one job at a time
with `FOR UPDATE SKIP LOCKED`, holds a 60-second lease that a heartbeat renews, and proves
ownership with the job's fencing token in every write it makes after the claim.

Requests with the same dedupe key share a job. At most one job per key waits (`queued` or
`retry_wait`), so a running job is followed by at most one waiting job with its key
(specs/002-push-reindexing, research R3).
"""

import logging
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from codeatlas import metrics
from codeatlas.config import get_settings
from codeatlas.logging import log_context
from codeatlas.models import ACTIVE_JOB_STATUSES, Job, JobEvent, Repository

logger = logging.getLogger(__name__)

LEASE_DURATION = timedelta(seconds=60)
BACKOFF_BASE_SECONDS = 5.0
BACKOFF_CAP_SECONDS = 60.0
WAITING_STATUSES = ("queued", "retry_wait")
INDEX_JOB = "index_repository"
# Bounds one claim call: unique-index races and jobs failed on sight each use one round.
MAX_CLAIM_ROUNDS = 10
# Each kind's deadline setting, and the work its timeout message names (FR-031, and
# specs/003-pr-review FR-028).
DEADLINES = {
    "index_repository": ("indexing_deadline", "Indexing"),
    "answer_question": ("question_deadline", "Answering the question"),
    "review_pull_request": ("review_deadline", "Reviewing the pull request"),
}
# Permanent failures that still count toward the `jobs-failing` alarm; other permanent failures
# come from the input or from access (specs/004-pilot-deployment, research R10).
COUNTED_PERMANENT_CODES = frozenset({"timeout", "internal_error"})


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
    trigger: str
    deadline_at: datetime


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _deadline(kind: str) -> timedelta:
    setting, _ = DEADLINES[kind]
    deadline: timedelta = getattr(get_settings(), setting)
    return deadline


def timeout_failure(kind: str) -> JobFailure:
    """The failure recorded when a job passes its deadline (FR-031)."""
    _, work = DEADLINES[kind]
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


def index_dedupe_key(repository_id: uuid.UUID, branch: str) -> str:
    """The dedupe key shared by every indexing request for one branch of a repository."""
    return f"index:{repository_id}:{branch}"


def _active_with_key(db: Session, workspace_id: uuid.UUID, dedupe_key: str) -> Job | None:
    """The active job a request with this key joins: the waiting one, else the running one."""
    return db.scalar(
        select(Job)
        .where(
            Job.workspace_id == workspace_id,
            Job.dedupe_key == dedupe_key,
            Job.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(Job.status == "running")
        .limit(1)
    )


def _waiting_with_key(db: Session, workspace_id: uuid.UUID, dedupe_key: str) -> Job | None:
    """Lock and return the waiting job with this key, if any.

    The lock keeps a worker from claiming the job while the caller changes it. A job claimed
    meanwhile is no longer waiting when the lock is granted, so it is not returned.
    """
    return db.scalar(
        select(Job)
        .where(
            Job.workspace_id == workspace_id,
            Job.dedupe_key == dedupe_key,
            Job.status.in_(WAITING_STATUSES),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _running_with_key(db: Session, workspace_id: uuid.UUID, dedupe_key: str) -> Job | None:
    """The running job with this key, with the commit its current attempt resolved, if any.

    The attempt that owns the job records that commit under its fencing token, so a stale
    attempt cannot make the job look as if it covers a newer push (research R3).
    """
    return db.scalar(
        select(Job)
        .where(
            Job.workspace_id == workspace_id,
            Job.dedupe_key == dedupe_key,
            Job.status == "running",
        )
        .execution_options(populate_existing=True)
    )


def _join(job: Job, trigger: str, created_by: uuid.UUID | None) -> None:
    """Merge a request into a waiting job it joins (data-model.md, trigger rules).

    A waiting `check` job takes the trigger of a `push` or `user` request, and a `user` request
    also becomes its creator. Other merges keep the existing trigger.
    """
    if job.status not in WAITING_STATUSES or job.trigger != "check" or trigger == "check":
        return
    job.trigger = trigger
    if job.created_by is None:
        job.created_by = created_by


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
    trigger: str = "user",
) -> tuple[Job, bool]:
    """Add a queued job, or return the active job with the same dedupe key (FR-029).

    A waiting job is preferred over a running one, so a manual re-index joins a waiting
    automatic run (specs/002-push-reindexing FR-005). Returns `(job, created)`. Does not commit.
    """
    if dedupe_key is not None:
        existing = _active_with_key(db, workspace_id, dedupe_key)
        if existing is not None:
            _join(existing, trigger, created_by)
            return existing, False
    job = Job(
        workspace_id=workspace_id,
        kind=kind,
        repository_id=repository_id,
        analysis_run_id=analysis_run_id,
        payload=payload or {},
        dedupe_key=dedupe_key,
        trigger=trigger,
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
        _join(existing, trigger, created_by)
        return existing, False
    append_event(db, job.id, event_type="queued", message="Waiting to start.")
    return job, True


def request_automatic_run(
    db: Session,
    *,
    repository: Repository,
    branch: str,
    trigger: str,
    pushed_commit_sha: str | None = None,
) -> Job:
    """Return the indexing job that covers an automatic request, creating one if needed
    (specs/002-push-reindexing research R3).

    1. A waiting job for the branch covers the request. A waiting `check` job takes the `push`
       trigger, and the job records the newest pushed commit.
    2. Otherwise, the running job covers a push once it has resolved the pushed commit.
    3. Otherwise, a new `queued` job is created, with no creator. A run on an older commit is
       therefore followed by exactly one waiting job.

    Every attempt resolves the branch head when it starts, so the covering job indexes the
    newest pushed commit (FR-003). Does not commit.
    """
    key = index_dedupe_key(repository.id, branch)
    waiting = _waiting_with_key(db, repository.workspace_id, key)
    if waiting is None:
        running = _running_with_key(db, repository.workspace_id, key)
        if (
            running is not None
            and pushed_commit_sha is not None
            and running.payload.get("commit_sha") == pushed_commit_sha
        ):
            return running
        payload: dict[str, Any] = {"branch": branch}
        if pushed_commit_sha is not None:
            payload["pushed_commit_sha"] = pushed_commit_sha
        job = Job(
            workspace_id=repository.workspace_id,
            kind=INDEX_JOB,
            repository_id=repository.id,
            payload=payload,
            dedupe_key=key,
            trigger=trigger,
            status="queued",
            run_after=datetime.now(UTC),
            created_by=None,
        )
        try:
            with db.begin_nested():
                db.add(job)
        except IntegrityError:
            # A concurrent request inserted a waiting job with the same key first.
            waiting = _waiting_with_key(db, repository.workspace_id, key)
            if waiting is None:
                raise
        else:
            append_event(db, job.id, event_type="queued", message="Waiting to start.")
            return job
    _join(waiting, trigger, None)
    if pushed_commit_sha is not None:
        waiting.payload = {**waiting.payload, "pushed_commit_sha": pushed_commit_sha}
    return waiting


def _waiting_and_due(now: datetime) -> ColumnElement[bool]:
    return and_(Job.status.in_(WAITING_STATUSES), Job.run_after <= now)


def _no_other_running() -> ColumnElement[bool]:
    running = aliased(Job)
    return ~exists().where(
        running.workspace_id == Job.workspace_id,
        running.kind == Job.kind,
        running.status == "running",
        running.id != Job.id,
    )


def runnable_waiting(now: datetime) -> ColumnElement[bool]:
    """Waiting jobs that `claim_next` could claim at `now`; the queue-age metric reads them."""
    return and_(_waiting_and_due(now), _no_other_running())


def _claimable(now: datetime) -> Select[Job]:
    return (
        select(Job)
        .where(
            or_(
                _waiting_and_due(now),
                and_(Job.status == "running", Job.lease_expires_at < now),
            ),
            _no_other_running(),
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
    """Fail the job, log its outcome, and count the failure if it matters.

    Both `fail` and `claim_next` end here, so failures found at claim time are logged too, with
    the job's ID (research R9). A failure counts toward `JobsFailed` when its retries are used up,
    it timed out, or it is an internal error (research R10).
    """
    job.status = "failed"
    job.error_code = failure.code
    job.error_message = failure.message
    job.error_retryable = failure.retryable
    job.finished_at = now
    job.lease_expires_at = None
    append_event(
        db, job.id, event_type="failed", message=failure.message, data={"code": failure.code}
    )
    with log_context(job_id=str(job.id)):
        logger.info(
            "job_finished",
            extra={"fields": {"kind": job.kind, "outcome": "failed", "attempt": job.attempt}},
        )
    if not failure.permanent or failure.code in COUNTED_PERMANENT_CODES:
        metrics.emit({"JobsFailed": 1})


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
                trigger=job.trigger,
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
) -> str:
    """Record a failed attempt: schedule a retry, or fail the job. Commits.

    The job fails when the failure is permanent, no attempts are left, or the deadline has
    passed (then with code `timeout`). Returns the job's new status, `retry_wait` or `failed`.
    Raises `LeaseLost` if `token` no longer owns the job.
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
    status = job.status
    db.commit()
    return status


def cancel_for_repository(
    db: Session,
    repository_id: uuid.UUID,
    *,
    message: str = "Canceled because the repository was disconnected.",
) -> int:
    """Cancel the repository's `queued` and `retry_wait` jobs; returns the count. No commit."""
    jobs = db.scalars(
        select(Job)
        .where(Job.repository_id == repository_id, Job.status.in_(WAITING_STATUSES))
        .order_by(Job.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    for job in jobs:
        cancel(db, job, message=message)
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
