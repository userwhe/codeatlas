"""EMF metric lines on standard output (T039, research R10, contracts/operations.md "Metrics")."""

import json
import time

import pytest

from codeatlas import metrics
from codeatlas.config import Settings


@pytest.fixture
def emitting(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(settings, "emit_metrics", True)
    monkeypatch.setattr(settings, "metrics_environment", "pilot")
    return settings


def test_emit_writes_one_emf_line(emitting: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    before = int(time.time() * 1000)

    metrics.emit({"QueuedJobs": 3, "OldestRunnableJobAgeSeconds": 42.0})

    after = int(time.time() * 1000)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    line = json.loads(lines[0])
    timestamp = line["_aws"]["Timestamp"]
    assert isinstance(timestamp, int) and before <= timestamp <= after
    assert line["_aws"]["CloudWatchMetrics"] == [
        {
            "Namespace": "CodeAtlas",
            "Dimensions": [["Environment"]],
            "Metrics": [
                {"Name": "QueuedJobs", "Unit": "Count"},
                {"Name": "OldestRunnableJobAgeSeconds", "Unit": "Seconds"},
            ],
        }
    ]
    assert {key: value for key, value in line.items() if key != "_aws"} == {
        "Environment": "pilot",
        "QueuedJobs": 3,
        "OldestRunnableJobAgeSeconds": 42.0,
    }


def test_emit_takes_explicit_units(emitting: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    metrics.emit({"CertificateDaysLeft": 30}, units={"CertificateDaysLeft": "Count"})
    metrics.emit({"ResponseSeconds": 1.5}, units={"ResponseSeconds": "Milliseconds"})

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["_aws"]["CloudWatchMetrics"][0]["Metrics"] for line in lines] == [
        [{"Name": "CertificateDaysLeft", "Unit": "Count"}],
        [{"Name": "ResponseSeconds", "Unit": "Milliseconds"}],
    ]


def test_emit_writes_nothing_when_metrics_are_off(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert settings.emit_metrics is False

    metrics.emit({"QueuedJobs": 3, "OldestRunnableJobAgeSeconds": 42.0})

    assert capsys.readouterr().out == ""
