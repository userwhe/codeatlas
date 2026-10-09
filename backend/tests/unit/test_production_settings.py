"""Production settings validation (T012, FR-009, research R5).

In production, `Settings` refuses a configuration that misses a credential or keeps a development
default. One error names every such setting by its environment variable and shows no value.
"""

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from codeatlas.config import Settings

# The secrets get distinctive values, so a test can tell whether any of them leaks into an error.
SECRETS = {
    "GITHUB_APP_ID": "secret-app-id-51c2",
    "GITHUB_APP_SLUG": "secret-app-slug-0b8e",
    "GITHUB_APP_CLIENT_ID": "secret-client-id-c47d",
    "GITHUB_APP_CLIENT_SECRET": "secret-client-secret-93aa",
    "GITHUB_WEBHOOK_SECRET": "secret-webhook-6e15",
    "TOKEN_ENCRYPTION_KEY": "secret-token-key-2f90",
    "GEMINI_API_KEY": "secret-gemini-7f3a",
    "VOYAGE_API_KEY": "secret-voyage-d81c",
}
DATABASE_URL = "postgresql+psycopg://codeatlas:secret-database-4b6f@db:5432/codeatlas"
REQUIRED = (*SECRETS, "GITHUB_APP_PRIVATE_KEY_PATH")


def production_values(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """A complete production configuration; `overrides` replaces or empties single settings."""
    key = tmp_path / "github-app.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\nnot a real key\n-----END PRIVATE KEY-----\n")
    return {
        "CODEATLAS_ENV": "production",
        "CODEATLAS_FAKE_EXTERNALS": False,
        "APP_ORIGIN": "https://codeatlas.example.com",
        "DATABASE_URL": DATABASE_URL,
        "GITHUB_APP_PRIVATE_KEY_PATH": str(key),
        **SECRETS,
        **overrides,
    }


def make(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def named(error: ValidationError) -> set[str]:
    """The settings an error names after "Missing or invalid settings for production:"."""
    match = re.search(r"Missing or invalid settings for production: ([A-Z_, ]+)", str(error))
    assert match is not None, str(error)
    return {name.strip() for name in match.group(1).split(",")}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read only what each test passes: tests/conftest.py sets test values in the environment."""
    for name, field in Settings.model_fields.items():
        monkeypatch.delenv(name.upper(), raising=False)
        if isinstance(field.validation_alias, str):
            monkeypatch.delenv(field.validation_alias, raising=False)


def test_a_complete_production_configuration_validates(tmp_path: Path) -> None:
    settings = make(**production_values(tmp_path))

    assert settings.env == "production"
    assert settings.gemini_api_key == SECRETS["GEMINI_API_KEY"]


@pytest.mark.parametrize("name", REQUIRED)
def test_each_missing_setting_is_named(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValidationError) as refused:
        make(**production_values(tmp_path, **{name: ""}))

    assert refused.value.error_count() == 1
    assert named(refused.value) == {name}


def test_every_missing_setting_is_named_in_one_error(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as refused:
        make(
            **production_values(
                tmp_path, GITHUB_WEBHOOK_SECRET="", GEMINI_API_KEY="", VOYAGE_API_KEY=""
            )
        )

    assert refused.value.error_count() == 1
    assert named(refused.value) == {"GITHUB_WEBHOOK_SECRET", "GEMINI_API_KEY", "VOYAGE_API_KEY"}


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITHUB_APP_PRIVATE_KEY_PATH", "/run/secrets/missing-key-e21d.pem"),
        ("APP_ORIGIN", "http://codeatlas.example.com"),
    ],
)
def test_an_invalid_setting_is_named(tmp_path: Path, name: str, value: str) -> None:
    with pytest.raises(ValidationError) as refused:
        make(**production_values(tmp_path, **{name: value}))

    assert named(refused.value) == {name}


def test_the_default_database_url_is_named(tmp_path: Path) -> None:
    values = production_values(tmp_path)
    del values["DATABASE_URL"]

    with pytest.raises(ValidationError) as refused:
        make(**values)

    assert named(refused.value) == {"DATABASE_URL"}


def test_no_value_appears_in_the_error(tmp_path: Path) -> None:
    # Before research R5, pydantic printed every input of the failed validation, secrets included.
    origin = "http://origin-5be0.example.com"
    key_path = "/run/secrets/missing-key-e21d.pem"

    with pytest.raises(ValidationError) as refused:
        make(
            **production_values(
                tmp_path,
                GITHUB_WEBHOOK_SECRET="",
                APP_ORIGIN=origin,
                GITHUB_APP_PRIVATE_KEY_PATH=key_path,
            )
        )

    message = str(refused.value)
    assert named(refused.value) == {
        "GITHUB_WEBHOOK_SECRET",
        "APP_ORIGIN",
        "GITHUB_APP_PRIVATE_KEY_PATH",
    }
    # Pydantic shortens a long input in the middle, so also check that it prints none at all.
    assert "input_value" not in message
    for value in [*SECRETS.values(), DATABASE_URL, "secret-database-4b6f", origin, key_path]:
        assert value not in message


def test_fake_externals_are_still_refused_in_production(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="only in test or development"):
        make(**production_values(tmp_path, CODEATLAS_FAKE_EXTERNALS=True))


def test_development_needs_nothing() -> None:
    settings = make()

    assert settings.env == "development"
    assert settings.gemini_api_key == ""


def test_the_access_list_is_enforced_in_production(tmp_path: Path) -> None:
    assert make(**production_values(tmp_path)).access_list_enforced is True
    assert make().access_list_enforced is False
    assert make(ACCESS_LIST_REQUIRED="1").access_list_enforced is True
