"""The alert conditions from plain values (T026, data-model.md "Alert conditions", research R10).

Each condition reports its name, whether it fails, its value, and its threshold, as
`GET /status/{condition}` returns them.
"""

from datetime import UTC, datetime, timedelta

from codeatlas import status
from codeatlas.status import Condition

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def ago(**delta: float) -> datetime:
    return NOW - timedelta(**delta)


# --- worker -------------------------------------------------------------------------------------


def test_the_worker_is_ok_until_its_newest_heartbeat_is_older_than_the_threshold() -> None:
    assert status.worker_condition(ago(seconds=300), NOW, 300) == Condition(
        "worker", failing=False, value=300.0, threshold=300
    )
    assert status.worker_condition(ago(seconds=301), NOW, 300) == Condition(
        "worker", failing=True, value=301.0, threshold=300
    )


def test_the_worker_fails_without_a_heartbeat() -> None:
    assert status.worker_condition(None, NOW, 300) == Condition(
        "worker", failing=True, value=None, threshold=300
    )


# --- jobs-waiting -------------------------------------------------------------------------------


def test_jobs_waiting_fails_once_the_oldest_runnable_job_is_due_longer_than_the_threshold() -> None:
    assert status.jobs_waiting_condition(ago(seconds=1800), NOW, 1800) == Condition(
        "jobs-waiting", failing=False, value=1800.0, threshold=1800
    )
    assert status.jobs_waiting_condition(ago(seconds=1801), NOW, 1800) == Condition(
        "jobs-waiting", failing=True, value=1801.0, threshold=1800
    )


def test_jobs_waiting_is_zero_without_a_runnable_job() -> None:
    assert status.jobs_waiting_condition(None, NOW, 1800) == Condition(
        "jobs-waiting", failing=False, value=0, threshold=1800
    )


# --- jobs-failing -------------------------------------------------------------------------------
# The reader returns each counted failure of the last two hours with the number of counted
# failures finished in the 60 minutes up to and including it.


def test_three_failures_within_60_minutes_fail_when_the_third_is_within_the_hour() -> None:
    failures = [(ago(minutes=65), 1), (ago(minutes=62), 2), (ago(minutes=59), 3)]

    assert status.jobs_failing_condition(failures, NOW, 3) == Condition(
        "jobs-failing", failing=True, value=3, threshold=3
    )


def test_three_failures_within_60_minutes_pass_once_the_third_is_over_an_hour_old() -> None:
    failures = [(ago(minutes=65), 1), (ago(minutes=62), 2), (ago(minutes=61), 3)]

    assert status.jobs_failing_condition(failures, NOW, 3) == Condition(
        "jobs-failing", failing=False, value=0, threshold=3
    )


def test_a_window_may_start_before_the_hour() -> None:
    failures = [(ago(minutes=59), 1), (ago(minutes=2), 2), (ago(minutes=1), 3)]

    assert status.jobs_failing_condition(failures, NOW, 3) == Condition(
        "jobs-failing", failing=True, value=3, threshold=3
    )


def test_two_failures_do_not_fail() -> None:
    failures = [(ago(minutes=30), 1), (ago(minutes=10), 2)]

    assert status.jobs_failing_condition(failures, NOW, 3) == Condition(
        "jobs-failing", failing=False, value=2, threshold=3
    )


def test_the_value_is_the_largest_window_ending_within_the_hour() -> None:
    # A run of three that ended 90 minutes ago, then two failures within the hour.
    failures = [(ago(minutes=100), 1), (ago(minutes=95), 2), (ago(minutes=90), 3)]
    failures += [(ago(minutes=25), 1), (ago(minutes=20), 2)]

    assert status.jobs_failing_condition(failures, NOW, 3) == Condition(
        "jobs-failing", failing=False, value=2, threshold=3
    )
    assert status.jobs_failing_condition([], NOW, 3) == Condition(
        "jobs-failing", failing=False, value=0, threshold=3
    )


# --- backup -------------------------------------------------------------------------------------


def test_the_backup_fails_once_the_newest_is_older_than_the_threshold() -> None:
    assert status.backup_condition(ago(hours=26), NOW, 26) == Condition(
        "backup", failing=False, value=26.0, threshold=26
    )
    late = status.backup_condition(ago(hours=26, seconds=1), NOW, 26)
    assert (late.name, late.failing, late.threshold) == ("backup", True, 26)
    assert late.value is not None and 26 < late.value < 26.001


def test_the_backup_fails_without_a_backup() -> None:
    assert status.backup_condition(None, NOW, 26) == Condition(
        "backup", failing=True, value=None, threshold=26
    )
