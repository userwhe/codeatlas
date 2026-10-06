"""Database tables. See specs/001-repository-qa/data-model.md for the rules behind each field."""

import uuid
from datetime import datetime
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
from sqlalchemy.dialects.postgresql import TSVECTOR
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
JOB_KINDS = ("index_repository", "answer_question")
JOB_STATUSES = ("queued", "running", "retry_wait", "succeeded", "failed", "canceled")
ACTIVE_JOB_STATUSES = ("queued", "running", "retry_wait")
JOB_EVENT_TYPES = (
    "queued",
    "stage_started",
    "stage_completed",
    "retry_scheduled",
    "succeeded",
    "failed",
    "canceled",
)
ANALYSIS_KINDS = ("repository_qa",)
QUALITY_STATES = ("answered", "insufficient_evidence")
EVIDENCE_SOURCE_TYPES = ("symbol", "code", "doc")
AUDIT_ACTIONS = (
    "sign_in",
    "sign_out",
    "repository_connect",
    "repository_disconnect",
    "question_submit",
    "access_denied",
)
AUDIT_OUTCOMES = ("success", "denied", "failure")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


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
