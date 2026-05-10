from __future__ import annotations

import argparse
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as datetime_time, timezone
from pathlib import Path
from typing import Callable, Literal, Sequence
from zoneinfo import ZoneInfo

from sqlmodel import Session


SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = Path(__file__).resolve().parents[1]
for path in (SCRIPTS_DIR, BACKEND_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import update_weather_cache

from app.config_loader import calculate_config_hash, load_config
from app.schemas import AppConfig
from app.simulation.solar import IdealSolarGenerator, IdealSolarPoint
from app.simulation.weather import (
    calculate_weather_factor,
    generate_weather_adjusted_solar,
    map_weather_code_to_state,
)
from app.storage.database import create_db_and_tables, get_engine
from app.storage.forecast_repository import WeatherForecast, list_forecast_for_station
from app.storage.forecast_solar_repository import (
    ForecastSolarProduction,
    delete_forecast_solar_for_config,
    list_forecast_solar_for_config,
    save_forecast_solar_points,
)
from app.storage.simulated_solar_repository import (
    delete_simulated_solar_for_config,
    list_simulated_solar_for_config,
    save_simulated_solar_points,
)
from app.storage.solar_repository import (
    IdealSolarProduction,
    delete_ideal_solar_for_config,
    list_ideal_solar_for_config,
    save_ideal_solar_points,
)
from app.storage.weather_repository import list_weather_observations


DEFAULT_CONFIG_PATH = Path("backend/config/station.default.yaml")
DEFAULT_DATABASE_URL = "sqlite:///backend/data/smartenergy.db"
DEFAULT_HISTORY_START = date(2025, 10, 6)
DEFAULT_DAYS_AHEAD = 2
DEFAULT_INTERVAL_HOURS = 12.0
SOLAR_TIMESTEP_MINUTES = 15
WEATHER_TIMESTEP_MINUTES = 60
SECONDS_PER_HOUR = 60 * 60
MIN_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_UTC = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


@dataclass(frozen=True)
class SchedulerSettings:
    config: Path
    database_url: str
    history_start: date
    days_ahead: int
    interval_hours: float


@dataclass(frozen=True)
class IdealSolarMaintenanceSummary:
    start_utc: datetime
    end_utc: datetime
    rows: int
    regenerated: bool


@dataclass(frozen=True)
class AdjustedSolarMaintenanceSummary:
    start_utc: datetime | None
    end_utc: datetime | None
    rows: int
    regenerated: bool


@dataclass(frozen=True)
class SolarDataMaintenanceSummary:
    station_id: str
    config_hash: str
    timezone_name: str
    current_local_date: date
    ideal_solar: IdealSolarMaintenanceSummary
    weather_cache: update_weather_cache.WeatherCacheSummary
    historical_adjusted_solar: AdjustedSolarMaintenanceSummary
    forecast_adjusted_solar: AdjustedSolarMaintenanceSummary


MaintenanceRunner = Callable[
    [Path, str | None, date, int],
    SolarDataMaintenanceSummary,
]
SleepFunction = Callable[[float], None]
CadenceMode = Literal["utc", "local_wall_time"]


def main() -> None:
    settings = parse_args()
    run_forever(settings)


def parse_args(argv: Sequence[str] | None = None) -> SchedulerSettings:
    parser = argparse.ArgumentParser(
        description="Maintain ideal, weather, historical solar, and forecast solar data.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument(
        "--history-start",
        type=_parse_date,
        default=DEFAULT_HISTORY_START,
    )
    parser.add_argument("--days-ahead", type=int, default=DEFAULT_DAYS_AHEAD)
    parser.add_argument(
        "--interval-hours",
        type=float,
        default=DEFAULT_INTERVAL_HOURS,
    )
    args = parser.parse_args(argv)

    if args.days_ahead < 0:
        parser.error("--days-ahead must be 0 or greater")
    if args.interval_hours <= 0:
        parser.error("--interval-hours must be greater than 0")

    return SchedulerSettings(
        config=args.config,
        database_url=args.database_url,
        history_start=args.history_start,
        days_ahead=args.days_ahead,
        interval_hours=args.interval_hours,
    )


def run_forever(
    settings: SchedulerSettings,
    maintenance_runner: MaintenanceRunner | None = None,
    sleep: SleepFunction = time.sleep,
) -> None:
    if maintenance_runner is None:
        maintenance_runner = run_solar_data_maintenance
    interval_seconds = settings.interval_hours * SECONDS_PER_HOUR
    while True:
        run_once(settings, maintenance_runner)
        _log(f"solar data scheduler sleeping for {settings.interval_hours:g} hours")
        sleep(interval_seconds)


def run_once(
    settings: SchedulerSettings,
    maintenance_runner: MaintenanceRunner | None = None,
) -> bool:
    if maintenance_runner is None:
        maintenance_runner = run_solar_data_maintenance
    _log(
        "solar data maintenance started "
        f"config={settings.config} "
        f"database_url={settings.database_url} "
        f"history_start={settings.history_start.isoformat()} "
        f"days_ahead={settings.days_ahead}"
    )
    try:
        summary = maintenance_runner(
            settings.config,
            settings.database_url,
            settings.history_start,
            settings.days_ahead,
        )
    except Exception as exc:
        _log_error(f"solar data maintenance failed: {exc}")
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return False

    _log(
        "solar data maintenance completed "
        f"station_id={summary.station_id} "
        f"current_local_date={summary.current_local_date.isoformat()} "
        f"ideal_rows={summary.ideal_solar.rows} "
        f"ideal_regenerated={summary.ideal_solar.regenerated} "
        f"weather_historical_rows={summary.weather_cache.historical_rows_inserted} "
        f"weather_forecast_rows={summary.weather_cache.forecast_rows_inserted} "
        f"historical_solar_rows={summary.historical_adjusted_solar.rows} "
        f"historical_solar_regenerated={summary.historical_adjusted_solar.regenerated} "
        f"forecast_solar_rows={summary.forecast_adjusted_solar.rows} "
        f"forecast_solar_regenerated={summary.forecast_adjusted_solar.regenerated}"
    )
    return True


def run_solar_data_maintenance(
    config_path: Path,
    database_url: str | None,
    history_start: date,
    days_ahead: int,
    now: datetime | None = None,
) -> SolarDataMaintenanceSummary:
    if days_ahead < 0:
        raise ValueError("days_ahead must be 0 or greater")

    config = load_config(config_path)
    station_id = config.station.id
    config_hash = calculate_config_hash(config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    current_local_date = _resolve_current_local_date(station_timezone, now)
    ideal_start_local, ideal_end_local = _required_ideal_solar_range(
        history_start,
        current_local_date,
        days_ahead,
        station_timezone,
    )

    engine = get_engine(database_url)
    create_db_and_tables(engine)
    with Session(engine) as session:
        ideal_summary = ensure_ideal_solar_coverage(
            session=session,
            config=config,
            station_id=station_id,
            config_hash=config_hash,
            start_local=ideal_start_local,
            end_local=ideal_end_local,
        )

    weather_summary = update_weather_cache.update_weather_cache(
        config_path=config_path,
        database_url=database_url,
        history_start=history_start,
        days_ahead=days_ahead,
        now=now,
    )

    with Session(engine) as session:
        historical_summary = ensure_historical_adjusted_solar_coverage(
            session=session,
            station_id=station_id,
            config_hash=config_hash,
            station_timezone=station_timezone,
            history_start=history_start,
            current_local_date=current_local_date,
        )
        forecast_summary = regenerate_forecast_adjusted_solar(
            session=session,
            station_id=station_id,
            config_hash=config_hash,
        )

    return SolarDataMaintenanceSummary(
        station_id=station_id,
        config_hash=config_hash,
        timezone_name=station_timezone.key,
        current_local_date=current_local_date,
        ideal_solar=ideal_summary,
        weather_cache=weather_summary,
        historical_adjusted_solar=historical_summary,
        forecast_adjusted_solar=forecast_summary,
    )


def ensure_ideal_solar_coverage(
    session: Session,
    config: AppConfig,
    station_id: str,
    config_hash: str,
    start_local: datetime,
    end_local: datetime,
) -> IdealSolarMaintenanceSummary:
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    existing_points = list_ideal_solar_for_config(
        session,
        station_id,
        config_hash,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    if _has_complete_coverage(
        existing_points,
        timestamp_attr="timestamp_utc",
        start_utc=start_utc,
        end_utc=end_utc,
        timestep_minutes=SOLAR_TIMESTEP_MINUTES,
    ):
        return IdealSolarMaintenanceSummary(
            start_utc=start_utc,
            end_utc=end_utc,
            rows=len(existing_points),
            regenerated=False,
        )

    generator = IdealSolarGenerator(config)
    generated_points = generator.generate(
        start_local,
        end_local,
        SOLAR_TIMESTEP_MINUTES,
    )
    rows = [
        _to_ideal_solar_production(point, station_id, config_hash)
        for point in generated_points
    ]
    delete_ideal_solar_for_config(session, station_id, config_hash)
    save_ideal_solar_points(session, rows)
    return IdealSolarMaintenanceSummary(
        start_utc=start_utc,
        end_utc=end_utc,
        rows=len(rows),
        regenerated=True,
    )


def ensure_historical_adjusted_solar_coverage(
    session: Session,
    station_id: str,
    config_hash: str,
    station_timezone: ZoneInfo,
    history_start: date,
    current_local_date: date,
) -> AdjustedSolarMaintenanceSummary:
    yesterday = current_local_date - timedelta(days=1)
    if yesterday < history_start:
        return AdjustedSolarMaintenanceSummary(
            start_utc=None,
            end_utc=None,
            rows=0,
            regenerated=False,
        )

    start_utc, end_utc = _date_range_to_utc_bounds(
        history_start,
        yesterday,
        station_timezone,
    )
    existing_points = list_simulated_solar_for_config(
        session,
        station_id,
        config_hash,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    if _has_complete_coverage(
        existing_points,
        timestamp_attr="timestamp_utc",
        start_utc=start_utc,
        end_utc=end_utc,
        timestep_minutes=SOLAR_TIMESTEP_MINUTES,
    ):
        return AdjustedSolarMaintenanceSummary(
            start_utc=start_utc,
            end_utc=end_utc,
            rows=len(existing_points),
            regenerated=False,
        )

    ideal_points = list_ideal_solar_for_config(
        session,
        station_id,
        config_hash,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    weather_observations = list_weather_observations(
        session,
        station_id,
        start_utc,
        end_utc,
    )
    _validate_complete_coverage(
        ideal_points,
        timestamp_attr="timestamp_utc",
        start_utc=start_utc,
        end_utc=end_utc,
        timestep_minutes=SOLAR_TIMESTEP_MINUTES,
        label="Ideal solar data",
    )
    _validate_complete_coverage(
        weather_observations,
        timestamp_attr="timestamp_utc",
        local_timestamp_attr="timestamp_local",
        start_utc=start_utc,
        end_utc=end_utc,
        start_local=datetime.combine(
            history_start,
            datetime_time.min,
            tzinfo=station_timezone,
        ),
        end_local=datetime.combine(
            yesterday + timedelta(days=1),
            datetime_time.min,
            tzinfo=station_timezone,
        ),
        timestep_minutes=WEATHER_TIMESTEP_MINUTES,
        label="Historical weather data",
        cadence_mode="local_wall_time",
    )

    simulated_points = generate_weather_adjusted_solar(
        ideal_points,
        weather_observations,
    )
    delete_simulated_solar_for_config(
        session,
        station_id,
        config_hash,
        start_utc,
        end_utc,
    )
    save_simulated_solar_points(session, simulated_points)
    return AdjustedSolarMaintenanceSummary(
        start_utc=start_utc,
        end_utc=end_utc,
        rows=len(simulated_points),
        regenerated=True,
    )


def regenerate_forecast_adjusted_solar(
    session: Session,
    station_id: str,
    config_hash: str,
) -> AdjustedSolarMaintenanceSummary:
    forecasts = list_forecast_for_station(session, station_id)
    if not forecasts:
        raise RuntimeError("Forecast weather cache is empty; cannot generate forecast solar")

    first_forecast = forecasts[0]
    last_forecast = forecasts[-1]
    start_utc = first_forecast.forecast_timestamp_utc.astimezone(timezone.utc)
    end_utc = last_forecast.forecast_timestamp_utc.astimezone(timezone.utc) + timedelta(
        minutes=last_forecast.resolution_minutes,
    )

    ideal_points = list_ideal_solar_for_config(
        session,
        station_id,
        config_hash,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    _validate_complete_coverage(
        ideal_points,
        timestamp_attr="timestamp_utc",
        start_utc=start_utc,
        end_utc=end_utc,
        timestep_minutes=SOLAR_TIMESTEP_MINUTES,
        label="Ideal solar data",
    )
    _validate_complete_coverage(
        forecasts,
        timestamp_attr="forecast_timestamp_utc",
        local_timestamp_attr="forecast_timestamp_local",
        start_utc=start_utc,
        end_utc=end_utc,
        start_local=first_forecast.forecast_timestamp_local,
        end_local=last_forecast.forecast_timestamp_local
        + timedelta(minutes=last_forecast.resolution_minutes),
        timestep_minutes=WEATHER_TIMESTEP_MINUTES,
        label="Forecast weather data",
        cadence_mode="local_wall_time",
    )

    forecast_solar_points = generate_forecast_adjusted_solar(
        ideal_points,
        forecasts,
    )
    delete_forecast_solar_for_config(
        session,
        station_id,
        config_hash,
        MIN_UTC,
        MAX_UTC,
    )
    save_forecast_solar_points(session, forecast_solar_points)
    return AdjustedSolarMaintenanceSummary(
        start_utc=start_utc,
        end_utc=end_utc,
        rows=len(forecast_solar_points),
        regenerated=True,
    )


def generate_forecast_adjusted_solar(
    ideal_points: list[IdealSolarProduction],
    weather_forecasts: list[WeatherForecast],
    seed: int = 42,
) -> list[ForecastSolarProduction]:
    if not ideal_points:
        return []
    if not weather_forecasts:
        raise ValueError("weather_forecasts must not be empty")

    sorted_ideal_points = sorted(ideal_points, key=lambda point: point.timestamp_utc)
    sorted_forecasts = sorted(
        weather_forecasts,
        key=lambda forecast: forecast.forecast_timestamp_utc,
    )

    forecast_index = 0
    forecast_solar_points: list[ForecastSolarProduction] = []
    for ideal_point in sorted_ideal_points:
        while (
            forecast_index + 1 < len(sorted_forecasts)
            and sorted_forecasts[forecast_index + 1].forecast_timestamp_utc
            <= ideal_point.timestamp_utc
        ):
            forecast_index += 1

        forecast = sorted_forecasts[forecast_index]
        if forecast.forecast_timestamp_utc > ideal_point.timestamp_utc:
            raise ValueError("forecast weather does not cover the first ideal timestamp")

        timestamp_key = ideal_point.timestamp_utc.isoformat()
        weather_factor = calculate_weather_factor(
            weather_code=forecast.weather_code,
            cloud_cover_percent=forecast.cloud_cover_percent,
            timestamp_key=timestamp_key,
            seed=seed,
        )
        forecast_power_w = (
            0.0
            if ideal_point.ideal_power_w <= 0
            else min(ideal_point.ideal_power_w, ideal_point.ideal_power_w * weather_factor)
        )

        forecast_solar_points.append(
            ForecastSolarProduction(
                station_id=ideal_point.station_id,
                config_hash=ideal_point.config_hash,
                timestamp_utc=ideal_point.timestamp_utc,
                timestamp_local=ideal_point.timestamp_local,
                ideal_power_w=ideal_point.ideal_power_w,
                weather_code=forecast.weather_code,
                weather_state=map_weather_code_to_state(forecast.weather_code),
                cloud_cover_percent=forecast.cloud_cover_percent,
                weather_factor=weather_factor,
                forecast_power_w=forecast_power_w,
            )
        )

    return forecast_solar_points


def _to_ideal_solar_production(
    point: IdealSolarPoint,
    station_id: str,
    config_hash: str,
) -> IdealSolarProduction:
    return IdealSolarProduction(
        station_id=station_id,
        config_hash=config_hash,
        timestamp_utc=point.timestamp_utc,
        timestamp_local=point.timestamp_local,
        sun_elevation_deg=point.sun_elevation_deg,
        sun_azimuth_deg=point.sun_azimuth_deg,
        incidence_factor=point.incidence_factor,
        ambient_factor=point.ambient_factor,
        direct_power_w=point.direct_power_w,
        ambient_power_w=point.ambient_power_w,
        ideal_power_w=point.ideal_power_w,
    )


def _required_ideal_solar_range(
    history_start: date,
    current_local_date: date,
    days_ahead: int,
    station_timezone: ZoneInfo,
) -> tuple[datetime, datetime]:
    forecast_end_date = current_local_date + timedelta(days=days_ahead)
    end_year = max(current_local_date, forecast_end_date).year + 1
    return (
        datetime.combine(history_start, datetime_time.min, tzinfo=station_timezone),
        datetime(end_year, 1, 1, tzinfo=station_timezone),
    )


def _date_range_to_utc_bounds(
    start_date: date,
    end_date: date,
    station_timezone: ZoneInfo,
) -> tuple[datetime, datetime]:
    start_local = datetime.combine(start_date, datetime_time.min, tzinfo=station_timezone)
    end_local = datetime.combine(
        end_date + timedelta(days=1),
        datetime_time.min,
        tzinfo=station_timezone,
    )
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _resolve_current_local_date(
    station_timezone: ZoneInfo,
    now: datetime | None = None,
) -> date:
    if now is None:
        return datetime.now(station_timezone).date()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(station_timezone).date()


def _validate_complete_coverage(
    rows: list[object],
    timestamp_attr: str,
    start_utc: datetime,
    end_utc: datetime,
    timestep_minutes: int,
    label: str,
    cadence_mode: CadenceMode = "utc",
    local_timestamp_attr: str | None = None,
    start_local: datetime | None = None,
    end_local: datetime | None = None,
) -> None:
    validation_error = _coverage_validation_error(
        rows,
        timestamp_attr=timestamp_attr,
        start_utc=start_utc,
        end_utc=end_utc,
        timestep_minutes=timestep_minutes,
        label=label,
        cadence_mode=cadence_mode,
        local_timestamp_attr=local_timestamp_attr,
        start_local=start_local,
        end_local=end_local,
    )
    if validation_error is not None:
        raise RuntimeError(validation_error)


def _has_complete_coverage(
    rows: list[object],
    timestamp_attr: str,
    start_utc: datetime,
    end_utc: datetime,
    timestep_minutes: int,
    cadence_mode: CadenceMode = "utc",
    local_timestamp_attr: str | None = None,
    start_local: datetime | None = None,
    end_local: datetime | None = None,
) -> bool:
    return (
        _coverage_validation_error(
            rows,
            timestamp_attr=timestamp_attr,
            start_utc=start_utc,
            end_utc=end_utc,
            timestep_minutes=timestep_minutes,
            label="Data",
            cadence_mode=cadence_mode,
            local_timestamp_attr=local_timestamp_attr,
            start_local=start_local,
            end_local=end_local,
        )
        is None
    )


def _coverage_validation_error(
    rows: list[object],
    timestamp_attr: str,
    start_utc: datetime,
    end_utc: datetime,
    timestep_minutes: int,
    label: str,
    cadence_mode: CadenceMode,
    local_timestamp_attr: str | None,
    start_local: datetime | None,
    end_local: datetime | None,
) -> str | None:
    if cadence_mode == "local_wall_time":
        if local_timestamp_attr is None:
            raise ValueError("local_timestamp_attr is required for local_wall_time")
        if start_local is None or end_local is None:
            raise ValueError("start_local and end_local are required for local_wall_time")
        return _local_wall_time_coverage_validation_error(
            rows=rows,
            local_timestamp_attr=local_timestamp_attr,
            start_local=start_local,
            end_local=end_local,
            timestep_minutes=timestep_minutes,
            label=label,
        )
    if cadence_mode != "utc":
        raise ValueError(f"unsupported cadence_mode: {cadence_mode}")
    return _utc_coverage_validation_error(
        rows=rows,
        timestamp_attr=timestamp_attr,
        start_utc=start_utc,
        end_utc=end_utc,
        timestep_minutes=timestep_minutes,
        label=label,
    )


def _utc_coverage_validation_error(
    rows: list[object],
    timestamp_attr: str,
    start_utc: datetime,
    end_utc: datetime,
    timestep_minutes: int,
    label: str,
) -> str | None:
    normalized_start_utc = _as_utc(start_utc)
    normalized_end_utc = _as_utc(end_utc)
    expected_count = _expected_row_count(
        normalized_start_utc,
        normalized_end_utc,
        timestep_minutes,
    )
    if not rows:
        return (
            f"{label} is empty for the required range: "
            f"{normalized_start_utc.isoformat()} through "
            f"{normalized_end_utc.isoformat()} exclusive"
        )
    if len(rows) != expected_count:
        return (
            f"{label} row count mismatch for the required range: "
            f"expected {expected_count} rows, found {len(rows)}"
        )

    timestamps = sorted(_row_timestamp_utc(row, timestamp_attr) for row in rows)
    first_timestamp = timestamps[0]
    if first_timestamp != normalized_start_utc:
        return (
            f"{label} first timestamp mismatch: "
            f"expected {normalized_start_utc.isoformat()}, "
            f"found {first_timestamp.isoformat()}"
        )

    step = timedelta(minutes=timestep_minutes)
    expected_last_timestamp = normalized_end_utc - step
    last_timestamp = timestamps[-1]
    if last_timestamp != expected_last_timestamp:
        return (
            f"{label} last timestamp mismatch: "
            f"expected {expected_last_timestamp.isoformat()}, "
            f"found {last_timestamp.isoformat()}"
        )

    for index, actual_timestamp in enumerate(timestamps):
        expected_timestamp = normalized_start_utc + step * index
        if actual_timestamp != expected_timestamp:
            previous_timestamp = timestamps[index - 1] if index > 0 else None
            previous_text = (
                "none"
                if previous_timestamp is None
                else previous_timestamp.isoformat()
            )
            return (
                f"{label} has a non-continuous {timestep_minutes}-minute timestep "
                f"at index {index}: expected {expected_timestamp.isoformat()}, "
                f"found {actual_timestamp.isoformat()}, "
                f"previous {previous_text}"
            )
    return None


def _local_wall_time_coverage_validation_error(
    rows: list[object],
    local_timestamp_attr: str,
    start_local: datetime,
    end_local: datetime,
    timestep_minutes: int,
    label: str,
) -> str | None:
    _require_timezone_aware(start_local)
    _require_timezone_aware(end_local)
    expected_count = _expected_wall_time_row_count(
        start_local,
        end_local,
        timestep_minutes,
    )
    start_wall = _wall_time(start_local)
    end_wall = _wall_time(end_local)
    if not rows:
        return (
            f"{label} is empty for the required local range: "
            f"{start_wall.isoformat()} through {end_wall.isoformat()} exclusive"
        )
    if len(rows) != expected_count:
        return (
            f"{label} row count mismatch for the required local range: "
            f"expected {expected_count} rows, found {len(rows)}"
        )

    timestamps = sorted(
        (_row_timestamp_local(row, local_timestamp_attr) for row in rows),
        key=_local_wall_sort_key,
    )
    first_wall = _wall_time(timestamps[0])
    if first_wall != start_wall:
        return (
            f"{label} first local timestamp mismatch: "
            f"expected {start_wall.isoformat()}, found {first_wall.isoformat()}"
        )

    step = timedelta(minutes=timestep_minutes)
    expected_last_wall = end_wall - step
    last_wall = _wall_time(timestamps[-1])
    if last_wall != expected_last_wall:
        return (
            f"{label} last local timestamp mismatch: "
            f"expected {expected_last_wall.isoformat()}, "
            f"found {last_wall.isoformat()}"
        )

    for index, actual_timestamp in enumerate(timestamps):
        expected_wall = start_wall + step * index
        actual_wall = _wall_time(actual_timestamp)
        if actual_wall != expected_wall:
            previous_timestamp = timestamps[index - 1] if index > 0 else None
            previous_text = (
                "none"
                if previous_timestamp is None
                else _wall_time(previous_timestamp).isoformat()
            )
            return (
                f"{label} has a non-continuous local wall-clock "
                f"{timestep_minutes}-minute timestep at index {index}: "
                f"expected {expected_wall.isoformat()}, "
                f"found {actual_wall.isoformat()}, previous {previous_text}"
            )
    return None


def _row_timestamp_utc(row: object, timestamp_attr: str) -> datetime:
    return _as_utc(getattr(row, timestamp_attr))


def _row_timestamp_local(row: object, local_timestamp_attr: str) -> datetime:
    value = getattr(row, local_timestamp_attr)
    _require_timezone_aware(value)
    return value


def _as_utc(value: datetime) -> datetime:
    _require_timezone_aware(value)
    return value.astimezone(timezone.utc)


def _require_timezone_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime is required")


def _wall_time(value: datetime) -> datetime:
    return value.replace(tzinfo=None)


def _local_wall_sort_key(value: datetime) -> tuple[datetime, float]:
    offset = value.utcoffset()
    offset_seconds = 0.0 if offset is None else offset.total_seconds()
    return _wall_time(value), -offset_seconds


def _expected_row_count(
    start_utc: datetime,
    end_utc: datetime,
    timestep_minutes: int,
) -> int:
    total_seconds = (end_utc - start_utc).total_seconds()
    return int(total_seconds // (timestep_minutes * 60))


def _expected_wall_time_row_count(
    start_local: datetime,
    end_local: datetime,
    timestep_minutes: int,
) -> int:
    total_seconds = (_wall_time(end_local) - _wall_time(start_local)).total_seconds()
    return int(total_seconds // (timestep_minutes * 60))


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _log(message: str) -> None:
    print(f"{_utc_timestamp()} {message}", flush=True)


def _log_error(message: str) -> None:
    print(f"{_utc_timestamp()} {message}", file=sys.stderr, flush=True)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    main()
