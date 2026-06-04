from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from app.config_loader import calculate_system_config_hash, load_config
from app.main import app
from app.api.system_dashboard import get_system_dashboard
from app.storage.battery_repository import (
    BatteryCachePoint,
    BatteryHistoryPoint,
    list_battery_cache_points,
    save_battery_cache_points,
    save_battery_history_points,
)
from app.storage.database import create_db_and_tables, get_engine
from app.storage.ems_repository import (
    EmsCachePoint,
    frontend_mode_id,
    list_ems_cache_points,
    save_ems_cache_points,
)
from app.storage.load_repository import (
    LoadCachePoint,
    LoadHistoryPoint,
    list_load_cache_points,
    save_load_cache_points,
    save_load_history_points,
)


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "station.default.yaml"
STATION_TIMEZONE = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)


def test_system_dashboard_route_is_registered() -> None:
    assert any(route.path == "/api/system/dashboard" for route in app.routes)


def test_system_dashboard_endpoint_returns_frontend_ready_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'system-dashboard.db'}"
    config = load_config(CONFIG_PATH)
    config_hash = calculate_system_config_hash(config)
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    _seed_system_rows(engine, config.station.id, config_hash)
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_system_dashboard(at=NOW)
    assert payload["station_id"] == config.station.id
    assert payload["timestamp_utc"] == NOW.isoformat()

    assert payload["ems"]["selected_mode"] == "backup_reserve"
    assert payload["ems"]["selected_mode_frontend_id"] == "battery_reserve"
    assert payload["ems"]["risk_score"] == 2
    assert payload["ems"]["flow"]["grid_power_w"] == 270.0
    assert payload["ems"]["flow"]["solar_power_w"] == 120.0
    assert payload["ems"]["flow"]["battery_net_power_w"] == -80.0
    assert payload["ems"]["flow"]["load_power_w"] == 300.0
    assert payload["ems"]["metrics"]["inverter_state"] == "pass_through"

    assert payload["battery"]["soc_percent"] == 70.0
    assert payload["battery"]["soh_percent"] == 98.0
    assert payload["battery"]["voltage_v"] == 12.4
    assert payload["battery"]["energy_wh"] == 900.0
    assert payload["battery"]["net_battery_power_w"] == -80.0
    assert payload["battery"]["info"] == {
        "chemistry": "lead_acid",
        "capacity_ah": 200.0,
        "nominal_voltage_v": 12,
        "installation_date": "2025-10-06",
    }
    assert payload["battery"]["energy_history"]

    assert payload["load"]["current_power_w"] == 320.0
    assert payload["load"]["daily_energy_kwh"] == 1.2
    assert payload["load"]["solar_covered_percent"] == pytest.approx(44.673913)
    assert payload["load"]["money_saved_uah"] == pytest.approx(17.7552)
    assert payload["load"]["monthly_solar_covered_percent"] == pytest.approx(44.673913)
    assert payload["load"]["monthly_money_saved_uah"] == pytest.approx(17.7552)
    assert payload["load"]["daily_solar_covered_percent"] == 55.0
    assert payload["load"]["daily_money_saved_uah"] == 2.25
    assert payload["load"]["monthly_energy_kwh"] == 9.2
    assert payload["load"]["power_24h"]
    assert payload["load"]["monthly_energy"] == [
        {
            "date": "2026-06-01",
            "energy_wh": 3000.0,
            "solar_covered_percent": 40.0,
            "money_saved_uah": 1.0,
        },
        {
            "date": "2026-06-02",
            "energy_wh": 5000.0,
            "solar_covered_percent": 45.0,
            "money_saved_uah": 1.5,
        },
        {
            "date": "2026-06-04",
            "energy_wh": 1200.0,
            "solar_covered_percent": 55.0,
            "money_saved_uah": 2.25,
        },
    ]


def test_system_dashboard_endpoint_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'read-only.db'}"
    config = load_config(CONFIG_PATH)
    config_hash = calculate_system_config_hash(config)
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    _seed_system_rows(engine, config.station.id, config_hash)
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    before = _current_cache_counts(engine, config.station.id, config_hash)
    payload = get_system_dashboard(at=NOW)
    after = _current_cache_counts(engine, config.station.id, config_hash)

    assert payload["station_id"] == config.station.id
    assert after == before


def test_system_dashboard_missing_data_returns_404(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'empty-system-dashboard.db'}"
    create_db_and_tables(get_engine(database_url))
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    with pytest.raises(HTTPException) as exc_info:
        get_system_dashboard(at=NOW)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["message"] == "System dashboard data is not available"
    assert set(exc_info.value.detail["missing"]) == {"load", "battery", "ems"}


def test_frontend_mode_mapping_is_used_even_if_stored_id_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'mode-mapping.db'}"
    config = load_config(CONFIG_PATH)
    config_hash = calculate_system_config_hash(config)
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    _seed_system_rows(engine, config.station.id, config_hash)
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_system_dashboard(at=NOW)

    assert frontend_mode_id("backup_reserve") == "battery_reserve"
    assert payload["ems"]["selected_mode_frontend_id"] == "battery_reserve"


def _seed_system_rows(engine, station_id: str, config_hash: str) -> None:
    with Session(engine) as session:
        save_load_history_points(
            session,
            [
                _load_history(
                    datetime(2026, 6, 1, 12, 0, tzinfo=STATION_TIMEZONE),
                    station_id,
                    config_hash,
                    daily_energy_wh=3000.0,
                    solar_percent=40.0,
                    money_saved=1.0,
                ),
                _load_history(
                    datetime(2026, 6, 2, 12, 0, tzinfo=STATION_TIMEZONE),
                    station_id,
                    config_hash,
                    daily_energy_wh=5000.0,
                    solar_percent=45.0,
                    money_saved=1.5,
                ),
            ],
        )
        save_load_cache_points(
            session,
            [
                _load_cache(
                    NOW,
                    station_id,
                    config_hash,
                    daily_energy_wh=1200.0,
                    solar_percent=55.0,
                    money_saved=2.25,
                )
            ],
        )
        save_battery_history_points(
            session,
            [
                _battery_history(
                    NOW - timedelta(hours=1),
                    station_id,
                    config_hash,
                    soc=68.0,
                    energy_wh=860.0,
                )
            ],
        )
        save_battery_cache_points(
            session,
            [
                _battery_cache(
                    NOW,
                    station_id,
                    config_hash,
                    soc=70.0,
                    energy_wh=900.0,
                )
            ],
        )
        save_ems_cache_points(
            session,
            [_ems_cache(NOW, station_id, config_hash)],
        )


def _load_history(
    timestamp: datetime,
    station_id: str,
    config_hash: str,
    *,
    daily_energy_wh: float,
    solar_percent: float,
    money_saved: float,
) -> LoadHistoryPoint:
    timestamp_utc = timestamp.astimezone(timezone.utc)
    return LoadHistoryPoint(
        station_id=station_id,
        config_hash=config_hash,
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp,
        total_load_power_w=300.0,
        effective_served_load_w=280.0,
        load_cut_by_ems_w=20.0,
        daily_energy_wh_so_far=daily_energy_wh,
        solar_covered_percent=solar_percent,
        money_saved_uah=money_saved,
    )


def _load_cache(
    timestamp: datetime,
    station_id: str,
    config_hash: str,
    *,
    daily_energy_wh: float,
    solar_percent: float,
    money_saved: float,
) -> LoadCachePoint:
    return LoadCachePoint(
        station_id=station_id,
        config_hash=config_hash,
        timestamp_utc=timestamp,
        timestamp_local=timestamp.astimezone(STATION_TIMEZONE),
        total_load_power_w=320.0,
        effective_served_load_w=300.0,
        load_cut_by_ems_w=20.0,
        daily_energy_wh_so_far=daily_energy_wh,
        solar_covered_percent=solar_percent,
        money_saved_uah=money_saved,
    )


def _battery_history(
    timestamp: datetime,
    station_id: str,
    config_hash: str,
    *,
    soc: float,
    energy_wh: float,
) -> BatteryHistoryPoint:
    return BatteryHistoryPoint(
        station_id=station_id,
        config_hash=config_hash,
        timestamp_utc=timestamp,
        timestamp_local=timestamp.astimezone(STATION_TIMEZONE),
        soc_percent=soc,
        soh_percent=98.1,
        voltage_v=12.3,
        energy_wh=energy_wh,
        usable_capacity_wh=1200.0,
        current_usable_capacity_wh=1176.0,
        applied_charge_power_w=0.0,
        applied_discharge_power_w=80.0,
        net_battery_power_w=-80.0,
        cycle_fraction_increment=0.001,
        soh_loss_percent=0.0,
        status="discharging",
    )


def _battery_cache(
    timestamp: datetime,
    station_id: str,
    config_hash: str,
    *,
    soc: float,
    energy_wh: float,
) -> BatteryCachePoint:
    return BatteryCachePoint(
        station_id=station_id,
        config_hash=config_hash,
        timestamp_utc=timestamp,
        timestamp_local=timestamp.astimezone(STATION_TIMEZONE),
        soc_percent=soc,
        soh_percent=98.0,
        voltage_v=12.4,
        energy_wh=energy_wh,
        usable_capacity_wh=1200.0,
        current_usable_capacity_wh=1176.0,
        applied_charge_power_w=0.0,
        applied_discharge_power_w=80.0,
        net_battery_power_w=-80.0,
        cycle_fraction_increment=0.001,
        soh_loss_percent=0.0,
        status="discharging",
    )


def _ems_cache(timestamp: datetime, station_id: str, config_hash: str) -> EmsCachePoint:
    return EmsCachePoint(
        station_id=station_id,
        config_hash=config_hash,
        timestamp_utc=timestamp,
        timestamp_local=timestamp.astimezone(STATION_TIMEZONE),
        control_mode="auto",
        selected_mode="backup_reserve",
        selected_mode_frontend_id="stale_mock_id",
        auto_risk_score=2,
        protection_active=False,
        inverter_output_enabled=True,
        inverter_state="pass_through",
        target_soc_percent=80.0,
        cutoff_soc_percent=10.0,
        requested_charge_power_w=0.0,
        grid_to_load_w=250.0,
        grid_to_battery_w=20.0,
        solar_to_load_w=50.0,
        solar_to_battery_w=70.0,
        battery_to_load_w=80.0,
        applied_charge_power_w=0.0,
        effective_load_power_w=300.0,
        curtailed_or_cut_load_w=20.0,
    )


def _current_cache_counts(engine, station_id: str, config_hash: str) -> tuple[int, int, int]:
    with Session(engine) as session:
        return (
            len(list_load_cache_points(session, station_id, config_hash)),
            len(list_battery_cache_points(session, station_id, config_hash)),
            len(list_ems_cache_points(session, station_id, config_hash)),
        )
