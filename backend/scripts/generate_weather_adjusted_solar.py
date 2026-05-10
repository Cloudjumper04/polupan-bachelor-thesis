from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config_loader import calculate_config_hash, load_config
from app.simulation.weather import generate_weather_adjusted_solar
from app.storage.database import create_db_and_tables, get_engine
from app.storage.simulated_solar_repository import (
    delete_simulated_solar_for_config,
    save_simulated_solar_points,
)
from app.storage.solar_repository import IdealSolarProduction
from app.storage.weather_repository import (
    WeatherObservation,
    list_weather_observations,
)


DEFAULT_CONFIG_PATH = BACKEND_DIR / "config" / "station.default.yaml"
DEFAULT_START_DATE = date(2025, 10, 6)
SOLAR_TIMESTEP_MINUTES = 15


def main() -> None:
    args = _parse_args()
    config = load_config(args.config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    end_date = args.end or _default_historical_end_date(station_timezone)
    if end_date < args.start:
        raise RuntimeError("--end must be on or after --start")
    start_utc, end_utc = _date_range_to_utc_bounds(
        args.start,
        end_date,
        station_timezone,
    )
    station_id = config.station.id
    config_hash = calculate_config_hash(config)

    engine = get_engine(args.database_url)
    create_db_and_tables(engine)
    with Session(engine) as session:
        ideal_points = _list_ideal_solar_for_range(
            session,
            station_id=station_id,
            config_hash=config_hash,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        weather_observations = list_weather_observations(
            session,
            station_id,
            start_utc,
            end_utc,
        )
        _validate_ideal_coverage(ideal_points, start_utc, end_utc)
        _validate_weather_coverage(weather_observations, start_utc, end_utc)

        simulated_points = generate_weather_adjusted_solar(
            ideal_points,
            weather_observations,
        )
        simulated_powers = [point.simulated_power_w for point in simulated_points]
        delete_simulated_solar_for_config(
            session,
            station_id,
            config_hash,
            start_utc,
            end_utc,
        )
        save_simulated_solar_points(session, simulated_points)

    print(f"station id: {station_id}")
    print(f"config hash: {config_hash}")
    print(f"start date: {args.start.isoformat()}")
    print(f"end date: {end_date.isoformat()}")
    print(f"ideal rows used: {len(ideal_points)}")
    print(f"weather rows used: {len(weather_observations)}")
    print(f"simulated rows generated: {len(simulated_points)}")
    print(
        "min simulated power W: "
        f"{min(simulated_powers) if simulated_powers else 0.0:.2f}"
    )
    print(
        "max simulated power W: "
        f"{max(simulated_powers) if simulated_powers else 0.0:.2f}"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate weather-adjusted solar production.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--start", type=_parse_date, default=DEFAULT_START_DATE)
    parser.add_argument("--end", type=_parse_date, default=None)
    args = parser.parse_args()

    if args.end is not None and args.end < args.start:
        parser.error("--end must be on or after --start")
    return args


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _default_historical_end_date(station_timezone: ZoneInfo) -> date:
    return datetime.now(station_timezone).date() - timedelta(days=1)


def _date_range_to_utc_bounds(
    start_date: date,
    end_date: date,
    station_timezone: ZoneInfo,
) -> tuple[datetime, datetime]:
    start_local = datetime.combine(start_date, time.min, tzinfo=station_timezone)
    end_local = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
        tzinfo=station_timezone,
    )
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _list_ideal_solar_for_range(
    session: Session,
    station_id: str,
    config_hash: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[IdealSolarProduction]:
    statement = (
        select(IdealSolarProduction)
        .where(IdealSolarProduction.station_id == station_id)
        .where(IdealSolarProduction.config_hash == config_hash)
        .where(IdealSolarProduction.timestamp_utc >= start_utc)
        .where(IdealSolarProduction.timestamp_utc < end_utc)
        .order_by(IdealSolarProduction.timestamp_utc)
    )
    return list(session.exec(statement).all())


def _validate_ideal_coverage(
    ideal_points: list[IdealSolarProduction],
    start_utc: datetime,
    end_utc: datetime,
) -> None:
    expected_count = _expected_row_count(
        start_utc,
        end_utc,
        minutes=SOLAR_TIMESTEP_MINUTES,
    )
    if len(ideal_points) != expected_count:
        raise RuntimeError(
            "Ideal solar data is missing for the requested range: "
            f"expected {expected_count} rows, found {len(ideal_points)}"
        )
    if not ideal_points:
        raise RuntimeError("Ideal solar data is missing for the requested range")
    if ideal_points[0].timestamp_utc.astimezone(timezone.utc) != start_utc:
        raise RuntimeError("Ideal solar data does not start at the requested time")


def _validate_weather_coverage(
    weather_observations: list[WeatherObservation],
    start_utc: datetime,
    end_utc: datetime,
) -> None:
    expected_count = _expected_row_count(start_utc, end_utc, minutes=60)
    if len(weather_observations) != expected_count:
        raise RuntimeError(
            "Weather data is missing for the requested range: "
            f"expected {expected_count} rows, found {len(weather_observations)}"
        )
    if not weather_observations:
        raise RuntimeError("Weather data is missing for the requested range")
    if weather_observations[0].timestamp_utc.astimezone(timezone.utc) != start_utc:
        raise RuntimeError("Weather data does not start at the requested time")


def _expected_row_count(
    start_utc: datetime,
    end_utc: datetime,
    minutes: int,
) -> int:
    total_seconds = (end_utc - start_utc).total_seconds()
    return int(total_seconds // (minutes * 60))


if __name__ == "__main__":
    main()
