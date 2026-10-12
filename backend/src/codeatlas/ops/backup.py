"""Backup keys, manifests, and restore verification (specs/004-pilot-deployment, research R12).

`ops backup` reads a counts document, `{"alembic_revision": ..., "row_counts": {table: rows}}`,
in the same repeatable-read transaction whose snapshot `pg_dump` uses, so the counts describe
exactly the dumped data. `build_manifest` adds the release and the dump's key, size, and checksum
(data-model.md, "Backup manifest"); after a restore, `compare` checks the restored database
against the manifest.

Each run stores its dump and manifest under `backup_prefix`, a key of its own; `expired_keys`
chooses what pruning deletes, and `newest_complete` the backup of a day that `ops restore` takes.
All are pure.
"""

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

BACKUPS_PREFIX = "backups/"
DUMP_NAME = "codeatlas.dump"
MANIFEST_NAME = "manifest.json"
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME = re.compile(r"[0-9]{6}")


class BackupError(Exception):
    """A backup or restore that cannot go on, explained in its message.

    For example an invalid counts document or manifest, a failed `pg_dump` or `pg_restore`, a
    missing backup or one that does not match its checksum, or a restored database that differs
    from its backup.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _counts(document: object, name: str) -> tuple[str, dict[str, int]]:
    """The Alembic revision and the row counts by table of a counts file or manifest."""
    if not isinstance(document, dict):
        raise BackupError(f"the {name} must be a JSON object")
    revision = document.get("alembic_revision")
    if not isinstance(revision, str) or not revision:
        raise BackupError(f"the {name} needs alembic_revision, a non-empty string")
    row_counts = document.get("row_counts")
    if not isinstance(row_counts, dict):
        raise BackupError(f"the {name} needs row_counts, an object of row counts by table")
    invalid = sorted(
        str(table)
        for table, count in row_counts.items()
        if isinstance(count, bool) or not isinstance(count, int) or count < 0
    )
    if invalid:
        raise BackupError(f"the {name} has invalid row counts for {', '.join(invalid)}")
    return revision, dict(sorted(row_counts.items()))


def build_manifest(
    counts: object,
    *,
    release: str,
    dump_key: str,
    bytes_: int,
    sha256: str,
    created_at: datetime,
) -> dict[str, Any]:
    """The manifest stored next to the dump, built from the counts file's JSON document.

    Raises `BackupError` if the document is not a valid counts file.
    """
    revision, row_counts = _counts(counts, "counts file")
    return {
        "created_at": created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "release": release,
        "alembic_revision": revision,
        "dump": {"key": dump_key, "bytes": bytes_, "sha256": sha256},
        "row_counts": row_counts,
    }


def compare(manifest: object, row_counts: Mapping[str, int], revision: str) -> list[str]:
    """Each difference between a backup's manifest and the restored database, in a readable line.

    Returns no lines when the Alembic revision and every table's row count are equal. Raises
    `BackupError` if the manifest is invalid.
    """
    expected_revision, expected = _counts(manifest, "manifest")
    differences: list[str] = []
    if revision != expected_revision:
        differences.append(
            f"Alembic revision: {expected_revision} in the backup, {revision} restored"
        )
    for table in sorted(expected.keys() | row_counts.keys()):
        if table not in row_counts:
            differences.append(
                f"{table}: row count {expected[table]} in the backup, table missing from the "
                "restored database"
            )
        elif table not in expected:
            differences.append(
                f"{table}: table not in the backup, row count {row_counts[table]} restored"
            )
        elif expected[table] != row_counts[table]:
            differences.append(
                f"{table}: row count {expected[table]} in the backup, {row_counts[table]} restored"
            )
    return differences


def backup_prefix(now: datetime) -> str:
    """`backups/<UTC date>/<UTC time as HHMMSS>/`, under which a run stores its dump and manifest.

    The time gives every run its own keys, so a second run on one day never overwrites the first.
    """
    return now.astimezone(UTC).strftime(f"{BACKUPS_PREFIX}%Y-%m-%d/%H%M%S/")


def _key_date(key: str) -> date | None:
    """The UTC date of a backup's key, or None when its second path segment is not a date."""
    parts = key.split("/")
    if len(parts) < 3 or f"{parts[0]}/" != BACKUPS_PREFIX or not _DATE.fullmatch(parts[1]):
        return None
    try:
        return date.fromisoformat(parts[1])
    except ValueError:
        return None


def expired_keys(keys: Iterable[str], today: date, retention_days: int) -> list[str]:
    """The keys dated before the last `retention_days` UTC days, today included.

    With 7 days, dates from `today - 6` on are kept. A key without a date is never expired.
    """
    oldest_kept = today - timedelta(days=retention_days - 1)
    expired = []
    for key in keys:
        day = _key_date(key)
        if day is not None and day < oldest_kept:
            expired.append(key)
    return expired


def newest_complete(keys: Iterable[str], day: date) -> str | None:
    """The prefix of the newest backup of `day` that has both its dump and its manifest.

    A dump uploaded without its manifest is incomplete, so it is never chosen.
    """
    names: dict[str, set[str]] = {}
    for key in keys:
        parts = key.split("/")
        if len(parts) == 4 and _key_date(key) == day and _TIME.fullmatch(parts[2]):
            names.setdefault(parts[2], set()).add(parts[3])
    complete = [time for time, found in names.items() if {DUMP_NAME, MANIFEST_NAME} <= found]
    if not complete:
        return None
    return f"{BACKUPS_PREFIX}{day.isoformat()}/{max(complete)}/"
