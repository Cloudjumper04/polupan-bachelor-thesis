from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config_loader import load_config
from app.simulation.weather import (
    OPEN_METEO_SOURCE,
    WeatherObservationData,
    fetch_open_meteo_historical_weather,
)
from app.storage.database import create_db_and_tables, get_engine
from app.storage.weather_repository import (
    WeatherObservation,
    delete_weather_observations,
    save_weather_observations,
)


DEFAULT_CONFIG_PATH = BACKEND_DIR / "config" / "station.default.yaml"
DEFAULT_START_DATE = date(2025, 10, 6)
DEFAULT_END_DATE = date(2026, 5, 8)
MAX_HISTORICAL_END_DATE = date(2026, 5, 8)


def main() -> None:
    args = _parse_args()
    config = load_config(args.config)
    installation = config.station.solar.installation
    station_timezone = ZoneInfo(installation.timezone)
    start_utc, end_utc = _date_range_to_utc_bounds(
        args.start,
        args.end,
        station_timezone,
    )

    observations = fetch_open_meteo_historical_weather(
        latitude=installation.latitude,
        longitude=installation.longitude,
        timezone=installation.timezone,
        start_date=args.start,
        end_date=args.end,
    )
    rows = [
        _to_db_model(observation, station_id=config.station.id)
        for observation in observations
    ]

    engine = get_engine(args.database_url)
    create_db_and_tables(engine)
    with Session(engine) as session:
        delete_weather_observations(
            session,
            config.station.id,
            start_utc,
            end_utc,
        )
        save_weather_observations(session, rows)

    print(f"station id: {config.station.id}")
    print(f"start date: {args.start.isoformat()}")
    print(f"end date: {args.end.isoformat()}")
    print(f"timezone: {station_timezone.key}")
    print(f"rows fetched: {len(rows)}")
    print(f"source: {OPEN_METEO_SOURCE}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch historical Open-Meteo weather observations.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--start", type=_parse_date, default=DEFAULT_START_DATE)
    parser.add_argument("--end", type=_parse_date, default=DEFAULT_END_DATE)
    args = parser.parse_args()

    if args.end < args.start:
        parser.error("--end must be on or after --start")
    if args.end > MAX_HISTORICAL_END_DATE:
        parser.error(
            "--end must not be later than "
            f"{MAX_HISTORICAL_END_DATE.isoformat()} for historical weather"
        )
    return args


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


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


def _to_db_model(
    observation: WeatherObservationData,
    station_id: str,
) -> WeatherObservation:
    return WeatherObservation(
        station_id=station_id,
        timestamp_utc=observation.timestamp_utc,
        timestamp_local=observation.timestamp_local,
        weather_code=observation.weather_code,
        cloud_cover_percent=observation.cloud_cover_percent,
        precipitation_mm=observation.precipitation_mm,
        rain_mm=observation.rain_mm,
        snowfall_cm=observation.snowfall_cm,
        shortwave_radiation_w_m2=observation.shortwave_radiation_w_m2,
        direct_radiation_w_m2=observation.direct_radiation_w_m2,
        diffuse_radiation_w_m2=observation.diffuse_radiation_w_m2,
        source=observation.source,
    )


if __name__ == "__main__":
    main()
