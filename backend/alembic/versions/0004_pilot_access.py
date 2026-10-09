"""Pilot access list and pilot-wide usage (specs/004-pilot-deployment/data-model.md).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_AUDIT_ACTIONS = (
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
NEW_AUDIT_ACTIONS = ("pilot_user_add", "pilot_user_remove", "pilot_user_delete_data")


def _in(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
    op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, condition)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "pilot_users",
        sa.Column("github_user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("github_login", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "note IS NULL OR char_length(note) <= 200", name=op.f("ck_pilot_users_note")
        ),
        sa.PrimaryKeyConstraint("github_user_id", name=op.f("pk_pilot_users")),
    )
    op.create_table(
        "pilot_usage_counters",
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("questions_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reviews_count", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("usage_date", name=op.f("pk_pilot_usage_counters")),
    )
    _replace_check("audit_events", "action", _in("action", OLD_AUDIT_ACTIONS + NEW_AUDIT_ACTIONS))


def downgrade() -> None:
    """Downgrade schema.

    Audit events with the new actions are deleted. The pilot list and the pilot-wide counts are
    dropped with their tables.
    """
    op.execute(
        sa.text("DELETE FROM audit_events WHERE action = ANY(:actions)").bindparams(
            actions=list(NEW_AUDIT_ACTIONS)
        )
    )
    _replace_check("audit_events", "action", _in("action", OLD_AUDIT_ACTIONS))

    op.drop_table("pilot_usage_counters")
    op.drop_table("pilot_users")
