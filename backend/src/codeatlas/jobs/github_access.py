"""GitHub access for jobs that read a repository: the workspace owner's token, GitHub answers as
job failures, and the guard before publishing.

Shared by indexing and review jobs. Requirement and research references are to
specs/002-push-reindexing.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.auth.github_login import forget_github_credential, get_user_token
from codeatlas.db import session_scope
from codeatlas.github.gateway import (
    AccessCheckFailed,
    AppCredentialsRejected,
    GitHubAccessDenied,
    GitHubGateway,
    GitHubNotFound,
    GitHubUnavailable,
    UserAuthorizationInvalid,
)
from codeatlas.jobs.queue import JobFailure, cancel
from codeatlas.jobs.worker import JobContext
from codeatlas.models import GitHubCredential, Job, Membership, Repository, User
from codeatlas.workspace.access import mark_access_lost, pause
from codeatlas.workspace.audit import record

DISCONNECTED_MESSAGE = "The repository was disconnected."
ACCESS_LOST_MESSAGE = "Access to the repository was lost."


@dataclass(frozen=True)
class OwnerToken:
    """The workspace owner's GitHub token, and the stored credential it came from."""

    token: str
    credential: bytes | None
    github_repository_id: int


def _record_denied(ctx: JobContext, detail: str, *, stage: str) -> None:
    """Audit a denied run. Automatic runs have no actor; the trigger says what started the run."""
    with session_scope() as db:
        record(
            db,
            action="access_denied",
            outcome="denied",
            workspace_id=ctx.workspace_id,
            actor_user_id=ctx.created_by,
            resource_type="repository",
            resource_id=str(ctx.repository_id),
            detail={"stage": stage, "reason": detail, "trigger": ctx.trigger},
        )


def _access_denied(ctx: JobContext, detail: str, *, stage: str) -> JobFailure:
    _record_denied(ctx, detail, stage=stage)
    return JobFailure(
        "access_denied",
        "CodeAtlas can no longer read this repository on GitHub.",
        permanent=True,
    )


def _access_lost(ctx: JobContext, reason: str, *, stage: str) -> JobFailure:
    """Record a definitive loss of access, then fail the run with `access_denied` (research R4).

    The loss commits in its own transaction, so it outlives the failed run. Marking it locks the
    repository row and cancels the repository's waiting jobs (research R7), so callers must hold
    no repository or job lock.
    """
    with session_scope() as db:
        repository = db.get(Repository, ctx.repository_id)
        if repository is not None and repository.deleted_at is None:
            mark_access_lost(db, repository, reason, trigger=ctx.trigger)
    return _access_denied(ctx, reason, stage=stage)


def _stored_token(db: Session, user_id: uuid.UUID, *, lock: bool = False) -> bytes | None:
    """The user's stored, encrypted GitHub access token. Each sign-in or refresh replaces it."""
    query = select(GitHubCredential.access_token_enc).where(GitHubCredential.user_id == user_id)
    return db.scalar(query.with_for_update() if lock else query)


def _sign_in_required(ctx: JobContext, credential: bytes | None) -> JobFailure:
    """Pause automatic updates because the owner's GitHub authorization lapsed, then fail the run
    with `github_sign_in_required` (research R8).

    `credential` is the stored token the failed call relied on. It no longer works, so it is
    deleted; sessions stay valid. A lost repository stays lost. Both changes commit in their own
    transaction, so they outlive the failed run. When another token was stored meanwhile, the
    owner signed in again or a refresh succeeded, so nothing changes: the new token may work.
    """
    with session_scope() as db:
        owner = _workspace_owner(db, ctx.workspace_id)
        stored = _stored_token(db, owner.id, lock=True) if owner is not None else None
        if owner is not None and stored in (None, credential):
            forget_github_credential(db, owner.id)
            repository = db.get(Repository, ctx.repository_id)
            if repository is not None and repository.deleted_at is None:
                pause(db, repository, "sign_in_required")
    # Retrying cannot help until the owner signs in again; then it can.
    return JobFailure(
        "github_sign_in_required",
        "The workspace owner's GitHub authorization is no longer valid. Sign in with GitHub "
        "again, then retry.",
        permanent=True,
        retryable=True,
    )


def disclosure_not_accepted() -> JobFailure:
    # Retrying cannot help until the owner accepts the disclosure (FR-017, research R8).
    return JobFailure(
        "external_processing_not_accepted",
        "The repository became private. Accept that selected source excerpts, pull request "
        "descriptions, and pull request changes may be sent to an external model provider, then "
        "re-index it.",
        permanent=True,
    )


def _app_misconfigured() -> JobFailure:
    # Says nothing about access to the repository, so nothing is marked (research R4).
    return JobFailure(
        "github_app_misconfigured",
        "GitHub rejected the CodeAtlas app's credentials.",
        permanent=False,
    )


def _github_unavailable() -> JobFailure:
    return JobFailure("github_unavailable", "GitHub is unavailable.", permanent=False)


@contextmanager
def github_answers(
    ctx: JobContext, *, stage: str, denied: str | None, credential: bytes | None = None
) -> Iterator[None]:
    """Turn GitHub errors raised inside the block into job failures (research R4).

    - A failed access check marks the repository lost with its reason.
    - A not-found or access-denied answer marks it lost with `denied`, the reason for reads made
      with the installation token. With `denied=None`, such an answer is not one that research
      R4 classifies, so the run fails with `access_denied` and nothing is marked (FR-013).
    - A rejected user authorization pauses automatic updates until the owner signs in again
      (research R8). `credential` is the stored token that calls in the block use.
    - Rejected App credentials or an unavailable GitHub fail the run without any state change.
    """
    try:
        yield
    except AccessCheckFailed as exc:
        raise _access_lost(ctx, exc.reason, stage=stage) from exc
    except UserAuthorizationInvalid as exc:
        raise _sign_in_required(ctx, credential) from exc
    except AppCredentialsRejected as exc:
        raise _app_misconfigured() from exc
    except (GitHubNotFound, GitHubAccessDenied) as exc:
        if denied is None:
            raise _access_denied(ctx, type(exc).__name__, stage=stage) from exc
        raise _access_lost(ctx, denied, stage=stage) from exc
    except GitHubUnavailable as exc:
        raise _github_unavailable() from exc


def cancel_run(ctx: JobContext, message: str) -> None:
    with ctx.publish() as (db, job):
        cancel(db, job, message=message)


def publishable_repository(db: Session, ctx: JobContext, job: Job) -> Repository | None:
    """Lock the repository a run publishes to. When it must not publish, cancel the locked job
    and return None.

    A repository whose access was lost is treated like a disconnected one (research R7, SC-007).
    """
    repository = db.get(Repository, ctx.repository_id, with_for_update=True)
    if repository is None or repository.deleted_at is not None:
        cancel(db, job, message=DISCONNECTED_MESSAGE)
        return None
    if repository.access_state == "access_lost":
        cancel(db, job, message=ACCESS_LOST_MESSAGE)
        return None
    return repository


def _workspace_owner(db: Session, workspace_id: uuid.UUID) -> User | None:
    return db.scalar(
        select(User)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.workspace_id == workspace_id, Membership.role == "owner")
    )


def owner_token(ctx: JobContext, gateway: GitHubGateway, *, stage: str) -> OwnerToken | None:
    """The workspace owner's GitHub token, or None when the repository was disconnected.

    Every run, whatever started it, checks the access of the workspace owner (FR-011, research
    R5). The transaction commits before any access check, so a refreshed token is kept even when
    the check fails: GitHub replaces the refresh token on every refresh. `stage` is the job stage
    that failures and audit events name.
    """
    with session_scope() as db:
        repository = db.get(Repository, ctx.repository_id)
        if repository is None or repository.deleted_at is not None:
            return None
        owner = _workspace_owner(db, ctx.workspace_id)
        if owner is None:
            raise _access_denied(ctx, "workspace owner no longer exists", stage=stage)
        credential = _stored_token(db, owner.id)
        with github_answers(ctx, stage=stage, denied=None, credential=credential):
            token = get_user_token(db, owner, gateway)
        # Read again: a refresh stores a new credential, which the later calls use.
        return OwnerToken(token, _stored_token(db, owner.id), repository.github_repository_id)
