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
