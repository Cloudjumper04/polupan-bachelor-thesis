from __future__ import annotations

import os
from bisect import bisect_right
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, TypeVar
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from app.config_loader import calculate_system_config_hash, load_config
from app.schemas import AppConfig
from app.storage.battery_repository import (
    BatteryCachePoint,
    BatteryHistoryPoint,
    get_latest_battery_cache_point,
    get_latest_battery_history_point,
    list_battery_cache_points,
    list_battery_history_points,
)
from app.storage.database import get_engine
from app.storage.ems_repository import (
    EmsCachePoint,
    EmsHistoryPoint,
    frontend_mode_id,
    get_latest_ems_cache_point,
    get_latest_ems_history_point,
)
from app.storage.load_repository import (
    LoadCachePoint,
    LoadHistoryPoint,
    get_latest_load_cache_point,
    get_latest_load_history_point,
    list_load_cache_points,
    list_load_history_points,
)


DEFAULT_CONFIG_PATH = Path("backend/config/station.default.yaml")
DEFAULT_DATABASE_URL = "sqlite:///backend/data/smartenergy.db"
LOAD_POWER_CHART_STEP_SECONDS = 5 * 60
BATTERY_HISTORY_MAX_POINTS = 150


router = APIRouter(prefix="/api/system", tags=["system-dashboard"])


@router.get("/dashboard")
def get_system_dashboard(
    at: datetime | None = None,
    station_id: str | None = None,
) -> dict[str, Any]:
    config = load_config(_config_path())
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    target_utc = _normalize_query_datetime(at, station_timezone)
    engine = get_engine(_database_url())
    with Session(engine) as session:
        return build_system_dashboard_payload(
            session,
            config,
            target_utc=target_utc,
            requested_station_id=station_id,
        )


def build_system_dashboard_payload(
    session: Session,
    config: AppConfig,
    *,
    target_utc: datetime,
    requested_station_id: str | None = None,
) -> dict[str, Any]:
    station_id = _resolve_station_id(config, requested_station_id)
    config_hash = calculate_system_config_hash(config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)

    try:
        load_point = _latest_load_point(session, station_id, config_hash, target_utc)
        battery_point = _latest_battery_point(session, station_id, config_hash, target_utc)
        ems_point = _latest_ems_point(session, station_id, config_hash, target_utc)
    except OperationalError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "System dashboard data tables are not available",
                "station_id": station_id,
            },
        ) from exc

    missing = [
        name
        for name, point in (
            ("load", load_point),
            ("battery", battery_point),
            ("ems", ems_point),
        )
        if point is None
    ]
    if missing:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "System dashboard data is not available",
                "station_id": station_id,
                "missing": missing,
                "at": target_utc.isoformat(),
            },
        )

    payload_timestamp = max(
        load_point.timestamp_utc,
        battery_point.timestamp_utc,
        ems_point.timestamp_utc,
    )
    return {
        "timestamp_utc": payload_timestamp.isoformat(),
        "station_id": station_id,
        "ems": _ems_payload(ems_point, battery_point),
        "battery": _battery_payload(
            session,
            station_id,
            config_hash,
            battery_point,
            config,
            station_timezone,
        ),
        "load": _load_payload(
            session,
            station_id,
            config_hash,
            load_point,
            ems_point,
            config,
            station_timezone,
        ),
    }


def _resolve_station_id(config: AppConfig, requested_station_id: str | None) -> str:
    if requested_station_id is None or requested_station_id == config.station.id:
        return config.station.id
    raise HTTPException(
        status_code=404,
        detail={
            "message": "Requested station is not configured",
            "station_id": requested_station_id,
        },
    )


def _latest_load_point(
    session: Session,
    station_id: str,
    config_hash: str,
    target_utc: datetime,
) -> LoadCachePoint | LoadHistoryPoint | None:
    return _newer_point(
        get_latest_load_cache_point(session, station_id, config_hash, target_utc),
        get_latest_load_history_point(session, station_id, config_hash, target_utc),
    )


def _latest_battery_point(
    session: Session,
    station_id: str,
    config_hash: str,
    target_utc: datetime,
) -> BatteryCachePoint | BatteryHistoryPoint | None:
    return _newer_point(
        get_latest_battery_cache_point(session, station_id, config_hash, target_utc),
        get_latest_battery_history_point(session, station_id, config_hash, target_utc),
    )


def _latest_ems_point(
    session: Session,
    station_id: str,
    config_hash: str,
    target_utc: datetime,
) -> EmsCachePoint | EmsHistoryPoint | None:
    return _newer_point(
        get_latest_ems_cache_point(session, station_id, config_hash, target_utc),
        get_latest_ems_history_point(session, station_id, config_hash, target_utc),
    )


T = TypeVar("T")


def _newer_point(first: T | None, second: T | None) -> T | None:
    if first is None:
        return second
    if second is None:
        return first
    return first if first.timestamp_utc >= second.timestamp_utc else second


def _ems_payload(
    ems_point: EmsCachePoint | EmsHistoryPoint,
    battery_point: BatteryCachePoint | BatteryHistoryPoint,
) -> dict[str, Any]:
    mapped_mode = frontend_mode_id(ems_point.selected_mode)
    battery_net_power_w = battery_point.net_battery_power_w
    return {
        "control_mode": ems_point.control_mode,
        "selected_mode": ems_point.selected_mode,
        "selected_mode_frontend_id": mapped_mode,
        "risk_score": ems_point.auto_risk_score,
        "title_tooltip": "Current EMS decision generated by the backend simulator.",
        "risk_tooltip": "Higher risk means recent outage/protection conditions are stronger.",
        "flow": {
            "grid_power_w": ems_point.grid_to_load_w + ems_point.grid_to_battery_w,
            "solar_power_w": ems_point.solar_to_load_w + ems_point.solar_to_battery_w,
            "battery_net_power_w": battery_net_power_w,
            "load_power_w": ems_point.effective_load_power_w,
            "grid_to_load_w": ems_point.grid_to_load_w,
            "grid_to_battery_w": ems_point.grid_to_battery_w,
            "solar_to_load_w": ems_point.solar_to_load_w,
            "solar_to_battery_w": ems_point.solar_to_battery_w,
            "battery_to_load_w": ems_point.battery_to_load_w,
            "applied_charge_power_w": ems_point.applied_charge_power_w,
            "effective_load_power_w": ems_point.effective_load_power_w,
            "curtailed_or_cut_load_w": ems_point.curtailed_or_cut_load_w,
        },
        "metrics": {
            "inverter_state": _inverter_state(ems_point),
            "battery_charge_power_w": ems_point.applied_charge_power_w,
            "target_soc_percent": ems_point.target_soc_percent,
            "cutoff_soc_percent": ems_point.cutoff_soc_percent,
            "requested_charge_power_w": ems_point.requested_charge_power_w,
            "protection_active": ems_point.protection_active,
            "inverter_output_enabled": ems_point.inverter_output_enabled,
        },
    }


def _battery_payload(
    session: Session,
    station_id: str,
    config_hash: str,
    battery_point: BatteryCachePoint | BatteryHistoryPoint,
    config: AppConfig,
    station_timezone: ZoneInfo,
) -> dict[str, Any]:
    history_start = battery_point.timestamp_utc - timedelta(hours=24)
    history_rows = _combined_battery_rows(
        session,
        station_id,
        config_hash,
        history_start,
        battery_point.timestamp_utc + timedelta(minutes=1),
    )
    return {
        "soc_percent": battery_point.soc_percent,
        "soh_percent": battery_point.soh_percent,
        "voltage_v": battery_point.voltage_v,
        "energy_wh": battery_point.energy_wh,
        "usable_capacity_wh": battery_point.usable_capacity_wh,
        "current_usable_capacity_wh": battery_point.current_usable_capacity_wh,
        "net_battery_power_w": battery_point.net_battery_power_w,
        "info": {
            "chemistry": config.station.battery.chemistry,
            "capacity_ah": config.station.battery.capacity_ah,
            "nominal_voltage_v": config.station.battery.nominal_voltage_v,
            "installation_date": config.station.battery.installation_date,
        },
        "energy_history": [
            _battery_history_point_payload(row, station_timezone)
            for row in _downsample(history_rows, BATTERY_HISTORY_MAX_POINTS)
        ],
    }


def _load_payload(
    session: Session,
    station_id: str,
    config_hash: str,
    load_point: LoadCachePoint | LoadHistoryPoint,
    ems_point: EmsCachePoint | EmsHistoryPoint,
    config: AppConfig,
    station_timezone: ZoneInfo,
) -> dict[str, Any]:
    current_local = load_point.timestamp_utc.astimezone(station_timezone)
    month_start_local = current_local.replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    month_start_utc = month_start_local.astimezone(timezone.utc)
    query_end_utc = load_point.timestamp_utc + timedelta(minutes=1)
    month_rows = _combined_load_rows(
        session,
        station_id,
        config_hash,
        month_start_utc,
        query_end_utc,
    )
    monthly_energy = _monthly_load_energy_points(
        month_rows,
        station_timezone,
        current_local.date(),
    )
    monthly_energy_wh = sum(point["energy_wh"] for point in monthly_energy)
    monthly_economy = _monthly_load_economy_summary(
        month_rows,
        station_timezone,
        current_local.date(),
        config.station.economics.grid_tariff_uah_per_kwh,
    )
    return {
        "current_power_w": load_point.total_load_power_w,
        "daily_energy_kwh": load_point.daily_energy_wh_so_far / 1000.0,
        "solar_covered_percent": monthly_economy["solar_covered_percent"],
        "money_saved_uah": monthly_economy["money_saved_uah"],
        "monthly_solar_covered_percent": monthly_economy["solar_covered_percent"],
        "monthly_money_saved_uah": monthly_economy["money_saved_uah"],
        "daily_solar_covered_percent": load_point.solar_covered_percent,
        "daily_money_saved_uah": load_point.money_saved_uah,
        "monthly_energy_kwh": monthly_energy_wh / 1000.0,
        "effective_served_load_w": load_point.effective_served_load_w,
        "load_cut_by_ems_w": load_point.load_cut_by_ems_w,
        "power_24h": _load_power_24h_points(
            session,
            station_id,
            config_hash,
            load_point.timestamp_utc,
        ),
        "monthly_energy": monthly_energy,
        "tariff_uah_per_kwh": config.station.economics.grid_tariff_uah_per_kwh,
        "current_solar_flow_to_load_w": ems_point.solar_to_load_w,
        "current_solar_flow_to_battery_w": ems_point.solar_to_battery_w,
    }


def _combined_load_rows(
    session: Session,
    station_id: str,
    config_hash: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[LoadCachePoint | LoadHistoryPoint]:
    rows: dict[datetime, LoadCachePoint | LoadHistoryPoint] = {}
    for row in list_load_history_points(session, station_id, config_hash, start_utc, end_utc):
        rows[row.timestamp_utc] = row
    for row in list_load_cache_points(session, station_id, config_hash, start_utc, end_utc):
        rows[row.timestamp_utc] = row
    return [rows[timestamp] for timestamp in sorted(rows)]


def _combined_battery_rows(
    session: Session,
    station_id: str,
    config_hash: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[BatteryCachePoint | BatteryHistoryPoint]:
    rows: dict[datetime, BatteryCachePoint | BatteryHistoryPoint] = {}
    for row in list_battery_history_points(
        session,
        station_id,
        config_hash,
        start_utc,
        end_utc,
    ):
        rows[row.timestamp_utc] = row
    for row in list_battery_cache_points(
        session,
        station_id,
        config_hash,
        start_utc,
        end_utc,
    ):
        rows[row.timestamp_utc] = row
    return [rows[timestamp] for timestamp in sorted(rows)]


def _load_power_24h_points(
    session: Session,
    station_id: str,
    config_hash: str,
    end_utc: datetime,
) -> list[dict[str, Any]]:
    aligned_end = _floor_utc_to_cadence(end_utc, LOAD_POWER_CHART_STEP_SECONDS)
    start_utc = aligned_end - timedelta(hours=24)
    rows = _combined_load_rows(
        session,
        station_id,
        config_hash,
        start_utc - timedelta(minutes=15),
        aligned_end + timedelta(minutes=1),
    )
    timestamps = [row.timestamp_utc for row in rows]
    points: list[dict[str, Any]] = []
    target = start_utc
    step = timedelta(seconds=LOAD_POWER_CHART_STEP_SECONDS)
    while target <= aligned_end:
        row = _row_at_or_before(rows, timestamps, target, max_age=timedelta(minutes=15))
        if row is not None:
            points.append(
                {
                    "timestamp_utc": target.isoformat(),
                    "power_w": row.total_load_power_w,
                }
            )
        target += step
    return points


def _monthly_load_energy_points(
    rows: Iterable[LoadCachePoint | LoadHistoryPoint],
    station_timezone: ZoneInfo,
    current_local_date: date,
) -> list[dict[str, Any]]:
    energy_by_date: dict[date, float] = {}
    solar_by_date: dict[date, float] = {}
    money_by_date: dict[date, float] = {}
    for row in rows:
        local_date = row.timestamp_utc.astimezone(station_timezone).date()
        if local_date > current_local_date:
            continue
        energy_by_date[local_date] = max(
            energy_by_date.get(local_date, 0.0),
            row.daily_energy_wh_so_far,
        )
        solar_by_date[local_date] = max(
            solar_by_date.get(local_date, 0.0),
            row.solar_covered_percent,
        )
        money_by_date[local_date] = max(
            money_by_date.get(local_date, 0.0),
            row.money_saved_uah,
        )
    return [
        {
            "date": local_date.isoformat(),
            "energy_wh": energy_by_date[local_date],
            "solar_covered_percent": solar_by_date.get(local_date, 0.0),
            "money_saved_uah": money_by_date.get(local_date, 0.0),
        }
        for local_date in sorted(energy_by_date)
    ]


def _monthly_load_economy_summary(
    rows: Iterable[LoadCachePoint | LoadHistoryPoint],
    station_timezone: ZoneInfo,
    current_local_date: date,
    tariff_uah_per_kwh: float,
) -> dict[str, float]:
    latest_energy_row_by_date: dict[date, LoadCachePoint | LoadHistoryPoint] = {}
    for row in rows:
        local_date = row.timestamp_utc.astimezone(station_timezone).date()
        if local_date > current_local_date:
            continue
        existing = latest_energy_row_by_date.get(local_date)
        if existing is None or row.daily_energy_wh_so_far >= existing.daily_energy_wh_so_far:
            latest_energy_row_by_date[local_date] = row

    total_load_energy_wh = 0.0
    total_solar_covered_energy_wh = 0.0
    for row in latest_energy_row_by_date.values():
        daily_energy_wh = max(0.0, row.daily_energy_wh_so_far)
        daily_solar_percent = _clamp(row.solar_covered_percent, 0.0, 100.0)
        total_load_energy_wh += daily_energy_wh
        total_solar_covered_energy_wh += daily_energy_wh * daily_solar_percent / 100.0

    if total_load_energy_wh <= 0.0:
        solar_percent = 0.0
    else:
        solar_percent = _clamp(
            total_solar_covered_energy_wh / total_load_energy_wh * 100.0,
            0.0,
            100.0,
        )
    return {
        "solar_covered_percent": solar_percent,
        "money_saved_uah": total_solar_covered_energy_wh / 1000.0 * tariff_uah_per_kwh,
    }


def _battery_history_point_payload(
    row: BatteryCachePoint | BatteryHistoryPoint,
    station_timezone: ZoneInfo,
) -> dict[str, Any]:
    return {
        "timestamp_utc": row.timestamp_utc.isoformat(),
        "timestamp_local": row.timestamp_utc.astimezone(station_timezone).isoformat(),
        "energy_wh": row.energy_wh,
        "soc_percent": row.soc_percent,
        "soh_percent": row.soh_percent,
        "voltage_v": row.voltage_v,
        "net_battery_power_w": row.net_battery_power_w,
    }


def _downsample(rows: list[T], max_points: int) -> list[T]:
    if len(rows) <= max_points:
        return rows
    step = max(1, (len(rows) + max_points - 1) // max_points)
    sampled = rows[::step]
    if sampled[-1] is not rows[-1]:
        sampled.append(rows[-1])
    return sampled


def _row_at_or_before(
    rows: list[T],
    timestamps: list[datetime],
    target_utc: datetime,
    *,
    max_age: timedelta,
) -> T | None:
    index = bisect_right(timestamps, target_utc) - 1
    if index < 0:
        return None
    row = rows[index]
    if target_utc - row.timestamp_utc <= max_age:
        return row
    return None


def _inverter_state(ems_point: EmsCachePoint | EmsHistoryPoint) -> str:
    if ems_point.inverter_state:
        return ems_point.inverter_state
    if ems_point.protection_active:
        return "protection"
    if not ems_point.inverter_output_enabled:
        return "disabled"
    return "enabled"


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))


def _floor_utc_to_cadence(value: datetime, cadence_seconds: int) -> datetime:
    value_utc = value.astimezone(timezone.utc)
    timestamp_seconds = int(value_utc.timestamp())
    aligned_seconds = timestamp_seconds - (timestamp_seconds % cadence_seconds)
    return datetime.fromtimestamp(aligned_seconds, tz=timezone.utc)


def _normalize_query_datetime(value: datetime | None, station_timezone: ZoneInfo) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=station_timezone).astimezone(timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _config_path() -> Path:
    return Path(os.environ.get("SMARTENERGY_CONFIG_PATH", DEFAULT_CONFIG_PATH))


def _database_url() -> str:
    return os.environ.get("SMARTENERGY_DATABASE_URL", DEFAULT_DATABASE_URL)
