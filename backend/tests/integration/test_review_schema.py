"""Integration tests for the pull request review schema (T008, data-model.md, migration 0003).

Rows are inserted directly, so each check constraint is tested on its own. A rejected row names
the constraint it breaks.
"""

import hashlib
import itertools
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from codeatlas.models import (
    AnalysisRun,
    AuditEvent,
    EvidenceItem,
    Job,
    Membership,
    Repository,
    Snapshot,
    UsageCounter,
    User,
    Workspace,
)

pytestmark = pytest.mark.integration

_github_ids = itertools.count(7000)
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
MERGE_BASE_SHA = "c" * 40
PULL_REQUEST = {
    "title": "Cache repository reads",
    "body": "Adds a cache.",
    "author": "octocat",
    "base_ref": "main",
    "head_ref": "cache-reads",
    "head_repository": "octo-org/review-app",
    "is_fork": False,
    "draft": False,
    "html_url": "https://github.com/octo-org/review-app/pull/12",
    "additions": 3,
    "deletions": 9,
    "changed_files": 1,
}


@pytest.fixture
def repository(db: Session) -> Repository:
    """A user, their workspace and membership, and one connected repository."""
    user = User(github_user_id=next(_github_ids), github_login="octocat")
    workspace = Workspace(name="octocat")
    db.add_all([user, workspace])
    db.flush()
    db.add(Membership(workspace_id=workspace.id, user_id=user.id))
    repository = Repository(
        workspace_id=workspace.id,
        github_repository_id=next(_github_ids),
        github_installation_id=5001,
        full_name="octo-org/review-app",
        default_branch="main",
        is_private=False,
        created_by=user.id,
    )
    db.add(repository)
    db.commit()
    return repository


@pytest.fixture
def snapshot(db: Session, repository: Repository) -> Snapshot:
    snapshot = Snapshot(
        workspace_id=repository.workspace_id,
        repository_id=repository.id,
        commit_sha=BASE_SHA,
        branch="main",
        index_version="idx-test",
        status="ready",
        coverage={},
    )
    db.add(snapshot)
    db.commit()
    return snapshot


def review_values(repository: Repository) -> dict[str, Any]:
    """The columns of a complete review row."""
    return {
        "workspace_id": repository.workspace_id,
        "repository_id": repository.id,
        "kind": "pull_request_review",
        "commit_sha": HEAD_SHA,
        "pull_request_number": 12,
        "base_sha": BASE_SHA,
        "head_sha": HEAD_SHA,
        "merge_base_sha": MERGE_BASE_SHA,
        "pull_request": PULL_REQUEST,
        "quality_state": "reviewed",
        "result": {"overview": "Adds a cache to repository reads."},
        "model": "fake",
        "thinking_level": "medium",
        "prompt_version": "review-v1",
        "expires_at": datetime.now(UTC) + timedelta(days=30),
    }


def question_values(snapshot: Snapshot) -> dict[str, Any]:
    """The columns of a complete question row."""
    return {
        "workspace_id": snapshot.workspace_id,
        "repository_id": snapshot.repository_id,
        "kind": "repository_qa",
        "snapshot_id": snapshot.id,
        "commit_sha": snapshot.commit_sha,
        "index_version": snapshot.index_version,
        "question": "Where are repository permissions checked?",
        "quality_state": "answered",
        "model": "fake",
        "thinking_level": "medium",
        "prompt_version": "qa-v1",
        "expires_at": datetime.now(UTC) + timedelta(days=30),
    }


def add_run(db: Session, **values: Any) -> AnalysisRun:
    run = AnalysisRun(**values)
    db.add(run)
    db.commit()
    return run


def add_evidence(db: Session, run: AnalysisRun, **values: Any) -> EvidenceItem:
    excerpt = "def check_access(user, repository):"
    defaults: dict[str, Any] = {
        "analysis_run_id": run.id,
        "label": "E1",
        "path": "app/auth/access.py",
        "commit_sha": HEAD_SHA,
        "start_line": 1,
        "end_line": 1,
        "excerpt": excerpt,
        "excerpt_sha256": hashlib.sha256(excerpt.encode()).digest(),
        "rank": 1,
    }
    item = EvidenceItem(**(defaults | values))
    db.add(item)
    db.commit()
    return item


def rejected_by(constraint: str) -> pytest.RaisesExc[IntegrityError]:
    return pytest.raises(IntegrityError, match=f'"{constraint}"')


# --- analysis_runs ------------------------------------------------------------------------------


def test_a_complete_review_row_is_accepted(db: Session, repository: Repository) -> None:
    run = add_run(db, **review_values(repository))

    db.expire_all()
    stored = db.get(AnalysisRun, run.id)
    assert stored is not None
    assert (stored.kind, stored.pull_request_number, stored.quality_state) == (
        "pull_request_review",
        12,
        "reviewed",
    )
    assert (stored.base_sha, stored.head_sha, stored.merge_base_sha, stored.commit_sha) == (
        BASE_SHA,
        HEAD_SHA,
        MERGE_BASE_SHA,
        HEAD_SHA,
    )
    assert stored.pull_request == PULL_REQUEST
    assert (stored.snapshot_id, stored.index_version, stored.question) == (None, None, None)


def test_a_review_is_accepted_before_its_merge_base_is_resolved(
    db: Session, repository: Repository
) -> None:
    values = review_values(repository) | {"merge_base_sha": None, "quality_state": None}

    run = add_run(db, **values)

    assert run.merge_base_sha is None


def test_a_question_row_is_still_accepted(db: Session, snapshot: Snapshot) -> None:
    run = add_run(db, **question_values(snapshot))

    assert (run.pull_request_number, run.base_sha, run.head_sha, run.pull_request) == (
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"question": "Where are repository permissions checked?"},
        {"index_version": "idx-test"},
        {"commit_sha": BASE_SHA},
        {"pull_request_number": None},
        {"base_sha": None},
        {"pull_request": None},
    ],
    ids=["question", "index_version", "commit_not_head", "no_number", "no_base", "no_details"],
)
def test_a_review_needs_its_pull_request_columns_and_no_question_columns(
    db: Session, repository: Repository, changes: dict[str, Any]
) -> None:
    with rejected_by("ck_analysis_runs_pull_request_review_columns"):
        add_run(db, **(review_values(repository) | changes))


def test_a_review_with_a_snapshot_is_rejected(
    db: Session, repository: Repository, snapshot: Snapshot
) -> None:
    with rejected_by("ck_analysis_runs_pull_request_review_columns"):
        add_run(db, **(review_values(repository) | {"snapshot_id": snapshot.id}))


@pytest.mark.parametrize(
    "changes",
    [
        {"snapshot_id": None},
        {"index_version": None},
        {"question": None},
        {"pull_request_number": 12},
        {"head_sha": HEAD_SHA},
        {"merge_base_sha": MERGE_BASE_SHA},
        {"pull_request": PULL_REQUEST},
    ],
    ids=[
        "no_snapshot",
        "no_index_version",
        "no_question",
        "number",
        "head",
        "merge_base",
        "details",
    ],
)
def test_a_question_needs_its_columns_and_no_pull_request_columns(
    db: Session, snapshot: Snapshot, changes: dict[str, Any]
) -> None:
    with rejected_by("ck_analysis_runs_repository_qa_columns"):
        add_run(db, **(question_values(snapshot) | changes))


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"base_sha": "main"}, "ck_analysis_runs_base_sha"),
        ({"head_sha": "B" * 40, "commit_sha": "B" * 40}, "ck_analysis_runs_head_sha"),
        ({"merge_base_sha": "c"}, "ck_analysis_runs_merge_base_sha"),
    ],
    ids=["branch_name", "uppercase", "short"],
)
def test_pull_request_commits_are_40_hex_characters(
    db: Session, repository: Repository, changes: dict[str, Any], constraint: str
) -> None:
    with rejected_by(constraint):
        add_run(db, **(review_values(repository) | changes))


def test_quality_states_belong_to_their_kind(
    db: Session, repository: Repository, snapshot: Snapshot
) -> None:
    with rejected_by("ck_analysis_runs_quality_state"):
        add_run(db, **(question_values(snapshot) | {"quality_state": "reviewed"}))
    db.rollback()
    with rejected_by("ck_analysis_runs_quality_state"):
        add_run(db, **(review_values(repository) | {"quality_state": "answered"}))
    db.rollback()

    accepted = [
        add_run(db, **(question_values(snapshot) | {"quality_state": "insufficient_evidence"})),
        add_run(db, **(review_values(repository) | {"quality_state": "nothing_to_review"})),
    ]
    assert [run.quality_state for run in accepted] == ["insufficient_evidence", "nothing_to_review"]


def test_the_question_length_check_still_applies(db: Session, snapshot: Snapshot) -> None:
    with rejected_by("ck_analysis_runs_question_length"):
        add_run(db, **(question_values(snapshot) | {"question": ""}))


def test_reviews_are_indexed_by_pull_request_and_head(db: Session) -> None:
    definition = db.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_analysis_runs_pull_request'")
    )

    assert definition is not None
    assert "(repository_id, pull_request_number, head_sha, created_at)" in definition
    assert "WHERE (kind = 'pull_request_review'::text)" in definition


# --- evidence_items -----------------------------------------------------------------------------


def test_review_evidence_items_are_accepted(db: Session, repository: Repository) -> None:
    run = add_run(db, **review_values(repository))

    items = [
        add_evidence(db, run, label="E1", source_type="change", side="before"),
        add_evidence(db, run, label="E2", source_type="change", side="after"),
        add_evidence(db, run, label="E3", source_type="reference", side="after"),
        add_evidence(db, run, label="E4", source_type="test", side="after"),
    ]

    assert [(item.source_type, item.side) for item in items] == [
        ("change", "before"),
        ("change", "after"),
        ("reference", "after"),
        ("test", "after"),
    ]


def test_question_evidence_items_have_no_side(db: Session, snapshot: Snapshot) -> None:
    run = add_run(db, **question_values(snapshot))

    items = [
        add_evidence(db, run, label=f"E{rank}", source_type=source_type)
        for rank, source_type in enumerate(("symbol", "code", "doc"), start=1)
    ]

    assert [item.side for item in items] == [None, None, None]


# PostgreSQL checks constraints in name order and reports the first one a row breaks.
@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"source_type": "change"}, "ck_evidence_items_side_by_source_type"),
        ({"source_type": "code", "side": "after"}, "ck_evidence_items_side_by_source_type"),
        (
            {"source_type": "reference", "side": "before"},
            "ck_evidence_items_reference_and_test_side",
        ),
        ({"source_type": "test", "side": "before"}, "ck_evidence_items_reference_and_test_side"),
        ({"source_type": "change", "side": "middle"}, "ck_evidence_items_side"),
        ({"source_type": "commit"}, "ck_evidence_items_source_type"),
    ],
    ids=[
        "change_without_side",
        "code_with_side",
        "reference_before",
        "test_before",
        "side",
        "type",
    ],
)
def test_database_rejects_inconsistent_evidence(
    db: Session, repository: Repository, values: dict[str, Any], constraint: str
) -> None:
    run = add_run(db, **review_values(repository))

    with rejected_by(constraint):
        add_evidence(db, run, **values)


# --- usage_counters, jobs, and audit events -----------------------------------------------------


def test_reviews_count_defaults_to_zero(db: Session, repository: Repository) -> None:
    # Without the model's defaults, as rows written before the migration were.
    db.execute(
        text(
            "INSERT INTO usage_counters (workspace_id, usage_date, questions_count) "
            "VALUES (:workspace_id, :usage_date, 3)"
        ),
        {"workspace_id": repository.workspace_id, "usage_date": date(2026, 10, 6)},
    )
    db.commit()

    counter = db.scalars(select(UsageCounter)).one()
    assert (counter.questions_count, counter.reviews_count) == (3, 0)


def test_a_review_job_and_its_audit_event_are_accepted(db: Session, repository: Repository) -> None:
    run = add_run(db, **review_values(repository))
    job = Job(
        workspace_id=repository.workspace_id,
        kind="review_pull_request",
        repository_id=repository.id,
        analysis_run_id=run.id,
        dedupe_key=f"run:{run.id}",
        created_by=repository.created_by,
    )
    event = AuditEvent(
        workspace_id=repository.workspace_id,
        actor_user_id=repository.created_by,
        action="pull_request_review_submit",
        resource_type="analysis_run",
        resource_id=str(run.id),
        outcome="success",
        detail={"pull_request_number": 12, "mode": "reuse", "reused": False},
    )
    db.add_all([job, event])
    db.commit()

    assert db.scalars(select(Job.kind)).one() == "review_pull_request"
    assert db.scalars(select(AuditEvent.action)).one() == "pull_request_review_submit"
