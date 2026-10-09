"""Backup manifests and restore verification (specs/004-pilot-deployment, research R12).

`backup.sh` writes a counts file, `{"alembic_revision": ..., "row_counts": {table: rows}}`, in
the same repeatable-read transaction whose snapshot `pg_dump` uses, so the counts describe exactly
the dumped data. `build_manifest` adds the release and the dump's key, size, and checksum
(data-model.md, "Backup manifest"); after a restore, `compare` checks the restored database
against the manifest. Both are pure.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


class BackupError(Exception):
    """An invalid counts file or manifest, or a restored database that differs from its backup."""

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
