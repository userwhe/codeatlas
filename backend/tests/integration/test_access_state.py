"""Integration tests for repository access-state transitions (T010, data model, research R7, R8)."""

import itertools
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from codeatlas.api.errors import ApiError
from codeatlas.db import new_session
from codeatlas.jobs import queue
from codeatlas.models import AuditEvent, Job, JobEvent, Membership, Repository, User, Workspace
from codeatlas.workspace import access

pytestmark = pytest.mark.integration

_github_ids = itertools.count(5000)
LOST_AT = datetime(2026, 10, 6, 9, 12, tzinfo=UTC)
LOST_MESSAGE = "Canceled because access to the repository was lost."


def make_repository(db: Session, **values: object) -> Repository:
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
        **values,
    )
    db.add(repository)
    db.commit()
    return repository


def add_job(db: Session, repository: Repository, status: str) -> Job:
    job = Job(
        workspace_id=repository.workspace_id,
        kind="index_repository",
        repository_id=repository.id,
        status=status,
        created_by=repository.created_by,
    )
    db.add(job)
    db.commit()
    return job


def last_event(db: Session, job: Job) -> JobEvent:
    event = db.scalars(
        select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.seq.desc())
    ).first()
    assert event is not None
    return event


def audit(db: Session, action: str) -> list[AuditEvent]:
    return list(
        db.scalars(select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.id))
    )


def state(repository: Repository) -> tuple[str, str | None, datetime | None]:
    return repository.access_state, repository.access_reason, repository.access_lost_at


def test_mark_access_lost_cancels_waiting_jobs_and_records_the_loss(db: Session) -> None:
    repository = make_repository(db)
    queued = add_job(db, repository, "queued")
    retrying = add_job(db, repository, "retry_wait")
    running = add_job(db, repository, "running")
    other = add_job(db, make_repository(db), "queued")

    assert access.mark_access_lost(db, repository, "app_uninstalled", trigger="push", now=LOST_AT)
    db.commit()

    db.expire_all()
    assert state(repository) == ("access_lost", "app_uninstalled", LOST_AT)
    assert repository.access_checked_at == LOST_AT
    statuses = {job.id: job.status for job in (queued, retrying, running, other)}
    assert statuses == {
        queued.id: "canceled",
        retrying.id: "canceled",
        running.id: "running",
        other.id: "queued",
    }
    for job in (queued, retrying):
        assert (last_event(db, job).event_type, last_event(db, job).message) == (
            "canceled",
            LOST_MESSAGE,
        )
    (event,) = audit(db, "repository_access_lost")
    assert event.actor_user_id is None
    assert event.workspace_id == repository.workspace_id
    assert (event.resource_type, event.resource_id) == ("repository", str(repository.id))
    assert event.outcome == "denied"
    assert event.detail == {"reason": "app_uninstalled", "trigger": "push"}


def test_a_later_loss_keeps_the_first_time_and_reason(db: Session) -> None:
    repository = make_repository(db)
    # A change notification is not an access check, so `access_checked_at` stays unset.
    assert access.mark_access_lost(
        db, repository, "app_suspended", trigger="notification", now=LOST_AT
    )
    db.commit()
    assert repository.access_checked_at is None
    reindex = add_job(db, repository, "queued")
    later = LOST_AT + timedelta(days=3)

    assert not access.mark_access_lost(
        db, repository, "repository_not_visible", trigger="check", now=later
    )
    db.commit()

    db.expire_all()
    assert state(repository) == ("access_lost", "app_suspended", LOST_AT)
    assert repository.access_checked_at == later
    assert reindex.status == "canceled"
    assert len(audit(db, "repository_access_lost")) == 1


def test_mark_access_lost_rereads_the_row_under_its_lock(db: Session) -> None:
    repository = make_repository(db)

    with new_session() as other:
        other_repository = other.get(Repository, repository.id)
        assert other_repository is not None
        assert access.mark_access_lost(
            other, other_repository, "app_uninstalled", trigger="notification", now=LOST_AT
        )
        # `repository` was loaded as `active` before this commit; the lock waits for it, and
        # the reread sees the loss.
        committer = threading.Timer(0.5, other.commit)
        committer.start()
        try:
            transitioned = access.mark_access_lost(
                db, repository, "repository_not_visible", trigger="check", now=LOST_AT
            )
        finally:
            committer.join()
    db.commit()

    assert not transitioned
    assert state(repository) == ("access_lost", "app_uninstalled", LOST_AT)
    assert len(audit(db, "repository_access_lost")) == 1


def test_a_loss_takes_precedence_over_a_pause(db: Session) -> None:
    repository = make_repository(db)
    assert access.pause(db, repository, "sign_in_required")
    db.commit()

    assert access.mark_access_lost(
        db, repository, "repository_removed_from_installation", trigger="notification", now=LOST_AT
    )
    db.commit()

    db.expire_all()
    assert state(repository) == ("access_lost", "repository_removed_from_installation", LOST_AT)


def test_pause_on_a_lost_repository_changes_nothing(db: Session) -> None:
    repository = make_repository(db)
    access.mark_access_lost(db, repository, "app_uninstalled", trigger="push", now=LOST_AT)
    db.commit()

    assert not access.pause(db, repository, "sign_in_required")
    db.commit()

    db.expire_all()
    assert state(repository) == ("access_lost", "app_uninstalled", LOST_AT)
    assert audit(db, "automatic_updates_paused") == []


def test_pause_records_the_transition_once(db: Session) -> None:
    repository = make_repository(db)

    assert access.pause(db, repository, "external_processing_not_accepted")
    db.commit()
    assert not access.pause(db, repository, "sign_in_required")
    db.commit()

    db.expire_all()
    assert state(repository) == ("paused", "sign_in_required", None)
    (event,) = audit(db, "automatic_updates_paused")
    assert event.actor_user_id is None
    assert event.workspace_id == repository.workspace_id
    assert (event.resource_type, event.resource_id) == ("repository", str(repository.id))
    assert event.outcome == "success"
    assert event.detail == {"reason": "external_processing_not_accepted"}


def test_restore_access_reactivates_a_lost_repository(db: Session) -> None:
    repository = make_repository(db)
    access.mark_access_lost(db, repository, "installation_cannot_read", trigger="push", now=LOST_AT)
    db.commit()
    checked = LOST_AT + timedelta(days=1)

    assert access.restore_access(db, repository, trigger="check", now=checked)
    db.commit()

    db.expire_all()
    assert state(repository) == ("active", None, None)
    assert repository.access_checked_at == checked
    (event,) = audit(db, "repository_access_restored")
    assert event.actor_user_id is None
    assert event.workspace_id == repository.workspace_id
    assert (event.resource_type, event.resource_id) == ("repository", str(repository.id))
    assert event.outcome == "success"
    assert event.detail == {"trigger": "check"}


def test_restore_access_on_a_repository_that_is_not_lost_only_records_the_check(
    db: Session,
) -> None:
    active = make_repository(db)
    paused = make_repository(db)
    access.pause(db, paused, "external_processing_not_accepted")
    db.commit()

    assert not access.restore_access(db, active, trigger="push", now=LOST_AT)
    assert not access.restore_access(db, paused, trigger="user", now=LOST_AT)
    db.commit()

    db.expire_all()
    assert state(active) == ("active", None, None)
    assert state(paused) == ("paused", "external_processing_not_accepted", None)
    assert active.access_checked_at == paused.access_checked_at == LOST_AT
    assert audit(db, "repository_access_restored") == []


def test_resume_records_the_owner(db: Session) -> None:
    repository = make_repository(db)
    access.pause(db, repository, "sign_in_required")
    db.commit()

    assert access.resume(db, repository, via="sign_in", actor_user_id=repository.created_by)
    db.commit()

    db.expire_all()
    assert state(repository) == ("active", None, None)
    (event,) = audit(db, "automatic_updates_resumed")
    assert event.actor_user_id == repository.created_by
    assert event.workspace_id == repository.workspace_id
    assert (event.resource_type, event.resource_id) == ("repository", str(repository.id))
    assert event.outcome == "success"
    assert event.detail == {"via": "sign_in"}


def test_resume_on_a_repository_that_is_not_paused_is_a_no_op(db: Session) -> None:
    active = make_repository(db)
    lost = make_repository(db)
    access.mark_access_lost(db, lost, "app_not_installed", trigger="check", now=LOST_AT)
    db.commit()

    assert not access.resume(db, active, via="acceptance", actor_user_id=active.created_by)
    assert not access.resume(db, lost, via="sign_in", actor_user_id=lost.created_by)
    db.commit()

    db.expire_all()
    assert state(active) == ("active", None, None)
    assert state(lost) == ("access_lost", "app_not_installed", LOST_AT)
    assert audit(db, "automatic_updates_resumed") == []


def test_ensure_readable_denies_only_lost_repositories(db: Session) -> None:
    repository = make_repository(db)
    access.ensure_readable(repository)
    assert access.purge_after(repository) is None
    access.pause(db, repository, "sign_in_required")
    db.commit()
    access.ensure_readable(repository)

    access.mark_access_lost(db, repository, "app_uninstalled", trigger="notification", now=LOST_AT)
    db.commit()
    db.expire_all()

    with pytest.raises(ApiError) as caught:
        access.ensure_readable(repository)
    error = caught.value
    assert (error.status, error.code, error.retryable) == (403, "repository_access_lost", False)
    assert error.details == {
        "repository_id": str(repository.id),
        "reason": "app_uninstalled",
        "lost_at": "2026-10-06T09:12:00Z",
        "purge_after": "2026-10-13T09:12:00Z",
    }
    assert access.purge_after(repository) == LOST_AT + timedelta(days=7)


def test_transitions_reject_reasons_of_the_other_kind(db: Session) -> None:
    repository = make_repository(db)

    with pytest.raises(ValueError):
        access.mark_access_lost(db, repository, "sign_in_required", trigger="push")
    with pytest.raises(ValueError):
        access.pause(db, repository, "app_uninstalled")

    assert state(repository) == ("active", None, None)


def test_disconnect_keeps_its_cancel_message(db: Session) -> None:
    repository = make_repository(db)
    job = add_job(db, repository, "queued")

    assert queue.cancel_for_repository(db, repository.id) == 1
    db.commit()

    assert last_event(db, job).message == "Canceled because the repository was disconnected."


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"access_state": "revoked", "access_reason": "app_uninstalled"}, "access_state"),
        ({"access_state": "active", "access_reason": "sign_in_required"}, "access_reason"),
        ({"access_state": "paused"}, "access_reason"),
        ({"access_state": "access_lost", "access_reason": "app_uninstalled"}, "access_lost_at"),
        (
            {
                "access_state": "paused",
                "access_reason": "sign_in_required",
                "access_lost_at": LOST_AT,
            },
            "access_lost_at",
        ),
        ({"access_state": "active", "access_lost_at": LOST_AT}, "access_lost_at"),
    ],
)
def test_database_rejects_inconsistent_access_states(
    db: Session, values: dict[str, object], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=f"ck_repositories_{constraint}"):
        make_repository(db, **values)
