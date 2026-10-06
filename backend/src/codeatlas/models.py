"""Database tables. See specs/001-repository-qa/data-model.md for the rules behind each field."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Text,
    func,
)
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
