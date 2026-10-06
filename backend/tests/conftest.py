"""Shared test fixtures.

Integration tests use a real PostgreSQL database named by `TEST_DATABASE_URL`
(default `codeatlas_test` on the Docker Compose server). Run parallel test sessions against
different database names. External services are always faked.
"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://codeatlas:codeatlas@localhost:5432/codeatlas_test",
)
APP_ORIGIN = "https://testserver"

# Settings are read from the environment, so set it before importing codeatlas modules.
os.environ.update(
    {
        "CODEATLAS_ENV": "test",
        "CODEATLAS_FAKE_EXTERNALS": "1",
        "DATABASE_URL": TEST_DATABASE_URL,
        "APP_ORIGIN": APP_ORIGIN,
        "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "GITHUB_APP_SLUG": "codeatlas-test",
        "DAILY_QUESTION_LIMIT": "20",
        "FAKE_ANSWER_MODEL_MODE": "ok",
        "FAKE_EMBEDDER_MODE": "ok",
    }
)

from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from alembic import command  # noqa: E402
from codeatlas.config import Settings, get_settings  # noqa: E402
from codeatlas.db import Base, new_session, reset_caches  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[1]
FIXTURE_REPOS_DIR = Path(__file__).resolve().parent / "fixtures" / "repos"


def _ensure_database() -> None:
    url = make_url(TEST_DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
        )
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def database() -> Iterator[None]:
    """Create and migrate the test database once per session."""
    get_settings.cache_clear()
    reset_caches()
    _ensure_database()
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(config, "head")
    yield
    reset_caches()


def _truncate_all() -> None:
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with new_session() as db:
        db.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        db.commit()


@pytest.fixture
def db(database: None) -> Iterator[Session]:
    """A clean database and a session for direct reads and writes in a test."""
    _truncate_all()
    session = new_session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """The live settings object; use `monkeypatch.setattr(settings, ...)` to change a value."""
    return get_settings()


@pytest.fixture(autouse=True)
def _reset_fakes() -> Iterator[None]:
    """Reset fake external services between tests."""
    yield
    from codeatlas.github.fake import reset_fake_github

    reset_fake_github()
    try:
        from codeatlas.providers.answer_model import reset_fake_answer_model
    except ImportError:
        return
    reset_fake_answer_model()


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    """An anonymous API client. Requests carry the allowed Origin header."""
    from codeatlas.api.app import app

    with TestClient(app, base_url=APP_ORIGIN, headers={"Origin": APP_ORIGIN}) as test_client:
        yield test_client


def sign_in(test_client: TestClient, login: str = "octocat") -> TestClient:
    """Sign `test_client` in as a fake GitHub user through the real OAuth routes."""
    start = test_client.get("/auth/github/login", follow_redirects=False)
    assert start.status_code == 302, start.text
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    callback = test_client.get(
        "/auth/github/callback",
        params={"code": f"fake:{login}", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 302, callback.text
    assert callback.headers["location"] == "/repositories", callback.headers["location"]
    return test_client


@pytest.fixture
def signed_in(db: Session) -> Iterator[Callable[[str], TestClient]]:
    """Factory returning a separate signed-in client per fake login (`octocat`, `hubot`)."""
    from codeatlas.api.app import app

    clients: list[TestClient] = []

    def factory(login: str = "octocat") -> TestClient:
        test_client = TestClient(app, base_url=APP_ORIGIN, headers={"Origin": APP_ORIGIN})
        test_client.__enter__()
        clients.append(test_client)
        return sign_in(test_client, login)

    yield factory
    for test_client in clients:
        test_client.__exit__(None, None, None)


@pytest.fixture
def run_worker_once(db: Session) -> Callable[[], bool]:
    """Claim and run one eligible job synchronously; returns False when nothing was claimable."""
    from codeatlas.jobs.worker import run_once

    return run_once
