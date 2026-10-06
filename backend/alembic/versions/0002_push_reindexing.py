"""Automatic re-indexing and access revocation (specs/002-push-reindexing/data-model.md).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_AUDIT_ACTIONS = (
    "sign_in",
    "sign_out",
    "repository_connect",
    "repository_disconnect",
    "question_submit",
    "access_denied",
)
NEW_AUDIT_ACTIONS = (
    "webhook_rejected",
    "repository_access_lost",
    "repository_access_restored",
    "automatic_updates_paused",
    "automatic_updates_resumed",
)


def _in(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "repositories",
        sa.Column("access_state", sa.Text(), server_default="active", nullable=False),
    )
    op.add_column("repositories", sa.Column("access_reason", sa.Text(), nullable=True))
    op.add_column(
        "repositories", sa.Column("access_lost_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "repositories", sa.Column("access_checked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("repositories", sa.Column("latest_push_sha", sa.Text(), nullable=True))
    op.add_column(
        "repositories", sa.Column("latest_push_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("repositories", sa.Column("latest_push_job_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_repositories_latest_push_job_id_jobs"),
        "repositories",
        "jobs",
        ["latest_push_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        op.f("ck_repositories_access_state"),
        "repositories",
        "access_state IN ('active', 'paused', 'access_lost')",
    )
    op.create_check_constraint(
        op.f("ck_repositories_access_reason"),
        "repositories",
        "(access_state = 'active') = (access_reason IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_repositories_access_lost_at"),
        "repositories",
        "(access_state = 'access_lost') = (access_lost_at IS NOT NULL)",
    )

    op.add_column("jobs", sa.Column("trigger", sa.Text(), server_default="user", nullable=False))
    op.create_check_constraint(
        op.f("ck_jobs_trigger"), "jobs", "trigger IN ('user', 'push', 'check')"
    )
    op.drop_index("uq_jobs_active_dedupe_key", table_name="jobs")
    op.create_index(
        "uq_jobs_waiting_dedupe_key",
        "jobs",
        ["workspace_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'retry_wait')"),
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("delivery_id", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("github_installation_id", sa.BigInteger(), nullable=True),
        sa.Column("github_repository_id", sa.BigInteger(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('processed', 'ignored')", name=op.f("ck_webhook_deliveries_outcome")
        ),
        sa.PrimaryKeyConstraint("delivery_id", name=op.f("pk_webhook_deliveries")),
    )
    op.create_index(
        "ix_webhook_deliveries_received_at", "webhook_deliveries", ["received_at"], unique=False
    )

    op.drop_constraint(op.f("ck_audit_events_action"), "audit_events", type_="check")
    op.create_check_constraint(
        op.f("ck_audit_events_action"),
        "audit_events",
        _in("action", OLD_AUDIT_ACTIONS + NEW_AUDIT_ACTIONS),
    )


def downgrade() -> None:
    """Downgrade schema.

    Audit events with the new actions are deleted. Restoring the 001 dedupe index fails if a
    running and a waiting job still share a dedupe key.
    """
    op.execute(
        sa.text("DELETE FROM audit_events WHERE action = ANY(:actions)").bindparams(
            actions=list(NEW_AUDIT_ACTIONS)
        )
    )
    op.drop_constraint(op.f("ck_audit_events_action"), "audit_events", type_="check")
    op.create_check_constraint(
        op.f("ck_audit_events_action"), "audit_events", _in("action", OLD_AUDIT_ACTIONS)
    )

    op.drop_index("ix_webhook_deliveries_received_at", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")

    op.drop_index("uq_jobs_waiting_dedupe_key", table_name="jobs")
    op.create_index(
        "uq_jobs_active_dedupe_key",
        "jobs",
        ["workspace_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'retry_wait')"),
    )
    op.drop_constraint(op.f("ck_jobs_trigger"), "jobs", type_="check")
    op.drop_column("jobs", "trigger")

    op.drop_constraint(op.f("ck_repositories_access_lost_at"), "repositories", type_="check")
    op.drop_constraint(op.f("ck_repositories_access_reason"), "repositories", type_="check")
    op.drop_constraint(op.f("ck_repositories_access_state"), "repositories", type_="check")
    op.drop_constraint(
        op.f("fk_repositories_latest_push_job_id_jobs"), "repositories", type_="foreignkey"
    )
    for column in (
        "latest_push_job_id",
        "latest_push_at",
        "latest_push_sha",
        "access_checked_at",
        "access_lost_at",
        "access_reason",
        "access_state",
    ):
        op.drop_column("repositories", column)
