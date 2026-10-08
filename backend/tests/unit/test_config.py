from datetime import timedelta

import pytest
from pydantic import ValidationError

from codeatlas.config import Settings


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


def test_webhook_secret_required_in_production() -> None:
    with pytest.raises(ValidationError, match="GITHUB_WEBHOOK_SECRET"):
        make(CODEATLAS_ENV="production", CODEATLAS_FAKE_EXTERNALS=False, GITHUB_WEBHOOK_SECRET="")


def test_webhook_secret_optional_in_development() -> None:
    settings = make(CODEATLAS_ENV="development", GITHUB_WEBHOOK_SECRET="")
    assert settings.github_webhook_secret == ""
    production = make(
        CODEATLAS_ENV="production", CODEATLAS_FAKE_EXTERNALS=False, GITHUB_WEBHOOK_SECRET="s3cret"
    )
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
    "EMIT_METRICS",
    "METRICS_ENVIRONMENT",
    "CODEATLAS_RELEASE",
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
    assert settings.emit_metrics is False
    assert settings.metrics_environment == "local"
    assert settings.release == "development"


def test_release_is_read_from_codeatlas_release() -> None:
    settings = make(CODEATLAS_RELEASE="3f9c2e1d4b5a69788c7d0e1f2a3b4c5d6e7f8091")

    assert settings.release == "3f9c2e1d4b5a69788c7d0e1f2a3b4c5d6e7f8091"
