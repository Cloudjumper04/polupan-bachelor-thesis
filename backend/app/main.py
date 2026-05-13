from __future__ import annotations

import os
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pvlib
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from app.config_loader import calculate_config_hash, load_config
from app.schemas import AppConfig
from app.simulation.solar import estimate_pv_array_operating_point
from app.simulation.weather import map_weather_code_to_state
from app.storage.database import get_engine
from app.storage.forecast_repository import get_nearest_forecast_for_station
from app.storage.forecast_solar_repository import (
    ForecastSolarProduction,
    get_forecast_solar_range,
    list_forecast_solar_for_config,
)
from app.storage.grid_repository import (
    GridAvailabilityPointRecord,
    get_nearest_grid_availability_point,
    list_grid_availability_points,
)
from app.storage.interpolated_solar_repository import (
    InterpolatedSolarProduction,
    get_interpolated_solar_range,
    get_nearest_interpolated_solar_for_config,
    list_interpolated_solar_for_config,
)
from app.storage.simulated_solar_repository import (
    SimulatedSolarProduction,
    get_simulated_solar_range,
    list_simulated_solar_for_config,
)


DEFAULT_CONFIG_PATH = Path("backend/config/station.default.yaml")
DEFAULT_DATABASE_URL = "sqlite:///backend/data/smartenergy.db"
SOLAR_DASHBOARD_CHARTS = {
    "last30m": {"minutes": 30, "resolution_seconds": 10},
    "last3h": {"minutes": 180, "resolution_seconds": 60},
    "last12h": {"minutes": 720, "resolution_seconds": 180},
    "last24h": {"minutes": 1440, "resolution_seconds": 300},
    "last7d": {"minutes": 10080, "resolution_seconds": 900},
}
CACHE_RESOLUTION_SECONDS = (1, 5, 30, 60, 300)
BASE_SOLAR_STEP_SECONDS = 15 * 60
MAX_POWER_HISTORY_DAYS = 31
WEATHER_LABELS_UK = {
    "clear": "ясно",
    "partly_cloudy": "мінлива хмарність",
    "cloudy": "хмарно",
    "fog": "туман",
    "drizzle": "мряка",
    "rain": "дощ",
    "snow": "сніг",
    "thunderstorm": "гроза",
    "unknown": "невідомо",
}


app = FastAPI(title="SmartEnergy Lab API")


@dataclass(frozen=True)
class PowerSourcePoint:
    timestamp_utc: datetime
    timestamp_local: datetime
    power_w: float
    source: str


@app.get("/api/solar/dashboard")
def get_solar_dashboard(
    now: datetime | None = None,
) -> dict[str, Any]:
    config = load_config(_config_path())
    engine = get_engine(_database_url())
    now_utc = _normalize_now(now)
    with Session(engine) as session:
        return build_solar_dashboard_payload(session, config, now_utc)


@app.get("/api/solar/current")
def get_solar_current(
    now: datetime | None = None,
) -> dict[str, Any]:
    config = load_config(_config_path())
    engine = get_engine(_database_url())
    now_utc = _normalize_now(now)
    with Session(engine) as session:
        return build_solar_current_payload(session, config, now_utc)


@app.get("/api/solar/current-buffer")
def get_solar_current_buffer(
    now: datetime | None = None,
    seconds: int = 60,
) -> dict[str, Any]:
    config = load_config(_config_path())
    engine = get_engine(_database_url())
    now_utc = _normalize_now(now)
    with Session(engine) as session:
        return build_solar_current_buffer_payload(session, config, now_utc, seconds)


@app.get("/api/solar/weather-current")
def get_solar_weather_current(
    now: datetime | None = None,
) -> dict[str, Any]:
    config = load_config(_config_path())
    engine = get_engine(_database_url())
    now_utc = _normalize_now(now)
    with Session(engine) as session:
        return build_solar_weather_current_payload(session, config, now_utc)


@app.get("/api/solar/history/power")
def get_solar_power_history(
    start: datetime,
    end: datetime,
    now: datetime | None = None,
) -> dict[str, Any]:
    config = load_config(_config_path())
    engine = get_engine(_database_url())
    with Session(engine) as session:
        return build_solar_power_history_payload(session, config, start, end, now=now)


@app.get("/api/solar/history/daily-energy")
def get_solar_daily_energy_history(
    start: datetime,
    end: datetime,
    now: datetime | None = None,
) -> dict[str, Any]:
    config = load_config(_config_path())
    engine = get_engine(_database_url())
    with Session(engine) as session:
        return build_solar_daily_energy_history_payload(
            session,
            config,
            start,
            end,
            now=now,
        )


@app.get("/api/solar/history/bounds")
def get_solar_history_bounds(now: datetime | None = None) -> dict[str, Any]:
    config = load_config(_config_path())
    engine = get_engine(_database_url())
    with Session(engine) as session:
        return build_solar_history_bounds_payload(session, config, now=now)


@app.get("/api/grid/current")
def get_grid_current(now: datetime | None = None) -> dict[str, Any]:
    config = load_config(_config_path())
    engine = get_engine(_database_url())
    now_utc = _normalize_now(now)
    with Session(engine) as session:
        return build_grid_current_payload(session, config, now_utc)


@app.get("/api/grid/history")
def get_grid_history(start: datetime, end: datetime) -> dict[str, Any]:
    config = load_config(_config_path())
    engine = get_engine(_database_url())
    station_timezone = ZoneInfo(config.station.grid.local_timezone)
    start_utc = _normalize_query_datetime(start, station_timezone)
    end_utc = _normalize_query_datetime(end, station_timezone)
    if end_utc < start_utc:
        start_utc, end_utc = end_utc, start_utc
    with Session(engine) as session:
        return build_grid_history_payload(session, start_utc, end_utc)


@app.get("/api/grid/outages")
def get_grid_outages(date: date) -> dict[str, Any]:
    config = load_config(_config_path())
    engine = get_engine(_database_url())
    station_timezone = ZoneInfo(config.station.grid.local_timezone)
    start_utc, end_utc = _grid_local_date_bounds(date, station_timezone)
    with Session(engine) as session:
        return build_grid_outages_payload(
            session,
            date,
            station_timezone,
            start_utc,
            end_utc,
        )


def build_solar_dashboard_payload(
    session: Session,
    config: AppConfig,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = _normalize_now(now)
    station_id = config.station.id
    config_hash = calculate_config_hash(config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)

    current_point = get_nearest_interpolated_solar_for_config(
        session,
        station_id,
        config_hash,
        now_utc,
    )
    weather_row = get_nearest_forecast_for_station(session, station_id, now_utc)
    available_start_utc, available_end_utc = get_interpolated_solar_range(
        session,
        station_id,
        config_hash,
    )

    return {
        "station": {
            "id": station_id,
            "name": config.station.name,
            "timezone": station_timezone.key,
        },
        "available_start_local": _optional_local_iso(
            available_start_utc,
            station_timezone,
        ),
        "available_end_local": _optional_local_iso(
            available_end_utc,
            station_timezone,
        ),
        "current": _current_payload(current_point, now_utc, station_timezone, config),
        "weather": _weather_payload(weather_row, now_utc, config, station_timezone),
        "charts": {
            chart_id: _chart_payload(
                session=session,
                station_id=station_id,
                config_hash=config_hash,
                station_timezone=station_timezone,
                now_utc=now_utc,
                minutes=int(definition["minutes"]),
                resolution_seconds=int(definition["resolution_seconds"]),
            )
            for chart_id, definition in SOLAR_DASHBOARD_CHARTS.items()
        },
    }


def build_solar_weather_current_payload(
    session: Session,
    config: AppConfig,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = _normalize_now(now)
    station_id = config.station.id
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    weather_row = get_nearest_forecast_for_station(session, station_id, now_utc)
    return _weather_payload(weather_row, now_utc, config, station_timezone)


def build_solar_current_payload(
    session: Session,
    config: AppConfig,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = _normalize_now(now)
    station_id = config.station.id
    config_hash = calculate_config_hash(config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    current_point = get_nearest_interpolated_solar_for_config(
        session,
        station_id,
        config_hash,
        now_utc,
    )
    current_payload = _current_payload(current_point, now_utc, station_timezone, config)
    return {
        "timestamp_local": current_payload["timestamp_local"],
        "solar_power_w": current_payload["solar_power_w"],
    }


def build_solar_current_buffer_payload(
    session: Session,
    config: AppConfig,
    now: datetime | None = None,
    seconds: int = 60,
) -> dict[str, Any]:
    now_utc = _normalize_now(now)
    buffer_seconds = min(max(seconds, 10), 120)
    station_id = config.station.id
    config_hash = calculate_config_hash(config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    current_point = get_nearest_interpolated_solar_for_config(
        session,
        station_id,
        config_hash,
        now_utc,
    )
    current_payload = _current_payload(current_point, now_utc, station_timezone, config)
    start_utc = _floor_utc_to_cadence(now_utc, 1)
    end_utc = start_utc + timedelta(seconds=buffer_seconds)
    rows = list_interpolated_solar_for_config(
        session,
        station_id,
        config_hash,
        start_utc=start_utc,
        end_utc=end_utc + timedelta(seconds=1),
    )
    rows_by_timestamp: dict[datetime, InterpolatedSolarProduction] = {}
    for row in rows:
        existing = rows_by_timestamp.get(row.timestamp_utc)
        if existing is None or row.resolution_seconds < existing.resolution_seconds:
            rows_by_timestamp[row.timestamp_utc] = row

    points = [
        _current_buffer_point_payload(timestamp, row, station_timezone, config)
        for timestamp, row in sorted(rows_by_timestamp.items())
        if start_utc <= timestamp <= end_utc
    ]
    return {
        "current": {
            "timestamp_local": current_payload["timestamp_local"],
            "solar_power_w": current_payload["solar_power_w"],
            "pv_voltage_v": current_payload["pv_voltage_v"],
            "pv_current_a": current_payload["pv_current_a"],
        },
        "buffer_start_local": start_utc.astimezone(station_timezone).isoformat(),
        "buffer_end_local": end_utc.astimezone(station_timezone).isoformat(),
        "points": points,
    }


def build_solar_power_history_payload(
    session: Session,
    config: AppConfig,
    start: datetime,
    end: datetime,
    now: datetime | None = None,
) -> dict[str, Any]:
    station_id = config.station.id
    config_hash = calculate_config_hash(config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    requested_start_utc = _normalize_query_datetime(start, station_timezone)
    requested_end_utc = _normalize_query_datetime(end, station_timezone)
    if requested_end_utc < requested_start_utc:
        requested_start_utc, requested_end_utc = requested_end_utc, requested_start_utc

    available_start_utc, available_end_utc = _get_weather_adjusted_solar_range(
        session,
        station_id,
        config_hash,
    )
    history_cap_utc = _history_now_hour_cap_utc(now, station_timezone)
    available_start_utc, available_end_utc = _cap_range_end(
        available_start_utc,
        available_end_utc,
        history_cap_utc,
    )
    if available_start_utc is None or available_end_utc is None:
        clamped_start_utc = requested_start_utc
        clamped_end_utc = requested_start_utc - timedelta(seconds=1)
    else:
        clamped_start_utc, clamped_end_utc = _clamp_requested_range(
            requested_start_utc,
            requested_end_utc,
            available_start_utc,
            available_end_utc,
        )
    max_range = timedelta(days=MAX_POWER_HISTORY_DAYS)
    if clamped_end_utc - clamped_start_utc > max_range:
        preferred_end_utc = clamped_start_utc + max_range
        if preferred_end_utc <= available_end_utc:
            clamped_end_utc = preferred_end_utc
        else:
            clamped_end_utc = available_end_utc
            shifted_start_utc = clamped_end_utc - max_range
            if shifted_start_utc < available_start_utc:
                shifted_start_utc = available_start_utc
            clamped_start_utc = shifted_start_utc

    visual_resolution_seconds = _power_history_resolution_seconds(
        clamped_end_utc - clamped_start_utc
    )
    points: list[dict[str, Any]] = []
    if clamped_end_utc >= clamped_start_utc:
        aligned_start_utc = _ceil_local_to_cadence(
            clamped_start_utc,
            station_timezone,
            visual_resolution_seconds,
        )
        aligned_end_utc = _floor_local_to_cadence(
            clamped_end_utc,
            station_timezone,
            visual_resolution_seconds,
        )
        if aligned_end_utc >= aligned_start_utc:
            source_points = _load_weather_adjusted_source_points(
                session,
                station_id,
                config_hash,
                start_utc=aligned_start_utc - timedelta(seconds=BASE_SOLAR_STEP_SECONDS),
                end_utc=aligned_end_utc + timedelta(seconds=1),
            )
            source_timestamps = [point.timestamp_utc for point in source_points]
            target_utc = aligned_start_utc
            step = timedelta(seconds=visual_resolution_seconds)
            while target_utc <= aligned_end_utc:
                point = _select_power_source_point(
                    source_points,
                    source_timestamps,
                    target_utc,
                )
                if point is not None:
                    points.append(
                        {
                            "timestamp_local": target_utc.astimezone(
                                station_timezone
                            ).isoformat(),
                            "power_w": point.power_w,
                            "source": point.source,
                        }
                    )
                target_utc += step

    actual_start = (
        datetime.fromisoformat(points[0]["timestamp_local"]) if points else None
    )
    actual_end = datetime.fromisoformat(points[-1]["timestamp_local"]) if points else None
    return {
        "metadata": {
            "mode": "power",
            "requested_start_local": requested_start_utc.astimezone(
                station_timezone
            ).isoformat(),
            "requested_end_local": requested_end_utc.astimezone(
                station_timezone
            ).isoformat(),
            "actual_start_local": None if actual_start is None else actual_start.isoformat(),
            "actual_end_local": None if actual_end is None else actual_end.isoformat(),
            "visual_resolution_seconds": visual_resolution_seconds,
            "returned_points": len(points),
            "available_start_local": _optional_local_iso(
                available_start_utc,
                station_timezone,
            ),
            "available_end_local": _optional_local_iso(
                available_end_utc,
                station_timezone,
            ),
            "clamped_start": clamped_start_utc != requested_start_utc,
            "clamped_end": clamped_end_utc != requested_end_utc,
            "max_range_days": MAX_POWER_HISTORY_DAYS,
        },
        "points": points,
    }


def build_solar_daily_energy_history_payload(
    session: Session,
    config: AppConfig,
    start: datetime,
    end: datetime,
    now: datetime | None = None,
) -> dict[str, Any]:
    station_id = config.station.id
    config_hash = calculate_config_hash(config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    requested_start_utc = _normalize_query_datetime(start, station_timezone)
    requested_end_utc = _normalize_query_datetime(end, station_timezone)
    if requested_end_utc < requested_start_utc:
        requested_start_utc, requested_end_utc = requested_end_utc, requested_start_utc

    available_start_utc, available_end_utc = _get_weather_adjusted_solar_range(
        session,
        station_id,
        config_hash,
    )
    history_cap_utc = _history_now_hour_cap_utc(now, station_timezone)
    available_start_utc, available_end_utc = _cap_range_end(
        available_start_utc,
        available_end_utc,
        history_cap_utc,
    )
    if available_start_utc is None or available_end_utc is None:
        clamped_start_utc = requested_start_utc
        clamped_end_utc = requested_start_utc - timedelta(seconds=1)
    else:
        clamped_start_utc, clamped_end_utc = _clamp_requested_range(
            requested_start_utc,
            requested_end_utc,
            available_start_utc,
            available_end_utc,
        )

    points: list[dict[str, Any]] = []
    if clamped_end_utc >= clamped_start_utc:
        start_local_date = clamped_start_utc.astimezone(station_timezone).date()
        end_local_date = clamped_end_utc.astimezone(station_timezone).date()
        load_start_utc = datetime.combine(
            start_local_date,
            datetime.min.time(),
            tzinfo=station_timezone,
        ).astimezone(timezone.utc)
        load_end_utc = (
            datetime.combine(
                end_local_date,
                datetime.min.time(),
                tzinfo=station_timezone,
            )
            + timedelta(days=1)
        ).astimezone(timezone.utc)
        weather_energy = _daily_weather_adjusted_energy_kwh(
            _load_weather_adjusted_source_points(
                session,
                station_id,
                config_hash,
                start_utc=load_start_utc,
                end_utc=load_end_utc,
            )
        )
        current_local_date = _normalize_now(now).astimezone(station_timezone).date()
        current_date = start_local_date
        while current_date <= end_local_date:
            if current_date != current_local_date:
                weather_value = weather_energy.get(current_date)
                if weather_value is not None:
                    points.append(
                        {
                            "date_local": current_date.isoformat(),
                            "weather_adjusted_daily_energy_kwh": weather_value,
                        }
                    )
            current_date += timedelta(days=1)

    actual_start = points[0]["date_local"] if points else None
    actual_end = points[-1]["date_local"] if points else None
    return {
        "metadata": {
            "mode": "daily_energy",
            "requested_start_local": requested_start_utc.astimezone(
                station_timezone
            ).isoformat(),
            "requested_end_local": requested_end_utc.astimezone(
                station_timezone
            ).isoformat(),
            "actual_start_local": actual_start,
            "actual_end_local": actual_end,
            "returned_days": len(points),
            "available_start_local": _optional_local_iso(
                available_start_utc,
                station_timezone,
            ),
            "available_end_local": _optional_local_iso(
                available_end_utc,
                station_timezone,
            ),
            "clamped_start": clamped_start_utc != requested_start_utc,
            "clamped_end": clamped_end_utc != requested_end_utc,
        },
        "points": points,
    }


def build_solar_history_bounds_payload(
    session: Session,
    config: AppConfig,
    now: datetime | None = None,
) -> dict[str, Any]:
    station_id = config.station.id
    config_hash = calculate_config_hash(config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)

    power_start_utc, power_end_utc = _get_weather_adjusted_solar_range(
        session,
        station_id,
        config_hash,
    )
    history_cap_utc = _history_now_hour_cap_utc(now, station_timezone)
    power_start_utc, power_end_utc = _cap_range_end(
        power_start_utc,
        power_end_utc,
        history_cap_utc,
    )
    daily_start_utc, daily_end_utc = power_start_utc, power_end_utc

    return {
        "power_start_local": _optional_local_iso(power_start_utc, station_timezone),
        "power_end_local": _optional_local_iso(power_end_utc, station_timezone),
        "daily_start_local": _optional_local_iso(daily_start_utc, station_timezone),
        "daily_end_local": _optional_local_iso(daily_end_utc, station_timezone),
    }


def build_grid_current_payload(
    session: Session,
    config: AppConfig,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = _normalize_now(now)
    try:
        point = get_nearest_grid_availability_point(session, now_utc)
    except OperationalError:
        point = None
    if point is None:
        return {
            "status": "empty",
            "station": {
                "id": config.station.id,
                "name": config.station.name,
                "timezone": config.station.grid.local_timezone,
            },
            "current": None,
        }
    return {
        "status": "ok",
        "station": {
            "id": config.station.id,
            "name": config.station.name,
            "timezone": config.station.grid.local_timezone,
        },
        "current": _grid_point_payload(point),
    }


def build_grid_history_payload(
    session: Session,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[str, Any]:
    try:
        rows = list_grid_availability_points(session, start_utc, end_utc)
        cache_status = "ok" if rows else "empty_range"
    except OperationalError:
        rows = []
        cache_status = "missing_table"
    return {
        "metadata": {
            "requested_start_utc": start_utc.isoformat(),
            "requested_end_utc": end_utc.isoformat(),
            "returned_points": len(rows),
            "cache_status": cache_status,
        },
        "points": [_grid_point_payload(row) for row in rows],
    }


def build_grid_outages_payload(
    session: Session,
    local_date: date,
    station_timezone: ZoneInfo,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[str, Any]:
    try:
        rows = list_grid_availability_points(session, start_utc, end_utc)
        cache_status = "ok" if rows else "empty_range"
    except OperationalError:
        rows = []
        cache_status = "missing_table"
    return {
        "date_local": local_date.isoformat(),
        "timezone": station_timezone.key,
        "cache_status": cache_status,
        "outage_queue": rows[0].outage_queue if rows else None,
        "daily_outage_hours": rows[0].daily_outage_hours if rows else 0.0,
        "windows": _grid_outage_windows_payload(rows, local_date, station_timezone),
    }


def _grid_point_payload(row: GridAvailabilityPointRecord) -> dict[str, Any]:
    return {
        "timestamp_utc": row.timestamp_utc.isoformat(),
        "timestamp_local": row.timestamp_local.isoformat(),
        "generation_health_percent": row.generation_health_percent,
        "delivery_health_percent": row.delivery_health_percent,
        "effective_health_percent": row.effective_health_percent,
        "national_deficit_percent": row.deficit_percent,
        "deficit_percent": row.deficit_percent,
        "daily_outage_hours": row.daily_outage_hours,
        "outage_level": row.outage_level,
        "outage_queue": row.outage_queue,
        "local_grid_available": row.local_grid_available,
        "is_outage_now": row.is_outage_now,
        "grid_voltage_v": row.grid_voltage_v,
        "reason": row.reason,
        "current_outage_window_start": _optional_iso(
            row.current_outage_window_start_utc,
        ),
        "current_outage_window_end": _optional_iso(
            row.current_outage_window_end_utc,
        ),
        "next_outage_window_start": _optional_iso(row.next_outage_window_start_utc),
        "next_outage_window_end": _optional_iso(row.next_outage_window_end_utc),
    }


def _grid_outage_windows_payload(
    rows: list[GridAvailabilityPointRecord],
    local_date: date,
    station_timezone: ZoneInfo,
) -> list[dict[str, Any]]:
    windows: dict[tuple[datetime, datetime], dict[str, Any]] = {}
    for row in rows:
        for start, end in (
            (row.current_outage_window_start_utc, row.current_outage_window_end_utc),
            (row.next_outage_window_start_utc, row.next_outage_window_end_utc),
        ):
            if start is None or end is None:
                continue
            if not _grid_window_overlaps_local_date(
                start,
                end,
                local_date,
                station_timezone,
            ):
                continue
            key = (start, end)
            windows[key] = {
                "start_utc": start.isoformat(),
                "end_utc": end.isoformat(),
                "start_local": start.astimezone(station_timezone).isoformat(),
                "end_local": end.astimezone(station_timezone).isoformat(),
            }
    return [
        windows[key]
        for key in sorted(windows, key=lambda value: value[0])
    ]


def _grid_window_overlaps_local_date(
    start_utc: datetime,
    end_utc: datetime,
    local_date: date,
    station_timezone: ZoneInfo,
) -> bool:
    day_start_local = datetime.combine(
        local_date,
        time.min,
        tzinfo=station_timezone,
    )
    day_end_local = day_start_local + timedelta(days=1)
    start_local = start_utc.astimezone(station_timezone)
    end_local = end_utc.astimezone(station_timezone)
    return start_local < day_end_local and end_local > day_start_local


def _grid_local_date_bounds(
    local_date: date,
    station_timezone: ZoneInfo,
) -> tuple[datetime, datetime]:
    start_local = datetime.combine(local_date, time.min, tzinfo=station_timezone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _current_payload(
    point: InterpolatedSolarProduction | None,
    now_utc: datetime,
    station_timezone: ZoneInfo,
    config: AppConfig,
) -> dict[str, Any]:
    timestamp_local = (
        point.timestamp_local
        if point is not None
        else now_utc.astimezone(station_timezone)
    )
    solar_power_w = None if point is None else point.power_w
    operating_point = (
        None
        if solar_power_w is None
        else estimate_pv_array_operating_point(solar_power_w, config)
    )
    return {
        "timestamp_local": timestamp_local.isoformat(),
        "solar_power_w": solar_power_w,
        "pv_voltage_v": (
            None if operating_point is None else operating_point.voltage_v
        ),
        "pv_current_a": (
            None if operating_point is None else operating_point.current_a
        ),
    }


def _current_buffer_point_payload(
    timestamp: datetime,
    row: InterpolatedSolarProduction,
    station_timezone: ZoneInfo,
    config: AppConfig,
) -> dict[str, Any]:
    operating_point = estimate_pv_array_operating_point(row.power_w, config)
    return {
        "timestamp_utc": timestamp.isoformat(),
        "timestamp_local": timestamp.astimezone(station_timezone).isoformat(),
        "solar_power_w": row.power_w,
        "pv_voltage_v": operating_point.voltage_v,
        "pv_current_a": operating_point.current_a,
    }


def _weather_payload(
    weather_row: object | None,
    now_utc: datetime,
    config: AppConfig,
    station_timezone: ZoneInfo,
) -> dict[str, Any]:
    local_today = now_utc.astimezone(station_timezone).date()
    sunrise_local, sunset_local = _station_sun_times(config, local_today)
    if weather_row is None:
        return {
            "timestamp_local": now_utc.astimezone(station_timezone).isoformat(),
            "cloud_cover_percent": None,
            "weather_code": None,
            "weather_state": "unknown",
            "weather_label": WEATHER_LABELS_UK["unknown"],
            "temperature_c": None,
            "sunrise_local": _optional_iso(sunrise_local),
            "sunset_local": _optional_iso(sunset_local),
            "sunrise": _optional_iso(sunrise_local),
            "sunset": _optional_iso(sunset_local),
        }

    weather_code = getattr(weather_row, "weather_code")
    weather_state = map_weather_code_to_state(weather_code)
    return {
        "timestamp_local": weather_row.forecast_timestamp_local.isoformat(),
        "cloud_cover_percent": weather_row.cloud_cover_percent,
        "weather_code": weather_code,
        "weather_state": weather_state,
        "weather_label": WEATHER_LABELS_UK.get(weather_state, WEATHER_LABELS_UK["unknown"]),
        "temperature_c": getattr(weather_row, "temperature_c", None),
        "sunrise_local": _optional_iso(sunrise_local),
        "sunset_local": _optional_iso(sunset_local),
        "sunrise": _optional_iso(sunrise_local),
        "sunset": _optional_iso(sunset_local),
    }


def _chart_payload(
    session: Session,
    station_id: str,
    config_hash: str,
    station_timezone: ZoneInfo,
    now_utc: datetime,
    minutes: int,
    resolution_seconds: int,
) -> dict[str, Any]:
    aligned_end_utc = _floor_utc_to_cadence(now_utc, resolution_seconds)
    start_utc = aligned_end_utc - timedelta(minutes=minutes)
    rows_by_resolution = _load_chart_source_rows(
        session=session,
        station_id=station_id,
        config_hash=config_hash,
        start_utc=start_utc - timedelta(seconds=resolution_seconds),
        end_utc=aligned_end_utc + timedelta(seconds=1),
        visual_resolution_seconds=resolution_seconds,
    )
    source_timestamps_by_resolution = {
        resolution: [row.timestamp_utc for row in rows]
        for resolution, rows in rows_by_resolution.items()
    }

    points: list[dict[str, Any]] = []
    used_resolutions: list[int] = []
    target_utc = start_utc
    step = timedelta(seconds=resolution_seconds)
    while target_utc <= aligned_end_utc:
        row = _select_chart_source_row(
            rows_by_resolution,
            source_timestamps_by_resolution,
            target_utc,
            resolution_seconds,
        )
        if row is not None:
            if row.resolution_seconds not in used_resolutions:
                used_resolutions.append(row.resolution_seconds)
            points.append(
                _chart_point_payload(
                    row,
                    target_utc,
                    station_timezone,
                    visual_resolution_seconds=resolution_seconds,
                )
            )
        target_utc += step

    cache_status = "ok"
    if not points:
        cache_status = "empty_range"
    elif len(points) < 2:
        cache_status = "insufficient_data"

    actual_start = (
        datetime.fromisoformat(points[0]["timestamp_utc"]) if points else None
    )
    actual_end = datetime.fromisoformat(points[-1]["timestamp_utc"]) if points else None
    return {
        "metadata": {
            "requested_start_local": start_utc.astimezone(station_timezone).isoformat(),
            "requested_end_local": aligned_end_utc.astimezone(
                station_timezone
            ).isoformat(),
            "actual_start_local": _optional_local_iso(actual_start, station_timezone),
            "actual_end_local": _optional_local_iso(actual_end, station_timezone),
            "visual_resolution_seconds": resolution_seconds,
            "source_resolutions_used": used_resolutions,
            "returned_points": len(points),
            "cache_status": cache_status,
        },
        "points": points,
    }


def _load_chart_source_rows(
    session: Session,
    station_id: str,
    config_hash: str,
    start_utc: datetime,
    end_utc: datetime,
    visual_resolution_seconds: int,
) -> dict[int, list[InterpolatedSolarProduction]]:
    rows = list_interpolated_solar_for_config(
        session,
        station_id,
        config_hash,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    rows_by_resolution = {
        resolution: []
        for resolution in CACHE_RESOLUTION_SECONDS
        if resolution <= visual_resolution_seconds
    }
    for row in rows:
        if row.resolution_seconds in rows_by_resolution:
            rows_by_resolution[row.resolution_seconds].append(row)
    for resolution_rows in rows_by_resolution.values():
        resolution_rows.sort(key=lambda point: point.timestamp_utc)
    return rows_by_resolution


def _select_chart_source_row(
    rows_by_resolution: dict[int, list[InterpolatedSolarProduction]],
    timestamps_by_resolution: dict[int, list[datetime]],
    target_utc: datetime,
    visual_resolution_seconds: int,
) -> InterpolatedSolarProduction | None:
    max_age = timedelta(seconds=visual_resolution_seconds)
    for resolution in sorted(rows_by_resolution):
        timestamps = timestamps_by_resolution[resolution]
        index = bisect_right(timestamps, target_utc) - 1
        if index < 0:
            continue
        row = rows_by_resolution[resolution][index]
        age = target_utc - row.timestamp_utc
        if age == timedelta(0) or age < max_age:
            return row
    return None


def _chart_point_payload(
    row: InterpolatedSolarProduction,
    target_utc: datetime,
    station_timezone: ZoneInfo,
    visual_resolution_seconds: int,
) -> dict[str, Any]:
    return {
        "timestamp_utc": target_utc.isoformat(),
        "timestamp_local": target_utc.astimezone(station_timezone).isoformat(),
        "power_w": row.power_w,
        "source": row.source_type,
        "resolution_seconds": visual_resolution_seconds,
        "source_resolution_seconds": row.resolution_seconds,
    }


def _load_weather_adjusted_source_points(
    session: Session,
    station_id: str,
    config_hash: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[PowerSourcePoint]:
    points_by_timestamp: dict[datetime, PowerSourcePoint] = {}
    for row in list_forecast_solar_for_config(
        session,
        station_id,
        config_hash,
        start_utc=start_utc,
        end_utc=end_utc,
    ):
        points_by_timestamp[row.timestamp_utc] = _forecast_power_source_point(row)
    for row in list_simulated_solar_for_config(
        session,
        station_id,
        config_hash,
        start_utc=start_utc,
        end_utc=end_utc,
    ):
        points_by_timestamp[row.timestamp_utc] = _simulated_power_source_point(row)
    return [
        points_by_timestamp[timestamp]
        for timestamp in sorted(points_by_timestamp)
    ]


def _simulated_power_source_point(row: SimulatedSolarProduction) -> PowerSourcePoint:
    return PowerSourcePoint(
        timestamp_utc=row.timestamp_utc,
        timestamp_local=row.timestamp_local,
        power_w=row.simulated_power_w,
        source="historical",
    )


def _forecast_power_source_point(row: ForecastSolarProduction) -> PowerSourcePoint:
    return PowerSourcePoint(
        timestamp_utc=row.timestamp_utc,
        timestamp_local=row.timestamp_local,
        power_w=row.forecast_power_w,
        source="forecast",
    )


def _select_power_source_point(
    points: list[PowerSourcePoint],
    timestamps: list[datetime],
    target_utc: datetime,
) -> PowerSourcePoint | None:
    index = bisect_right(timestamps, target_utc) - 1
    if index < 0:
        return None
    point = points[index]
    age = target_utc - point.timestamp_utc
    if age == timedelta(0) or age < timedelta(seconds=BASE_SOLAR_STEP_SECONDS):
        return point
    return None


def _daily_weather_adjusted_energy_kwh(
    points: list[PowerSourcePoint],
) -> dict[Any, float]:
    energy_by_date: dict[Any, float] = {}
    for point in points:
        local_date = point.timestamp_local.date()
        energy_by_date[local_date] = energy_by_date.get(local_date, 0.0) + (
            point.power_w * BASE_SOLAR_STEP_SECONDS / 3600.0 / 1000.0
        )
    return {local_date: round(value, 4) for local_date, value in energy_by_date.items()}


def _power_history_resolution_seconds(duration: timedelta) -> int:
    if duration <= timedelta(days=1):
        return 5 * 60
    if duration <= timedelta(days=7):
        return 15 * 60
    if duration <= timedelta(days=12):
        return 30 * 60
    if duration <= timedelta(days=15):
        return 45 * 60
    if duration <= timedelta(days=21):
        return 60 * 60
    if duration <= timedelta(days=25):
        return 75 * 60
    return 90 * 60


def _get_weather_adjusted_solar_range(
    session: Session,
    station_id: str,
    config_hash: str,
) -> tuple[datetime | None, datetime | None]:
    simulated_range = get_simulated_solar_range(session, station_id, config_hash)
    forecast_range = get_forecast_solar_range(session, station_id, config_hash)
    return _combine_ranges(simulated_range, forecast_range)


def _combine_ranges(
    *ranges: tuple[datetime | None, datetime | None],
) -> tuple[datetime | None, datetime | None]:
    starts = [start for start, _ in ranges if start is not None]
    ends = [end for _, end in ranges if end is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _history_now_hour_cap_utc(
    now: datetime | None,
    station_timezone: ZoneInfo,
) -> datetime:
    current_local = _normalize_now(now).astimezone(station_timezone)
    return current_local.replace(minute=0, second=0, microsecond=0).astimezone(
        timezone.utc
    )


def _cap_range_end(
    available_start_utc: datetime | None,
    available_end_utc: datetime | None,
    max_end_utc: datetime,
) -> tuple[datetime | None, datetime | None]:
    if available_start_utc is None or available_end_utc is None:
        return None, None
    capped_end_utc = min(available_end_utc, max_end_utc)
    if available_start_utc > capped_end_utc:
        return None, None
    return available_start_utc, capped_end_utc


def _station_sun_times(
    config: AppConfig,
    local_date: date,
) -> tuple[datetime | None, datetime | None]:
    installation = config.station.solar.installation
    station_timezone = ZoneInfo(installation.timezone)
    noon_local = datetime.combine(local_date, time(12, 0), tzinfo=station_timezone)
    times = pd.DatetimeIndex([noon_local])
    sun_times = pvlib.solarposition.sun_rise_set_transit_spa(
        times,
        installation.latitude,
        installation.longitude,
    )
    row = sun_times.iloc[0]
    return (
        _pandas_datetime_to_python(row.get("sunrise")),
        _pandas_datetime_to_python(row.get("sunset")),
    )


def _pandas_datetime_to_python(value: Any) -> datetime | None:
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "to_pydatetime"):
        return value.round("us").to_pydatetime()
    if isinstance(value, datetime):
        return value
    return None


def _clamp_requested_range(
    requested_start_utc: datetime,
    requested_end_utc: datetime,
    available_start_utc: datetime | None,
    available_end_utc: datetime | None,
) -> tuple[datetime, datetime]:
    start_utc = requested_start_utc
    end_utc = requested_end_utc
    if available_start_utc is not None and start_utc < available_start_utc:
        start_utc = available_start_utc
    if available_end_utc is not None and end_utc > available_end_utc:
        end_utc = available_end_utc
    return start_utc, end_utc


def _floor_utc_to_cadence(value: datetime, cadence_seconds: int) -> datetime:
    value_utc = value.astimezone(timezone.utc)
    timestamp_seconds = int(value_utc.timestamp())
    aligned_seconds = timestamp_seconds - (timestamp_seconds % cadence_seconds)
    return datetime.fromtimestamp(aligned_seconds, tz=timezone.utc)


def _floor_local_to_cadence(
    value_utc: datetime,
    station_timezone: ZoneInfo,
    cadence_seconds: int,
) -> datetime:
    local_value = value_utc.astimezone(station_timezone)
    day_start = local_value.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_since_midnight = (
        local_value.hour * 3600 + local_value.minute * 60 + local_value.second
    )
    aligned_seconds = seconds_since_midnight - (
        seconds_since_midnight % cadence_seconds
    )
    return (day_start + timedelta(seconds=aligned_seconds)).astimezone(timezone.utc)


def _ceil_local_to_cadence(
    value_utc: datetime,
    station_timezone: ZoneInfo,
    cadence_seconds: int,
) -> datetime:
    floored = _floor_local_to_cadence(value_utc, station_timezone, cadence_seconds)
    if floored == value_utc.astimezone(timezone.utc):
        return floored
    return (
        floored.astimezone(station_timezone) + timedelta(seconds=cadence_seconds)
    ).astimezone(timezone.utc)


def _normalize_query_datetime(value: datetime, station_timezone: ZoneInfo) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=station_timezone).astimezone(timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _optional_local_iso(
    value: datetime | None,
    station_timezone: ZoneInfo,
) -> str | None:
    if value is None:
        return None
    return value.astimezone(station_timezone).isoformat()


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _config_path() -> Path:
    return Path(os.environ.get("SMARTENERGY_CONFIG_PATH", DEFAULT_CONFIG_PATH))


def _database_url() -> str:
    return os.environ.get("SMARTENERGY_DATABASE_URL", DEFAULT_DATABASE_URL)


def _normalize_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc).replace(microsecond=0)
