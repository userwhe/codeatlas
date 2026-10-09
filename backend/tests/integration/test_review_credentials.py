"""Credential files in pull request reviews (T056, SC-008, FR-019, research R4).

The fake GitHub gateway injects `.env` with `API_TOKEN=review-fixture-not-a-secret` into pull
request #4 of `octo-org/review-app` (tests/fixtures/pull-requests/README.md). Its content must
never reach the model, the stored review, or anything a user sees: only its name, in a rule risk.
"""

import json
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from codeatlas.db import Base
from codeatlas.github import fake as fake_module
from codeatlas.models import EvidenceItem
from tests.integration.test_review_job import (
    coverage_by_path,
    fake_model,
    load_job,
    load_run,
    review_pull_request,
)
from tests.integration.test_review_submit import connect

pytestmark = pytest.mark.integration

CREDENTIAL_PATH = ".env"
CREDENTIAL_VALUE = "review-fixture-not-a-secret"
CREDENTIAL_CONTENT = f"API_TOKEN={CREDENTIAL_VALUE}\n"


@pytest.fixture
def octocat(signed_in: Callable[[str], TestClient]) -> TestClient:
    return signed_in("octocat")


@pytest.fixture
def repository_id(octocat: TestClient, run_worker_once: Callable[[], bool]) -> str:
    return connect(octocat, run_worker_once)


def add_credential_file(monkeypatch: pytest.MonkeyPatch, overlay: str) -> None:
    """Make the fake write the same `.env` as #4's into the pull request of `overlay` too."""
    injected = fake_module._injected_overlay_files

    def with_credential_file(name: str) -> dict[str, bytes]:
        files = injected(name)
        if name == overlay:
            files[CREDENTIAL_PATH] = CREDENTIAL_CONTENT.encode()
        return files

    monkeypatch.setattr(fake_module, "_injected_overlay_files", with_credential_file)


def stored_rows(db: Session) -> list[str]:
    """Every row of every table, as text."""
    db.expire_all()
    return [
        json.dumps(dict(row._mapping), default=str)
        for table in Base.metadata.sorted_tables
        for row in db.execute(select(table))
    ]


def assert_credential_never_shown(
    db: Session,
    client: TestClient,
    repository_id: str,
    submitted: Mapping[str, Any],
    risk: Mapping[str, Any],
) -> None:
    """The value is in no evidence item, stored result, Markdown export, or API response, while
    the rule risk names the file (FR-019).
    """
    assert (risk["origin"], risk["path"]) == ("rule", CREDENTIAL_PATH)
    assert (risk["severity"], risk["category"], risk["evidence_ids"]) == ("high", "security", [])

    items = db.scalars(
        select(EvidenceItem).where(EvidenceItem.analysis_run_id == uuid.UUID(submitted["run_id"]))
    ).all()
    assert all(item.path != CREDENTIAL_PATH for item in items)
    assert all(CREDENTIAL_VALUE not in item.excerpt for item in items)
    run = load_run(db, submitted)
    assert CREDENTIAL_VALUE not in json.dumps(run.result)
    # Hashed, never kept (research R4): no table holds the value, though the run's own data is
    # there to find.
    rows = stored_rows(db)
    assert run.pull_request is not None
    assert any(run.pull_request["title"] in row for row in rows)
    assert not [row for row in rows if CREDENTIAL_VALUE in row]

    exported = client.get(f"{submitted['result_url']}/markdown")
    assert exported.status_code == 200, exported.text
    markdown = exported.json()["markdown"]
    assert f"`{CREDENTIAL_PATH}`" in markdown
    responses = {
        "run": client.get(submitted["result_url"]),
        "markdown": exported,
        "runs": client.get(
            "/v1/analysis-runs",
            params={"repository_id": repository_id, "kind": "pull_request_review"},
        ),
        "pull requests": client.get(f"/v1/repositories/{repository_id}/pull-requests"),
        "job": client.get(f"/v1/jobs/{submitted['job_id']}"),
        "job events": client.get(submitted["events_url"]),
    }
    for name, response in responses.items():
        assert response.status_code == 200, (name, response.text)
        assert CREDENTIAL_VALUE not in response.text, name
    shown = responses["run"].json()["review"]["risks"]
    assert [entry["path"] for entry in shown if entry["origin"] == "rule"] == [CREDENTIAL_PATH]


def test_credential_file_content_is_never_sent_or_shown(
    db: Session, octocat: TestClient, run_worker_once: Callable[[], bool], repository_id: str
) -> None:
    submitted = review_pull_request(octocat, run_worker_once, repository_id, 4)

    assert load_job(db, submitted).status == "succeeded"
    # Nothing in #4 can be reviewed, so the model is not called at all.
    model = fake_model()
    assert (model.calls, model.prompts) == (0, [])
    run = load_run(db, submitted)
    assert run.quality_state == "nothing_to_review"
    assert run.result is not None
    entry = coverage_by_path(run)[CREDENTIAL_PATH]
    assert (entry["reviewed"], entry["reason"]) == (False, "credential_file")
    [risk] = run.result["risks"]
    assert_credential_never_shown(db, octocat, repository_id, submitted, risk)


def test_credential_file_next_to_source_changes_stays_out_of_the_prompt(
    db: Session,
    octocat: TestClient,
    run_worker_once: Callable[[], bool],
    repository_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #1 changes `app/auth/permissions.py`; it now adds #4's `.env` as well.
    add_credential_file(monkeypatch, "seeded-defect")

    submitted = review_pull_request(octocat, run_worker_once, repository_id, 1)

    assert load_job(db, submitted).status == "succeeded"
    run = load_run(db, submitted)
    assert run.pull_request is not None
    assert run.pull_request["changed_files"] == 2
    assert run.quality_state == "reviewed"
    assert run.result is not None
    coverage = coverage_by_path(run)
    assert coverage["app/auth/permissions.py"]["reviewed"] is True
    entry = coverage[CREDENTIAL_PATH]
    assert (entry["change"], entry["reviewed"], entry["reason"]) == (
        "added",
        False,
        "credential_file",
    )
    # The model reviewed the source change and was told only that one credential file was left
    # out, never its content.
    [prompt] = fake_model().prompts
    assert '<change path="app/auth/permissions.py"' in prompt
    assert "credential_file: 1" in prompt
    assert CREDENTIAL_VALUE not in prompt
    assert "API_TOKEN" not in prompt
    rule_risks = [risk for risk in run.result["risks"] if risk["origin"] == "rule"]
    assert len(rule_risks) == 1
    assert any(risk["origin"] == "model" for risk in run.result["risks"])
    assert_credential_never_shown(db, octocat, repository_id, submitted, rule_risks[0])
