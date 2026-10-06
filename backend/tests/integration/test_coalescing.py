"""Integration tests for coalescing automatic and manual indexing requests (T017, research R3)."""

import itertools
import threading
import uuid

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from codeatlas.db import new_session
from codeatlas.jobs import queue
from codeatlas.jobs.worker import JobContext
from codeatlas.models import Job, JobEvent, Membership, Repository, User, Workspace
from codeatlas.workspace import repositories

pytestmark = pytest.mark.integration

_github_ids = itertools.count(7000)
SHA_X = "1" * 40
SHA_Y = "2" * 40
SHA_Z = "3" * 40


def make_repository(db: Session) -> Repository:
    """Insert a user, their workspace and membership, and one connected repository."""
    user = User(github_user_id=next(_github_ids), github_login="octocat")
    workspace = Workspace(name="octocat")
    db.add_all([user, workspace])
    db.flush()
    db.add(Membership(workspace_id=workspace.id, user_id=user.id))
    repository = Repository(
        workspace_id=workspace.id,
        github_repository_id=next(_github_ids),
        github_installation_id=1,
        full_name="octocat/service",
        default_branch="main",
        is_private=False,
        created_by=user.id,
    )
    db.add(repository)
    db.commit()
    return repository


def request(
    db: Session,
    repository: Repository,
    *,
    trigger: str = "push",
    pushed_commit_sha: str | None = None,
    branch: str = "main",
) -> Job:
    job = queue.request_automatic_run(
        db,
        repository=repository,
        branch=branch,
        trigger=trigger,
        pushed_commit_sha=pushed_commit_sha,
    )
    db.commit()
    return job


def manual_reindex(db: Session, repository: Repository) -> Job:
    """The 001 re-index path, as `POST /v1/repositories/{id}/index` runs it."""
    user = db.get(User, repository.created_by)
    workspace = db.get(Workspace, repository.workspace_id)
    assert user is not None and workspace is not None
    job = repositories.reindex(
        db,
        user=user,
        workspace=workspace,
        repository_id=repository.id,
        branch=None,
        request_id=None,
    )
    db.commit()
    return job


def start(db: Session, job: Job, *, commit_sha: str | None = None) -> queue.Claim:
    """Claim the job; with `commit_sha`, also record it as the commit the attempt resolved."""
    claimed = queue.claim_next(db)
    assert claimed is not None and claimed.job_id == job.id
    if commit_sha is not None:
        db.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(payload={**claimed.payload, "commit_sha": commit_sha})
        )
        db.commit()
    return claimed


def reload(db: Session, job_id: uuid.UUID) -> Job:
    job = db.get(Job, job_id, populate_existing=True)
    assert job is not None
    return job


def job_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Job)) or 0


def test_a_push_queues_an_automatic_run(db: Session) -> None:
    repository = make_repository(db)

    job = request(db, repository, pushed_commit_sha=SHA_X)

    row = reload(db, job.id)
    assert (row.status, row.kind, row.trigger, row.created_by) == (
        "queued",
        "index_repository",
        "push",
        None,
    )
    assert row.payload == {"branch": "main", "pushed_commit_sha": SHA_X}
    assert row.dedupe_key == queue.index_dedupe_key(repository.id, "main")
    events = db.scalars(select(JobEvent.event_type).where(JobEvent.job_id == job.id)).all()
    assert events == ["queued"]


def test_a_check_request_stores_no_pushed_commit(db: Session) -> None:
    repository = make_repository(db)

    job = request(db, repository, trigger="check")

    row = reload(db, job.id)
    assert (row.trigger, row.created_by, row.payload) == ("check", None, {"branch": "main"})


@pytest.mark.parametrize("status", ["queued", "retry_wait"])
def test_a_push_joins_the_waiting_run_and_records_the_newer_commit(
    db: Session, status: str
) -> None:
    repository = make_repository(db)
    first = request(db, repository, pushed_commit_sha=SHA_X)
    db.execute(update(Job).where(Job.id == first.id).values(status=status))
    db.commit()

    again = request(db, repository, pushed_commit_sha=SHA_Y)

    assert again.id == first.id
    assert job_count(db) == 1
    row = reload(db, first.id)
    assert (row.status, row.trigger) == (status, "push")
    assert row.payload == {"branch": "main", "pushed_commit_sha": SHA_Y}


def test_a_running_job_that_resolved_the_pushed_commit_covers_the_push(db: Session) -> None:
    repository = make_repository(db)
    running = request(db, repository, pushed_commit_sha=SHA_X)
    start(db, running, commit_sha=SHA_X)

    covering = request(db, repository, pushed_commit_sha=SHA_X)

    assert covering.id == running.id
    assert job_count(db) == 1
    assert reload(db, running.id).status == "running"


def test_a_push_during_a_run_on_an_older_commit_queues_one_follow_up(db: Session) -> None:
    repository = make_repository(db)
    running = request(db, repository, pushed_commit_sha=SHA_X)
    start(db, running, commit_sha=SHA_X)

    follow_up = request(db, repository, pushed_commit_sha=SHA_Y)
    third = request(db, repository, pushed_commit_sha=SHA_Z)

    assert follow_up.id != running.id
    assert third.id == follow_up.id
    assert job_count(db) == 2
    row = reload(db, follow_up.id)
    assert (row.status, row.trigger, row.created_by) == ("queued", "push", None)
    assert row.payload == {"branch": "main", "pushed_commit_sha": SHA_Z}


def test_a_push_before_the_running_job_resolves_its_commit_queues_a_follow_up(
    db: Session,
) -> None:
    # The running job may resolve an older head, so it cannot cover the push yet.
    repository = make_repository(db)
    running = request(db, repository, pushed_commit_sha=SHA_X)
    start(db, running)

    follow_up = request(db, repository, pushed_commit_sha=SHA_X)

    assert follow_up.id != running.id
    assert reload(db, follow_up.id).status == "queued"


def test_a_check_is_never_covered_by_a_running_job(db: Session) -> None:
    repository = make_repository(db)
    running = request(db, repository, trigger="check")
    start(db, running)

    follow_up = request(db, repository, trigger="check")

    assert follow_up.id != running.id
    assert reload(db, follow_up.id).trigger == "check"


def test_requests_for_another_branch_do_not_join(db: Session) -> None:
    repository = make_repository(db)
    main = request(db, repository, pushed_commit_sha=SHA_X)

    trunk = request(db, repository, pushed_commit_sha=SHA_Y, branch="trunk")

    assert trunk.id != main.id
    assert reload(db, trunk.id).payload == {"branch": "trunk", "pushed_commit_sha": SHA_Y}


def test_a_waiting_check_takes_the_push_trigger(db: Session) -> None:
    repository = make_repository(db)
    check = request(db, repository, trigger="check")

    pushed = request(db, repository, pushed_commit_sha=SHA_X)

    assert pushed.id == check.id
    row = reload(db, check.id)
    assert (row.trigger, row.created_by) == ("push", None)
    assert row.payload == {"branch": "main", "pushed_commit_sha": SHA_X}


def test_a_waiting_check_takes_the_user_trigger_from_a_manual_reindex(db: Session) -> None:
    repository = make_repository(db)
    check = request(db, repository, trigger="check")

    manual = manual_reindex(db, repository)

    assert manual.id == check.id
    row = reload(db, check.id)
    assert (row.trigger, row.created_by) == ("user", repository.created_by)
    assert job_count(db) == 1


def test_a_check_joining_a_waiting_push_keeps_the_push_trigger(db: Session) -> None:
    repository = make_repository(db)
    pushed = request(db, repository, pushed_commit_sha=SHA_X)

    check = request(db, repository, trigger="check")

    assert check.id == pushed.id
    row = reload(db, pushed.id)
    assert row.trigger == "push"
    assert row.payload == {"branch": "main", "pushed_commit_sha": SHA_X}


def test_a_manual_reindex_joining_a_waiting_push_keeps_the_push_trigger(db: Session) -> None:
    repository = make_repository(db)
    pushed = request(db, repository, pushed_commit_sha=SHA_X)

    manual = manual_reindex(db, repository)

    assert manual.id == pushed.id
    assert (reload(db, pushed.id).trigger, reload(db, pushed.id).created_by) == ("push", None)


def test_a_push_joining_a_waiting_manual_run_keeps_the_user_trigger(db: Session) -> None:
    repository = make_repository(db)
    manual = manual_reindex(db, repository)

    pushed = request(db, repository, pushed_commit_sha=SHA_X)

    assert pushed.id == manual.id
    row = reload(db, manual.id)
    assert (row.trigger, row.created_by) == ("user", repository.created_by)
    assert row.payload == {"branch": "main", "pushed_commit_sha": SHA_X}


def test_a_manual_reindex_during_a_run_returns_the_running_job(db: Session) -> None:
    # The 001 contract: a manual request returns any active job with the key.
    repository = make_repository(db)
    running = manual_reindex(db, repository)
    start(db, running, commit_sha=SHA_X)

    again = manual_reindex(db, repository)

    assert again.id == running.id
    assert job_count(db) == 1


def test_a_manual_reindex_prefers_the_waiting_run_over_the_running_one(db: Session) -> None:
    repository = make_repository(db)
    running = request(db, repository, pushed_commit_sha=SHA_X)
    start(db, running, commit_sha=SHA_X)
    waiting = request(db, repository, pushed_commit_sha=SHA_Y)

    manual = manual_reindex(db, repository)

    assert manual.id == waiting.id
    assert job_count(db) == 2


def test_claims_and_contexts_carry_the_trigger(db: Session) -> None:
    repository = make_repository(db)
    job = request(db, repository, pushed_commit_sha=SHA_X)

    claimed = start(db, job)

    assert claimed.trigger == "push"
    assert JobContext.from_claim(claimed).trigger == "push"


def test_concurrent_automatic_requests_create_one_job(db: Session) -> None:
    repository = make_repository(db)

    with new_session() as other:
        # The other request has inserted the job but not committed: this request waits on the
        # unique index, then joins the other request's job.
        winner = queue.request_automatic_run(
            other, repository=repository, branch="main", trigger="push", pushed_commit_sha=SHA_X
        )
        committer = threading.Timer(0.5, other.commit)
        committer.start()
        try:
            job = queue.request_automatic_run(
                db,
                repository=repository,
                branch="main",
                trigger="push",
                pushed_commit_sha=SHA_Y,
            )
        finally:
            committer.join()
    db.commit()

    assert job.id == winner.id
    assert job_count(db) == 1
    assert reload(db, job.id).payload == {"branch": "main", "pushed_commit_sha": SHA_Y}


def test_the_index_allows_one_running_and_one_waiting_job_per_key(db: Session) -> None:
    repository = make_repository(db)
    key = queue.index_dedupe_key(repository.id, "main")

    def add(status: str) -> None:
        db.add(
            Job(
                workspace_id=repository.workspace_id,
                kind="index_repository",
                repository_id=repository.id,
                dedupe_key=key,
                status=status,
            )
        )
        db.flush()

    add("running")
    add("queued")
    db.commit()

    for status in ("queued", "retry_wait"):
        with pytest.raises(IntegrityError):
            add(status)
        db.rollback()
    assert job_count(db) == 2
