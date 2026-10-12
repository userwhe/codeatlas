from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from codeatlas.config import Settings
from tests.unit.test_production_settings import production_values


def make(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_fake_externals_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="only in test or development"):
        make(CODEATLAS_ENV="production", CODEATLAS_FAKE_EXTERNALS=True)


@pytest.mark.parametrize("env", ["test", "development"])
def test_fake_externals_allowed_in_test_and_development(env: str) -> None:
    settings = make(CODEATLAS_ENV=env, CODEATLAS_FAKE_EXTERNALS=True)
    assert settings.fake_externals is True


def test_minimal_thinking_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make(answer_thinking_level="minimal")


def test_spec_limit_defaults() -> None:
    settings = make()
    assert settings.max_repositories_per_workspace == 10
    assert settings.max_files_per_snapshot == 5000
    assert settings.max_source_lines_per_snapshot == 100_000
    assert settings.max_expanded_bytes == 100 * 1024 * 1024
    assert settings.max_file_bytes == 1024 * 1024
    assert settings.daily_question_limit == 20
    assert settings.answer_model == "gemini-3.8-flash"


def test_webhook_secret_required_in_production(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="GITHUB_WEBHOOK_SECRET"):
        make(**production_values(tmp_path, GITHUB_WEBHOOK_SECRET=""))


def test_webhook_secret_optional_in_development(tmp_path: Path) -> None:
    settings = make(CODEATLAS_ENV="development", GITHUB_WEBHOOK_SECRET="")
    assert settings.github_webhook_secret == ""
    production = make(**production_values(tmp_path, GITHUB_WEBHOOK_SECRET="s3cret"))
    assert production.github_webhook_secret == "s3cret"


def test_review_defaults() -> None:
    settings = make()
    assert settings.daily_review_limit == 10
    assert settings.review_deadline == timedelta(minutes=5)
    assert settings.review_max_files == 100
    assert settings.review_max_changed_lines == 2000
    assert settings.review_max_hunks == 80
    assert settings.review_max_diff_tokens == 40_000
    assert settings.review_max_input_tokens == 48_000
    assert settings.fake_review_model_mode == "ok"


def test_unknown_fake_review_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make(fake_review_model_mode="sometimes")


PILOT_ENVIRONMENT = (
    "ACCESS_LIST_REQUIRED",
    "PILOT_USER_LIMIT",
    "PILOT_DAILY_QUESTION_LIMIT",
    "PILOT_DAILY_REVIEW_LIMIT",
    "RATE_LIMIT_PER_MINUTE",
    "CODEATLAS_RELEASE",
    "RAILWAY_GIT_COMMIT_SHA",
)


def test_pilot_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # tests/conftest.py relaxes some of these limits in the environment.
    for name in PILOT_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)

    settings = make()

    assert settings.access_list_required is False
    assert settings.pilot_user_limit == 10
    assert settings.pilot_daily_question_limit == 30
    assert settings.pilot_daily_review_limit == 15
    assert settings.rate_limit_per_minute == 60
    assert settings.release == "development"


def test_an_emit_metrics_variable_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    # The status endpoints replaced the embedded metric format and its settings (research R10).
    monkeypatch.setenv("EMIT_METRICS", "1")
    monkeypatch.setenv("METRICS_ENVIRONMENT", "pilot")

    settings = make()

    assert {"emit_metrics", "metrics_environment"}.isdisjoint(Settings.model_fields)
    assert not hasattr(settings, "emit_metrics")


PLATFORM_ENVIRONMENT = (
    "GITHUB_APP_PRIVATE_KEY",
    "CLIENT_ADDRESS_HEADER",
    "API_ORIGIN",
    "WORKER_REPORT_SECONDS",
    "WORKER_SILENT_SECONDS",
    "JOBS_WAITING_ALERT_SECONDS",
    "JOBS_FAILING_ALERT_COUNT",
    "BACKUP_MISSING_HOURS",
    "BACKUP_ENDPOINT_URL",
    "BACKUP_REGION",
    "BACKUP_BUCKET",
    "BACKUP_ACCESS_KEY_ID",
    "BACKUP_SECRET_ACCESS_KEY",
    "BACKUP_RETENTION_DAYS",
)


def test_platform_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PLATFORM_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)

    settings = make()

    assert settings.github_app_private_key == ""
    assert settings.client_address_header == ""
    assert settings.api_origin == "http://localhost:8000"
    assert settings.worker_report_seconds == 60
    assert settings.worker_silent_seconds == 300
    assert settings.jobs_waiting_alert_seconds == 1800
    assert settings.jobs_failing_alert_count == 3
    assert settings.backup_missing_hours == 26
    assert settings.backup_retention_days == 7
    assert settings.backup_endpoint_url == ""
    assert settings.backup_region == ""
    assert settings.backup_bucket == ""
    assert settings.backup_access_key_id == ""
    assert settings.backup_secret_access_key == ""


PLATFORM_COMMIT = "3f9c2e1d4b5a69788c7d0e1f2a3b4c5d6e7f8091"
IMAGE_RELEASE = "image-release-5d2a"


@pytest.mark.parametrize(
    ("environment", "release"),
    [
        ({"RAILWAY_GIT_COMMIT_SHA": PLATFORM_COMMIT}, PLATFORM_COMMIT),
        ({"CODEATLAS_RELEASE": IMAGE_RELEASE}, IMAGE_RELEASE),
        ({}, "development"),
        # The image always defines CODEATLAS_RELEASE, so the platform's commit wins (research R6).
        (
            {"RAILWAY_GIT_COMMIT_SHA": PLATFORM_COMMIT, "CODEATLAS_RELEASE": IMAGE_RELEASE},
            PLATFORM_COMMIT,
        ),
    ],
)
def test_release_prefers_the_platforms_commit(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str], release: str
) -> None:
    for name in ("RAILWAY_GIT_COMMIT_SHA", "CODEATLAS_RELEASE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert make().release == release


def test_platform_commit_reads_only_the_platforms_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.setenv("CODEATLAS_RELEASE", IMAGE_RELEASE)
    assert make().platform_commit == ""

    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", PLATFORM_COMMIT)
    assert make().platform_commit == PLATFORM_COMMIT
