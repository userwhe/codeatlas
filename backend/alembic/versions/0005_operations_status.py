"""Operations status for the alert checks (specs/004-pilot-deployment/data-model.md).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-12 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id", name=op.f("pk_worker_heartbeats")),
    )
    op.create_table(
        "backups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("alembic_revision", sa.Text(), nullable=False),
        sa.Column("release", sa.Text(), nullable=False),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_backups_sha256")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backups")),
        sa.UniqueConstraint("object_key", name=op.f("uq_backups_object_key")),
    )
    op.add_column("jobs", sa.Column("error_permanent", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    The heartbeats, the backup records, and whether each failure was permanent are dropped. The
    backups themselves stay in the bucket.
    """
    op.drop_column("jobs", "error_permanent")
    op.drop_table("backups")
    op.drop_table("worker_heartbeats")
