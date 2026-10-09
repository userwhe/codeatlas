"""Integration tests for the pilot access schema (T004, data-model.md, migration 0004).

Rows are inserted with SQL rather than through the models, so the database's own defaults and
checks are tested. A rejected row names the constraint it breaks.
"""

from datetime import date, datetime

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


def test_the_database_is_at_revision_0004(db: Session) -> None:
    assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
