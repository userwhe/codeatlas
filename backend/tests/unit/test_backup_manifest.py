"""The backup manifest and the restore comparison (T041, data-model.md "Backup manifest")."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from codeatlas.ops.backup import BackupError, build_manifest, compare

RELEASE = "3f9c2e1d4b5a69788c7d0e1f2a3b4c5d6e7f8091"
SHA256 = "9" * 64
COUNTS: dict[str, Any] = {
    "alembic_revision": "0004",
    "row_counts": {"users": 6, "workspaces": 6, "repositories": 21, "alembic_version": 1},
}


def manifest(counts: dict[str, Any] = COUNTS) -> dict[str, Any]:
    return build_manifest(
        counts,
        release=RELEASE,
        dump_key="backups/2026-10-08/codeatlas.dump",
        bytes_=734003200,
        sha256=SHA256,
        created_at=datetime(2026, 10, 8, 3, 30, 12, 345678, tzinfo=UTC),
    )


def test_build_manifest_has_the_documented_shape() -> None:
    assert manifest() == {
        "created_at": "2026-10-08T03:30:12Z",
        "release": RELEASE,
        "alembic_revision": "0004",
        "dump": {
            "key": "backups/2026-10-08/codeatlas.dump",
            "bytes": 734003200,
            "sha256": SHA256,
        },
        "row_counts": {"alembic_version": 1, "repositories": 21, "users": 6, "workspaces": 6},
    }


def test_build_manifest_writes_created_at_in_utc() -> None:
    eastern = timezone(timedelta(hours=-4))

    built = build_manifest(
        COUNTS,
        release=RELEASE,
        dump_key="k",
        bytes_=1,
        sha256=SHA256,
        created_at=datetime(2026, 10, 7, 23, 30, 12, tzinfo=eastern),
    )

    assert built["created_at"] == "2026-10-08T03:30:12Z"


INVALID_COUNTS: dict[str, tuple[object, str]] = {
    "not an object": ([1, 2], "JSON object"),
    "missing revision": ({"row_counts": {"users": 1}}, "alembic_revision"),
    "empty revision": ({**COUNTS, "alembic_revision": ""}, "alembic_revision"),
    "missing row counts": ({"alembic_revision": "0004"}, "row_counts"),
    "row counts not an object": ({**COUNTS, "row_counts": [["users", 1]]}, "row_counts"),
    "text count": ({**COUNTS, "row_counts": {"users": "6"}}, "users"),
    "fractional count": ({**COUNTS, "row_counts": {"users": 6.5}}, "users"),
    "boolean count": ({**COUNTS, "row_counts": {"users": True}}, "users"),
    "negative count": ({**COUNTS, "row_counts": {"users": -1}}, "users"),
}


@pytest.mark.parametrize(("counts", "named"), INVALID_COUNTS.values(), ids=INVALID_COUNTS.keys())
def test_invalid_counts_are_refused_with_a_clear_error(counts: Any, named: str) -> None:
    with pytest.raises(BackupError) as raised:
        manifest(counts)

    assert named in raised.value.message


def test_compare_finds_no_difference_for_equal_input() -> None:
    assert compare(manifest(), dict(COUNTS["row_counts"]), "0004") == []


def test_compare_reports_a_count_mismatch() -> None:
    restored = {**COUNTS["row_counts"], "repositories": 20}

    assert compare(manifest(), restored, "0004") == [
        "repositories: row count 21 in the backup, 20 restored"
    ]


def test_compare_reports_a_missing_table() -> None:
    restored = {name: count for name, count in COUNTS["row_counts"].items() if name != "users"}

    assert compare(manifest(), restored, "0004") == [
        "users: row count 6 in the backup, table missing from the restored database"
    ]


def test_compare_reports_an_extra_table() -> None:
    restored = {**COUNTS["row_counts"], "snapshots": 0}

    assert compare(manifest(), restored, "0004") == [
        "snapshots: table not in the backup, row count 0 restored"
    ]


def test_compare_reports_a_revision_mismatch() -> None:
    assert compare(manifest(), dict(COUNTS["row_counts"]), "0003") == [
        "Alembic revision: 0004 in the backup, 0003 restored"
    ]


def test_compare_reports_every_difference_in_order() -> None:
    restored = {"alembic_version": 1, "repositories": 20, "snapshots": 2, "workspaces": 6}

    assert compare(manifest(), restored, "0003") == [
        "Alembic revision: 0004 in the backup, 0003 restored",
        "repositories: row count 21 in the backup, 20 restored",
        "snapshots: table not in the backup, row count 2 restored",
        "users: row count 6 in the backup, table missing from the restored database",
    ]


def test_compare_refuses_an_invalid_manifest() -> None:
    with pytest.raises(BackupError, match="row_counts"):
        compare({"alembic_revision": "0004"}, {}, "0004")
