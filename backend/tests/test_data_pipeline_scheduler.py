from __future__ import annotations

import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import run_data_pipeline_scheduler

from app.config_loader import calculate_system_config_hash, load_config
from app.storage.battery_repository import BatteryHistoryPoint, save_battery_history_points
from app.storage.database import create_db_and_tables, get_engine
from app.storage.ems_repository import EmsHistoryPoint, save_ems_history_points
from app.storage.load_repository import LoadHistoryPoint, save_load_history_points


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "station.default.yaml"


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
    assert settings.solar_cache_interval_minutes == 5.0
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
            "--solar-cache-interval-minutes",
            "2",
            "--db-path",
            str(db_path),
            "--lock-path",
            str(lock_path),
        ]
    )

    assert settings.allow_fallbacks is True
    assert settings.interval_minutes == 15.0
    assert settings.solar_cache_interval_minutes == 2.0
    assert settings.database_url == f"sqlite:///{db_path}"
    assert settings.lock_path == lock_path


def test_scheduler_solar_cache_cycle_uses_lock_and_runner(tmp_path: Path) -> None:
    calls: list[tuple[Path, str | None]] = []
    db_path = tmp_path / "target.db"
    lock_path = tmp_path / "target.lock"
    settings = run_data_pipeline_scheduler.parse_args(
        ["--db-path", str(db_path), "--lock-path", str(lock_path)]
    )

    def fake_cache(config: Path, database_url: str | None) -> object:
        assert lock_path.exists()
        calls.append((config, database_url))
        return SimpleNamespace(
            rows=3,
            windows=1,
            start_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_utc=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        )

    assert run_data_pipeline_scheduler.run_solar_cache_cycle(
        settings,
        cache_runner=fake_cache,
    )
    assert calls == [(settings.config, f"sqlite:///{db_path}")]
    assert not lock_path.exists()


def test_scheduler_solar_cache_cycle_dry_run_does_not_call_runner(
    tmp_path: Path,
) -> None:
    calls = 0
    lock_path = tmp_path / "target.lock"
    settings = run_data_pipeline_scheduler.parse_args(
        ["--dry-run", "--lock-path", str(lock_path)]
    )

    def fake_cache(config: Path, database_url: str | None) -> object:
        nonlocal calls
        calls += 1
        return object()

    assert run_data_pipeline_scheduler.run_solar_cache_cycle(
        settings,
        cache_runner=fake_cache,
    )
    assert calls == 0
    assert not lock_path.exists()


def test_scheduler_does_not_refresh_solar_cache_before_successful_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StopLoop(RuntimeError):
        pass

    class StopAfterWait:
        def __init__(self) -> None:
            self.waits: list[float] = []

        def is_set(self) -> bool:
            return False

        def wait(self, seconds: float) -> bool:
            self.waits.append(seconds)
            raise StopLoop

    stop_event = StopAfterWait()
    lock_path = tmp_path / "pipeline.lock"
    settings = run_data_pipeline_scheduler.parse_args(
        [
            "--interval-minutes",
            "60",
            "--solar-cache-interval-minutes",
            "1",
            "--lock-path",
            str(lock_path),
        ]
    )

    def fail_pipeline(**kwargs: object) -> object:
        raise RuntimeError("source maintenance failed")

    def fail_cache(settings: object) -> bool:
        raise AssertionError("solar cache must not run before source pipeline succeeds")

    monkeypatch.setattr(run_data_pipeline_scheduler, "run_solar_cache_cycle", fail_cache)

    with pytest.raises(StopLoop):
        run_data_pipeline_scheduler.run_scheduler(
            settings,
            pipeline_runner=fail_pipeline,
            stop_event=stop_event,
        )

    assert stop_event.waits


def test_empty_db_triggers_full_history_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []
    settings = _scheduler_settings(tmp_path)
    monkeypatch.setattr(
        run_data_pipeline_scheduler.update_data_pipeline,
        "print_data_pipeline_summary",
        lambda summary: None,
    )

    def fake_pipeline(**kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    assert run_data_pipeline_scheduler.run_pipeline_cycle(
        settings,
        pipeline_runner=fake_pipeline,
    )

    captured = capsys.readouterr()
    assert calls[0]["full_history"] is True
    assert "first-run full bootstrap detected" in captured.out
    assert "missing system history tables: load,battery,ems" in captured.out
    assert "first-run full bootstrap executed successfully" in captured.out


def test_initialized_db_does_not_force_full_history_every_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []
    settings = _scheduler_settings(tmp_path)
    _seed_history_baseline(
        settings.database_url,
        include_load=True,
        include_battery=True,
        include_ems=True,
    )
    monkeypatch.setattr(
        run_data_pipeline_scheduler.update_data_pipeline,
        "print_data_pipeline_summary",
        lambda summary: None,
    )

    def fake_pipeline(**kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    assert run_data_pipeline_scheduler.run_pipeline_cycle(
        settings,
        pipeline_runner=fake_pipeline,
    )

    captured = capsys.readouterr()
    assert calls[0]["full_history"] is False
    assert "first-run full bootstrap detected" not in captured.out


def test_missing_required_history_table_triggers_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []
    settings = _scheduler_settings(tmp_path)
    _seed_history_baseline(
        settings.database_url,
        include_load=True,
        include_battery=True,
        include_ems=False,
    )
    monkeypatch.setattr(
        run_data_pipeline_scheduler.update_data_pipeline,
        "print_data_pipeline_summary",
        lambda summary: None,
    )

    def fake_pipeline(**kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    assert run_data_pipeline_scheduler.run_pipeline_cycle(
        settings,
        pipeline_runner=fake_pipeline,
    )

    captured = capsys.readouterr()
    assert calls[0]["full_history"] is True
    assert "missing system history tables: ems" in captured.out


def test_compose_uses_data_scheduler_not_weather_scheduler() -> None:
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    text = compose_path.read_text(encoding="utf-8")

    assert "  data-scheduler:" in text
    assert "container_name: smartenergy-data-scheduler" in text
    assert "backend/scripts/run_data_pipeline_scheduler.py" in text
    assert "  weather-scheduler:" not in text
    assert "smartenergy-weather-scheduler" not in text


def _scheduler_settings(tmp_path: Path) -> run_data_pipeline_scheduler.SchedulerSettings:
    db_path = tmp_path / "scheduler.db"
    lock_path = tmp_path / "pipeline.lock"
    return run_data_pipeline_scheduler.parse_args(
        [
            "--config",
            str(CONFIG_PATH),
            "--db-path",
            str(db_path),
            "--lock-path",
            str(lock_path),
        ]
    )


def _seed_history_baseline(
    database_url: str | None,
    *,
    include_load: bool,
    include_battery: bool,
    include_ems: bool,
) -> None:
    config = load_config(CONFIG_PATH)
    station_id = config.station.id
    config_hash = calculate_system_config_hash(config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    history_start = date.fromisoformat(config.station.installation_date)
    timestamp = datetime.combine(
        history_start,
        datetime.min.time(),
        tzinfo=station_timezone,
    ).astimezone(timezone.utc)

    run_data_pipeline_scheduler.update_data_pipeline.import_all_storage_models()
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    with Session(engine) as session:
        if include_load:
            save_load_history_points(
                session,
                [_load_history(timestamp, station_id, config_hash)],
            )
        if include_battery:
            save_battery_history_points(
                session,
                [_battery_history(timestamp, station_id, config_hash)],
            )
        if include_ems:
            save_ems_history_points(
                session,
                [_ems_history(timestamp, station_id, config_hash)],
            )


def _load_history(
    timestamp_utc: datetime,
    station_id: str,
    config_hash: str,
) -> LoadHistoryPoint:
    return LoadHistoryPoint(
        station_id=station_id,
        config_hash=config_hash,
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc,
        total_load_power_w=300.0,
        effective_served_load_w=300.0,
    )


def _battery_history(
    timestamp_utc: datetime,
    station_id: str,
    config_hash: str,
) -> BatteryHistoryPoint:
    return BatteryHistoryPoint(
        station_id=station_id,
        config_hash=config_hash,
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc,
        soc_percent=70.0,
        soh_percent=98.0,
        voltage_v=12.4,
        energy_wh=900.0,
        usable_capacity_wh=1200.0,
        current_usable_capacity_wh=1176.0,
        applied_charge_power_w=0.0,
        applied_discharge_power_w=0.0,
        net_battery_power_w=0.0,
    )


def _ems_history(
    timestamp_utc: datetime,
    station_id: str,
    config_hash: str,
) -> EmsHistoryPoint:
    return EmsHistoryPoint(
        station_id=station_id,
        config_hash=config_hash,
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc,
        control_mode="auto",
        selected_mode="backup_reserve",
        selected_mode_frontend_id="battery_reserve",
        auto_risk_score=0,
        protection_active=False,
        inverter_output_enabled=True,
        inverter_state="pass_through",
        target_soc_percent=80.0,
        cutoff_soc_percent=10.0,
        requested_charge_power_w=0.0,
        grid_to_load_w=300.0,
        grid_to_battery_w=0.0,
        solar_to_load_w=0.0,
        solar_to_battery_w=0.0,
        battery_to_load_w=0.0,
        applied_charge_power_w=0.0,
        effective_load_power_w=300.0,
    )
