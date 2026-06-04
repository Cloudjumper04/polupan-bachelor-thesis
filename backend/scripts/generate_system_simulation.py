from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timezone
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from sqlmodel import Session


SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = Path(__file__).resolve().parents[1]
for path in (SCRIPTS_DIR, BACKEND_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.config_loader import calculate_system_config_hash, load_config
from app.schemas import AppConfig
from app.simulation.engine import (
    SimulationFallbackError,
    SystemSimulationPersistSummary,
    SystemSimulationWindows,
    build_default_system_simulation_windows,
    persist_integrated_system_result,
    simulate_integrated_system_window,
)
from app.storage.database import create_db_and_tables, get_engine


DEFAULT_CONFIG_PATH = Path("backend/config/station.default.yaml")
DEFAULT_DATABASE_URL = "sqlite:///backend/data/smartenergy.db"
DEFAULT_LOAD_DAYS_AHEAD = 2
DEFAULT_BATTERY_DAYS_AHEAD = 2
DEFAULT_EMS_DAYS_AHEAD = 2


@dataclass(frozen=True)
class SystemGenerationSummary:
    station_id: str
    config_hash: str
    start_utc: datetime
    end_utc: datetime
    cache_only: bool
    history_writes_enabled: bool
    allow_fallbacks: bool
    dry_run: bool
    load_cache_start_utc: datetime
    load_cache_end_utc: datetime
    battery_cache_start_utc: datetime
    battery_cache_end_utc: datetime
    ems_cache_start_utc: datetime
    ems_cache_end_utc: datetime
    persisted: SystemSimulationPersistSummary | None = None
    solar_fallback_minutes: int = 0
    grid_fallback_minutes: int = 0
    weather_fallback_minutes: int = 0
    battery_seed_source: str = "not_simulated"
    battery_seed_timestamp_utc: datetime | None = None


def main() -> None:
    args = parse_args()
    try:
        summary = run_system_simulation_generation(
            config_path=args.config,
            database_url=_database_url_from_args(args),
            start_utc=args.start,
            end_utc=args.end,
            history_start_date=args.history_start,
            cache_only=args.cache_only,
            load_days_ahead=args.load_days_ahead,
            battery_days_ahead=args.battery_days_ahead,
            ems_days_ahead=args.ems_days_ahead,
            replace_existing=not args.no_replace,
            allow_fallbacks=args.allow_fallbacks,
            dry_run=args.dry_run,
        )
    except SimulationFallbackError as exc:
        print(f"system simulation generation failed: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2) from exc
    print(
        "system simulation generation completed "
        f"station_id={summary.station_id} "
        f"config_hash={summary.config_hash} "
        f"start_utc={summary.start_utc.isoformat()} "
        f"end_utc={summary.end_utc.isoformat()} "
        f"cache_only={summary.cache_only} "
        f"history_writes_enabled={summary.history_writes_enabled} "
        f"allow_fallbacks={summary.allow_fallbacks} "
        f"dry_run={summary.dry_run} "
        f"load_cache_start_utc={summary.load_cache_start_utc.isoformat()} "
        f"load_cache_end_utc={summary.load_cache_end_utc.isoformat()} "
        f"battery_cache_start_utc={summary.battery_cache_start_utc.isoformat()} "
        f"battery_cache_end_utc={summary.battery_cache_end_utc.isoformat()} "
        f"ems_cache_start_utc={summary.ems_cache_start_utc.isoformat()} "
        f"ems_cache_end_utc={summary.ems_cache_end_utc.isoformat()} "
        f"solar_fallback_minutes={summary.solar_fallback_minutes} "
        f"grid_fallback_minutes={summary.grid_fallback_minutes} "
        f"weather_fallback_minutes={summary.weather_fallback_minutes} "
        f"battery_seed_source={summary.battery_seed_source} "
        f"battery_seed_timestamp_utc={_optional_iso(summary.battery_seed_timestamp_utc)} "
        f"load_history_rows={_persisted_rows(summary, 'load_history_rows')} "
        f"load_cache_rows={_persisted_rows(summary, 'load_cache_rows')} "
        f"battery_history_rows={_persisted_rows(summary, 'battery_history_rows')} "
        f"battery_cache_rows={_persisted_rows(summary, 'battery_cache_rows')} "
        f"ems_history_rows={_persisted_rows(summary, 'ems_history_rows')} "
        f"ems_cache_rows={_persisted_rows(summary, 'ems_cache_rows')}",
        flush=True,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate integrated Load, Battery, and EMS dashboard data.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument(
        "--history-start",
        type=_parse_date,
        default=None,
        help="Override station.installation_date for default full-history generation.",
    )
    parser.add_argument("--start", type=_parse_datetime, default=None)
    parser.add_argument("--end", type=_parse_datetime, default=None)
    parser.set_defaults(cache_only=True)
    parser.add_argument(
        "--cache-only",
        dest="cache_only",
        action="store_true",
        help="Generate only the live cache window from local midnight through horizons.",
    )
    parser.add_argument(
        "--full-history",
        dest="cache_only",
        action="store_false",
        help="Write history from --history-start or station.installation_date plus cache.",
    )
    parser.add_argument("--load-days-ahead", type=int, default=DEFAULT_LOAD_DAYS_AHEAD)
    parser.add_argument(
        "--battery-days-ahead",
        type=int,
        default=DEFAULT_BATTERY_DAYS_AHEAD,
    )
    parser.add_argument("--ems-days-ahead", type=int, default=DEFAULT_EMS_DAYS_AHEAD)
    parser.add_argument("--no-replace", action="store_true")
    parser.add_argument("--allow-fallbacks", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.load_days_ahead < 0:
        parser.error("--load-days-ahead must be 0 or greater")
    if args.battery_days_ahead < 0:
        parser.error("--battery-days-ahead must be 0 or greater")
    if args.ems_days_ahead < 0:
        parser.error("--ems-days-ahead must be 0 or greater")
    if (args.start is None) != (args.end is None):
        parser.error("--start and --end must be provided together")
    if args.start is not None and args.end <= args.start:
        parser.error("--end must be later than --start")
    return args


def run_system_simulation_generation(
    config_path: Path,
    database_url: str | None = None,
    *,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
    history_start_date: date | None = None,
    cache_only: bool = True,
    load_days_ahead: int = DEFAULT_LOAD_DAYS_AHEAD,
    battery_days_ahead: int = DEFAULT_BATTERY_DAYS_AHEAD,
    ems_days_ahead: int = DEFAULT_EMS_DAYS_AHEAD,
    replace_existing: bool = True,
    now: datetime | None = None,
    allow_fallbacks: bool = False,
    dry_run: bool = False,
) -> SystemGenerationSummary:
    if load_days_ahead < 0:
        raise ValueError("load_days_ahead must be 0 or greater")
    if battery_days_ahead < 0:
        raise ValueError("battery_days_ahead must be 0 or greater")
    if ems_days_ahead < 0:
        raise ValueError("ems_days_ahead must be 0 or greater")
    if (start_utc is None) != (end_utc is None):
        raise ValueError("start_utc and end_utc must be provided together")

    config = load_config(config_path)
    timezone_info = ZoneInfo(config.station.solar.installation.timezone)
    resolved_now = _resolve_now_utc(now)
    history_enabled = not cache_only
    windows = _build_windows(
        config,
        timezone_info,
        resolved_now,
        start_utc=start_utc,
        end_utc=end_utc,
        history_start_date=history_start_date,
        history_enabled=history_enabled,
        load_days_ahead=load_days_ahead,
        battery_days_ahead=battery_days_ahead,
        ems_days_ahead=ems_days_ahead,
    )
    config_hash = calculate_system_config_hash(config)

    if dry_run:
        return SystemGenerationSummary(
            station_id=config.station.id,
            config_hash=config_hash,
            start_utc=windows.start_utc,
            end_utc=windows.end_utc,
            cache_only=cache_only,
            history_writes_enabled=history_enabled,
            allow_fallbacks=allow_fallbacks,
            dry_run=True,
            load_cache_start_utc=windows.load_cache_start_utc,
            load_cache_end_utc=windows.load_cache_end_utc,
            battery_cache_start_utc=windows.battery_cache_start_utc,
            battery_cache_end_utc=windows.battery_cache_end_utc,
            ems_cache_start_utc=windows.ems_cache_start_utc,
            ems_cache_end_utc=windows.ems_cache_end_utc,
        )

    engine = get_engine(database_url)
    create_db_and_tables(engine)
    with Session(engine) as session:
        result = simulate_integrated_system_window(
            session,
            config,
            windows,
            allow_fallbacks=allow_fallbacks,
        )
        persisted = persist_integrated_system_result(
            session,
            result,
            replace_existing=replace_existing,
        )

    return SystemGenerationSummary(
        station_id=result.station_id,
        config_hash=result.config_hash,
        start_utc=result.start_utc,
        end_utc=result.end_utc,
        cache_only=cache_only,
        history_writes_enabled=history_enabled,
        allow_fallbacks=allow_fallbacks,
        dry_run=False,
        load_cache_start_utc=windows.load_cache_start_utc,
        load_cache_end_utc=windows.load_cache_end_utc,
        battery_cache_start_utc=windows.battery_cache_start_utc,
        battery_cache_end_utc=windows.battery_cache_end_utc,
        ems_cache_start_utc=windows.ems_cache_start_utc,
        ems_cache_end_utc=windows.ems_cache_end_utc,
        persisted=persisted,
        solar_fallback_minutes=result.fallbacks.solar_fallback_minutes,
        grid_fallback_minutes=result.fallbacks.grid_fallback_minutes,
        weather_fallback_minutes=result.fallbacks.weather_fallback_minutes,
        battery_seed_source=result.seed.battery_seed_source,
        battery_seed_timestamp_utc=result.seed.battery_seed_timestamp_utc,
    )


def _build_windows(
    config: AppConfig,
    timezone_info: ZoneInfo,
    now_utc: datetime,
    *,
    start_utc: datetime | None,
    end_utc: datetime | None,
    history_start_date: date | None,
    history_enabled: bool,
    load_days_ahead: int,
    battery_days_ahead: int,
    ems_days_ahead: int,
) -> SystemSimulationWindows:
    if start_utc is not None and end_utc is not None:
        start = _as_utc(start_utc)
        end = _as_utc(end_utc)
        if end <= start:
            raise ValueError("end_utc must be later than start_utc")
        history_end = min(now_utc, end) if history_enabled else None
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

    if not history_enabled:
        return build_default_system_simulation_windows(
            now_utc,
            timezone_info,
            load_days_ahead=load_days_ahead,
            battery_days_ahead=battery_days_ahead,
            ems_days_ahead=ems_days_ahead,
            history_enabled=False,
        )

    install_start = _station_installation_start_utc(
        config,
        timezone_info,
        history_start_date=history_start_date,
    )
    default_cache = build_default_system_simulation_windows(
        now_utc,
        timezone_info,
        load_days_ahead=load_days_ahead,
        battery_days_ahead=battery_days_ahead,
        ems_days_ahead=ems_days_ahead,
        history_enabled=True,
    )
    return SystemSimulationWindows(
        start_utc=install_start,
        end_utc=default_cache.end_utc,
        history_end_utc=now_utc,
        load_cache_start_utc=default_cache.load_cache_start_utc,
        load_cache_end_utc=default_cache.load_cache_end_utc,
        battery_cache_start_utc=default_cache.battery_cache_start_utc,
        battery_cache_end_utc=default_cache.battery_cache_end_utc,
        ems_cache_start_utc=default_cache.ems_cache_start_utc,
        ems_cache_end_utc=default_cache.ems_cache_end_utc,
    )


def _station_installation_start_utc(
    config: AppConfig,
    timezone_info: ZoneInfo,
    *,
    history_start_date: date | None = None,
) -> datetime:
    install_date = history_start_date or datetime.strptime(
        config.station.installation_date,
        "%Y-%m-%d",
    ).date()
    install_local = datetime.combine(
        install_date,
        datetime_time.min,
        tzinfo=timezone_info,
    )
    return install_local.astimezone(timezone.utc)


def _database_url_from_args(args: argparse.Namespace) -> str:
    if args.db_path is None:
        return args.database_url
    return f"sqlite:///{args.db_path}"


def _resolve_now_utc(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    return _as_utc(now)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime is required")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _parse_datetime(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    return _as_utc(parsed)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _persisted_rows(summary: SystemGenerationSummary, field_name: str) -> int:
    if summary.persisted is None:
        return 0
    return int(getattr(summary.persisted, field_name))


def _optional_iso(value: datetime | None) -> str:
    return "none" if value is None else value.isoformat()


if __name__ == "__main__":
    main()
