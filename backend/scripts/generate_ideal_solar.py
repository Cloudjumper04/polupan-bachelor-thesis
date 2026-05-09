from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config_loader import calculate_config_hash, load_config
from app.simulation.solar import IdealSolarGenerator, IdealSolarPoint
from app.storage.database import create_db_and_tables, get_engine
from app.storage.solar_repository import (
    IdealSolarProduction,
    delete_ideal_solar_for_config,
    save_ideal_solar_points,
)


DEFAULT_CONFIG_PATH = BACKEND_DIR / "config" / "station.default.yaml"


def main() -> None:
    args = _parse_args()
    config = load_config(args.config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    start, end = _resolve_date_range(args, station_timezone)
    config_hash = calculate_config_hash(config)
    generator = IdealSolarGenerator(config)
    points = generator.generate(start, end, args.timestep_minutes)

    engine = get_engine(args.database_url)
    create_db_and_tables(engine)
    station_id = config.station.id
    rows = [
        _to_db_model(point, station_id=station_id, config_hash=config_hash)
        for point in points
    ]
    with Session(engine) as session:
        delete_ideal_solar_for_config(session, station_id, config_hash)
        save_ideal_solar_points(session, rows)

    powers = [point.ideal_power_w for point in points]
    print(f"station id: {station_id}")
    print(f"config hash: {config_hash}")
    print(f"timezone: {station_timezone.key}")
    print(f"start datetime: {start.isoformat()}")
    print(f"end datetime: {end.isoformat()}")
    print(f"timestep minutes: {args.timestep_minutes}")
    print(f"rows generated: {len(points)}")
    print(f"min ideal power W: {min(powers) if powers else 0.0:.2f}")
    print(f"max ideal power W: {max(powers) if powers else 0.0:.2f}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate ideal solar production.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--start", type=str, default=None)
    parser.add_argument("--end", type=str, default=None)
    parser.add_argument("--timestep-minutes", type=int, default=15)
    args = parser.parse_args()

    if args.year is None and (args.start is None or args.end is None):
        parser.error("provide --year or both --start and --end")
    if args.year is not None and (args.start is not None or args.end is not None):
        parser.error("use either --year or --start/--end, not both")
    return args


def _resolve_date_range(
    args: argparse.Namespace,
    station_timezone: ZoneInfo,
) -> tuple[datetime, datetime]:
    if args.year is not None:
        return (
            datetime(args.year, 1, 1, tzinfo=station_timezone),
            datetime(args.year + 1, 1, 1, tzinfo=station_timezone),
        )
    return (
        _parse_datetime(args.start, station_timezone),
        _parse_datetime(args.end, station_timezone),
    )


def _parse_datetime(value: str, station_timezone: ZoneInfo) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=station_timezone)
    return parsed.astimezone(station_timezone)


def _to_db_model(
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


if __name__ == "__main__":
    main()
