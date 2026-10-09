"""Integration tests for reading reviews, access to them, and their retention (T028).

Reviews are written directly to the database, as the review job would publish them, so these tests
cover only the reads: the review response, 403 while access is lost, 404 after disconnecting,
purging by the maintenance pass, and the audit of denied reads.
"""

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from codeatlas.github.fake import REVIEW_APP_ID, commit_sha, get_fake_github
from codeatlas.jobs.maintenance import run_maintenance
from codeatlas.models import AnalysisRun, AuditEvent, EvidenceItem, Job, Repository
from codeatlas.workspace import access
from tests.integration.test_review_submit import connect

pytestmark = pytest.mark.integration

BASE_SHA = commit_sha(REVIEW_APP_ID, "initial")
HEAD_SHA = commit_sha(REVIEW_APP_ID, "pr-1")
PULL_REQUEST = {
    "title": "Simplify write checks",
    "body": "Drops the role lookup in `can_write`.",
    "author": "hubot",
    "base_ref": "main",
    "head_ref": "simplify-write-checks",
    "head_repository": "octo-org/review-app",
    "is_fork": False,
    "draft": False,
    "html_url": "https://github.com/octo-org/review-app/pull/1",
    "additions": 2,
    "deletions": 2,
    "changed_files": 1,
}
COVERAGE = {
    "files": [
        {
            "path": "app/auth/permissions.py",
            "previous_path": None,
            "change": "modified",
            "additions": 2,
            "deletions": 2,
            "reviewed": True,
            "reason": None,
        }
    ],
    "changed_files": 1,
    "reviewed_files": 1,
    "changed_lines_reviewed": 4,
    "context_items": 1,
}
# A review result as the job stores it (data-model.md): labels are under `evidence_ids`.
RESULT = {
    "overall_risk": {"level": "high", "partial": False},
    "overview": "Removes the role check from `can_write`.",
    "summary": [
        {
            "area": "app/auth",
            "points": [
                {
                    "change": "modified",
                    "text": "`can_write` no longer checks the role.",
                    "evidence_ids": ["E1", "E2"],
                    "origin": "model",
                }
            ],
        }
    ],
    "risks": [
        {
            "id": "R1",
            "title": "Role check removed",
            "severity": "high",
            "category": "security",
            "basis": "observed",
            "explanation": "Any listed user can now write.",
            "suggested_check": "Confirm that viewers cannot write.",
            "evidence_ids": ["E2", "E3"],
            "origin": "model",
            "path": None,
        }
    ],
    "checklist": [],
    "tests": {"changed": [], "candidates": [], "new_cases": []},
    "coverage": COVERAGE,
    "omitted_items": 0,
}


@dataclass(frozen=True)
class Evidence:
    label: str
    source_type: str
    side: str
    path: str
    commit_sha: str
    start_line: int
    end_line: int
    excerpt: str


EVIDENCE = [
    Evidence(
        "E1",
        "change",
        "after",
        "app/auth/permissions.py",
        HEAD_SHA,
        16,
        18,
        'def can_write(user: User, repository_id: int) -> bool:\n    """Return True."""\n'
        "    return repository_id in user.repository_ids",
    ),
    Evidence(
        "E2",
        "change",
        "before",
        "app/auth/permissions.py",
        BASE_SHA,
        16,
        18,
        'def can_write(user: User, repository_id: int) -> bool:\n    """Return True."""\n'
        "    return user.role in WRITE_ROLES and repository_id in user.repository_ids",
    ),
    Evidence(
        "E3",
        "reference",
        "after",
        "app/repositories.py",
        HEAD_SHA,
        5,
        7,
        "def rename(user, repository_id, name):\n    if not can_write(user, repository_id):\n"
        "        raise PermissionError(name)",
    ),
    # Stored but cited by nothing, so it is not returned.
    Evidence("E4", "reference", "after", "app/text.py", HEAD_SHA, 1, 2, "def slugify(text):\n"),
]


def add_review(
    db: Session,
    repository_id: str,
    *,
    number: int = 1,
    head_sha: str = HEAD_SHA,
    job_status: str = "succeeded",
    result: dict[str, Any] | None = None,
    evidence: list[Evidence] | None = None,
    created_at: datetime | None = None,
    merge_base_sha: str | None = BASE_SHA,
) -> AnalysisRun:
    """Write a review run and its job as the submission and the review job would.

    A succeeded job gets `result` (by default `RESULT`), `quality_state` `reviewed`, and
    `evidence` (by default `EVIDENCE`). Other statuses have no result and no evidence.
    """
    repository = db.get(Repository, uuid.UUID(repository_id))
    assert repository is not None
    created = created_at or datetime.now(UTC)
    succeeded = job_status == "succeeded"
    run = AnalysisRun(
        workspace_id=repository.workspace_id,
        repository_id=repository.id,
        kind="pull_request_review",
        commit_sha=head_sha,
        pull_request_number=number,
        base_sha=BASE_SHA,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        pull_request={
            **PULL_REQUEST,
            "html_url": f"https://github.com/octo-org/review-app/pull/{number}",
        },
        quality_state="reviewed" if succeeded else None,
        result=(result if result is not None else RESULT) if succeeded else None,
        model="fake",
        thinking_level="medium",
        prompt_version="review-v1",
        created_by=repository.created_by,
        created_at=created,
        completed_at=created + timedelta(seconds=30) if succeeded else None,
        expires_at=created + timedelta(days=30),
    )
    db.add(run)
    db.flush()
    failed = job_status in ("failed", "canceled")
    job = Job(
        workspace_id=repository.workspace_id,
        kind="review_pull_request",
        repository_id=repository.id,
        analysis_run_id=run.id,
        dedupe_key=f"run:{run.id}",
        status=job_status,
        created_by=repository.created_by,
        created_at=created,
        error_code="provider_unavailable" if failed else None,
        error_message="The model provider is unavailable." if failed else None,
        error_retryable=True if failed else None,
    )
    db.add(job)
    db.flush()
    run.job_id = job.id
    for item in (evidence if evidence is not None else EVIDENCE) if succeeded else []:
        db.add(
            EvidenceItem(
                analysis_run_id=run.id,
                label=item.label,
                source_type=item.source_type,
                side=item.side,
                path=item.path,
                commit_sha=item.commit_sha,
                start_line=item.start_line,
                end_line=item.end_line,
                excerpt=item.excerpt,
                excerpt_sha256=hashlib.sha256(item.excerpt.encode()).digest(),
                rank=int(item.label[1:]),
            )
        )
    db.commit()
    return run


def github_url(item: Evidence) -> str:
    return (
        f"https://github.com/octo-org/review-app/blob/{item.commit_sha}/{item.path}"
        f"#L{item.start_line}-L{item.end_line}"
    )


def counts(db: Session) -> tuple[int | None, int | None]:
    db.expire_all()
    return (
        db.scalar(select(func.count()).select_from(AnalysisRun)),
        db.scalar(select(func.count()).select_from(EvidenceItem)),
    )


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


@pytest.fixture
def repository_id(octocat: TestClient, run_worker_once: Callable[[], bool]) -> str:
    return connect(octocat, run_worker_once)


def test_review_cites_stored_evidence(db: Session, octocat: TestClient, repository_id: str) -> None:
    run = add_review(db, repository_id)

    response = octocat.get(f"/v1/analysis-runs/{run.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "pull_request_review"
    assert (body["status"], body["quality_state"]) == ("succeeded", "reviewed")
    assert body["repository_id"] == repository_id
    assert body["repository_full_name"] == "octo-org/review-app"
    assert body["pull_request"] == {
        "number": 1,
        "title": "Simplify write checks",
        "author": "hubot",
        "draft": False,
        "base_ref": "main",
        "head_ref": "simplify-write-checks",
        "head_repository": "octo-org/review-app",
        "is_fork": False,
        "html_url": "https://github.com/octo-org/review-app/pull/1",
    }
    assert body["commits"] == {
        "base_sha": BASE_SHA,
        "head_sha": HEAD_SHA,
        "merge_base_sha": BASE_SHA,
    }
    review = body["review"]
    assert review["overall_risk"] == {"level": "high", "partial": False}
    assert review["overview"] == RESULT["overview"]
    (area,) = review["summary"]
    assert area["area"] == "app/auth"
    assert area["points"] == [
        {
            "change": "modified",
            "text": "`can_write` no longer checks the role.",
            "citations": ["E1", "E2"],
            "origin": "model",
        }
    ]
    (risk,) = review["risks"]
    assert (risk["id"], risk["severity"], risk["category"], risk["basis"]) == (
        "R1",
        "high",
        "security",
        "observed",
    )
    assert (risk["citations"], risk["origin"], risk["path"]) == (["E2", "E3"], "model", None)
    assert review["checklist"] == []
    assert review["tests"] == {"changed": [], "candidates": [], "new_cases": []}
    assert review["omitted_items"] == 0
    assert "coverage" not in review
    # File entries are returned with their entry type; only directory entries have a count.
    assert body["coverage"] == {
        **COVERAGE,
        "files": [{**entry, "entry_type": "file", "count": None} for entry in COVERAGE["files"]],
    }
    # Only the cited items, each with its side, commit, and a link built from stored evidence.
    assert body["citations"] == [
        {
            "label": item.label,
            "source_type": item.source_type,
            "side": item.side,
            "path": item.path,
            "commit_sha": item.commit_sha,
            "start_line": item.start_line,
            "end_line": item.end_line,
            "excerpt": item.excerpt,
            "github_url": github_url(item),
        }
        for item in EVIDENCE[:3]
    ]
    assert body["error"] is None
    assert body["job_id"] == str(run.job_id)
    assert body["completed_at"] is not None


def test_unfinished_and_failed_reviews_have_no_review_yet(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    queued = add_review(db, repository_id, job_status="queued", merge_base_sha=None)
    failed = add_review(db, repository_id, number=2, job_status="failed")

    waiting = octocat.get(f"/v1/analysis-runs/{queued.id}").json()
    assert (waiting["status"], waiting["quality_state"]) == ("queued", None)
    assert (waiting["review"], waiting["coverage"], waiting["citations"]) == (None, None, [])
    assert waiting["commits"]["merge_base_sha"] is None
    assert waiting["error"] is None

    ended = octocat.get(f"/v1/analysis-runs/{failed.id}").json()
    assert (ended["status"], ended["review"]) == ("failed", None)
    assert ended["error"] == {
        "code": "provider_unavailable",
        "message": "The model provider is unavailable.",
        "retryable": True,
    }


def test_nothing_to_review_result(db: Session, octocat: TestClient, repository_id: str) -> None:
    rule_risk = {
        "id": "R1",
        "title": "Credential file changed",
        "severity": "high",
        "category": "security",
        "basis": "observed",
        "explanation": "A file that usually holds credentials changed.",
        "suggested_check": "Confirm that no secret is committed.",
        "evidence_ids": [],
        "origin": "rule",
        "path": ".env",
    }
    result = {
        **RESULT,
        "overall_risk": {"level": "high", "partial": False},
        "overview": "",
        "summary": [],
        "risks": [rule_risk],
        "coverage": {**COVERAGE, "reviewed_files": 0, "changed_lines_reviewed": 0},
    }
    run = add_review(db, repository_id, number=4, result=result, evidence=[])
    db.execute(
        update(AnalysisRun)
        .where(AnalysisRun.id == run.id)
        .values(quality_state="nothing_to_review")
    )
    db.commit()

    body = octocat.get(f"/v1/analysis-runs/{run.id}").json()

    assert body["quality_state"] == "nothing_to_review"
    assert body["review"]["summary"] == []
    (risk,) = body["review"]["risks"]
    assert (risk["origin"], risk["path"], risk["citations"]) == ("rule", ".env", [])
    assert body["citations"] == []


def test_the_review_history_lists_reviews_of_closed_and_open_pull_requests(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    # Reviews stay reachable after their pull request leaves the open list (FR-024): the run
    # list is read from the database, never from GitHub's open pull requests.
    get_fake_github().merge_pull_request(REVIEW_APP_ID, 3)
    merged = add_review(
        db, repository_id, number=3, created_at=datetime.now(UTC) - timedelta(hours=1)
    )
    running = add_review(db, repository_id, number=1, job_status="running")
    db.commit()

    reviews = octocat.get(
        "/v1/analysis-runs", params={"repository_id": repository_id, "kind": "pull_request_review"}
    ).json()["items"]
    questions = octocat.get(
        "/v1/analysis-runs", params={"repository_id": repository_id, "kind": "repository_qa"}
    ).json()["items"]

    assert [item["id"] for item in reviews] == [str(running.id), str(merged.id)]
    assert reviews[1] | {"id": None, "created_at": None} == {
        "id": None,
        "kind": "pull_request_review",
        "question": None,
        "status": "succeeded",
        "quality_state": "reviewed",
        "commit_sha": HEAD_SHA,
        "created_at": None,
        "pull_request_number": 3,
        "pull_request_title": PULL_REQUEST["title"],
        "overall_risk_level": RESULT["overall_risk"]["level"],
    }
    # A review without a result yet has no level.
    assert (reviews[0]["status"], reviews[0]["overall_risk_level"]) == ("running", None)
    assert questions == []


def test_reviews_are_denied_while_access_is_lost_and_return_with_it(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    run = add_review(db, repository_id)
    review_url = f"/v1/analysis-runs/{run.id}"
    list_params = {"repository_id": repository_id, "kind": "pull_request_review"}
    before = octocat.get(review_url).json()
    listed_before = octocat.get("/v1/analysis-runs", params=list_params).json()
    repository = db.get(Repository, uuid.UUID(repository_id))
    assert repository is not None

    assert access.mark_access_lost(db, repository, "app_uninstalled", trigger="notification")
    db.commit()
    for response in (
        octocat.get(review_url),
        octocat.get("/v1/analysis-runs", params=list_params),
        octocat.get(f"/v1/repositories/{repository_id}/pull-requests"),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "repository_access_lost"

    assert access.restore_access(db, repository, trigger="check")
    db.commit()
    assert octocat.get(review_url).json() == before
    assert octocat.get("/v1/analysis-runs", params=list_params).json() == listed_before
    assert octocat.get(f"/v1/repositories/{repository_id}/pull-requests").status_code == 200
    # Nothing was reviewed again.
    assert counts(db) == (1, len(EVIDENCE))


def test_disconnect_hides_the_review_then_purges_it(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    run = add_review(db, repository_id)

    assert octocat.delete(f"/v1/repositories/{repository_id}").status_code == 204
    assert octocat.get(f"/v1/analysis-runs/{run.id}").status_code == 404
    assert counts(db) == (1, len(EVIDENCE))

    run_maintenance()

    assert counts(db) == (0, 0)


def test_expired_review_is_deleted_with_its_evidence(
    db: Session, octocat: TestClient, repository_id: str
) -> None:
    expired = add_review(db, repository_id).id
    kept = add_review(db, repository_id, number=2).id
    db.execute(
        update(AnalysisRun)
        .where(AnalysisRun.id == expired)
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    db.commit()

    result = run_maintenance()

    assert result is not None and result.runs_expired == 1
    db.expire_all()
    assert db.get(AnalysisRun, expired) is None
    assert db.get(AnalysisRun, kept) is not None
    remaining = db.scalars(select(EvidenceItem.analysis_run_id)).all()
    assert set(remaining) == {kept}
    assert octocat.get(f"/v1/analysis-runs/{expired}").status_code == 404
    assert octocat.get(f"/v1/analysis-runs/{kept}").status_code == 200


def test_denied_read_is_audited(
    db: Session,
    signed_in: Callable[[str], TestClient],
    octocat: TestClient,
    repository_id: str,
) -> None:
    run = add_review(db, repository_id)
    hubot = signed_in("hubot")

    response = hubot.get(f"/v1/analysis-runs/{run.id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    denied = db.scalars(select(AuditEvent).where(AuditEvent.action == "access_denied")).all()
    assert [(event.resource_type, event.resource_id) for event in denied] == [
        ("analysis_run", str(run.id))
    ]
    assert all(event.outcome == "denied" for event in denied)
