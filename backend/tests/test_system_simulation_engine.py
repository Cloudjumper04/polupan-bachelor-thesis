from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session

from app.config_loader import calculate_system_config_hash, load_config
from app.simulation.engine import (
    SimulationFallbackError,
    SystemSimulationWindows,
    build_default_system_simulation_windows,
    persist_integrated_system_result,
    simulate_integrated_system_window,
)
from app.storage.battery_repository import (
    BatteryCachePoint,
    list_battery_cache_points,
    save_battery_cache_points,
)
from app.storage.database import create_db_and_tables, get_engine
from app.storage.ems_repository import list_ems_cache_points
from app.storage.load_repository import list_load_cache_points
from scripts.generate_system_simulation import run_system_simulation_generation


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "station.default.yaml"


def test_integrated_system_window_persists_consistent_module_points() -> None:
    engine = get_engine("sqlite:///:memory:")
    create_db_and_tables(engine)
    config = load_config(CONFIG_PATH)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    start = datetime(2026, 1, 1, 22, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=20)
    windows = SystemSimulationWindows(
        start_utc=start,
        end_utc=end,
        history_end_utc=end,
        load_cache_start_utc=start,
        load_cache_end_utc=end,
        battery_cache_start_utc=start,
        battery_cache_end_utc=end,
        ems_cache_start_utc=start,
        ems_cache_end_utc=end,
    )

    with Session(engine) as session:
        result = simulate_integrated_system_window(
            session,
            config,
            windows,
            allow_fallbacks=True,
        )
        history_minutes = [point.timestamp_utc.minute for point in result.load_history]
        summary = persist_integrated_system_result(session, result)

        load_cache = list_load_cache_points(
            session,
            config.station.id,
            calculate_system_config_hash(config),
        )
        battery_cache = list_battery_cache_points(
            session,
            config.station.id,
            calculate_system_config_hash(config),
        )
        ems_cache = list_ems_cache_points(
            session,
            config.station.id,
            calculate_system_config_hash(config),
        )

    assert summary.load_cache_rows == 20
    assert summary.battery_cache_rows == 20
    assert summary.ems_cache_rows == 20
    assert summary.load_history_rows == 2
    assert summary.battery_history_rows == 2
    assert summary.ems_history_rows == 2
    assert history_minutes == [0, 15]
    assert result.fallbacks.solar_fallback_minutes == 20
    assert result.fallbacks.grid_fallback_minutes == 20

    assert len(load_cache) == 20
    assert len(battery_cache) == 20
    assert len(ems_cache) == 20
    assert {point.timestamp_utc.astimezone(station_timezone).second for point in load_cache} == {
        0
    }
    assert battery_cache[-1].energy_wh > battery_cache[0].energy_wh
    assert all(0.0 <= point.soc_percent <= 100.0 for point in battery_cache)

    for load_point, ems_point in zip(load_cache, ems_cache, strict=True):
        assert ems_point.effective_load_power_w <= load_point.total_load_power_w
        served_by_flows = (
            ems_point.grid_to_load_w
            + ems_point.solar_to_load_w
            + ems_point.battery_to_load_w
        )
        assert served_by_flows == pytest.approx(
            ems_point.effective_load_power_w,
            abs=0.01,
        )
        assert ems_point.applied_charge_power_w >= 0.0
        assert ems_point.selected_mode_frontend_id


def test_default_system_windows_use_two_day_cache_horizons_and_no_history() -> None:
    config = load_config(CONFIG_PATH)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    now = datetime(2026, 1, 1, 10, 5, 30, tzinfo=timezone.utc)

    windows = build_default_system_simulation_windows(now, station_timezone)

    expected_end = datetime(2026, 1, 3, 10, 6, tzinfo=timezone.utc)
    assert windows.history_end_utc is None
    assert windows.load_cache_end_utc == expected_end
    assert windows.battery_cache_end_utc == expected_end
    assert windows.ems_cache_end_utc == expected_end


def test_cache_only_window_writes_no_history_rows() -> None:
    engine = get_engine("sqlite:///:memory:")
    create_db_and_tables(engine)
    config = load_config(CONFIG_PATH)
    start = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=20)
    windows = _windows(start, end, history_end=None)

    with Session(engine) as session:
        result = simulate_integrated_system_window(
            session,
            config,
            windows,
            allow_fallbacks=True,
        )
        summary = persist_integrated_system_result(session, result)

    assert result.load_history == []
    assert result.battery_history == []
    assert result.ems_history == []
    assert summary.load_history_rows == 0
    assert summary.battery_history_rows == 0
    assert summary.ems_history_rows == 0
    assert summary.load_cache_rows == 20


def test_history_rows_stop_at_history_endpoint() -> None:
    engine = get_engine("sqlite:///:memory:")
    create_db_and_tables(engine)
    config = load_config(CONFIG_PATH)
    start = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=20)
    windows = _windows(start, end, history_end=start + timedelta(minutes=10))

    with Session(engine) as session:
        result = simulate_integrated_system_window(
            session,
            config,
            windows,
            allow_fallbacks=True,
        )

    assert [point.timestamp_utc.minute for point in result.load_history] == [0]
    assert [point.timestamp_utc.minute for point in result.battery_history] == [0]
    assert [point.timestamp_utc.minute for point in result.ems_history] == [0]


def test_fallback_disabled_fails_before_persisting() -> None:
    engine = get_engine("sqlite:///:memory:")
    create_db_and_tables(engine)
    config = load_config(CONFIG_PATH)
    start = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    windows = _windows(start, start + timedelta(minutes=1), history_end=None)

    with Session(engine) as session:
        with pytest.raises(SimulationFallbackError, match="source fallback used"):
            simulate_integrated_system_window(session, config, windows)

        assert list_load_cache_points(
            session,
            config.station.id,
            calculate_system_config_hash(config),
        ) == []


def test_fallback_enabled_reports_counts() -> None:
    engine = get_engine("sqlite:///:memory:")
    create_db_and_tables(engine)
    config = load_config(CONFIG_PATH)
    start = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    windows = _windows(start, start + timedelta(minutes=3), history_end=None)

    with Session(engine) as session:
        result = simulate_integrated_system_window(
            session,
            config,
            windows,
            allow_fallbacks=True,
        )

    assert result.fallbacks.solar_fallback_minutes == 3
    assert result.fallbacks.grid_fallback_minutes == 3
    assert result.fallbacks.weather_fallback_minutes == 3


def test_cache_window_uses_persisted_battery_seed() -> None:
    engine = get_engine("sqlite:///:memory:")
    create_db_and_tables(engine)
    config = load_config(CONFIG_PATH)
    config_hash = calculate_system_config_hash(config)
    start = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    seed_timestamp = start - timedelta(minutes=1)
    windows = _windows(start, start + timedelta(minutes=2), history_end=None)

    with Session(engine) as session:
        save_battery_cache_points(
            session,
            [
                BatteryCachePoint(
                    station_id=config.station.id,
                    config_hash=config_hash,
                    timestamp_utc=seed_timestamp,
                    timestamp_local=seed_timestamp,
                    soc_percent=10.416667,
                    soh_percent=100.0,
                    voltage_v=11.0,
                    energy_wh=100.0,
                    usable_capacity_wh=960.0,
                    current_usable_capacity_wh=960.0,
                    applied_charge_power_w=0.0,
                    applied_discharge_power_w=0.0,
                    net_battery_power_w=0.0,
                    cycle_fraction_increment=0.0,
                    soh_loss_percent=0.0,
                    status="idle",
                )
            ],
        )
        result = simulate_integrated_system_window(
            session,
            config,
            windows,
            allow_fallbacks=True,
        )

    assert result.seed.battery_seed_source == "cache"
    assert result.seed.battery_seed_timestamp_utc == seed_timestamp
    assert result.battery_cache[0].energy_wh < 150.0


def test_generator_dry_run_uses_safe_cache_only_plan() -> None:
    now = datetime(2026, 1, 1, 10, 5, 30, tzinfo=timezone.utc)

    summary = run_system_simulation_generation(
        CONFIG_PATH,
        database_url="sqlite:///:memory:",
        now=now,
        dry_run=True,
    )

    assert summary.dry_run is True
    assert summary.persisted is None
    assert summary.cache_only is True
    assert summary.history_writes_enabled is False
    assert summary.load_cache_end_utc == datetime(2026, 1, 3, 10, 6, tzinfo=timezone.utc)


def _windows(
    start: datetime,
    end: datetime,
    *,
    history_end: datetime | None,
) -> SystemSimulationWindows:
    return SystemSimulationWindows(
        start_utc=start,
        end_utc=end,
        history_end_utc=history_end,
        load_cache_start_utc=start,
        load_cache_end_utc=end,
        battery_cache_start_utc=start,
        battery_cache_end_utc=end,
        ems_cache_start_utc=start,
        ems_cache_end_utc=end,
    )
