from __future__ import annotations

import argparse
from bisect import bisect_left
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
import generate_grid_availability

from app.config_loader import calculate_config_hash, load_config
from app.schemas import AppConfig
from app.simulation.solar import IdealSolarGenerator, IdealSolarPoint
from app.simulation.solar_interpolation import (
    apply_interpolation_variation,
    calculate_deterministic_variation_factor,
    interpolate_power,
)
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
from app.storage.interpolated_solar_repository import (
    InterpolatedSolarProduction,
    delete_interpolated_solar_for_config,
    save_interpolated_solar_points,
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
DEFAULT_MAINTENANCE_INTERVAL_HOURS = 12.0
DEFAULT_FAST_CACHE_INTERVAL_SECONDS = 60.0
DEFAULT_FULL_CACHE_INTERVAL_MINUTES = 45.0
SOLAR_TIMESTEP_MINUTES = 15
WEATHER_TIMESTEP_MINUTES = 60
SECONDS_PER_HOUR = 60 * 60
SOURCE_TIMESTEP_MINUTES = 15
MIN_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_UTC = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


@dataclass(frozen=True)
class SchedulerSettings:
    config: Path
    database_url: str
    history_start: date
    days_ahead: int
    maintenance_interval_hours: float
    fast_cache_interval_seconds: float
    full_cache_interval_minutes: float


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
    grid_availability: generate_grid_availability.GridGenerationSummary | None = None


@dataclass(frozen=True)
class InterpolationWindow:
    start_utc: datetime
    end_utc: datetime
    resolution_seconds: int


@dataclass(frozen=True)
class SourceSolarPoint:
    timestamp_utc: datetime
    timestamp_local: datetime
    power_w: float
    weather_state: str | None
    source_type: str


@dataclass(frozen=True)
class InterpolatedCacheSummary:
    rows: int
    windows: int
    start_utc: datetime | None
    end_utc: datetime | None
    fast_only: bool


MaintenanceRunner = Callable[
    [Path, str | None, date, int],
    SolarDataMaintenanceSummary,
]
CacheRunner = Callable[[Path, str | None], InterpolatedCacheSummary]
SleepFunction = Callable[[float], None]
ClockFunction = Callable[[], float]
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
        "--maintenance-interval-hours",
        "--interval-hours",
        dest="maintenance_interval_hours",
        type=float,
        default=DEFAULT_MAINTENANCE_INTERVAL_HOURS,
    )
    parser.add_argument(
        "--fast-cache-interval-seconds",
        type=float,
        default=DEFAULT_FAST_CACHE_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--full-cache-interval-minutes",
        type=float,
        default=DEFAULT_FULL_CACHE_INTERVAL_MINUTES,
    )
    args = parser.parse_args(argv)

    if args.days_ahead < 0:
        parser.error("--days-ahead must be 0 or greater")
    if args.maintenance_interval_hours <= 0:
        parser.error("--maintenance-interval-hours must be greater than 0")
    if args.fast_cache_interval_seconds <= 0:
        parser.error("--fast-cache-interval-seconds must be greater than 0")
    if args.full_cache_interval_minutes <= 0:
        parser.error("--full-cache-interval-minutes must be greater than 0")

    return SchedulerSettings(
        config=args.config,
        database_url=args.database_url,
        history_start=args.history_start,
        days_ahead=args.days_ahead,
        maintenance_interval_hours=args.maintenance_interval_hours,
        fast_cache_interval_seconds=args.fast_cache_interval_seconds,
        full_cache_interval_minutes=args.full_cache_interval_minutes,
    )


def run_forever(
    settings: SchedulerSettings,
    maintenance_runner: MaintenanceRunner | None = None,
    full_cache_runner: CacheRunner | None = None,
    fast_cache_runner: CacheRunner | None = None,
    sleep: SleepFunction = time.sleep,
    monotonic: ClockFunction = time.monotonic,
) -> None:
    if maintenance_runner is None:
        maintenance_runner = run_solar_data_maintenance
    if full_cache_runner is None:
        full_cache_runner = run_full_interpolated_solar_cache_refresh
    if fast_cache_runner is None:
        fast_cache_runner = run_fast_interpolated_solar_cache_refresh

    maintenance_interval_seconds = (
        settings.maintenance_interval_hours * SECONDS_PER_HOUR
    )
    full_cache_interval_seconds = settings.full_cache_interval_minutes * 60.0
    fast_cache_interval_seconds = settings.fast_cache_interval_seconds

    if run_once(settings, maintenance_runner):
        _run_cache_once("full interpolation cache refresh", settings, full_cache_runner)
    last_maintenance = monotonic()
    last_full_cache = last_maintenance
    last_fast_cache = last_maintenance

    while True:
        now_monotonic = monotonic()
        if now_monotonic - last_maintenance >= maintenance_interval_seconds:
            if run_once(settings, maintenance_runner):
                _run_cache_once(
                    "full interpolation cache refresh",
                    settings,
                    full_cache_runner,
                )
                last_full_cache = now_monotonic
                last_fast_cache = now_monotonic
            last_maintenance = now_monotonic
        elif now_monotonic - last_full_cache >= full_cache_interval_seconds:
            _run_cache_once("full interpolation cache refresh", settings, full_cache_runner)
            last_full_cache = now_monotonic
            last_fast_cache = now_monotonic
        elif now_monotonic - last_fast_cache >= fast_cache_interval_seconds:
            _run_cache_once("fast interpolation cache refresh", settings, fast_cache_runner)
            last_fast_cache = now_monotonic

        sleep_seconds = min(5.0, fast_cache_interval_seconds)
        _log(f"solar data scheduler sleeping for {sleep_seconds:g} seconds")
        sleep(sleep_seconds)


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

    grid_summary = getattr(summary, "grid_availability", None)
    grid_message = ""
    if grid_summary is not None:
        grid_message = (
            f" grid_availability_rows={grid_summary.availability_rows_inserted} "
            f"grid_damage_events={grid_summary.damage_events_inserted}"
        )

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
        f"{grid_message}"
    )
    return True


def _run_cache_once(
    label: str,
    settings: SchedulerSettings,
    cache_runner: CacheRunner,
) -> bool:
    _log(
        f"{label} started "
        f"config={settings.config} database_url={settings.database_url}"
    )
    try:
        summary = cache_runner(settings.config, settings.database_url)
    except Exception as exc:
        _log_error(f"{label} failed: {exc}")
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return False

    _log(
        f"{label} completed "
        f"rows={summary.rows} windows={summary.windows} "
        f"start_utc={_format_optional_datetime(summary.start_utc)} "
        f"end_utc={_format_optional_datetime(summary.end_utc)} "
        f"fast_only={summary.fast_only}"
    )
    return True


def run_full_interpolated_solar_cache_refresh(
    config_path: Path,
    database_url: str | None,
    now: datetime | None = None,
) -> InterpolatedCacheSummary:
    config = load_config(config_path)
    station_id = config.station.id
    config_hash = calculate_config_hash(config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    generated_at_utc = _resolve_now_utc(now)
    windows = build_default_interpolation_windows(generated_at_utc)

    engine = get_engine(database_url)
    create_db_and_tables(engine)
    with Session(engine) as session:
        points = generate_interpolated_solar_cache_points(
            session=session,
            station_id=station_id,
            config_hash=config_hash,
            station_timezone=station_timezone,
            windows=windows,
            generated_at_utc=generated_at_utc,
        )
        delete_interpolated_solar_for_config(session, station_id, config_hash)
        save_interpolated_solar_points(session, points)

    return InterpolatedCacheSummary(
        rows=len(points),
        windows=len(windows),
        start_utc=windows[0].start_utc if windows else None,
        end_utc=windows[-1].end_utc if windows else None,
        fast_only=False,
    )


def run_fast_interpolated_solar_cache_refresh(
    config_path: Path,
    database_url: str | None,
    now: datetime | None = None,
) -> InterpolatedCacheSummary:
    config = load_config(config_path)
    station_id = config.station.id
    config_hash = calculate_config_hash(config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    generated_at_utc = _resolve_now_utc(now)
    windows = build_fast_interpolation_windows(generated_at_utc)

    engine = get_engine(database_url)
    create_db_and_tables(engine)
    with Session(engine) as session:
        points = generate_interpolated_solar_cache_points(
            session=session,
            station_id=station_id,
            config_hash=config_hash,
            station_timezone=station_timezone,
            windows=windows,
            generated_at_utc=generated_at_utc,
        )
        for window in windows:
            delete_interpolated_solar_for_config(
                session,
                station_id,
                config_hash,
                start_utc=window.start_utc,
                end_utc=window.end_utc,
                resolution_seconds=window.resolution_seconds,
            )
        save_interpolated_solar_points(session, points)

    return InterpolatedCacheSummary(
        rows=len(points),
        windows=len(windows),
        start_utc=windows[0].start_utc if windows else None,
        end_utc=windows[-1].end_utc if windows else None,
        fast_only=True,
    )


def build_default_interpolation_windows(now: datetime) -> list[InterpolationWindow]:
    now_utc = _resolve_now_utc(now)
    return [
        InterpolationWindow(
            start_utc=now_utc - timedelta(days=7),
            end_utc=now_utc - timedelta(hours=24),
            resolution_seconds=300,
        ),
        InterpolationWindow(
            start_utc=now_utc - timedelta(hours=24),
            end_utc=now_utc - timedelta(hours=12),
            resolution_seconds=60,
        ),
        InterpolationWindow(
            start_utc=now_utc - timedelta(hours=12),
            end_utc=now_utc - timedelta(hours=3),
            resolution_seconds=30,
        ),
        InterpolationWindow(
            start_utc=now_utc - timedelta(hours=3),
            end_utc=now_utc - timedelta(minutes=30),
            resolution_seconds=5,
        ),
        InterpolationWindow(
            start_utc=now_utc - timedelta(minutes=30),
            end_utc=now_utc + timedelta(minutes=30),
            resolution_seconds=1,
        ),
        InterpolationWindow(
            start_utc=now_utc + timedelta(minutes=30),
            end_utc=now_utc + timedelta(hours=3),
            resolution_seconds=5,
        ),
    ]


def build_fast_interpolation_windows(now: datetime) -> list[InterpolationWindow]:
    now_utc = _resolve_now_utc(now)
    return [
        InterpolationWindow(
            start_utc=now_utc - timedelta(minutes=30),
            end_utc=now_utc + timedelta(minutes=30),
            resolution_seconds=1,
        )
    ]


def generate_interpolated_solar_cache_points(
    session: Session,
    station_id: str,
    config_hash: str,
    station_timezone: ZoneInfo,
    windows: Sequence[InterpolationWindow],
    generated_at_utc: datetime,
) -> list[InterpolatedSolarProduction]:
    generated_at = _resolve_now_utc(generated_at_utc)
    current_local_date = generated_at.astimezone(station_timezone).date()
    transition_utc = datetime.combine(
        current_local_date,
        datetime_time.min,
        tzinfo=station_timezone,
    ).astimezone(timezone.utc)

    points: list[InterpolatedSolarProduction] = []
    for window in windows:
        points.extend(
            _generate_interpolated_solar_window(
                session=session,
                station_id=station_id,
                config_hash=config_hash,
                station_timezone=station_timezone,
                window=window,
                generated_at_utc=generated_at,
                transition_utc=transition_utc,
            )
        )
    return points


def _generate_interpolated_solar_window(
    session: Session,
    station_id: str,
    config_hash: str,
    station_timezone: ZoneInfo,
    window: InterpolationWindow,
    generated_at_utc: datetime,
    transition_utc: datetime,
) -> list[InterpolatedSolarProduction]:
    if window.resolution_seconds <= 0:
        raise ValueError("resolution_seconds must be greater than 0")
    start_utc = _as_utc(window.start_utc)
    end_utc = _as_utc(window.end_utc)
    if end_utc <= start_utc:
        return []

    historical_points = _load_historical_source_points(
        session,
        station_id,
        config_hash,
        start_utc=start_utc - timedelta(minutes=SOURCE_TIMESTEP_MINUTES),
        end_utc=min(end_utc + timedelta(minutes=SOURCE_TIMESTEP_MINUTES), transition_utc),
    )
    forecast_points = _load_forecast_source_points(
        session,
        station_id,
        config_hash,
        start_utc=max(start_utc - timedelta(minutes=SOURCE_TIMESTEP_MINUTES), transition_utc),
        end_utc=end_utc + timedelta(minutes=SOURCE_TIMESTEP_MINUTES),
    )

    rows: list[InterpolatedSolarProduction] = []
    current = start_utc
    step = timedelta(seconds=window.resolution_seconds)
    while current < end_utc:
        source_type = "historical" if current < transition_utc else "forecast"
        source_points = historical_points if source_type == "historical" else forecast_points
        lower_point, upper_point = _find_bracketing_source_points(
            source_points,
            current,
            source_type,
            transition_utc=transition_utc,
        )

        baseline_power_w, interpolation_ratio = interpolate_power(
            current,
            lower_point.timestamp_utc,
            lower_point.power_w,
            upper_point.timestamp_utc,
            upper_point.power_w,
        )
        weather_state = (
            lower_point.weather_state
            if interpolation_ratio < 0.5
            else upper_point.weather_state
        )
        if current == lower_point.timestamp_utc or current == upper_point.timestamp_utc:
            variation_factor = 1.0
            power_w = baseline_power_w
        else:
            variation_factor = calculate_deterministic_variation_factor(
                timestamp_utc=current,
                station_id=station_id,
                config_hash=config_hash,
                source_type=source_type,
                lower_power_w=lower_point.power_w,
                upper_power_w=upper_point.power_w,
                resolution_seconds=window.resolution_seconds,
                weather_state=weather_state,
            )
            power_w = apply_interpolation_variation(
                baseline_power_w=baseline_power_w,
                variation_factor=variation_factor,
                lower_power_w=lower_point.power_w,
                upper_power_w=upper_point.power_w,
                weather_state=weather_state,
            )

        rows.append(
            InterpolatedSolarProduction(
                station_id=station_id,
                config_hash=config_hash,
                timestamp_utc=current,
                timestamp_local=current.astimezone(station_timezone),
                source_type=source_type,
                resolution_seconds=window.resolution_seconds,
                lower_source_timestamp_utc=lower_point.timestamp_utc,
                upper_source_timestamp_utc=upper_point.timestamp_utc,
                lower_power_w=lower_point.power_w,
                upper_power_w=upper_point.power_w,
                interpolation_ratio=interpolation_ratio,
                baseline_power_w=baseline_power_w,
                variation_factor=variation_factor,
                power_w=power_w,
                generated_at_utc=generated_at_utc,
            )
        )
        current += step
    return rows


def _load_historical_source_points(
    session: Session,
    station_id: str,
    config_hash: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[SourceSolarPoint]:
    if end_utc <= start_utc:
        return []
    rows = list_simulated_solar_for_config(
        session,
        station_id,
        config_hash,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    return [
        SourceSolarPoint(
            timestamp_utc=row.timestamp_utc.astimezone(timezone.utc),
            timestamp_local=row.timestamp_local,
            power_w=row.simulated_power_w,
            weather_state=row.weather_state,
            source_type="historical",
        )
        for row in rows
    ]


def _load_forecast_source_points(
    session: Session,
    station_id: str,
    config_hash: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[SourceSolarPoint]:
    if end_utc <= start_utc:
        return []
    rows = list_forecast_solar_for_config(
        session,
        station_id,
        config_hash,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    return [
        SourceSolarPoint(
            timestamp_utc=row.timestamp_utc.astimezone(timezone.utc),
            timestamp_local=row.timestamp_local,
            power_w=row.forecast_power_w,
            weather_state=row.weather_state,
            source_type="forecast",
        )
        for row in rows
    ]


def _find_bracketing_source_points(
    source_points: list[SourceSolarPoint],
    timestamp_utc: datetime,
    source_type: str,
    transition_utc: datetime,
) -> tuple[SourceSolarPoint, SourceSolarPoint]:
    timestamp = _as_utc(timestamp_utc)
    if not source_points:
        raise RuntimeError(
            f"Missing {source_type} bracketing solar data for "
            f"{timestamp.isoformat()}: source table has no rows in the lookup range"
        )

    timestamps = [point.timestamp_utc for point in source_points]
    index = bisect_left(timestamps, timestamp)
    if index < len(source_points) and timestamps[index] == timestamp:
        point = source_points[index]
        return point, point
    lower = source_points[index - 1] if index > 0 else None
    upper = source_points[index] if index < len(source_points) else None

    if upper is None and source_type == "historical" and lower is not None:
        transition = _as_utc(transition_utc)
        source_step = timedelta(minutes=SOURCE_TIMESTEP_MINUTES)
        if lower.timestamp_utc < timestamp < transition <= lower.timestamp_utc + source_step:
            upper = SourceSolarPoint(
                timestamp_utc=transition,
                timestamp_local=transition.astimezone(lower.timestamp_local.tzinfo),
                power_w=lower.power_w,
                weather_state=lower.weather_state,
                source_type=lower.source_type,
            )

    if lower is None or upper is None:
        lower_text = lower.timestamp_utc.isoformat() if lower is not None else "missing"
        upper_text = upper.timestamp_utc.isoformat() if upper is not None else "missing"
        raise RuntimeError(
            f"Missing {source_type} bracketing solar data for {timestamp.isoformat()}: "
            f"lower={lower_text}, upper={upper_text}"
        )
    return lower, upper


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

    grid_summary = None
    try:
        grid_summary = generate_grid_availability.run_grid_availability_generation(
            config_path=config_path,
            database_url=database_url,
            start_date=history_start,
            days_ahead=7,
            seed=config.station.grid.outage_schedule_seed,
            now=now,
        )
    except Exception as exc:
        _log_error(f"grid availability maintenance failed: {exc}")

    return SolarDataMaintenanceSummary(
        station_id=station_id,
        config_hash=config_hash,
        timezone_name=station_timezone.key,
        current_local_date=current_local_date,
        ideal_solar=ideal_summary,
        weather_cache=weather_summary,
        historical_adjusted_solar=historical_summary,
        forecast_adjusted_solar=forecast_summary,
        grid_availability=grid_summary,
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

    rebuild_start_date = history_start
    if existing_points:
        latest_existing_date = existing_points[-1].timestamp_local.astimezone(
            station_timezone,
        ).date()
        latest_existing_end_utc = datetime.combine(
            latest_existing_date + timedelta(days=1),
            datetime_time.min,
            tzinfo=station_timezone,
        ).astimezone(timezone.utc)
        prefix_points = [
            point
            for point in existing_points
            if point.timestamp_utc < latest_existing_end_utc
        ]
        prefix_is_complete = _has_complete_coverage(
            prefix_points,
            timestamp_attr="timestamp_utc",
            start_utc=start_utc,
            end_utc=latest_existing_end_utc,
            timestep_minutes=SOLAR_TIMESTEP_MINUTES,
        )
        if prefix_is_complete and latest_existing_date < yesterday:
            rebuild_start_date = latest_existing_date + timedelta(days=1)

    generated_rows = _regenerate_historical_adjusted_solar_range(
        session=session,
        station_id=station_id,
        config_hash=config_hash,
        station_timezone=station_timezone,
        start_date=rebuild_start_date,
        end_date=yesterday,
    )
    return AdjustedSolarMaintenanceSummary(
        start_utc=start_utc,
        end_utc=end_utc,
        rows=generated_rows,
        regenerated=True,
    )


def _regenerate_historical_adjusted_solar_range(
    session: Session,
    station_id: str,
    config_hash: str,
    station_timezone: ZoneInfo,
    start_date: date,
    end_date: date,
) -> int:
    start_utc, end_utc = _date_range_to_utc_bounds(
        start_date,
        end_date,
        station_timezone,
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
            start_date,
            datetime_time.min,
            tzinfo=station_timezone,
        ),
        end_local=datetime.combine(
            end_date + timedelta(days=1),
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
    return len(simulated_points)


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


def _resolve_now_utc(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    _require_timezone_aware(now)
    return now.astimezone(timezone.utc).replace(microsecond=0)


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


def _format_optional_datetime(value: datetime | None) -> str:
    return "none" if value is None else value.isoformat()


def _log(message: str) -> None:
    print(f"{_utc_timestamp()} {message}", flush=True)


def _log_error(message: str) -> None:
    print(f"{_utc_timestamp()} {message}", file=sys.stderr, flush=True)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    main()
