"""Backup keys and the pruning choice (T030, research R12).

Every run stores its dump and manifest under `backups/<UTC date>/<UTC time>/`, prunes the dates
before the last `BACKUP_RETENTION_DAYS` UTC days, and `ops restore` picks the newest complete
backup of a day. All three choices are pure functions of keys and dates.
"""

from datetime import UTC, date, datetime, timedelta, timezone

from codeatlas.ops.backup import backup_prefix, expired_keys, newest_complete

TODAY = date(2026, 10, 20)


def test_backup_prefix_is_the_utc_date_and_time() -> None:
    now = datetime(2026, 10, 13, 3, 30, 12, 345678, tzinfo=UTC)

    assert backup_prefix(now) == "backups/2026-10-13/033012/"


def test_backup_prefix_converts_to_utc() -> None:
    eastern = timezone(timedelta(hours=-4))

    assert backup_prefix(datetime(2026, 10, 12, 23, 30, 12, tzinfo=eastern)) == (
        "backups/2026-10-13/033012/"
    )


def test_two_runs_on_one_day_get_different_prefixes() -> None:
    nightly = backup_prefix(datetime(2026, 10, 13, 3, 30, 12, tzinfo=UTC))
    by_hand = backup_prefix(datetime(2026, 10, 13, 14, 5, 0, tzinfo=UTC))

    assert nightly == "backups/2026-10-13/033012/"
    assert by_hand == "backups/2026-10-13/140500/"


def backup_keys(day: str, time: str = "033012") -> list[str]:
    return [f"backups/{day}/{time}/codeatlas.dump", f"backups/{day}/{time}/manifest.json"]


def test_expired_keys_are_those_dated_before_the_last_seven_days() -> None:
    kept_days = [(TODAY - timedelta(days=age)).isoformat() for age in range(7)]
    assert kept_days[-1] == "2026-10-14"
    kept = [key for day in kept_days for key in backup_keys(day)]
    expired = [
        *backup_keys("2026-10-13"),
        *backup_keys("2026-10-13", "140500"),
        *backup_keys("2026-09-30"),
        # A dump uploaded without its manifest is pruned with its date.
        "backups/2026-10-12/033012/codeatlas.dump",
    ]

    assert sorted(expired_keys([*kept, *expired], TODAY, 7)) == sorted(expired)


def test_keys_whose_second_segment_is_not_a_date_are_never_expired() -> None:
    malformed = [
        "backups/latest/codeatlas.dump",
        "backups/2026-13-01/033012/codeatlas.dump",
        "backups/2026-1-05/033012/codeatlas.dump",
        "backups/20261005/033012/codeatlas.dump",
        "backups/2026-W40-1/033012/codeatlas.dump",
        "backups/notes.txt",
        "backups/",
    ]

    assert expired_keys(malformed, TODAY, 7) == []


def test_a_shorter_retention_expires_more_dates() -> None:
    keys = [*backup_keys("2026-10-20"), *backup_keys("2026-10-19")]

    assert expired_keys(keys, TODAY, 1) == backup_keys("2026-10-19")


def test_newest_complete_picks_the_newest_backup_with_its_manifest() -> None:
    day = date(2026, 10, 13)
    keys = [
        *backup_keys("2026-10-13", "033012"),
        *backup_keys("2026-10-13", "120000"),
        # Newer, but its manifest was never uploaded.
        "backups/2026-10-13/180000/codeatlas.dump",
        # Complete, but on another day.
        *backup_keys("2026-10-14", "033012"),
    ]

    assert newest_complete(keys, day) == "backups/2026-10-13/120000/"


def test_newest_complete_ignores_a_backup_without_its_dump() -> None:
    keys = [
        *backup_keys("2026-10-13", "033012"),
        "backups/2026-10-13/120000/manifest.json",
    ]

    assert newest_complete(keys, date(2026, 10, 13)) == "backups/2026-10-13/033012/"


def test_newest_complete_finds_nothing_without_a_complete_backup_that_day() -> None:
    keys = [
        "backups/2026-10-13/180000/codeatlas.dump",
        *backup_keys("2026-10-12"),
        "backups/2026-10-13/latest/codeatlas.dump",
        "backups/2026-10-13/latest/manifest.json",
    ]

    assert newest_complete(keys, date(2026, 10, 13)) is None
    assert newest_complete([], date(2026, 10, 13)) is None
