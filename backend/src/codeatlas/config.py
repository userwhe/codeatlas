"""Application settings loaded from environment variables and an optional `.env` file."""

from datetime import timedelta
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIB = 1024 * 1024
GIB = 1024 * MIB

Environment = Literal["development", "test", "production"]
# Gemini 3.8 Flash supports these levels; "minimal" is rejected by the model (research R11).
ThinkingLevel = Literal["low", "medium", "high"]
FakeAnswerModelMode = Literal["ok", "unavailable", "insufficient", "invalid_citations", "refusal"]
FakeEmbedderMode = Literal["ok", "unavailable"]
FakeReviewModelMode = Literal[
    "ok", "no_risks", "partly_invalid", "unavailable", "invalid_citations", "refusal"
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    env: Environment = Field(default="development", validation_alias="CODEATLAS_ENV")
    fake_externals: bool = Field(default=False, validation_alias="CODEATLAS_FAKE_EXTERNALS")
    database_url: str = "postgresql+psycopg://codeatlas:codeatlas@localhost:5432/codeatlas"
    app_origin: str = "http://localhost:3000"

    github_app_id: str = ""
    github_app_slug: str = ""
    github_app_client_id: str = ""
    github_app_client_secret: str = ""
    github_app_private_key_path: str = ""
    # Verifies GitHub webhook deliveries (research R2); required in production.
    github_webhook_secret: str = ""
    token_encryption_key: str = ""

    gemini_api_key: str = ""
    answer_model: str = "gemini-3.8-flash"
    answer_thinking_level: ThinkingLevel = "medium"
    answer_max_output_tokens: int = 16000

    voyage_api_key: str = ""
    embedding_model: str = "voyage-4"
    embedding_dimensions: int = 1024

    daily_question_limit: int = 20
    fake_answer_model_mode: FakeAnswerModelMode = "ok"
    fake_embedder_mode: FakeEmbedderMode = "ok"
    daily_review_limit: int = 10
    fake_review_model_mode: FakeReviewModelMode = "ok"

    # Spec limits (FR-009), counted over eligible files after filtering (research R7).
    max_repositories_per_workspace: int = 10
    max_files_per_snapshot: int = 5000
    max_source_lines_per_snapshot: int = 100_000
    max_expanded_bytes: int = 100 * MIB
    max_file_bytes: int = 1 * MIB
    # Extraction safety caps against archive bombs (research R7); not user-facing.
    max_archive_members: int = 100_000
    max_archive_bytes: int = 1 * GIB
    # Pull request review limits (specs/003-pr-review FR-018, research R4 and R6).
    review_max_files: int = 100
    review_max_changed_lines: int = 2000
    review_max_hunks: int = 80
    review_max_diff_tokens: int = 40_000
    review_max_input_tokens: int = 48_000

    indexing_deadline: timedelta = timedelta(minutes=15)
    question_deadline: timedelta = timedelta(minutes=3)
    review_deadline: timedelta = timedelta(minutes=5)
    session_ttl: timedelta = timedelta(days=7)

    # Pilot deployment (specs/004-pilot-deployment/data-model.md, "Settings added").
    # The access list always applies in production; this enables it elsewhere (research R13).
    access_list_required: bool = False
    pilot_user_limit: int = 10
    # Pilot-wide daily limits across every workspace (FR-005).
    pilot_daily_question_limit: int = 30
    pilot_daily_review_limit: int = 15
    # Requests per client address per minute on `/auth/*` and `/webhooks/*` (FR-006); 0 disables.
    rate_limit_per_minute: int = 60
    emit_metrics: bool = False
    metrics_environment: str = "local"
    # The commit SHA baked into the image at build time (research R6).
    release: str = Field(default="development", validation_alias="CODEATLAS_RELEASE")

    @model_validator(mode="after")
    def _fakes_only_outside_production(self) -> Self:
        if self.fake_externals and self.env not in ("test", "development"):
            raise ValueError("CODEATLAS_FAKE_EXTERNALS is allowed only in test or development")
        return self

    @model_validator(mode="after")
    def _webhook_secret_in_production(self) -> Self:
        if self.env == "production" and not self.github_webhook_secret:
            raise ValueError("GITHUB_WEBHOOK_SECRET is required in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
