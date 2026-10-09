"""Database tables.

See specs/001-repository-qa/data-model.md for the rules behind each field,
specs/002-push-reindexing/data-model.md for the access-state, trigger, and webhook additions, and
specs/003-pr-review/data-model.md for the pull request review additions.
"""

import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from codeatlas.db import Base

EMBEDDING_DIMENSIONS = 1024

SNAPSHOT_STATUSES = ("building", "ready", "failed", "discarded")
COVERAGE_ENTRY_TYPES = ("file", "directory")
COVERAGE_REASONS = (
    "excluded_directory",
    "credential_file",
    "generated",
    "binary",
    "unsupported_encoding",
    "too_large",
    "link",
    "unsafe_path",
    "unsupported_syntax",
    "embeddings_unavailable",
)
FILE_LANGUAGES = ("python", "typescript", "tsx", "markdown", "text")
SYMBOL_KINDS = ("class", "function", "method", "interface", "type_alias", "enum")
JOB_KINDS = ("index_repository", "answer_question", "review_pull_request")
JOB_STATUSES = ("queued", "running", "retry_wait", "succeeded", "failed", "canceled")
ACTIVE_JOB_STATUSES = ("queued", "running", "retry_wait")
WAITING_JOB_STATUSES = ("queued", "retry_wait")
JOB_TRIGGERS = ("user", "push", "check")
ACCESS_STATES = ("active", "paused", "access_lost")
WEBHOOK_OUTCOMES = ("processed", "ignored")
JOB_EVENT_TYPES = (
    "queued",
    "stage_started",
    "stage_completed",
    "retry_scheduled",
    "succeeded",
    "failed",
    "canceled",
)
ANALYSIS_KINDS = ("repository_qa", "pull_request_review")
QUESTION_QUALITY_STATES = ("answered", "insufficient_evidence")
REVIEW_QUALITY_STATES = ("reviewed", "nothing_to_review")
QUALITY_STATES = QUESTION_QUALITY_STATES + REVIEW_QUALITY_STATES
# Review evidence: diff lines (`change`), related code (`reference`), and candidate tests (`test`).
REVIEW_EVIDENCE_SOURCE_TYPES = ("change", "reference", "test")
EVIDENCE_SOURCE_TYPES = ("symbol", "code", "doc", *REVIEW_EVIDENCE_SOURCE_TYPES)
EVIDENCE_SIDES = ("before", "after")
AUDIT_ACTIONS = (
    "sign_in",
    "sign_out",
    "repository_connect",
    "repository_disconnect",
    "question_submit",
    "access_denied",
    "webhook_rejected",
    "repository_access_lost",
    "repository_access_restored",
    "automatic_updates_paused",
    "automatic_updates_resumed",
    "pull_request_review_submit",
)
AUDIT_OUTCOMES = ("success", "denied", "failure")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _commit_sha(column: str) -> str:
    return f"{column} IS NULL OR {column} ~ '^[0-9a-f]{{40}}$'"


# --- Identity -----------------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    github_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    github_login: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_sign_in_at: Mapped[datetime] = mapped_column(server_default=func.now())


class GitHubCredential(Base):
    __tablename__ = "github_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    access_token_enc: Mapped[bytes]
    access_token_expires_at: Mapped[datetime | None]
    refresh_token_enc: Mapped[bytes | None]
    refresh_token_expires_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class UserSession(Base):
    __tablename__ = "sessions"

    token_hash: Mapped[bytes] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (CheckConstraint("role = 'owner'", name="role"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, default="owner")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


# --- Repositories and indexed versions ----------------------------------------------------------


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        Index(
            "uq_repositories_active_github_repository",
            "workspace_id",
            "github_repository_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        CheckConstraint(_in("access_state", ACCESS_STATES), name="access_state"),
        CheckConstraint(
            "(access_state = 'active') = (access_reason IS NULL)", name="access_reason"
        ),
        CheckConstraint(
            "(access_state = 'access_lost') = (access_lost_at IS NOT NULL)", name="access_lost_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    github_repository_id: Mapped[int] = mapped_column(BigInteger)
    github_installation_id: Mapped[int] = mapped_column(BigInteger)
    full_name: Mapped[str] = mapped_column(Text)
    default_branch: Mapped[str] = mapped_column(Text)
    is_private: Mapped[bool]
    external_processing_accepted_at: Mapped[datetime | None]
    active_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL", use_alter=True)
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    deleted_at: Mapped[datetime | None]
    # Access to the repository on GitHub (specs/002-push-reindexing, research R7 and R8).
    access_state: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    access_reason: Mapped[str | None] = mapped_column(Text)
    access_lost_at: Mapped[datetime | None]
    access_checked_at: Mapped[datetime | None]
    # The latest default-branch push received, and the job that covers it.
    latest_push_sha: Mapped[str | None] = mapped_column(Text)
    latest_push_at: Mapped[datetime | None]
    latest_push_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL", use_alter=True)
    )


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "repository_id"],
            ["repositories.workspace_id", "repositories.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("workspace_id", "id"),
        Index(
            "uq_snapshots_ready_commit",
            "repository_id",
            "commit_sha",
            "index_version",
            unique=True,
            postgresql_where=text("status = 'ready'"),
        ),
        CheckConstraint(_in("status", SNAPSHOT_STATUSES), name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID]
    repository_id: Mapped[uuid.UUID]
    commit_sha: Mapped[str] = mapped_column(Text)
    branch: Mapped[str] = mapped_column(Text)
    index_version: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="building")
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    job_attempt: Mapped[int] = mapped_column(default=0)
    coverage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    failure_code: Mapped[str | None] = mapped_column(Text)
    failure_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    ready_at: Mapped[datetime | None]


class CoverageEntry(Base):
    __tablename__ = "coverage_entries"
    __table_args__ = (
        CheckConstraint(_in("entry_type", COVERAGE_ENTRY_TYPES), name="entry_type"),
        CheckConstraint(_in("reason", COVERAGE_REASONS), name="reason"),
        Index("ix_coverage_entries_snapshot", "snapshot_id", "path"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(Text)
    entry_type: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)


class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "snapshot_id"],
            ["snapshots.workspace_id", "snapshots.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("snapshot_id", "path"),
        CheckConstraint(_in("language", FILE_LANGUAGES), name="language"),
        CheckConstraint("size_bytes <= 1048576", name="size_bytes"),
        Index(
            "ix_files_content_trgm",
            "content",
            postgresql_using="gin",
            postgresql_ops={"content": "gin_trgm_ops"},
        ),
        Index(
            "ix_files_path_trgm",
            "path",
            postgresql_using="gin",
            postgresql_ops={"path": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID]
    snapshot_id: Mapped[uuid.UUID]
    path: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int]
    line_count: Mapped[int]
    content_sha256: Mapped[bytes]
    content: Mapped[str] = mapped_column(Text)


class Symbol(Base):
    __tablename__ = "symbols"
    __table_args__ = (
        CheckConstraint(_in("kind", SYMBOL_KINDS), name="kind"),
        Index("ix_symbols_snapshot_name", "snapshot_id", "name"),
        Index(
            "ix_symbols_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"))
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    qualified_name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int]
    end_line: Mapped[int]


class CodeChunk(Base):
    __tablename__ = "code_chunks"
    __table_args__ = (
        Index("ix_code_chunks_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_code_chunks_snapshot", "snapshot_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"))
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    start_line: Mapped[int]
    end_line: Mapped[int]
    search_vector: Mapped[Any] = mapped_column(TSVECTOR)


class DocChunk(Base):
    __tablename__ = "doc_chunks"
    __table_args__ = (
        Index("ix_doc_chunks_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_doc_chunks_snapshot", "snapshot_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"))
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    start_line: Mapped[int]
    end_line: Mapped[int]
    heading_path: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text)
    search_vector: Mapped[Any] = mapped_column(TSVECTOR)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))


# --- Jobs and progress --------------------------------------------------------------------------


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "repository_id"],
            ["repositories.workspace_id", "repositories.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(_in("kind", JOB_KINDS), name="kind"),
        CheckConstraint(_in("status", JOB_STATUSES), name="status"),
        CheckConstraint(_in("trigger", JOB_TRIGGERS), name="trigger"),
        Index(
            "uq_jobs_one_running_per_kind",
            "workspace_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        # Waiting jobs only: a running job and one waiting job may share a key (research R3).
        Index(
            "uq_jobs_waiting_dedupe_key",
            "workspace_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text(_in("status", WAITING_JOB_STATUSES)),
        ),
        Index("ix_jobs_claim", "status", "run_after"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    repository_id: Mapped[uuid.UUID]
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE", use_alter=True)
    )
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    # What started the job: `user`, `push`, or `check` (specs/002-push-reindexing, research R3).
    trigger: Mapped[str] = mapped_column(Text, default="user", server_default="user")
    status: Mapped[str] = mapped_column(Text, default="queued")
    attempt: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=0)
    lease_expires_at: Mapped[datetime | None]
    run_after: Mapped[datetime] = mapped_column(server_default=func.now())
    deadline_at: Mapped[datetime | None]
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    error_retryable: Mapped[bool | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (CheckConstraint(_in("event_type", JOB_EVENT_TYPES), name="event_type"),)

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(Text)
    stage: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


# --- Questions, answers, and reviews --------------------------------------------------------------


class AnalysisRun(Base):
    """A question and its answer (kind `repository_qa`), or a pull request review.

    A review (kind `pull_request_review`) reads its commits from GitHub archives and references no
    indexed version (specs/003-pr-review, research R3).
    """

    __tablename__ = "analysis_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "repository_id"],
            ["repositories.workspace_id", "repositories.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "snapshot_id"],
            ["snapshots.workspace_id", "snapshots.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(_in("kind", ANALYSIS_KINDS), name="kind"),
        # A null question passes, so the check applies to questions only.
        CheckConstraint("char_length(question) BETWEEN 1 AND 2000", name="question_length"),
        CheckConstraint(
            "quality_state IS NULL"
            f" OR (kind = 'repository_qa' AND {_in('quality_state', QUESTION_QUALITY_STATES)})"
            f" OR (kind = 'pull_request_review' AND {_in('quality_state', REVIEW_QUALITY_STATES)})",
            name="quality_state",
        ),
        CheckConstraint(
            "kind <> 'repository_qa' OR (snapshot_id IS NOT NULL AND index_version IS NOT NULL"
            " AND question IS NOT NULL AND pull_request_number IS NULL AND base_sha IS NULL"
            " AND head_sha IS NULL AND merge_base_sha IS NULL AND pull_request IS NULL)",
            name="repository_qa_columns",
        ),
        CheckConstraint(
            "kind <> 'pull_request_review' OR (pull_request_number IS NOT NULL"
            " AND base_sha IS NOT NULL AND head_sha IS NOT NULL AND pull_request IS NOT NULL"
            " AND snapshot_id IS NULL AND index_version IS NULL AND question IS NULL"
            " AND commit_sha = head_sha)",
            name="pull_request_review_columns",
        ),
        CheckConstraint(_commit_sha("base_sha"), name="base_sha"),
        CheckConstraint(_commit_sha("head_sha"), name="head_sha"),
        CheckConstraint(_commit_sha("merge_base_sha"), name="merge_base_sha"),
        Index("ix_analysis_runs_repository_created", "repository_id", "created_at"),
        # Reuse lookups and the latest review per pull request (specs/003-pr-review, research R9).
        Index(
            "ix_analysis_runs_pull_request",
            "repository_id",
            "pull_request_number",
            "head_sha",
            "created_at",
            postgresql_where=text("kind = 'pull_request_review'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID]
    repository_id: Mapped[uuid.UUID]
    snapshot_id: Mapped[uuid.UUID | None]
    # For a review, the pinned head commit (equal to `head_sha`).
    commit_sha: Mapped[str] = mapped_column(Text)
    index_version: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, default="repository_qa")
    question: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    quality_state: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None]
    model: Mapped[str] = mapped_column(Text)
    thinking_level: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    usage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None]
    expires_at: Mapped[datetime]
    # The reviewed pull request, copied at submission (specs/003-pr-review/data-model.md). The
    # merge base stays null until the job resolves it (research R2).
    pull_request_number: Mapped[int | None]
    base_sha: Mapped[str | None] = mapped_column(Text)
    head_sha: Mapped[str | None] = mapped_column(Text)
    merge_base_sha: Mapped[str | None] = mapped_column(Text)
    # None is stored as SQL NULL rather than JSON null, so the kind checks see it.
    pull_request: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))


class EvidenceItem(Base):
    __tablename__ = "evidence_items"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "label"),
        CheckConstraint(_in("source_type", EVIDENCE_SOURCE_TYPES), name="source_type"),
        CheckConstraint(f"side IS NULL OR {_in('side', EVIDENCE_SIDES)}", name="side"),
        CheckConstraint(
            f"({_in('source_type', REVIEW_EVIDENCE_SOURCE_TYPES)}) = (side IS NOT NULL)",
            name="side_by_source_type",
        ),
        CheckConstraint(
            "source_type NOT IN ('reference', 'test') OR side = 'after'",
            name="reference_and_test_side",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE")
    )
    label: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    commit_sha: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int]
    end_line: Mapped[int]
    excerpt: Mapped[str] = mapped_column(Text)
    excerpt_sha256: Mapped[bytes]
    rank: Mapped[int]
    # Review evidence only: `before` (the merge base) or `after` (the head).
    side: Mapped[str | None] = mapped_column(Text)


# --- Supporting records ---------------------------------------------------------------------------


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    route: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    payload_sha256: Mapped[bytes]
    response_status: Mapped[int]
    response_body: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]


class UsageCounter(Base):
    __tablename__ = "usage_counters"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    usage_date: Mapped[date] = mapped_column(primary_key=True)
    questions_count: Mapped[int] = mapped_column(default=0)
    reviews_count: Mapped[int] = mapped_column(default=0, server_default="0")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(_in("action", AUDIT_ACTIONS), name="action"),
        CheckConstraint(_in("outcome", AUDIT_OUTCOMES), name="outcome"),
        Index("ix_audit_events_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL")
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(Text)
    resource_type: Mapped[str | None] = mapped_column(Text)
    resource_id: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class WebhookDelivery(Base):
    """A verified GitHub webhook delivery, kept 14 days to recognize redeliveries.

    Not tenant-owned and never returned by the API. Unverified requests are only audited
    (specs/002-push-reindexing, research R2).
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        CheckConstraint(_in("outcome", WEBHOOK_OUTCOMES), name="outcome"),
        Index("ix_webhook_deliveries_received_at", "received_at"),
    )

    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event: Mapped[str] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(Text)
    github_installation_id: Mapped[int | None] = mapped_column(BigInteger)
    github_repository_id: Mapped[int | None] = mapped_column(BigInteger)
    outcome: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
