"""Submitting questions and reading answers (US2). A question and its answer are an analysis run."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.api.errors import ApiError, not_found
from codeatlas.config import get_settings
from codeatlas.jobs.queue import enqueue
from codeatlas.models import AnalysisRun, EvidenceItem, Job, Repository, Snapshot, User, Workspace
from codeatlas.qa.prompt import PROMPT_VERSION
from codeatlas.qa.schema import AnswerOutput
from codeatlas.qa.validate import cited_labels
from codeatlas.workspace import repositories as repos
from codeatlas.workspace.audit import deny, record
from codeatlas.workspace.quotas import reserve_question

ANALYSIS_KIND = "repository_qa"
JOB_KIND = "answer_question"
RUN_TTL = timedelta(days=30)
MAX_QUESTION_CHARS = 2000


def _invalid_question() -> ApiError:
    return ApiError(
        422, "invalid_question", f"A question must be 1 to {MAX_QUESTION_CHARS} characters."
    )


def submit(
    db: Session,
    *,
    user: User,
    workspace: Workspace,
    repository_id: uuid.UUID,
    snapshot_id: uuid.UUID | None,
    question: str,
    request_id: str | None,
) -> tuple[AnalysisRun, Job]:
    """Pin a ready snapshot, reserve quota, and queue the answer job. The caller commits."""
    text = question.strip()
    if not 1 <= len(text) <= MAX_QUESTION_CHARS:
        raise _invalid_question()
    repository = repos.get_scoped(
        db, user=user, workspace=workspace, repository_id=repository_id, request_id=request_id
    )
    if snapshot_id is not None:
        snapshot = repos.get_scoped_snapshot(
            db, user=user, workspace=workspace, snapshot_id=snapshot_id, request_id=request_id
        )
        if snapshot.repository_id != repository.id:
            raise not_found()
    else:
        if repository.active_snapshot_id is None:
            raise ApiError(409, "snapshot_not_ready", "This repository has no ready version yet.")
        found = db.get(Snapshot, repository.active_snapshot_id)
        if found is None or found.status != "ready":
            raise ApiError(409, "snapshot_not_ready", "This repository has no ready version yet.")
        snapshot = found

    reserve_question(db, workspace.id)
    settings = get_settings()
    now = datetime.now(UTC)
    run = AnalysisRun(
        workspace_id=workspace.id,
        repository_id=repository.id,
        snapshot_id=snapshot.id,
        commit_sha=snapshot.commit_sha,
        index_version=snapshot.index_version,
        kind=ANALYSIS_KIND,
        question=text,
        model=settings.answer_model,
        thinking_level=settings.answer_thinking_level,
        prompt_version=PROMPT_VERSION,
        created_by=user.id,
        created_at=now,
        expires_at=now + RUN_TTL,
    )
    db.add(run)
    db.flush()
    job, _ = enqueue(
        db,
        workspace_id=workspace.id,
        kind=JOB_KIND,
        repository_id=repository.id,
        analysis_run_id=run.id,
        created_by=user.id,
        dedupe_key=f"run:{run.id}",
    )
    run.job_id = job.id
    record(
        db,
        action="question_submit",
        outcome="success",
        workspace_id=workspace.id,
        actor_user_id=user.id,
        resource_type="analysis_run",
        resource_id=str(run.id),
        request_id=request_id,
    )
    return run, job


def get_scoped(
    db: Session,
    *,
    user: User,
    workspace: Workspace,
    run_id: uuid.UUID,
    request_id: str | None,
) -> AnalysisRun:
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise not_found()
    repository = db.get(Repository, run.repository_id)
    if run.workspace_id != workspace.id or repository is None or repository.deleted_at is not None:
        raise deny(
            actor_user_id=user.id,
            workspace_id=workspace.id,
            resource_type="analysis_run",
            resource_id=str(run_id),
            request_id=request_id,
        )
    return run


def list_for_repository(
    db: Session, repository_id: uuid.UUID, *, offset: int, limit: int
) -> list[AnalysisRun]:
    return list(
        db.scalars(
            select(AnalysisRun)
            .where(AnalysisRun.repository_id == repository_id)
            .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id)
            .offset(offset)
            .limit(limit)
        )
    )


def view_url(snapshot_id: uuid.UUID, path: str, start_line: int, end_line: int) -> str:
    query = urlencode({"path": path, "lines": f"{start_line}-{end_line}"})
    return f"/snapshots/{snapshot_id}/browse?{query}"


def citations(db: Session, run: AnalysisRun) -> list[dict[str, Any]]:
    """Citations built from stored evidence items, never from model text (FR-020)."""
    if run.result is None:
        return []
    labels = cited_labels(AnswerOutput.model_validate(run.result))
    items = {
        item.label: item
        for item in db.scalars(select(EvidenceItem).where(EvidenceItem.analysis_run_id == run.id))
    }
    return [
        {
            "label": item.label,
            "source_type": item.source_type,
            "path": item.path,
            "commit_sha": item.commit_sha,
            "start_line": item.start_line,
            "end_line": item.end_line,
            "excerpt": item.excerpt,
            "view_url": view_url(run.snapshot_id, item.path, item.start_line, item.end_line),
        }
        for label in labels
        if (item := items.get(label)) is not None
    ]
