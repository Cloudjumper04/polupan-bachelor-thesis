from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Sequence
from zoneinfo import ZoneInfo

from sqlmodel import Session


SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = Path(__file__).resolve().parents[1]
for path in (SCRIPTS_DIR, BACKEND_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import generate_grid_availability
import generate_system_simulation
import solar_data_scheduler

from app.config_loader import (
    calculate_config_hash,
    calculate_system_config_hash,
    load_config,
)
from app.schemas import AppConfig
from app.storage.battery_repository import get_battery_history_range
from app.storage.database import create_db_and_tables, get_engine
from app.storage.ems_repository import get_ems_history_range
from app.storage.forecast_solar_repository import list_forecast_solar_for_config
from app.storage.grid_repository import find_missing_grid_availability_timestamps
from app.storage.load_repository import get_load_history_range
from app.storage.simulated_solar_repository import list_simulated_solar_for_config


DEFAULT_CONFIG_PATH = Path("backend/config/station.default.yaml")
DEFAULT_DATABASE_URL = "sqlite:///backend/data/smartenergy.db"
DEFAULT_SOURCE_DAYS_AHEAD = 2
DEFAULT_GRID_DAYS_AHEAD = 7
SOLAR_TIMESTEP_MINUTES = 15
GRID_TIMESTEP_MINUTES = 30


class SourceCoverageError(RuntimeError):
    """Raised when source data cannot safely drive system simulation."""


@dataclass(frozen=True)
class SourcePlanSummary:
    current_local_date: date
    ideal_solar_start_local: datetime
    ideal_solar_end_local: datetime
    historical_weather_start_date: date
    historical_weather_end_date: date | None
    forecast_weather_start_date: date
    forecast_weather_end_date: date
    grid_start_date: date
    grid_end_date: date


@dataclass(frozen=True)
class SourceMaintenanceSummary:
    station_id: str
    config_hash: str
    timezone_name: str
    current_local_date: date
    ideal_solar: solar_data_scheduler.IdealSolarMaintenanceSummary
    weather_cache: object
    historical_adjusted_solar: solar_data_scheduler.AdjustedSolarMaintenanceSummary
    forecast_adjusted_solar: solar_data_scheduler.AdjustedSolarMaintenanceSummary
    grid_availability: generate_grid_availability.GridGenerationSummary
    interpolated_solar_cache: solar_data_scheduler.InterpolatedCacheSummary


@dataclass(frozen=True)
class SourceCoverageSummary:
    start_utc: datetime
    end_utc: datetime
    historical_solar_start_utc: datetime | None
    historical_solar_end_utc: datetime | None
    historical_solar_rows: int
    forecast_solar_start_utc: datetime | None
    forecast_solar_end_utc: datetime | None
    forecast_solar_rows: int
    grid_start_utc: datetime
    grid_end_utc: datetime
    grid_missing_points: int


@dataclass(frozen=True)
class DataPipelineSummary:
    station_id: str
    database_url: str | None
    history_start: date
    dry_run: bool
    full_history: bool
    allow_fallbacks: bool
    source_days_ahead: int
    grid_days_ahead: int
    source_plan: SourcePlanSummary
    system_plan: generate_system_simulation.SystemGenerationSummary
    source_maintenance: SourceMaintenanceSummary | None = None
    source_coverage: SourceCoverageSummary | None = None
    system_generation: generate_system_simulation.SystemGenerationSummary | None = None


@dataclass(frozen=True)
class SystemHistoryBootstrapStatus:
    required: bool
    reason: str
    station_id: str
    config_hash: str
    expected_history_start_utc: datetime
    load_history_range: tuple[datetime | None, datetime | None]
    battery_history_range: tuple[datetime | None, datetime | None]
    ems_history_range: tuple[datetime | None, datetime | None]


def main() -> None:
    args = parse_args()
    database_url = _database_url_from_args(args)
    if args.allow_fallbacks:
        print(
            "warning: --allow-fallbacks is enabled; generated system data may use "
            "synthetic fallback source values",
            file=sys.stderr,
            flush=True,
        )

    try:
        summary = run_data_pipeline(
            config_path=args.config,
            database_url=database_url,
            history_start=args.history_start,
            source_days_ahead=args.source_days_ahead,
            grid_days_ahead=args.grid_days_ahead,
            full_history=args.full_history,
            allow_fallbacks=args.allow_fallbacks,
            dry_run=args.dry_run,
        )
    except SourceCoverageError as exc:
        print(f"data pipeline failed source coverage validation: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except Exception as exc:
        print(f"data pipeline failed: {exc}", file=sys.stderr)
        raise

    print_data_pipeline_summary(summary)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one dependency-ordered SmartEnergy data update pass: "
            "weather/solar/grid sources, then Load/Battery/EMS simulation."
        ),
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument(
        "--history-start",
        type=_parse_date,
        default=None,
        help="Override station.installation_date for source/system history start.",
    )
    parser.add_argument(
        "--source-days-ahead",
        "--days-ahead",
        dest="source_days_ahead",
        type=int,
        default=DEFAULT_SOURCE_DAYS_AHEAD,
    )
    parser.add_argument(
        "--grid-days-ahead",
        type=int,
        default=DEFAULT_GRID_DAYS_AHEAD,
    )
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="Explicitly write system history as well as cache.",
    )
    parser.add_argument(
        "--allow-fallbacks",
        action="store_true",
        help="Allow system simulation source fallbacks; intended only for test/demo use.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned pipeline windows without creating tables or writing rows.",
    )
    args = parser.parse_args(argv)

    if args.source_days_ahead < 0:
        parser.error("--source-days-ahead must be 0 or greater")
    if args.grid_days_ahead < 0:
        parser.error("--grid-days-ahead must be 0 or greater")
    return args


def run_data_pipeline(
    config_path: Path = DEFAULT_CONFIG_PATH,
    database_url: str | None = DEFAULT_DATABASE_URL,
    *,
    history_start: date | None = None,
    source_days_ahead: int = DEFAULT_SOURCE_DAYS_AHEAD,
    grid_days_ahead: int = DEFAULT_GRID_DAYS_AHEAD,
    full_history: bool = False,
    allow_fallbacks: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> DataPipelineSummary:
    if source_days_ahead < 0:
        raise ValueError("source_days_ahead must be 0 or greater")
    if grid_days_ahead < 0:
        raise ValueError("grid_days_ahead must be 0 or greater")

    config = load_config(config_path)
    resolved_history_start = history_start or date.fromisoformat(
        config.station.installation_date,
    )
    resolved_now = _resolve_now_utc(now)
    source_plan = build_source_plan(
        config=config,
        history_start=resolved_history_start,
        source_days_ahead=source_days_ahead,
        grid_days_ahead=grid_days_ahead,
        now=resolved_now,
    )
    system_plan = _plan_system_generation(
        config_path=config_path,
        database_url=database_url,
        history_start=resolved_history_start,
        full_history=full_history,
        allow_fallbacks=allow_fallbacks,
        now=resolved_now,
    )
    if dry_run:
        return DataPipelineSummary(
            station_id=config.station.id,
            database_url=database_url,
            history_start=resolved_history_start,
            dry_run=True,
            full_history=full_history,
            allow_fallbacks=allow_fallbacks,
            source_days_ahead=source_days_ahead,
            grid_days_ahead=grid_days_ahead,
            source_plan=source_plan,
            system_plan=system_plan,
        )

    import_all_storage_models()
    engine = get_engine(database_url)
    create_db_and_tables(engine)

    source_summary = run_source_maintenance(
        config_path=config_path,
        database_url=database_url,
        config=config,
        history_start=resolved_history_start,
        source_days_ahead=source_days_ahead,
        grid_days_ahead=grid_days_ahead,
        now=resolved_now,
    )
    source_coverage = validate_system_source_coverage(
        engine=engine,
        config=config,
        system_plan=system_plan,
        now=resolved_now,
    )
    system_summary = generate_system_simulation.run_system_simulation_generation(
        config_path=config_path,
        database_url=database_url,
        history_start_date=resolved_history_start,
        cache_only=not full_history,
        replace_existing=True,
        now=resolved_now,
        allow_fallbacks=allow_fallbacks,
        dry_run=False,
    )
    return DataPipelineSummary(
        station_id=config.station.id,
        database_url=database_url,
        history_start=resolved_history_start,
        dry_run=False,
        full_history=full_history,
        allow_fallbacks=allow_fallbacks,
        source_days_ahead=source_days_ahead,
        grid_days_ahead=grid_days_ahead,
        source_plan=source_plan,
        system_plan=system_plan,
        source_maintenance=source_summary,
        source_coverage=source_coverage,
        system_generation=system_summary,
    )


def inspect_system_history_bootstrap_status(
    *,
    config_path: Path,
    database_url: str | None,
    history_start: date | None = None,
) -> SystemHistoryBootstrapStatus:
    config = load_config(config_path)
    resolved_history_start = history_start or date.fromisoformat(
        config.station.installation_date,
    )
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    expected_start_utc = datetime.combine(
        resolved_history_start,
        datetime_time.min,
        tzinfo=station_timezone,
    ).astimezone(timezone.utc)
    station_id = config.station.id
    config_hash = calculate_system_config_hash(config)

    import_all_storage_models()
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    with Session(engine) as session:
        load_range = get_load_history_range(session, station_id, config_hash)
        battery_range = get_battery_history_range(session, station_id, config_hash)
        ems_range = get_ems_history_range(session, station_id, config_hash)

    ranges = {
        "load": load_range,
        "battery": battery_range,
        "ems": ems_range,
    }
    missing = [
        name
        for name, (start_utc, end_utc) in ranges.items()
        if start_utc is None or end_utc is None
    ]
    if missing:
        return SystemHistoryBootstrapStatus(
            required=True,
            reason=f"missing system history tables: {','.join(missing)}",
            station_id=station_id,
            config_hash=config_hash,
            expected_history_start_utc=expected_start_utc,
            load_history_range=load_range,
            battery_history_range=battery_range,
            ems_history_range=ems_range,
        )

    late_starts = [
        name
        for name, (start_utc, _) in ranges.items()
        if start_utc is not None and start_utc > expected_start_utc
    ]
    if late_starts:
        return SystemHistoryBootstrapStatus(
            required=True,
            reason=(
                "system history starts after required history start for "
                f"{','.join(late_starts)}"
            ),
            station_id=station_id,
            config_hash=config_hash,
            expected_history_start_utc=expected_start_utc,
            load_history_range=load_range,
            battery_history_range=battery_range,
            ems_history_range=ems_range,
        )

    return SystemHistoryBootstrapStatus(
        required=False,
        reason="system history baseline is present",
        station_id=station_id,
        config_hash=config_hash,
        expected_history_start_utc=expected_start_utc,
        load_history_range=load_range,
        battery_history_range=battery_range,
        ems_history_range=ems_range,
    )


def import_all_storage_models() -> list[ModuleType]:
    """Import every SQLModel table family needed before create_all."""

    modules: list[ModuleType] = []
    module_names = [
        "app.storage.weather_repository",
        "app.storage.forecast_repository",
        "app.storage.solar_repository",
        "app.storage.simulated_solar_repository",
        "app.storage.forecast_solar_repository",
        "app.storage.interpolated_solar_repository",
        "app.storage.grid_repository",
        "app.storage.load_repository",
        "app.storage.battery_repository",
        "app.storage.ems_repository",
    ]
    for module_name in module_names:
        __import__(module_name)
        modules.append(sys.modules[module_name])
    return modules


def build_source_plan(
    *,
    config: AppConfig,
    history_start: date,
    source_days_ahead: int,
    grid_days_ahead: int,
    now: datetime,
) -> SourcePlanSummary:
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    current_local_date = _resolve_now_utc(now).astimezone(station_timezone).date()
    ideal_start_local, ideal_end_local = solar_data_scheduler._required_ideal_solar_range(
        history_start,
        current_local_date,
        source_days_ahead,
        station_timezone,
    )
    yesterday = current_local_date - timedelta(days=1)
    return SourcePlanSummary(
        current_local_date=current_local_date,
        ideal_solar_start_local=ideal_start_local,
        ideal_solar_end_local=ideal_end_local,
        historical_weather_start_date=history_start,
        historical_weather_end_date=yesterday if yesterday >= history_start else None,
        forecast_weather_start_date=current_local_date,
        forecast_weather_end_date=current_local_date + timedelta(days=source_days_ahead),
        grid_start_date=history_start,
        grid_end_date=current_local_date + timedelta(days=grid_days_ahead),
    )


def run_source_maintenance(
    *,
    config_path: Path,
    database_url: str | None,
    config: AppConfig,
    history_start: date,
    source_days_ahead: int,
    grid_days_ahead: int,
    now: datetime,
) -> SourceMaintenanceSummary:
    if source_days_ahead < 0:
        raise ValueError("source_days_ahead must be 0 or greater")
    if grid_days_ahead < 0:
        raise ValueError("grid_days_ahead must be 0 or greater")

    station_id = config.station.id
    config_hash = calculate_config_hash(config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    current_local_date = now.astimezone(station_timezone).date()
    ideal_start_local, ideal_end_local = solar_data_scheduler._required_ideal_solar_range(
        history_start,
        current_local_date,
        source_days_ahead,
        station_timezone,
    )

    engine = get_engine(database_url)
    with Session(engine) as session:
        ideal_summary = solar_data_scheduler.ensure_ideal_solar_coverage(
            session=session,
            config=config,
            station_id=station_id,
            config_hash=config_hash,
            start_local=ideal_start_local,
            end_local=ideal_end_local,
        )

    weather_summary = solar_data_scheduler.update_weather_cache.update_weather_cache(
        config_path=config_path,
        database_url=database_url,
        history_start=history_start,
        days_ahead=source_days_ahead,
        now=now,
    )

    with Session(engine) as session:
        historical_summary = (
            solar_data_scheduler.ensure_historical_adjusted_solar_coverage(
                session=session,
                station_id=station_id,
                config_hash=config_hash,
                station_timezone=station_timezone,
                history_start=history_start,
                current_local_date=current_local_date,
            )
        )
        forecast_summary = solar_data_scheduler.regenerate_forecast_adjusted_solar(
            session=session,
            station_id=station_id,
            config_hash=config_hash,
        )

    grid_summary = generate_grid_availability.run_grid_availability_generation(
        config_path=config_path,
        database_url=database_url,
        start_date=history_start,
        days_ahead=grid_days_ahead,
        seed=config.station.grid.outage_schedule_seed,
        now=now,
    )
    interpolated_summary = solar_data_scheduler.run_full_interpolated_solar_cache_refresh(
        config_path=config_path,
        database_url=database_url,
        now=now,
    )
    return SourceMaintenanceSummary(
        station_id=station_id,
        config_hash=config_hash,
        timezone_name=station_timezone.key,
        current_local_date=current_local_date,
        ideal_solar=ideal_summary,
        weather_cache=weather_summary,
        historical_adjusted_solar=historical_summary,
        forecast_adjusted_solar=forecast_summary,
        grid_availability=grid_summary,
        interpolated_solar_cache=interpolated_summary,
    )


def validate_system_source_coverage(
    *,
    engine: object,
    config: AppConfig,
    system_plan: generate_system_simulation.SystemGenerationSummary,
    now: datetime,
) -> SourceCoverageSummary:
    station_id = config.station.id
    config_hash = calculate_config_hash(config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    current_local_date = _resolve_now_utc(now).astimezone(station_timezone).date()
    transition_utc = datetime.combine(
        current_local_date,
        datetime_time.min,
        tzinfo=station_timezone,
    ).astimezone(timezone.utc)

    start_utc = _floor_to_cadence(system_plan.start_utc, SOLAR_TIMESTEP_MINUTES)
    end_utc = _ceil_to_cadence(system_plan.end_utc, SOLAR_TIMESTEP_MINUTES)
    historical_start = start_utc
    historical_end = min(end_utc, transition_utc)
    forecast_start = max(start_utc, transition_utc)
    forecast_end = end_utc
    historical_rows_count = 0
    forecast_rows_count = 0

    with Session(engine) as session:
        if historical_end > historical_start:
            historical_rows = list_simulated_solar_for_config(
                session,
                station_id,
                config_hash,
                start_utc=historical_start,
                end_utc=historical_end,
            )
            _validate_complete_source_rows(
                rows=historical_rows,
                timestamp_attr="timestamp_utc",
                start_utc=historical_start,
                end_utc=historical_end,
                timestep_minutes=SOLAR_TIMESTEP_MINUTES,
                label="Historical weather-adjusted solar source",
            )
            _validate_weather_state_rows(
                historical_rows,
                "Historical weather-adjusted solar source",
            )
            historical_rows_count = len(historical_rows)

        if forecast_end > forecast_start:
            forecast_rows = list_forecast_solar_for_config(
                session,
                station_id,
                config_hash,
                start_utc=forecast_start,
                end_utc=forecast_end,
            )
            _validate_complete_source_rows(
                rows=forecast_rows,
                timestamp_attr="timestamp_utc",
                start_utc=forecast_start,
                end_utc=forecast_end,
                timestep_minutes=SOLAR_TIMESTEP_MINUTES,
                label="Forecast weather-adjusted solar source",
            )
            _validate_weather_state_rows(
                forecast_rows,
                "Forecast weather-adjusted solar source",
            )
            forecast_rows_count = len(forecast_rows)

        grid_start = _floor_to_cadence(system_plan.start_utc, GRID_TIMESTEP_MINUTES)
        grid_end = _ceil_to_cadence(system_plan.end_utc, GRID_TIMESTEP_MINUTES)
        missing_grid = find_missing_grid_availability_timestamps(
            session,
            grid_start,
            grid_end,
            cadence_minutes=GRID_TIMESTEP_MINUTES,
        )
        if missing_grid:
            preview = ", ".join(item.isoformat() for item in missing_grid[:5])
            suffix = "" if len(missing_grid) <= 5 else ", ..."
            raise SourceCoverageError(
                "Grid availability source is missing required timestamps: "
                f"requested {grid_start.isoformat()} through "
                f"{grid_end.isoformat()} exclusive, missing {len(missing_grid)} "
                f"points ({preview}{suffix})"
            )

    return SourceCoverageSummary(
        start_utc=start_utc,
        end_utc=end_utc,
        historical_solar_start_utc=(
            historical_start if historical_end > historical_start else None
        ),
        historical_solar_end_utc=(
            historical_end if historical_end > historical_start else None
        ),
        historical_solar_rows=historical_rows_count,
        forecast_solar_start_utc=forecast_start if forecast_end > forecast_start else None,
        forecast_solar_end_utc=forecast_end if forecast_end > forecast_start else None,
        forecast_solar_rows=forecast_rows_count,
        grid_start_utc=grid_start,
        grid_end_utc=grid_end,
        grid_missing_points=0,
    )


def print_data_pipeline_summary(summary: DataPipelineSummary) -> None:
    system = summary.system_generation or summary.system_plan
    persisted = getattr(system, "persisted", None)
    source = summary.source_maintenance
    coverage = summary.source_coverage
    source_plan = summary.source_plan
    print(
        "data pipeline completed "
        f"station_id={summary.station_id} "
        f"database_url={summary.database_url} "
        f"history_start={summary.history_start.isoformat()} "
        f"full_history={summary.full_history} "
        f"history_writes_enabled={system.history_writes_enabled} "
        f"allow_fallbacks={summary.allow_fallbacks} "
        f"dry_run={summary.dry_run} "
        f"source_days_ahead={summary.source_days_ahead} "
        f"grid_days_ahead={summary.grid_days_ahead} "
        f"source_current_local_date={source_plan.current_local_date.isoformat()} "
        f"ideal_solar_start_local={source_plan.ideal_solar_start_local.isoformat()} "
        f"ideal_solar_end_local={source_plan.ideal_solar_end_local.isoformat()} "
        f"historical_weather_start_date={source_plan.historical_weather_start_date.isoformat()} "
        f"historical_weather_end_date={_optional_date(source_plan.historical_weather_end_date)} "
        f"forecast_weather_start_date={source_plan.forecast_weather_start_date.isoformat()} "
        f"forecast_weather_end_date={source_plan.forecast_weather_end_date.isoformat()} "
        f"grid_start_date={source_plan.grid_start_date.isoformat()} "
        f"grid_end_date={source_plan.grid_end_date.isoformat()} "
        f"system_start_utc={system.start_utc.isoformat()} "
        f"system_end_utc={system.end_utc.isoformat()} "
        f"load_cache_end_utc={system.load_cache_end_utc.isoformat()} "
        f"battery_cache_end_utc={system.battery_cache_end_utc.isoformat()} "
        f"ems_cache_end_utc={system.ems_cache_end_utc.isoformat()} "
        f"source_ideal_rows={_source_rows(source, 'ideal_solar')} "
        f"source_weather_history_rows={_weather_rows(source, 'historical_rows_inserted')} "
        f"source_weather_forecast_rows={_weather_rows(source, 'forecast_rows_inserted')} "
        f"source_historical_solar_rows={_source_rows(source, 'historical_adjusted_solar')} "
        f"source_forecast_solar_rows={_source_rows(source, 'forecast_adjusted_solar')} "
        f"grid_rows_inserted={_grid_rows(source)} "
        f"interpolated_solar_rows={_source_rows(source, 'interpolated_solar_cache')} "
        f"validated_historical_solar_rows={0 if coverage is None else coverage.historical_solar_rows} "
        f"validated_forecast_solar_rows={0 if coverage is None else coverage.forecast_solar_rows} "
        f"validated_grid_missing_points={0 if coverage is None else coverage.grid_missing_points} "
        f"solar_fallback_minutes={system.solar_fallback_minutes} "
        f"grid_fallback_minutes={system.grid_fallback_minutes} "
        f"weather_fallback_minutes={system.weather_fallback_minutes} "
        f"battery_seed_source={system.battery_seed_source} "
        f"load_history_rows={_persisted_rows(persisted, 'load_history_rows')} "
        f"load_cache_rows={_persisted_rows(persisted, 'load_cache_rows')} "
        f"battery_history_rows={_persisted_rows(persisted, 'battery_history_rows')} "
        f"battery_cache_rows={_persisted_rows(persisted, 'battery_cache_rows')} "
        f"ems_history_rows={_persisted_rows(persisted, 'ems_history_rows')} "
        f"ems_cache_rows={_persisted_rows(persisted, 'ems_cache_rows')}",
        flush=True,
    )


def _plan_system_generation(
    *,
    config_path: Path,
    database_url: str | None,
    history_start: date,
    full_history: bool,
    allow_fallbacks: bool,
    now: datetime,
) -> generate_system_simulation.SystemGenerationSummary:
    return generate_system_simulation.run_system_simulation_generation(
        config_path=config_path,
        database_url=database_url,
        history_start_date=history_start,
        cache_only=not full_history,
        replace_existing=True,
        now=now,
        allow_fallbacks=allow_fallbacks,
        dry_run=True,
    )


def _validate_complete_source_rows(
    *,
    rows: list[object],
    timestamp_attr: str,
    start_utc: datetime,
    end_utc: datetime,
    timestep_minutes: int,
    label: str,
) -> None:
    try:
        solar_data_scheduler._validate_complete_coverage(
            rows,
            timestamp_attr=timestamp_attr,
            start_utc=start_utc,
            end_utc=end_utc,
            timestep_minutes=timestep_minutes,
            label=label,
        )
    except RuntimeError as exc:
        raise SourceCoverageError(
            f"{label} coverage is insufficient: requested "
            f"{start_utc.isoformat()} through {end_utc.isoformat()} exclusive; {exc}"
        ) from exc


def _validate_weather_state_rows(rows: list[object], label: str) -> None:
    missing = [
        getattr(row, "timestamp_utc")
        for row in rows
        if not getattr(row, "weather_state", None)
    ]
    if missing:
        preview = ", ".join(_as_utc(item).isoformat() for item in missing[:5])
        suffix = "" if len(missing) <= 5 else ", ..."
        raise SourceCoverageError(
            f"{label} has missing weather-derived state in {len(missing)} rows "
            f"({preview}{suffix})"
        )


def _database_url_from_args(args: argparse.Namespace) -> str:
    if args.db_path is None:
        return args.database_url
    return f"sqlite:///{args.db_path}"


def _floor_to_cadence(value: datetime, cadence_minutes: int) -> datetime:
    if cadence_minutes <= 0:
        raise ValueError("cadence_minutes must be greater than 0")
    normalized = _as_utc(value)
    minute = (normalized.minute // cadence_minutes) * cadence_minutes
    return normalized.replace(minute=minute, second=0, microsecond=0)


def _ceil_to_cadence(value: datetime, cadence_minutes: int) -> datetime:
    floored = _floor_to_cadence(value, cadence_minutes)
    if floored == _as_utc(value):
        return floored
    return floored + timedelta(minutes=cadence_minutes)


def _resolve_now_utc(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    return _as_utc(now)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime is required")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _source_rows(source: SourceMaintenanceSummary | None, field_name: str) -> int:
    if source is None:
        return 0
    return int(getattr(getattr(source, field_name), "rows", 0))


def _weather_rows(source: SourceMaintenanceSummary | None, field_name: str) -> int:
    if source is None:
        return 0
    return int(getattr(source.weather_cache, field_name, 0))


def _grid_rows(source: SourceMaintenanceSummary | None) -> int:
    if source is None:
        return 0
    return int(getattr(source.grid_availability, "availability_rows_inserted", 0))


def _persisted_rows(persisted: object | None, field_name: str) -> int:
    if persisted is None:
        return 0
    return int(getattr(persisted, field_name, 0))


def _optional_date(value: date | None) -> str:
    return "none" if value is None else value.isoformat()


if __name__ == "__main__":
    main()
