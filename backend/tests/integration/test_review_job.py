"""Integration tests for the review job (T027, research R2, R3, and R7 to R9).

`octocat` connects `octo-org/review-app`, requests reviews of its fixture pull requests (see
tests/fixtures/pull-requests/README.md), and runs the worker against the fake GitHub gateway and
the fake model, in `ok` mode unless a test sets `fake_review_model_mode`.
"""

import io
import re
import tarfile
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from codeatlas.config import Settings, get_settings
from codeatlas.db import session_scope
from codeatlas.github.fake import (
    EMPTY_ID,
    NO_CODE_ID,
    OVERSIZED_ID,
    REVIEW_APP_ID,
    REVIEW_APP_PRIVATE_ID,
    SAMPLE_APP_ID,
    SAMPLE_APP_PRIVATE_ID,
    UNSAFE_PATHS_ID,
    VENDORED_HEAVY_ID,
    FakeGitHub,
    commit_sha,
    get_fake_github,
)
from codeatlas.github.gateway import GitHubNotFound
from codeatlas.models import AnalysisRun, EvidenceItem, Job, JobEvent, Repository
from codeatlas.providers.answer_model import FakeAnswerModel, ReviewResult, get_answer_model
from codeatlas.review import review
from codeatlas.review.diff import Tree, read_tree
from tests.integration.test_questions import drain
from tests.integration.test_review_submit import connect, request_review

pytestmark = pytest.mark.integration

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
MERGE_BASE = commit_sha(REVIEW_APP_ID, "initial")
STAGES = [
    "checking_access",
    "resolving_commits",
    "fetching_source",
    "comparing",
    "gathering_context",
    "generating_review",
    "validating_citations",
    "publishing",
]
# A review that needs no model call (nothing to review, or only files renamed without changes)
# goes straight from gathering context to publishing.
STAGES_WITHOUT_MODEL = [
    stage for stage in STAGES if stage not in ("generating_review", "validating_citations")
]
# Every octo-org repository octocat reaches. The App's installation on octo-org covers them all,
# so octocat loses access to the public review-app through it only when every one is revoked.
OCTO_ORG_IDS = (
    SAMPLE_APP_ID,
    SAMPLE_APP_PRIVATE_ID,
    NO_CODE_ID,
    OVERSIZED_ID,
    VENDORED_HEAVY_ID,
    UNSAFE_PATHS_ID,
    EMPTY_ID,
    REVIEW_APP_ID,
    REVIEW_APP_PRIVATE_ID,
)
# A path, a code name, or a commit SHA in a stage message.
_NOT_A_COUNT = re.compile(r"[/`]|\.py\b|can_write|\b[0-9a-f]{7,40}\b")


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


@pytest.fixture
def repository_id(octocat: TestClient, run_worker_once: Callable[[], bool]) -> str:
    return connect(octocat, run_worker_once)


def review_pull_request(
    client: TestClient, run_worker_once: Callable[[], bool], repository_id: str, number: int = 1
) -> dict[str, Any]:
    """Request a review, run the worker until nothing is left, and return the submission."""
    response = request_review(client, repository_id, number)
    assert response.status_code == 202, response.text
    drain(run_worker_once)
    submitted: dict[str, Any] = response.json()
    return submitted


def load_run(db: Session, submitted: Mapping[str, Any]) -> AnalysisRun:
    db.expire_all()
    run = db.get(AnalysisRun, uuid.UUID(submitted["run_id"]))
    assert run is not None
    return run


def load_job(db: Session, submitted: Mapping[str, Any]) -> Job:
    db.expire_all()
    job = db.get(Job, uuid.UUID(submitted["job_id"]))
    assert job is not None
    return job


def load_repository(db: Session, repository_id: str) -> Repository:
    db.expire_all()
    repository = db.get(Repository, uuid.UUID(repository_id))
    assert repository is not None
    return repository


def stage_events(db: Session, submitted: Mapping[str, Any]) -> list[JobEvent]:
    return list(
        db.scalars(
            select(JobEvent)
            .where(
                JobEvent.job_id == uuid.UUID(submitted["job_id"]),
                JobEvent.event_type == "stage_started",
            )
            .order_by(JobEvent.seq)
        )
    )


def stages(db: Session, submitted: Mapping[str, Any]) -> list[str | None]:
    return [event.stage for event in stage_events(db, submitted)]


def evidence(db: Session, submitted: Mapping[str, Any]) -> dict[str, EvidenceItem]:
    items = db.scalars(
        select(EvidenceItem).where(EvidenceItem.analysis_run_id == uuid.UUID(submitted["run_id"]))
    )
    return {item.label: item for item in items}


def fake_model() -> FakeAnswerModel:
    model = get_answer_model()
    assert isinstance(model, FakeAnswerModel)
    return model


def reviews_used(client: TestClient) -> int:
    used: int = client.get("/v1/usage").json()["reviews_used"]
    return used


def coverage_by_path(run: AnalysisRun) -> dict[str, dict[str, Any]]:
    assert run.result is not None
    return {entry["path"]: entry for entry in run.result["coverage"]["files"]}


def assert_failed(job: Job, code: str, *, retryable: bool) -> None:
    assert (job.status, job.error_code, job.error_retryable) == ("failed", code, retryable)


def assert_nothing_published(db: Session, submitted: Mapping[str, Any]) -> None:
    run = load_run(db, submitted)
    assert (run.result, run.quality_state, run.completed_at) == (None, None, None)
    assert evidence(db, submitted) == {}


# Pull request #1 ----------------------------------------------------------------------------


def test_review_of_a_seeded_defect(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], repository_id: str
) -> None:
    head = commit_sha(REVIEW_APP_ID, "pr-1")
    active_before = load_repository(db, repository_id).active_snapshot_id
    assert active_before is not None

    submitted = review_pull_request(octocat, run_worker_once, repository_id)

    assert load_job(db, submitted).status == "succeeded"
    assert stages(db, submitted) == STAGES
    for event in stage_events(db, submitted):
        assert not _NOT_A_COUNT.search(event.message), event.message
        assert all(isinstance(value, int) for value in event.data.values()), event.data
    run = load_run(db, submitted)
    assert run.merge_base_sha == MERGE_BASE
    assert run.quality_state == "reviewed"
    assert run.completed_at is not None
    assert run.usage["model_calls"] == 1
    assert run.usage["omitted_items"] == 0

    # Change items hold the exact lines of their side, at that side's commit.
    items = evidence(db, submitted)
    changes = [item for item in items.values() if item.source_type == "change"]
    assert {item.side for item in changes} == {"before", "after"}
    sides = {
        "after": (head, FIXTURES_DIR / "pull-requests/seeded-defect/files"),
        "before": (MERGE_BASE, FIXTURES_DIR / "repos/review-app"),
    }
    for item in changes:
        commit, root = sides[item.side or ""]
        lines = (root / item.path).read_text().splitlines()
        assert item.path == "app/auth/permissions.py"
        assert item.commit_sha == commit
        assert item.excerpt == "\n".join(lines[item.start_line - 1 : item.end_line])
    references = {item.path: item for item in items.values() if item.source_type == "reference"}
    assert "app/repositories.py" in references
    caller = references["app/repositories.py"]
    assert (caller.side, caller.commit_sha) == ("after", head)
    assert "can_write(user, repository_id)" in caller.excerpt

    tests = {item.path: item for item in items.values() if item.source_type == "test"}
    assert "tests/test_permissions.py" in tests
    test_item = tests["tests/test_permissions.py"]
    assert (test_item.side, test_item.commit_sha) == ("after", head)
    assert "can_write(viewer, 7)" in test_item.excerpt

    result = run.result
    assert result is not None
    assert result["overall_risk"] == {"level": "high", "partial": False}
    [risk] = [risk for risk in result["risks"] if risk["category"] == "security"]
    assert any(
        items[label].source_type == "change" and items[label].side == "before"
        for label in risk["evidence_ids"]
    )
    assert [group["area"] for group in result["summary"]] == ["app/auth"]
    assert coverage_by_path(run)["app/auth/permissions.py"]["reviewed"] is True
    assert result["coverage"]["context_items"] == len(references) + len(tests)
    assert result["omitted_items"] == 0

    # The checklist names the changed file and the risk; the tests come from the head tree.
    assert result["checklist"] == [
        {
            "text": result["checklist"][0]["text"],
            "paths": ["app/auth/permissions.py"],
            "risk_ids": [risk["id"]],
        }
    ]
    assert result["tests"]["changed"] == []
    candidates = {item["path"]: item for item in result["tests"]["candidates"]}
    assert candidates["tests/test_permissions.py"] == {
        "path": "tests/test_permissions.py",
        "reason": "refers to `can_write`",
        "evidence_ids": [test_item.label],
    }
    [new_case] = result["tests"]["new_cases"]
    assert new_case["evidence_ids"]
    assert all(items[label].source_type == "change" for label in new_case["evidence_ids"])
    # A review indexes nothing: the default version stays as it was.
    assert load_repository(db, repository_id).active_snapshot_id == active_before

    body = octocat.get(submitted["result_url"]).json()
    assert (body["status"], body["quality_state"]) == ("succeeded", "reviewed")
    assert body["commits"]["merge_base_sha"] == MERGE_BASE
    review_body = body["review"]
    assert review_body["overall_risk"]["level"] == "high"
    assert review_body["checklist"] == result["checklist"]
    assert review_body["tests"]["candidates"][0]["citations"] == [test_item.label]
    assert review_body["tests"]["new_cases"][0]["citations"] == new_case["evidence_ids"]
    cited = {*risk["evidence_ids"], *new_case["evidence_ids"], test_item.label}
    for group in result["summary"]:
        for point in group["points"]:
            cited.update(point["evidence_ids"])
    for candidate in result["tests"]["candidates"]:
        cited.update(candidate["evidence_ids"])
    assert {citation["label"] for citation in body["citations"]} == cited


# Other pull requests ------------------------------------------------------------------------


def test_pull_request_on_another_base_branch(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], repository_id: str
) -> None:
    submitted = review_pull_request(octocat, run_worker_once, repository_id, 2)

    assert load_job(db, submitted).status == "succeeded"
    run = load_run(db, submitted)
    assert run.quality_state == "reviewed"
    assert run.merge_base_sha == MERGE_BASE
    assert coverage_by_path(run)["web/src/format.ts"]["reviewed"] is True
    # The pull request changes its own test, which is listed with the change, not as a candidate.
    assert run.result is not None
    tests = run.result["tests"]
    assert tests["changed"] == [{"path": "web/src/format.test.ts", "change": "modified"}]
    assert "web/src/format.test.ts" not in {item["path"] for item in tests["candidates"]}


def test_fork_pull_request(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    repository_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = get_fake_github()
    compare = fake.compare_commits
    head_owners: list[str | None] = []

    def recording_compare(*args: Any, **kwargs: Any) -> Any:
        head_owners.append(kwargs["head_owner"])
        return compare(*args, **kwargs)

    monkeypatch.setattr(fake, "compare_commits", recording_compare)

    submitted = review_pull_request(octocat, run_worker_once, repository_id, 3)

    assert load_job(db, submitted).status == "succeeded"
    assert load_run(db, submitted).quality_state == "reviewed"
    assert head_owners == ["hubot"]


def test_renamed_file_with_one_changed_line(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], repository_id: str
) -> None:
    submitted = review_pull_request(octocat, run_worker_once, repository_id, 5)

    assert load_job(db, submitted).status == "succeeded"
    entry = coverage_by_path(load_run(db, submitted))["app/strings.py"]
    assert (entry["change"], entry["previous_path"]) == ("renamed", "app/text.py")
    assert (entry["additions"], entry["deletions"], entry["reviewed"]) == (1, 1, True)
    # The merge-base side of a renamed file is cited at its old path.
    before = [item for item in evidence(db, submitted).values() if item.side == "before"]
    assert {(item.source_type, item.path) for item in before} == {("change", "app/text.py")}


def test_large_pull_request_is_reviewed_in_part(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    repository_id: str,
    settings: Settings,
) -> None:
    submitted = review_pull_request(octocat, run_worker_once, repository_id, 6)

    assert load_job(db, submitted).status == "succeeded"
    run = load_run(db, submitted)
    assert run.result is not None
    coverage = run.result["coverage"]
    assert run.result["overall_risk"]["partial"] is True
    assert coverage["changed_files"] == 120
    assert 0 < coverage["reviewed_files"] <= settings.review_max_files
    assert coverage["changed_lines_reviewed"] <= settings.review_max_changed_lines
    left_out = [entry for entry in coverage["files"] if not entry["reviewed"]]
    assert len(left_out) == 120 - coverage["reviewed_files"]
    assert {entry["reason"] for entry in left_out} == {"review_limit"}


def test_nothing_to_review(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], repository_id: str
) -> None:
    response = request_review(octocat, repository_id, 4)
    assert response.status_code == 202, response.text
    submitted = response.json()
    assert reviews_used(octocat) == 1

    drain(run_worker_once)

    assert load_job(db, submitted).status == "succeeded"
    assert stages(db, submitted) == STAGES_WITHOUT_MODEL
    run = load_run(db, submitted)
    assert run.quality_state == "nothing_to_review"
    assert run.result is not None
    [risk] = run.result["risks"]
    assert (risk["origin"], risk["path"], risk["severity"]) == ("rule", ".env", "high")
    assert run.result["overall_risk"] == {"level": "high", "partial": False}
    assert (run.result["overview"], run.result["summary"]) == ("", [])
    assert {entry["reason"] for entry in run.result["coverage"]["files"]} == {
        "credential_file",
        "binary",
    }
    assert evidence(db, submitted) == {}
    assert fake_model().calls == 0
    assert run.usage["model_calls"] == 0
    # A review with nothing to review does not count against the allowance (FR-020).
    assert reviews_used(octocat) == 0


def _tree(files: Mapping[str, bytes], *, skip: Callable[[str, bytes], bool] | None = None) -> Tree:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=1) as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(f"octo-org-review-app-abc1234/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    stream: IO[bytes] = buffer
    return read_tree(stream, get_settings(), skip=skip)


@pytest.mark.parametrize(
    ("added", "level"),
    [({}, "none"), ({".env": b"API_TOKEN=review-fixture-not-a-secret\n"}, "high")],
    ids=["rename-only", "rename-and-credential-file"],
)
def test_files_renamed_without_changes_need_no_model_call(
    settings: Settings, added: dict[str, bytes], level: str
) -> None:
    text = (FIXTURES_DIR / "repos/review-app/app/text.py").read_bytes()
    caller = b"from app.text import slugify\n\n\ndef title(value: str) -> str:\n"
    caller += b"    return slugify(value)\n"
    head = _tree({"app/strings.py": text, "app/titles.py": caller, **added})
    base = _tree(
        {"app/text.py": text, "app/titles.py": caller},
        skip=lambda path, digest: head.hashes.get(path) == digest,
    )
    model = FakeAnswerModel(settings)
    reported: list[str] = []

    analysis = review.analyze(
        {"title": "Rename text helpers", "body": ""},
        head,
        base,
        {"app/strings.py": "app/text.py"},
        head_sha=commit_sha(REVIEW_APP_ID, "pr-5"),
        merge_base_sha=MERGE_BASE,
        settings=settings,
        model=model,
        progress=lambda stage, message, data: reported.append(stage),
    )

    # No line changed, so there is nothing to cite and nothing to ask the model.
    assert model.calls == 0
    assert reported == ["comparing", "gathering_context"]
    assert analysis.quality_state == "reviewed"
    assert analysis.evidence == ()
    assert analysis.usage.as_dict()["model_calls"] == 0
    result = analysis.result
    assert result["overall_risk"] == {"level": level, "partial": False}
    assert result["summary"] == [
        {
            "area": "app",
            "points": [
                {
                    "change": "renamed",
                    "text": "Renamed `app/text.py` to `app/strings.py` without changes",
                    "evidence_ids": [],
                    "origin": "rule",
                }
            ],
        }
    ]
    assert [(risk["origin"], risk["path"]) for risk in result["risks"]] == [
        ("rule", path) for path in added
    ]
    assert result["coverage"]["reviewed_files"] == 1
    assert result["omitted_items"] == 0


# Model outcomes -----------------------------------------------------------------------------


def test_review_without_risks(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    repository_id: str,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "fake_review_model_mode", "no_risks")

    submitted = review_pull_request(octocat, run_worker_once, repository_id)

    run = load_run(db, submitted)
    assert run.quality_state == "reviewed"
    assert run.result is not None
    assert run.result["risks"] == []
    assert run.result["overall_risk"] == {"level": "none", "partial": False}
    assert run.result["coverage"]["files"]
    assert run.result["coverage"]["reviewed_files"] == 1


def test_partly_invalid_output_drops_items_after_one_repair(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    repository_id: str,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "fake_review_model_mode", "partly_invalid")

    submitted = review_pull_request(octocat, run_worker_once, repository_id)

    assert load_job(db, submitted).status == "succeeded"
    assert fake_model().calls == 2
    assert stages(db, submitted) == STAGES
    run = load_run(db, submitted)
    assert run.result is not None
    assert run.result["omitted_items"] == 1
    assert (run.usage["model_calls"], run.usage["omitted_items"]) == (2, 1)
    [risk] = run.result["risks"]
    assert set(risk["evidence_ids"]) <= set(evidence(db, submitted))
    # The checklist item also referred to the dropped risk; only that reference was removed.
    [item] = run.result["checklist"]
    assert (item["paths"], item["risk_ids"]) == (["app/auth/permissions.py"], [risk["id"]])
    # The repair request shows the rejected review and why it was rejected.
    assert "<previous_review>" in fake_model().prompts[1]


@pytest.mark.parametrize(
    ("mode", "code", "retryable", "calls_per_attempt"),
    [
        ("invalid_citations", "review_validation_failed", True, 2),
        ("unavailable", "provider_unavailable", True, 1),
        ("refusal", "model_refused", False, 1),
    ],
)
def test_model_failures(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    repository_id: str,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    code: str,
    retryable: bool,
    calls_per_attempt: int,
) -> None:
    monkeypatch.setattr(settings, "fake_review_model_mode", mode)

    submitted = review_pull_request(octocat, run_worker_once, repository_id)

    job = load_job(db, submitted)
    assert_failed(job, code, retryable=retryable)
    # Only an unavailable provider is retried; each attempt makes its own calls.
    assert job.attempt == (job.max_attempts if mode == "unavailable" else 1)
    assert fake_model().calls == calls_per_attempt * job.attempt
    assert_nothing_published(db, submitted)
    body = octocat.get(submitted["result_url"]).json()
    assert body["error"]["code"] == code
    assert (body["review"], body["coverage"], body["citations"]) == (None, None, [])


# GitHub outcomes ----------------------------------------------------------------------------


def _drop_head_commit(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.drop_commit(commit_sha(REVIEW_APP_ID, "pr-1"))


def _head_archive_not_found(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    head = commit_sha(REVIEW_APP_ID, "pr-1")
    open_tarball = fake.open_tarball

    def missing_head(installation_id: int, full_name: str, sha: str) -> Any:
        if sha == head:
            raise GitHubNotFound(f"{full_name} has no commit {sha}")
        return open_tarball(installation_id, full_name, sha)

    monkeypatch.setattr(fake, "open_tarball", missing_head)


@pytest.mark.parametrize(
    "lose_commit",
    [_drop_head_commit, _head_archive_not_found],
    ids=["comparison", "archive"],
)
def test_commit_no_longer_served(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    repository_id: str,
    monkeypatch: pytest.MonkeyPatch,
    lose_commit: Callable[[FakeGitHub, pytest.MonkeyPatch], None],
) -> None:
    lose_commit(get_fake_github(), monkeypatch)

    submitted = review_pull_request(octocat, run_worker_once, repository_id)

    assert_failed(load_job(db, submitted), "commit_unavailable", retryable=False)
    assert_nothing_published(db, submitted)
    # After the access check passed, a missing commit says nothing about access.
    repository = load_repository(db, repository_id)
    assert (repository.access_state, repository.access_reason) == ("active", None)
    assert fake_model().calls == 0


def test_unrelated_history(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], repository_id: str
) -> None:
    get_fake_github().unrelated_history(REVIEW_APP_ID, 1)

    submitted = review_pull_request(octocat, run_worker_once, repository_id)

    assert_failed(load_job(db, submitted), "no_common_history", retryable=False)
    assert_nothing_published(db, submitted)
    assert load_run(db, submitted).merge_base_sha is None


def test_lost_access_stops_before_any_archive(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], repository_id: str
) -> None:
    response = request_review(octocat, repository_id)
    assert response.status_code == 202, response.text
    submitted = response.json()
    fake = get_fake_github()
    for github_id in OCTO_ORG_IDS:
        fake.revoke_access("octocat", github_id)
    fake.calls.clear()

    drain(run_worker_once)

    assert_failed(load_job(db, submitted), "access_denied", retryable=False)
    assert load_repository(db, repository_id).access_state == "access_lost"
    assert (fake.calls["compare_commits"], fake.calls["open_tarball"]) == (0, 0)
    assert stages(db, submitted) == ["checking_access"]
    assert_nothing_published(db, submitted)


def test_private_repository_needs_the_disclosure(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], repository_id: str
) -> None:
    fake = get_fake_github()
    fake.make_private(REVIEW_APP_ID)
    response = request_review(octocat, repository_id)
    assert response.status_code == 202, response.text
    submitted = response.json()
    fake.calls.clear()

    drain(run_worker_once)

    assert_failed(load_job(db, submitted), "external_processing_not_accepted", retryable=False)
    repository = load_repository(db, repository_id)
    assert repository.is_private is True
    assert (repository.access_state, repository.access_reason) == (
        "paused",
        "external_processing_not_accepted",
    )
    assert (fake.calls["compare_commits"], fake.calls["open_tarball"]) == (0, 0)
    assert fake_model().calls == 0
    assert_nothing_published(db, submitted)


# Disconnecting and fencing ------------------------------------------------------------------


def test_disconnect_while_the_review_runs(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    repository_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DisconnectingModel(FakeAnswerModel):
        def review(self, *, system: str, user_content: str) -> ReviewResult:
            assert octocat.delete(f"/v1/repositories/{repository_id}").status_code == 204
            return super().review(system=system, user_content=user_content)

    monkeypatch.setattr(review, "get_answer_model", lambda: DisconnectingModel(get_settings()))

    submitted = review_pull_request(octocat, run_worker_once, repository_id)

    assert load_job(db, submitted).status == "canceled"
    assert_nothing_published(db, submitted)


def test_interrupted_review_publishes_exactly_once(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    repository_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = request_review(octocat, repository_id)
    assert response.status_code == 202, response.text
    submitted = response.json()
    job_id = submitted["job_id"]

    class TakeoverModel(FakeAnswerModel):
        def review(self, *, system: str, user_content: str) -> ReviewResult:
            # Another worker takes the job over while this attempt waits for the model.
            with session_scope() as other:
                other.execute(
                    update(Job).where(Job.id == job_id).values(fencing_token=Job.fencing_token + 1)
                )
            return super().review(system=system, user_content=user_content)

    monkeypatch.setattr(review, "get_answer_model", lambda: TakeoverModel(get_settings()))
    run_worker_once()
    assert stages(db, submitted)[-1] == "generating_review"
    assert_nothing_published(db, submitted)

    monkeypatch.undo()
    with session_scope() as other:
        other.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    drain(run_worker_once)

    final = octocat.get(submitted["result_url"]).json()
    assert (final["status"], final["quality_state"]) == ("succeeded", "reviewed")
    labels = db.scalars(
        select(EvidenceItem.label).where(EvidenceItem.analysis_run_id == submitted["run_id"])
    ).all()
    assert len(labels) == len(set(labels)) > 0
    published = select(func.count()).where(
        JobEvent.job_id == job_id, JobEvent.event_type == "succeeded"
    )
    assert db.scalar(published) == 1
