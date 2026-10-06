from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from codeatlas.config import Settings
from codeatlas.db import session_scope
from codeatlas.github.fake import SAMPLE_APP_ID, commit_sha, get_fake_github
from codeatlas.jobs import queue
from codeatlas.models import AnalysisRun, AuditEvent, EvidenceItem, File, Job

pytestmark = pytest.mark.integration

QUESTION = "Where are repository permissions checked?"


def drain(run_worker_once: Callable[[], bool]) -> None:
    """Run jobs until none is left, fast-forwarding retry backoff."""
    for _ in range(50):
        if run_worker_once():
            continue
        with session_scope() as db:
            waiting = db.execute(
                update(Job)
                .where(Job.status == "retry_wait")
                .values(run_after=datetime.now(UTC) - timedelta(seconds=1))
            )
            if not waiting.rowcount:  # type: ignore[attr-defined]
                return
    raise AssertionError("jobs did not finish")


def indexed_repository(
    client: TestClient, run_worker_once: Callable[[], bool], github_id: int = SAMPLE_APP_ID
) -> str:
    response = client.post("/v1/repositories", json={"github_repository_id": github_id})
    assert response.status_code == 202, response.text
    drain(run_worker_once)
    repository_id: str = response.json()["repository"]["id"]
    return repository_id


def ask(client: TestClient, repository_id: str, question: str = QUESTION, **extra: Any) -> Any:
    return client.post(
        "/v1/analysis-runs",
        json={"repository_id": repository_id, "question": question},
        **extra,
    )


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


def test_answer_cites_exact_snapshot_lines(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = indexed_repository(octocat, run_worker_once)
    active = octocat.get(f"/v1/repositories/{repository_id}").json()["active_snapshot"]

    submitted = ask(octocat, repository_id)
    assert submitted.status_code == 202, submitted.text
    assert submitted.json()["snapshot_id"] == active["id"]
    drain(run_worker_once)

    run = octocat.get(submitted.json()["result_url"]).json()
    assert run["status"] == "succeeded"
    assert run["quality_state"] == "answered"
    assert run["commit_sha"] == commit_sha(SAMPLE_APP_ID, "initial")
    assert run["citations"], run
    for citation in run["citations"]:
        content = db.scalar(
            select(File.content).where(
                File.snapshot_id == active["id"], File.path == citation["path"]
            )
        )
        assert content is not None
        lines = content.split("\n")[citation["start_line"] - 1 : citation["end_line"]]
        assert citation["excerpt"] == "\n".join(lines)
        assert citation["view_url"].startswith(f"/snapshots/{active['id']}/browse?")
    labels = {c["label"] for c in run["citations"]}
    assert {label for claim in run["answer"]["claims"] for label in claim["citations"]} <= labels
    audit = db.scalars(select(AuditEvent).where(AuditEvent.action == "question_submit")).one()
    assert audit.outcome == "success"


def test_insufficient_evidence(
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_id = indexed_repository(octocat, run_worker_once)
    monkeypatch.setattr(settings, "fake_answer_model_mode", "insufficient")

    run_url = ask(octocat, repository_id, "Which payment provider does this use?").json()[
        "result_url"
    ]
    drain(run_worker_once)

    run = octocat.get(run_url).json()
    assert (run["status"], run["quality_state"]) == ("succeeded", "insufficient_evidence")
    assert run["answer"]["gaps"]


def test_provider_unavailable_is_retryable_error(
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_id = indexed_repository(octocat, run_worker_once)
    monkeypatch.setattr(settings, "fake_answer_model_mode", "unavailable")

    run_url = ask(octocat, repository_id).json()["result_url"]
    drain(run_worker_once)

    run = octocat.get(run_url).json()
    assert run["status"] == "failed"
    assert run["quality_state"] is None
    assert run["error"] == {
        "code": "provider_unavailable",
        "message": run["error"]["message"],
        "retryable": True,
    }
    assert octocat.get("/v1/repositories").status_code == 200


def test_invalid_citations_get_one_repair_then_fail(
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codeatlas.providers.answer_model import get_answer_model

    repository_id = indexed_repository(octocat, run_worker_once)
    monkeypatch.setattr(settings, "fake_answer_model_mode", "invalid_citations")

    run_url = ask(octocat, repository_id).json()["result_url"]
    drain(run_worker_once)

    run = octocat.get(run_url).json()
    assert run["error"]["code"] == "citation_validation_failed"
    assert get_answer_model().calls == 2  # type: ignore[attr-defined]


def test_refusal(
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_id = indexed_repository(octocat, run_worker_once)
    monkeypatch.setattr(settings, "fake_answer_model_mode", "refusal")

    run_url = ask(octocat, repository_id).json()["result_url"]
    drain(run_worker_once)

    assert octocat.get(run_url).json()["error"]["code"] == "model_refused"


def test_earlier_answer_stays_pinned_after_reindex(
    octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = indexed_repository(octocat, run_worker_once)
    run_url = ask(octocat, repository_id).json()["result_url"]
    drain(run_worker_once)
    before = octocat.get(run_url).json()

    get_fake_github().advance(SAMPLE_APP_ID)
    octocat.post(f"/v1/repositories/{repository_id}/index", json={})
    drain(run_worker_once)

    after = octocat.get(run_url).json()
    new_active = octocat.get(f"/v1/repositories/{repository_id}").json()["active_snapshot"]
    assert new_active["commit_sha"] == commit_sha(SAMPLE_APP_ID, "second")
    assert after["commit_sha"] == commit_sha(SAMPLE_APP_ID, "initial")
    assert after["citations"] == before["citations"]


def test_daily_limit(
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "daily_question_limit", 2)
    repository_id = indexed_repository(octocat, run_worker_once)

    statuses = [ask(octocat, repository_id).status_code for _ in range(2)]
    refused = ask(octocat, repository_id)

    assert statuses == [202, 202]
    assert refused.status_code == 429
    error = refused.json()["error"]
    assert error["code"] == "daily_limit_reached"
    assert error["details"]["resets_at"].endswith("T00:00:00Z")
    usage = octocat.get("/v1/usage").json()
    assert (usage["questions_used"], usage["questions_limit"]) == (2, 2)


def test_same_idempotency_key_creates_one_run(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = indexed_repository(octocat, run_worker_once)
    headers = {"Idempotency-Key": "ask-once"}

    responses = [ask(octocat, repository_id, headers=headers) for _ in range(100)]

    assert {response.status_code for response in responses} == {202}
    assert len({response.json()["run_id"] for response in responses}) == 1
    assert db.scalar(select(func.count()).select_from(AnalysisRun)) == 1
    answer_jobs = db.scalar(
        select(func.count()).select_from(Job).where(Job.kind == "answer_question")
    )
    assert answer_jobs == 1
    assert octocat.get("/v1/usage").json()["questions_used"] == 1


def test_second_question_waits_while_one_runs(
    octocat: TestClient, run_worker_once: Callable[[], bool]
) -> None:
    repository_id = indexed_repository(octocat, run_worker_once)
    ask(octocat, repository_id)
    second = ask(octocat, repository_id, "What does the README say about setup?").json()
    with session_scope() as db:
        assert queue.claim_next(db) is not None

    job = octocat.get(f"/v1/jobs/{second['job_id']}").json()

    assert (job["status"], job["queued_behind"]) == ("queued", 1)


@pytest.mark.parametrize("question", ["", "   ", "x" * 2001])
def test_invalid_question(
    octocat: TestClient, run_worker_once: Callable[[], bool], question: str
) -> None:
    repository_id = indexed_repository(octocat, run_worker_once)

    response = ask(octocat, repository_id, question)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_question"


def test_no_ready_snapshot(octocat: TestClient) -> None:
    connected = octocat.post("/v1/repositories", json={"github_repository_id": SAMPLE_APP_ID})

    response = ask(octocat, connected.json()["repository"]["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "snapshot_not_ready"


def test_other_workspace_cannot_read_a_run(
    db: Session,
    signed_in: Callable[[str], TestClient],
    run_worker_once: Callable[[], bool],
) -> None:
    owner = signed_in("octocat")
    other = signed_in("hubot")
    repository_id = indexed_repository(owner, run_worker_once)
    run_url = ask(owner, repository_id).json()["result_url"]

    assert other.get(run_url).status_code == 404
    assert (
        other.get("/v1/analysis-runs", params={"repository_id": repository_id}).status_code == 404
    )
    denied = db.scalars(select(AuditEvent).where(AuditEvent.action == "access_denied")).all()
    assert any(event.resource_type == "analysis_run" for event in denied)


def test_interrupted_answer_publishes_exactly_once(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codeatlas.config import get_settings
    from codeatlas.providers.answer_model import AnswerResult, FakeAnswerModel
    from codeatlas.qa import answer

    repository_id = indexed_repository(octocat, run_worker_once)
    submitted = ask(octocat, repository_id).json()
    job_id = submitted["job_id"]

    class TakeoverModel(FakeAnswerModel):
        def answer(self, *, system: str, user_content: str) -> AnswerResult:
            # Another worker takes the job over while this attempt waits for the model.
            with session_scope() as other:
                other.execute(
                    update(Job).where(Job.id == job_id).values(fencing_token=Job.fencing_token + 1)
                )
            return super().answer(system=system, user_content=user_content)

    monkeypatch.setattr(answer, "get_answer_model", lambda: TakeoverModel(get_settings()))
    run_worker_once()
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(EvidenceItem)) == 0
    run = db.get(AnalysisRun, submitted["run_id"])
    assert run is not None and run.result is None

    monkeypatch.undo()
    with session_scope() as other:
        other.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    drain(run_worker_once)

    final = octocat.get(submitted["result_url"]).json()
    assert final["status"] == "succeeded"
    labels = db.scalars(
        select(EvidenceItem.label).where(EvidenceItem.analysis_run_id == submitted["run_id"])
    ).all()
    assert len(labels) == len(set(labels)) > 0
