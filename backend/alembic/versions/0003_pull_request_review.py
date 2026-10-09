"""Pull request review (specs/003-pr-review/data-model.md).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_ANALYSIS_KINDS = ("repository_qa",)
OLD_QUALITY_STATES = ("answered", "insufficient_evidence")
OLD_EVIDENCE_SOURCE_TYPES = ("symbol", "code", "doc")
OLD_JOB_KINDS = ("index_repository", "answer_question")
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
)
REVIEW_EVIDENCE_SOURCE_TYPES = ("change", "reference", "test")
NEW_AUDIT_ACTION = "pull_request_review_submit"
PULL_REQUEST_COLUMNS = (
    ("pull_request_number", sa.Integer()),
    ("base_sha", sa.Text()),
    ("head_sha", sa.Text()),
    ("merge_base_sha", sa.Text()),
    ("pull_request", postgresql.JSONB(astext_type=sa.Text())),
)
COMMIT_SHA_COLUMNS = ("base_sha", "head_sha", "merge_base_sha")
QUESTION_COLUMNS = (
    ("snapshot_id", sa.Uuid()),
    ("index_version", sa.Text()),
    ("question", sa.Text()),
)

QUALITY_STATE = (
    "quality_state IS NULL"
    " OR (kind = 'repository_qa' AND quality_state IN ('answered', 'insufficient_evidence'))"
    " OR (kind = 'pull_request_review' AND quality_state IN ('reviewed', 'nothing_to_review'))"
)
REPOSITORY_QA_COLUMNS = (
    "kind <> 'repository_qa' OR (snapshot_id IS NOT NULL AND index_version IS NOT NULL"
    " AND question IS NOT NULL AND pull_request_number IS NULL AND base_sha IS NULL"
    " AND head_sha IS NULL AND merge_base_sha IS NULL AND pull_request IS NULL)"
)
PULL_REQUEST_REVIEW_COLUMNS = (
    "kind <> 'pull_request_review' OR (pull_request_number IS NOT NULL"
    " AND base_sha IS NOT NULL AND head_sha IS NOT NULL AND pull_request IS NOT NULL"
    " AND snapshot_id IS NULL AND index_version IS NULL AND question IS NULL"
    " AND commit_sha = head_sha)"
)
EVIDENCE_SIDE_CHECKS = (
    ("side", "side IS NULL OR side IN ('before', 'after')"),
    (
        "side_by_source_type",
        "(source_type IN ('change', 'reference', 'test')) = (side IS NOT NULL)",
    ),
    ("reference_and_test_side", "source_type NOT IN ('reference', 'test') OR side = 'after'"),
)


def _in(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _commit_sha(column: str) -> str:
    return f"{column} IS NULL OR {column} ~ '^[0-9a-f]{{40}}$'"


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
    op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, condition)


def upgrade() -> None:
    """Upgrade schema."""
    for column, type_ in QUESTION_COLUMNS:
        op.alter_column("analysis_runs", column, existing_type=type_, nullable=True)
    for column, type_ in PULL_REQUEST_COLUMNS:
        op.add_column("analysis_runs", sa.Column(column, type_, nullable=True))
    _replace_check(
        "analysis_runs", "kind", _in("kind", (*OLD_ANALYSIS_KINDS, "pull_request_review"))
    )
    _replace_check("analysis_runs", "quality_state", QUALITY_STATE)
    op.create_check_constraint(
        op.f("ck_analysis_runs_repository_qa_columns"), "analysis_runs", REPOSITORY_QA_COLUMNS
    )
    op.create_check_constraint(
        op.f("ck_analysis_runs_pull_request_review_columns"),
        "analysis_runs",
        PULL_REQUEST_REVIEW_COLUMNS,
    )
    for column in COMMIT_SHA_COLUMNS:
        op.create_check_constraint(
            op.f(f"ck_analysis_runs_{column}"), "analysis_runs", _commit_sha(column)
        )
    op.create_index(
        "ix_analysis_runs_pull_request",
        "analysis_runs",
        ["repository_id", "pull_request_number", "head_sha", "created_at"],
        unique=False,
        postgresql_where=sa.text("kind = 'pull_request_review'"),
    )

    op.add_column("evidence_items", sa.Column("side", sa.Text(), nullable=True))
    _replace_check(
        "evidence_items",
        "source_type",
        _in("source_type", OLD_EVIDENCE_SOURCE_TYPES + REVIEW_EVIDENCE_SOURCE_TYPES),
    )
    for name, condition in EVIDENCE_SIDE_CHECKS:
        op.create_check_constraint(op.f(f"ck_evidence_items_{name}"), "evidence_items", condition)

    op.add_column(
        "usage_counters",
        sa.Column("reviews_count", sa.Integer(), server_default="0", nullable=False),
    )

    _replace_check("jobs", "kind", _in("kind", (*OLD_JOB_KINDS, "review_pull_request")))
    _replace_check("audit_events", "action", _in("action", (*OLD_AUDIT_ACTIONS, NEW_AUDIT_ACTION)))


def downgrade() -> None:
    """Downgrade schema.

    Refused while any pull request review exists; their jobs and evidence go with them. Audit
    events with the new action are deleted.
    """
    reviews = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM analysis_runs WHERE kind = 'pull_request_review'")
    )
    if reviews:
        raise RuntimeError(
            f"Cannot downgrade while pull request reviews exist (found {reviews}). "
            "Delete them first."
        )

    op.execute(
        sa.text("DELETE FROM audit_events WHERE action = :action").bindparams(
            action=NEW_AUDIT_ACTION
        )
    )
    _replace_check("audit_events", "action", _in("action", OLD_AUDIT_ACTIONS))
    _replace_check("jobs", "kind", _in("kind", OLD_JOB_KINDS))

    op.drop_column("usage_counters", "reviews_count")

    for name, _ in EVIDENCE_SIDE_CHECKS:
        op.drop_constraint(op.f(f"ck_evidence_items_{name}"), "evidence_items", type_="check")
    _replace_check("evidence_items", "source_type", _in("source_type", OLD_EVIDENCE_SOURCE_TYPES))
    op.drop_column("evidence_items", "side")

    op.drop_index("ix_analysis_runs_pull_request", table_name="analysis_runs")
    for column in COMMIT_SHA_COLUMNS:
        op.drop_constraint(op.f(f"ck_analysis_runs_{column}"), "analysis_runs", type_="check")
    op.drop_constraint(
        op.f("ck_analysis_runs_pull_request_review_columns"), "analysis_runs", type_="check"
    )
    op.drop_constraint(
        op.f("ck_analysis_runs_repository_qa_columns"), "analysis_runs", type_="check"
    )
    _replace_check(
        "analysis_runs",
        "quality_state",
        f"quality_state IS NULL OR {_in('quality_state', OLD_QUALITY_STATES)}",
    )
    _replace_check("analysis_runs", "kind", _in("kind", OLD_ANALYSIS_KINDS))
    for column, _ in reversed(PULL_REQUEST_COLUMNS):
        op.drop_column("analysis_runs", column)
    for column, type_ in QUESTION_COLUMNS:
        op.alter_column("analysis_runs", column, existing_type=type_, nullable=False)
