"""Application settings loaded from environment variables and an optional `.env` file."""

import os
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
# The default embeds a password, so production refuses it (research R5).
DEVELOPMENT_DATABASE_URL = "postgresql+psycopg://codeatlas:codeatlas@localhost:5432/codeatlas"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        # Validation errors would otherwise print every input, secrets included (research R5).
        # `ValidationError.errors()` and `.json()` still contain them: never log those.
        hide_input_in_errors=True,
    )

    env: Environment = Field(default="development", validation_alias="CODEATLAS_ENV")
    fake_externals: bool = Field(default=False, validation_alias="CODEATLAS_FAKE_EXTERNALS")
    database_url: str = DEVELOPMENT_DATABASE_URL
    app_origin: str = "http://localhost:3000"

    github_app_id: str = ""
    github_app_slug: str = ""
    github_app_client_id: str = ""
    github_app_client_secret: str = ""
    # The App's private key in PEM, as the platform holds it, or a file holding it; production
    # needs one of them, and the content wins when both are set (research R5).
    github_app_private_key: str = ""
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

    # Pilot deployment (specs/004-pilot-deployment/data-model.md, "Settings").
    # The access list always applies in production; this enables it elsewhere (research R13).
    access_list_required: bool = False
    pilot_user_limit: int = 10
    # Pilot-wide daily limits across every workspace (FR-005).
    pilot_daily_question_limit: int = 30
    pilot_daily_review_limit: int = 15
    # Requests per client address per minute on `/auth/*` and `/webhooks/*` (FR-006); 0 disables.
    rate_limit_per_minute: int = 60
    # The request header that carries the client's address; empty uses the connection's address
    # (research R4).
    client_address_header: str = ""
    emit_metrics: bool = False
    metrics_environment: str = "local"
    # The commit reported by `/version`. The platform's commit replaces it (see
    # `_platform_commit_first`) because the image always defines `CODEATLAS_RELEASE`, which would
    # otherwise hide it (research R6).
    release: str = Field(default="development", validation_alias="CODEATLAS_RELEASE")
    # The platform's commit alone, empty outside a deployment from GitHub. The release gate waits
    # for it, never for the image's local default, which no deployed API reports (research R7). Two
    # fields cannot share an alias: pydantic-settings would give both the value it found first.
    platform_commit: str = Field(default="", validation_alias="RAILWAY_GIT_COMMIT_SHA")
    # The API's private address, which `ops wait-for-api` polls (research R7).
    api_origin: str = "http://localhost:8000"
    # The worker's report interval and the status conditions' thresholds (research R10).
    worker_report_seconds: int = 60
    worker_silent_seconds: int = 300
    jobs_waiting_alert_seconds: int = 1800
    jobs_failing_alert_count: int = 3
    backup_missing_hours: int = 26
    # The S3-compatible bucket of the backups (research R12).
    backup_endpoint_url: str = ""
    backup_region: str = ""
    backup_bucket: str = ""
    backup_access_key_id: str = ""
    backup_secret_access_key: str = ""
    backup_retention_days: int = 7

    @model_validator(mode="after")
    def _platform_commit_first(self) -> Self:
        if self.platform_commit.strip():
            self.release = self.platform_commit
        return self

    @model_validator(mode="after")
    def _fakes_only_outside_production(self) -> Self:
        if self.fake_externals and self.env not in ("test", "development"):
            raise ValueError("CODEATLAS_FAKE_EXTERNALS is allowed only in test or development")
        return self

    @model_validator(mode="after")
    def _complete_in_production(self) -> Self:
        """In production, refuse a missing credential or a development default (FR-009).

        One error names every such setting by its environment variable and never shows a value.
        """
        if self.env != "production":
            return self
        valid = {
            "APP_ORIGIN": self.app_origin.startswith("https://"),
            "DATABASE_URL": self.database_url != DEVELOPMENT_DATABASE_URL,
            "GITHUB_APP_ID": bool(self.github_app_id.strip()),
            "GITHUB_APP_SLUG": bool(self.github_app_slug.strip()),
            "GITHUB_APP_CLIENT_ID": bool(self.github_app_client_id.strip()),
            "GITHUB_APP_CLIENT_SECRET": bool(self.github_app_client_secret.strip()),
            "GITHUB_APP_PRIVATE_KEY": bool(self.github_app_private_key.strip())
            or _readable_file(self.github_app_private_key_path),
            "GITHUB_WEBHOOK_SECRET": bool(self.github_webhook_secret.strip()),
            "TOKEN_ENCRYPTION_KEY": bool(self.token_encryption_key.strip()),
            "GEMINI_API_KEY": bool(self.gemini_api_key.strip()),
            "VOYAGE_API_KEY": bool(self.voyage_api_key.strip()),
        }
        invalid = [name for name, ok in valid.items() if not ok]
        if invalid:
            raise ValueError(f"Missing or invalid settings for production: {', '.join(invalid)}")
        return self

    @property
    def access_list_enforced(self) -> bool:
        """Whether sign-in requires a row in `pilot_users` (research R13)."""
        return self.env == "production" or self.access_list_required


def _readable_file(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.R_OK)


@lru_cache
def get_settings() -> Settings:
    return Settings()
