from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlmodel import Session

from app.storage.battery_repository import (
    BatteryCachePoint,
    BatteryHistoryPoint,
    list_battery_cache_points,
    list_battery_history_points,
    save_battery_cache_points,
    save_battery_history_points,
)
from app.storage.database import create_db_and_tables, get_engine
from app.storage.ems_repository import (
    EmsCachePoint,
    EmsHistoryPoint,
    frontend_mode_id,
    list_ems_cache_points,
    list_ems_history_points,
    save_ems_cache_points,
    save_ems_history_points,
)
from app.storage.load_repository import (
    LoadCachePoint,
    LoadHistoryPoint,
    decode_event_tags,
    encode_event_tags,
    list_load_cache_points,
    list_load_history_points,
    save_load_cache_points,
    save_load_history_points,
)


STATION_ID = "smartenergy-lab"
CONFIG_HASH = "test-config"


def test_load_repository_saves_history_and_cache() -> None:
    engine = _memory_engine()
    timestamp = datetime(2026, 1, 1, 10, 15, tzinfo=timezone.utc)

    with Session(engine) as session:
        assert save_load_history_points(session, [_load_history(timestamp)]) == 1
        assert save_load_cache_points(session, [_load_cache(timestamp)]) == 1

        history = list_load_history_points(session, STATION_ID, CONFIG_HASH)
        cache = list_load_cache_points(session, STATION_ID, CONFIG_HASH)

    assert len(history) == 1
    assert len(cache) == 1
    assert history[0].total_load_power_w == 320.0
    assert cache[0].solar_covered_percent == 42.5
    assert decode_event_tags(history[0].active_event_tags_json) == ("lighting", "pc")


def test_battery_repository_saves_history_and_cache() -> None:
    engine = _memory_engine()
    timestamp = datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc)

    with Session(engine) as session:
        assert save_battery_history_points(session, [_battery_history(timestamp)]) == 1
        assert save_battery_cache_points(session, [_battery_cache(timestamp)]) == 1

        history = list_battery_history_points(session, STATION_ID, CONFIG_HASH)
        cache = list_battery_cache_points(session, STATION_ID, CONFIG_HASH)

    assert len(history) == 1
    assert len(cache) == 1
    assert history[0].soc_percent == 74.0
    assert cache[0].net_battery_power_w == -120.0


def test_ems_repository_saves_history_and_cache() -> None:
    engine = _memory_engine()
    timestamp = datetime(2026, 1, 1, 10, 45, tzinfo=timezone.utc)

    with Session(engine) as session:
        assert save_ems_history_points(session, [_ems_history(timestamp)]) == 1
        assert save_ems_cache_points(session, [_ems_cache(timestamp)]) == 1

        history = list_ems_history_points(session, STATION_ID, CONFIG_HASH)
        cache = list_ems_cache_points(session, STATION_ID, CONFIG_HASH)

    assert len(history) == 1
    assert len(cache) == 1
    assert history[0].selected_mode == "grid_priority"
    assert history[0].selected_mode_frontend_id == "grid"
    assert cache[0].grid_to_load_w == 250.0


def test_timestamp_policy_history_requires_quarter_hour_cache_accepts_minute() -> None:
    engine = _memory_engine()
    valid_cache_time = datetime(2026, 1, 1, 10, 7, tzinfo=timezone.utc)
    invalid_history_time = datetime(2026, 1, 1, 10, 7, tzinfo=timezone.utc)
    invalid_cache_time = datetime(2026, 1, 1, 10, 7, 5, tzinfo=timezone.utc)

    with Session(engine) as session:
        assert save_load_cache_points(session, [_load_cache(valid_cache_time)]) == 1
        with pytest.raises(ValueError, match="15-minute"):
            save_load_history_points(session, [_load_history(invalid_history_time)])
        with pytest.raises(ValueError, match="minute-aligned"):
            save_load_cache_points(session, [_load_cache(invalid_cache_time)])


def test_battery_and_ems_history_reject_non_quarter_hour_timestamps() -> None:
    engine = _memory_engine()
    invalid_history_time = datetime(2026, 1, 1, 10, 7, tzinfo=timezone.utc)

    with Session(engine) as session:
        with pytest.raises(ValueError, match="15-minute"):
            save_battery_history_points(session, [_battery_history(invalid_history_time)])
        with pytest.raises(ValueError, match="15-minute"):
            save_ems_history_points(session, [_ems_history(invalid_history_time)])


def test_frontend_mode_mapping_is_explicit() -> None:
    assert frontend_mode_id("grid_priority") == "grid"
    assert frontend_mode_id("solar_priority") == "solar"
    assert frontend_mode_id("backup_reserve") == "battery_reserve"
    assert frontend_mode_id("force_charge") == "forced_charge"


def _memory_engine():
    engine = get_engine("sqlite:///:memory:")
    create_db_and_tables(engine)
    return engine


def _load_history(timestamp: datetime) -> LoadHistoryPoint:
    return LoadHistoryPoint(**_load_kwargs(timestamp))


def _load_cache(timestamp: datetime) -> LoadCachePoint:
    return LoadCachePoint(**_load_kwargs(timestamp))


def _load_kwargs(timestamp: datetime) -> dict[str, object]:
    return {
        "station_id": STATION_ID,
        "config_hash": CONFIG_HASH,
        "timestamp_utc": timestamp,
        "timestamp_local": timestamp,
        "total_load_power_w": 320.0,
        "effective_served_load_w": 300.0,
        "load_cut_by_ems_w": 20.0,
        "daily_energy_wh_so_far": 1200.0,
        "solar_covered_percent": 42.5,
        "money_saved_uah": 2.5,
        "active_student_count": 4,
        "active_professor_count": 1,
        "active_event_tags_json": encode_event_tags(["lighting", "pc"]),
        "lighting_active": True,
        "high_power_active": False,
    }


def _battery_history(timestamp: datetime) -> BatteryHistoryPoint:
    return BatteryHistoryPoint(**_battery_kwargs(timestamp))


def _battery_cache(timestamp: datetime) -> BatteryCachePoint:
    return BatteryCachePoint(**_battery_kwargs(timestamp))


def _battery_kwargs(timestamp: datetime) -> dict[str, object]:
    return {
        "station_id": STATION_ID,
        "config_hash": CONFIG_HASH,
        "timestamp_utc": timestamp,
        "timestamp_local": timestamp,
        "soc_percent": 74.0,
        "soh_percent": 96.5,
        "voltage_v": 12.4,
        "energy_wh": 900.0,
        "usable_capacity_wh": 1200.0,
        "current_usable_capacity_wh": 1158.0,
        "applied_charge_power_w": 0.0,
        "applied_discharge_power_w": 120.0,
        "net_battery_power_w": -120.0,
        "cycle_fraction_increment": 0.001,
        "soh_loss_percent": 0.0,
        "status": "discharging",
    }


def _ems_history(timestamp: datetime) -> EmsHistoryPoint:
    return EmsHistoryPoint(**_ems_kwargs(timestamp))


def _ems_cache(timestamp: datetime) -> EmsCachePoint:
    return EmsCachePoint(**_ems_kwargs(timestamp))


def _ems_kwargs(timestamp: datetime) -> dict[str, object]:
    return {
        "station_id": STATION_ID,
        "config_hash": CONFIG_HASH,
        "timestamp_utc": timestamp,
        "timestamp_local": timestamp,
        "control_mode": "auto",
        "selected_mode": "grid_priority",
        "selected_mode_frontend_id": frontend_mode_id("grid_priority"),
        "auto_risk_score": 0.2,
        "protection_active": False,
        "inverter_output_enabled": True,
        "inverter_state": "pass_through",
        "target_soc_percent": 80.0,
        "cutoff_soc_percent": 10.0,
        "requested_charge_power_w": 0.0,
        "grid_to_load_w": 250.0,
        "grid_to_battery_w": 0.0,
        "solar_to_load_w": 50.0,
        "solar_to_battery_w": 0.0,
        "battery_to_load_w": 0.0,
        "applied_charge_power_w": 0.0,
        "effective_load_power_w": 300.0,
        "curtailed_or_cut_load_w": 20.0,
    }
