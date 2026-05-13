from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from sqlmodel import Session


SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = Path(__file__).resolve().parents[1]
for path in (SCRIPTS_DIR, BACKEND_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.config_loader import load_config
from app.schemas import AppConfig
from app.simulation.grid import (
    DEFAULT_GRID_HISTORY_START,
    DEFAULT_GRID_SIMULATION_SEED,
    GRID_AVAILABILITY_CADENCE_MINUTES,
    GridAvailabilityPoint,
    GridDamageEvent,
    GridSimulationSettings,
    generate_grid_availability_points,
)
from app.storage.database import create_db_and_tables, get_engine
from app.storage.grid_repository import (
    GridAvailabilityPointRecord,
    GridDamageEventRecord,
    encode_metadata,
    find_missing_grid_availability_timestamps,
    get_grid_availability_range,
    save_grid_availability_points,
    save_grid_damage_events,
)


DEFAULT_CONFIG_PATH = Path("backend/config/station.default.yaml")
DEFAULT_DATABASE_URL = "sqlite:///backend/data/smartenergy.db"
DEFAULT_DAYS_AHEAD = 7


@dataclass(frozen=True)
class GridGenerationSummary:
    start_date: date
    end_date: date
    timezone_name: str
    seed: int
    generated_points: int
    missing_points_before_insert: int
    availability_rows_inserted: int
    damage_events_generated: int
    damage_events_inserted: int


def main() -> None:
    args = parse_args()
    summary = run_grid_availability_generation(
        config_path=args.config,
        database_url=_database_url_from_args(args),
        start_date=args.start_date,
        end_date=args.end_date,
        days_ahead=args.days_ahead,
        seed=args.seed,
    )
    print(
        "grid availability generation completed "
        f"start_date={summary.start_date.isoformat()} "
        f"end_date={summary.end_date.isoformat()} "
        f"timezone={summary.timezone_name} "
        f"seed={summary.seed} "
        f"generated_points={summary.generated_points} "
        f"missing_before_insert={summary.missing_points_before_insert} "
        f"availability_rows_inserted={summary.availability_rows_inserted} "
        f"damage_events_generated={summary.damage_events_generated} "
        f"damage_events_inserted={summary.damage_events_inserted}",
        flush=True,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate deterministic grid damage and availability cache data.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--start-date", type=_parse_date, default=None)
    parser.add_argument("--end-date", type=_parse_date, default=None)
    parser.add_argument("--days-ahead", type=int, default=DEFAULT_DAYS_AHEAD)
    parser.add_argument("--seed", type=int, default=DEFAULT_GRID_SIMULATION_SEED)
    args = parser.parse_args(argv)

    if args.days_ahead < 0:
        parser.error("--days-ahead must be 0 or greater")
    if args.start_date is not None and args.end_date is not None:
        if args.end_date < args.start_date:
            parser.error("--end-date must be the same as or later than --start-date")
    return args


def run_grid_availability_generation(
    config_path: Path,
    database_url: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    seed: int = DEFAULT_GRID_SIMULATION_SEED,
    now: datetime | None = None,
) -> GridGenerationSummary:
    if days_ahead < 0:
        raise ValueError("days_ahead must be 0 or greater")

    config = load_config(config_path)
    settings = grid_settings_from_config(config, seed=seed)
    timezone_info = ZoneInfo(settings.local_timezone)
    current_local_date = _resolve_now_utc(now).astimezone(timezone_info).date()
    resolved_end_date = end_date or current_local_date + timedelta(days=days_ahead)

    engine = get_engine(database_url)
    create_db_and_tables(engine)
    with Session(engine) as session:
        existing_start_utc, _ = get_grid_availability_range(session)
        resolved_start_date = _resolve_start_date(
            explicit_start_date=start_date,
            existing_start_utc=existing_start_utc,
            timezone_info=timezone_info,
        )
        generated_points, generated_events = generate_grid_availability_points(
            resolved_start_date,
            resolved_end_date,
            settings=settings,
        )
        start_utc, end_utc = _date_range_to_utc_bounds(
            resolved_start_date,
            resolved_end_date,
            timezone_info,
        )
        missing_timestamps = find_missing_grid_availability_timestamps(
            session,
            start_utc,
            end_utc,
            cadence_minutes=GRID_AVAILABILITY_CADENCE_MINUTES,
        )
        event_rows = [_damage_event_record(event) for event in generated_events]
        point_rows = [_availability_point_record(point) for point in generated_points]
        inserted_events = save_grid_damage_events(session, event_rows)
        inserted_points = save_grid_availability_points(session, point_rows)

    return GridGenerationSummary(
        start_date=resolved_start_date,
        end_date=resolved_end_date,
        timezone_name=settings.local_timezone,
        seed=settings.outage_schedule_seed,
        generated_points=len(generated_points),
        missing_points_before_insert=len(missing_timestamps),
        availability_rows_inserted=inserted_points,
        damage_events_generated=len(generated_events),
        damage_events_inserted=inserted_events,
    )


def grid_settings_from_config(
    config: AppConfig,
    seed: int | None = None,
) -> GridSimulationSettings:
    grid_config = getattr(config.station, "grid", None)
    if grid_config is None:
        return GridSimulationSettings(
            outage_schedule_seed=seed or DEFAULT_GRID_SIMULATION_SEED,
        )

    return GridSimulationSettings(
        base_delivery_health_percent=grid_config.base_delivery_health_percent,
        base_generation_health_percent=grid_config.base_generation_health_percent,
        regeneration_cap_percent=grid_config.regeneration_cap_percent,
        minimum_health_percent=grid_config.minimum_health_percent,
        outage_queue=grid_config.outage_queue,
        outage_schedule_seed=(
            seed if seed is not None else grid_config.outage_schedule_seed
        ),
        local_timezone=grid_config.local_timezone,
    )


def _resolve_start_date(
    explicit_start_date: date | None,
    existing_start_utc: datetime | None,
    timezone_info: ZoneInfo,
) -> date:
    if explicit_start_date is not None:
        return explicit_start_date
    if existing_start_utc is not None:
        return existing_start_utc.astimezone(timezone_info).date()
    return DEFAULT_GRID_HISTORY_START


def _damage_event_record(event: GridDamageEvent) -> GridDamageEventRecord:
    return GridDamageEventRecord(
        event_key=event.event_key,
        event_date=event.event_date.isoformat(),
        event_timestamp_utc=event.event_timestamp_utc,
        attack_state=event.attack_state,
        kyiv_focus_mode=event.kyiv_focus_mode,
        element_type=event.element_type,
        damage_class=event.damage_class,
        raw_damage_percent=event.raw_damage_percent,
        applied_generation_damage_percent=event.applied_generation_damage_percent,
        applied_delivery_damage_percent=event.applied_delivery_damage_percent,
        recovery_days=event.recovery_days,
        seed=event.seed,
        metadata_json=encode_metadata(event.metadata),
    )


def _availability_point_record(
    point: GridAvailabilityPoint,
) -> GridAvailabilityPointRecord:
    return GridAvailabilityPointRecord(
        timestamp_utc=point.timestamp_utc,
        timestamp_local=point.timestamp_local,
        generation_health_percent=point.generation_health_percent,
        delivery_health_percent=point.delivery_health_percent,
        effective_health_percent=point.effective_health_percent,
        deficit_percent=point.deficit_percent,
        daily_outage_hours=point.daily_outage_hours,
        outage_level=point.outage_level,
        outage_queue=point.outage_queue,
        local_grid_available=point.local_grid_available,
        is_outage_now=point.is_outage_now,
        grid_voltage_v=point.grid_voltage_v,
        reason=point.reason,
        current_outage_window_start_utc=point.current_outage_window_start_utc,
        current_outage_window_end_utc=point.current_outage_window_end_utc,
        next_outage_window_start_utc=point.next_outage_window_start_utc,
        next_outage_window_end_utc=point.next_outage_window_end_utc,
    )


def _date_range_to_utc_bounds(
    start_date: date,
    end_date: date,
    timezone_info: ZoneInfo,
) -> tuple[datetime, datetime]:
    start_local = datetime.combine(start_date, datetime_time.min, tzinfo=timezone_info)
    end_local = datetime.combine(
        end_date + timedelta(days=1),
        datetime_time.min,
        tzinfo=timezone_info,
    )
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _database_url_from_args(args: argparse.Namespace) -> str:
    if args.db_path is None:
        return args.database_url
    return f"sqlite:///{args.db_path}"


def _resolve_now_utc(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc).replace(microsecond=0)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


if __name__ == "__main__":
    main()
