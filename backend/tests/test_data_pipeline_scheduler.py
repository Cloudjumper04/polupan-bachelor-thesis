from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import run_data_pipeline_scheduler


def test_scheduler_run_once_dry_run_calls_pipeline_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    lock_path = tmp_path / "pipeline.lock"
    db_path = tmp_path / "scheduler.db"
    settings = run_data_pipeline_scheduler.parse_args(
        [
            "--run-once",
            "--dry-run",
            "--db-path",
            str(db_path),
            "--lock-path",
            str(lock_path),
        ]
    )
    monkeypatch.setattr(
        run_data_pipeline_scheduler.update_data_pipeline,
        "print_data_pipeline_summary",
        lambda summary: None,
    )

    def fake_pipeline(**kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    exit_code = run_data_pipeline_scheduler.run_scheduler(
        settings,
        pipeline_runner=fake_pipeline,
        stop_event=threading.Event(),
    )

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0]["dry_run"] is True
    assert calls[0]["allow_fallbacks"] is False
    assert calls[0]["database_url"] == f"sqlite:///{db_path}"
    assert not lock_path.exists()


def test_scheduler_skips_cycle_when_lock_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    lock_path = tmp_path / "pipeline.lock"
    lock_path.write_text("pid=123\ncreated_utc=2026-01-01T00:00:00+00:00\n")
    settings = run_data_pipeline_scheduler.parse_args(
        ["--run-once", "--dry-run", "--lock-path", str(lock_path)]
    )
    monkeypatch.setattr(
        run_data_pipeline_scheduler.update_data_pipeline,
        "print_data_pipeline_summary",
        lambda summary: None,
    )

    def fake_pipeline(**kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return object()

    cycle_ok = run_data_pipeline_scheduler.run_pipeline_cycle(
        settings,
        pipeline_runner=fake_pipeline,
    )

    assert cycle_ok is False
    assert calls == 0
    assert lock_path.exists()


def test_scheduler_cli_defaults_keep_fallbacks_disabled() -> None:
    settings = run_data_pipeline_scheduler.parse_args([])

    assert settings.interval_minutes == 360.0
    assert settings.allow_fallbacks is False
    assert settings.full_history is False
    assert settings.dry_run is False
    assert settings.run_once is False
    assert settings.database_url == "sqlite:///backend/data/smartenergy.db"
    assert settings.lock_path == Path("backend/data/pipeline.lock")


def test_scheduler_cli_accepts_explicit_fallbacks_and_interval(tmp_path: Path) -> None:
    db_path = tmp_path / "target.db"
    lock_path = tmp_path / "target.lock"

    settings = run_data_pipeline_scheduler.parse_args(
        [
            "--allow-fallbacks",
            "--interval-minutes",
            "15",
            "--db-path",
            str(db_path),
            "--lock-path",
            str(lock_path),
        ]
    )

    assert settings.allow_fallbacks is True
    assert settings.interval_minutes == 15.0
    assert settings.database_url == f"sqlite:///{db_path}"
    assert settings.lock_path == lock_path


def test_compose_uses_data_scheduler_not_weather_scheduler() -> None:
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    text = compose_path.read_text(encoding="utf-8")

    assert "  data-scheduler:" in text
    assert "container_name: smartenergy-data-scheduler" in text
    assert "backend/scripts/run_data_pipeline_scheduler.py" in text
    assert "  weather-scheduler:" not in text
    assert "smartenergy-weather-scheduler" not in text
