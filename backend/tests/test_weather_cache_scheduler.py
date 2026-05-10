from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import weather_cache_scheduler


def test_parse_args_uses_expected_defaults() -> None:
    settings = weather_cache_scheduler.parse_args([])

    assert settings.config == Path("backend/config/station.default.yaml")
    assert settings.database_url == "sqlite:///backend/data/smartenergy.db"
    assert settings.history_start == date(2025, 10, 6)
    assert settings.days_ahead == 2
    assert settings.interval_hours == 12.0


def test_parse_args_rejects_non_positive_interval() -> None:
    with pytest.raises(SystemExit):
        weather_cache_scheduler.parse_args(["--interval-hours", "0"])


def test_run_forever_runs_maintenance_before_first_sleep() -> None:
    settings = weather_cache_scheduler.SchedulerSettings(
        config=Path("config.yaml"),
        database_url="sqlite:///cache.db",
        history_start=date(2025, 10, 6),
        days_ahead=2,
        interval_hours=12.0,
    )
    events: list[object] = []

    class StopScheduler(Exception):
        pass

    def fake_runner(
        config_path: Path,
        database_url: str | None,
        history_start: date | None,
        days_ahead: int,
    ) -> SimpleNamespace:
        events.append((config_path, database_url, history_start, days_ahead))
        return SimpleNamespace(
            historical_rows_inserted=24,
            forecast_rows_inserted=72,
            validation_result="ok",
        )

    def fake_sleep(seconds: float) -> None:
        events.append(seconds)
        raise StopScheduler

    with pytest.raises(StopScheduler):
        weather_cache_scheduler.run_forever(
            settings,
            maintenance_runner=fake_runner,
            sleep=fake_sleep,
        )

    assert events == [
        (Path("config.yaml"), "sqlite:///cache.db", date(2025, 10, 6), 2),
        43200.0,
    ]


def test_run_once_logs_failure_and_returns_false(capsys: pytest.CaptureFixture[str]) -> None:
    settings = weather_cache_scheduler.SchedulerSettings(
        config=Path("config.yaml"),
        database_url="sqlite:///cache.db",
        history_start=date(2025, 10, 6),
        days_ahead=2,
        interval_hours=12.0,
    )

    def failing_runner(
        config_path: Path,
        database_url: str | None,
        history_start: date | None,
        days_ahead: int,
    ) -> SimpleNamespace:
        raise RuntimeError("network unavailable")

    result = weather_cache_scheduler.run_once(settings, failing_runner)

    captured = capsys.readouterr()
    assert result is False
    assert "weather cache update failed: network unavailable" in captured.err
