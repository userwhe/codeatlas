"""Integration tests for the pilot schema (T004, T025, data-model.md, migrations 0004 and 0005).

Rows are inserted with SQL rather than through the models, so the database's own defaults and
checks are tested. A rejected row names the constraint it breaks.
"""

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from codeatlas.models import AuditEvent

pytestmark = pytest.mark.integration

PILOT_USER_ACTIONS = ("pilot_user_add", "pilot_user_remove", "pilot_user_delete_data")


def columns(db: Session, table: str) -> dict[str, tuple[str, str, str | None]]:
    """Each column's type, nullability, and default, as PostgreSQL reports them."""
    rows = db.execute(
        text(
            "SELECT column_name, data_type, is_nullable, column_default"
            " FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = :table"
        ),
        {"table": table},
    )
    return {name: (data_type, nullable, default) for name, data_type, nullable, default in rows}


def primary_key(db: Session, table: str) -> list[str]:
    rows = db.execute(
        text(
            "SELECT a.attname FROM pg_index i"
            " JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)"
            " WHERE i.indrelid = CAST(:table AS regclass) AND i.indisprimary"
        ),
        {"table": table},
    )
    return list(rows.scalars())


def add_pilot_user(db: Session, github_user_id: int, note: str | None = None) -> None:
    db.execute(
        text(
            "INSERT INTO pilot_users (github_user_id, github_login, note)"
            " VALUES (:github_user_id, 'octocat', :note)"
        ),
        {"github_user_id": github_user_id, "note": note},
    )
    db.commit()


def add_backup(db: Session, object_key: str, sha256: str = "0" * 64) -> None:
    db.execute(
        text(
            "INSERT INTO backups"
            " (id, object_key, completed_at, bytes, sha256, alembic_revision, release)"
            " VALUES (:id, :object_key, :completed_at, :bytes, :sha256, '0005', 'development')"
        ),
        {
            "id": uuid.uuid4(),
            "object_key": object_key,
            "completed_at": datetime(2026, 10, 13, 3, 30, 12, tzinfo=UTC),
            # Dumps can exceed 32 bits.
            "bytes": 5 * 2**30,
            "sha256": sha256,
        },
    )
    db.commit()


def rejected_by(constraint: str) -> pytest.RaisesExc[IntegrityError]:
    return pytest.raises(IntegrityError, match=f'"{constraint}"')


# --- pilot_users --------------------------------------------------------------------------------


def test_pilot_users_columns(db: Session) -> None:
    assert columns(db, "pilot_users") == {
        "github_user_id": ("bigint", "NO", None),
        "github_login": ("text", "NO", None),
        "note": ("text", "YES", None),
        "added_at": ("timestamp with time zone", "NO", "now()"),
    }
    assert primary_key(db, "pilot_users") == ["github_user_id"]


def test_a_pilot_user_gets_the_time_it_was_added(db: Session) -> None:
    # GitHub user IDs exceed 32 bits.
    add_pilot_user(db, 2**40)

    row = db.execute(text("SELECT github_user_id, github_login, note, added_at FROM pilot_users"))
    github_user_id, github_login, note, added_at = row.one()
    assert (github_user_id, github_login, note) == (2**40, "octocat", None)
    assert isinstance(added_at, datetime)


def test_the_github_user_id_is_not_generated(db: Session) -> None:
    with pytest.raises(IntegrityError, match="github_user_id"):
        db.execute(text("INSERT INTO pilot_users (github_login) VALUES ('octocat')"))


def test_a_github_user_is_listed_once(db: Session) -> None:
    add_pilot_user(db, 583231)

    with rejected_by("pk_pilot_users"):
        add_pilot_user(db, 583231)


def test_a_note_has_at_most_200_characters(db: Session) -> None:
    add_pilot_user(db, 1, note="n" * 200)

    with rejected_by("ck_pilot_users_note"):
        add_pilot_user(db, 2, note="n" * 201)


# --- pilot_usage_counters -----------------------------------------------------------------------


def test_pilot_usage_counters_columns(db: Session) -> None:
    assert columns(db, "pilot_usage_counters") == {
        "usage_date": ("date", "NO", None),
        "questions_count": ("integer", "NO", "0"),
        "reviews_count": ("integer", "NO", "0"),
    }
    assert primary_key(db, "pilot_usage_counters") == ["usage_date"]


def test_pilot_usage_counts_start_at_zero(db: Session) -> None:
    db.execute(
        text("INSERT INTO pilot_usage_counters (usage_date) VALUES (:usage_date)"),
        {"usage_date": date(2026, 10, 8)},
    )
    db.commit()

    row = db.execute(text("SELECT questions_count, reviews_count FROM pilot_usage_counters"))
    assert tuple(row.one()) == (0, 0)


def test_one_pilot_usage_row_per_day(db: Session) -> None:
    insert = text("INSERT INTO pilot_usage_counters (usage_date) VALUES (:usage_date)")
    db.execute(insert, {"usage_date": date(2026, 10, 8)})
    db.commit()

    with rejected_by("pk_pilot_usage_counters"):
        db.execute(insert, {"usage_date": date(2026, 10, 8)})


# --- worker_heartbeats --------------------------------------------------------------------------


def test_worker_heartbeats_columns(db: Session) -> None:
    assert columns(db, "worker_heartbeats") == {
        "worker_id": ("text", "NO", None),
        "started_at": ("timestamp with time zone", "NO", None),
        "beat_at": ("timestamp with time zone", "NO", None),
    }
    assert primary_key(db, "worker_heartbeats") == ["worker_id"]


# --- backups ------------------------------------------------------------------------------------


def test_backups_columns(db: Session) -> None:
    assert columns(db, "backups") == {
        "id": ("uuid", "NO", None),
        "object_key": ("text", "NO", None),
        "completed_at": ("timestamp with time zone", "NO", None),
        "bytes": ("bigint", "NO", None),
        "sha256": ("text", "NO", None),
        "alembic_revision": ("text", "NO", None),
        "release": ("text", "NO", None),
    }
    assert primary_key(db, "backups") == ["id"]


def test_a_dump_is_recorded_once(db: Session) -> None:
    key = "backups/2026-10-13/033012/codeatlas.dump"
    add_backup(db, key)

    with rejected_by("uq_backups_object_key"):
        add_backup(db, key)


def test_a_backups_sha256_is_64_lowercase_hexadecimal_characters(db: Session) -> None:
    add_backup(db, "backups/2026-10-13/033012/codeatlas.dump", sha256="0123456789abcdef" * 4)

    with rejected_by("ck_backups_sha256"):
        add_backup(db, "backups/2026-10-14/033012/codeatlas.dump", sha256="ABC")
    db.rollback()
    with rejected_by("ck_backups_sha256"):
        add_backup(db, "backups/2026-10-15/033012/codeatlas.dump", sha256="a" * 63)


# --- jobs ---------------------------------------------------------------------------------------


def test_a_jobs_failure_permanence_is_nullable(db: Session) -> None:
    # Null for jobs that have not failed and for failures recorded before the migration.
    assert columns(db, "jobs")["error_permanent"] == ("boolean", "YES", None)


# --- audit_events and the migration -------------------------------------------------------------


def test_audit_events_accept_the_pilot_user_actions(db: Session) -> None:
    # The developer acts through the host, so these events have no actor (data-model.md).
    db.add_all(
        AuditEvent(
            action=action,
            outcome="success",
            detail={"github_user_id": 583231, "github_login": "octocat"},
        )
        for action in PILOT_USER_ACTIONS
    )
    db.commit()

    assert set(db.scalars(select(AuditEvent.action))) == set(PILOT_USER_ACTIONS)


def test_audit_events_still_reject_an_unknown_action(db: Session) -> None:
    db.add(AuditEvent(action="pilot_user_promote", outcome="success"))

    with rejected_by("ck_audit_events_action"):
        db.commit()


def test_the_database_is_at_revision_0005(db: Session) -> None:
    assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
