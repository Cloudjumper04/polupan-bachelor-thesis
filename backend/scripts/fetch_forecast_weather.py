from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config_loader import load_config
from app.simulation.weather import (
    OPEN_METEO_FORECAST_SOURCE,
    WeatherForecastData,
    fetch_open_meteo_forecast,
)
from app.storage.database import create_db_and_tables, get_engine
from app.storage.forecast_repository import (
    WeatherForecast,
    delete_forecast_for_station,
    save_forecast_rows,
)


DEFAULT_CONFIG_PATH = BACKEND_DIR / "config" / "station.default.yaml"
DEFAULT_DAYS_AHEAD = 2


def main() -> None:
    args = _parse_args()
    config = load_config(args.config)
    installation = config.station.solar.installation
    station_timezone = ZoneInfo(installation.timezone)
    requested_start_date: date | None = None
    requested_end_date: date | None = None

    if args.forecast_hours is None:
        requested_start_date, requested_end_date = _resolve_forecast_date_window(
            station_timezone,
            args.days_ahead,
        )
        forecasts = fetch_open_meteo_forecast(
            latitude=installation.latitude,
            longitude=installation.longitude,
            timezone=installation.timezone,
            start_date=requested_start_date,
            end_date=requested_end_date,
        )
    else:
        forecasts = fetch_open_meteo_forecast(
            latitude=installation.latitude,
            longitude=installation.longitude,
            timezone=installation.timezone,
            forecast_hours=args.forecast_hours,
        )
    fetched_at_utc = datetime.now(timezone.utc)
    rows = _to_db_rows(
        forecasts,
        station_id=config.station.id,
        fetched_at_utc=fetched_at_utc,
    )
    forecast_timestamps = [
        forecast.forecast_timestamp_utc for forecast in forecasts
    ]
    source = forecasts[0].source if forecasts else OPEN_METEO_FORECAST_SOURCE

    engine = get_engine(args.database_url)
    create_db_and_tables(engine)
    with Session(engine) as session:
        delete_forecast_for_station(session, config.station.id)
        save_forecast_rows(session, rows)

    print(f"station id: {config.station.id}")
    print(f"timezone: {station_timezone.key}")
    print(f"fetched_at_utc: {fetched_at_utc.isoformat()}")
    print(
        "requested start_date: "
        f"{requested_start_date.isoformat() if requested_start_date else 'not used'}"
    )
    print(
        "requested end_date: "
        f"{requested_end_date.isoformat() if requested_end_date else 'not used'}"
    )
    print(f"rows fetched: {len(rows)}")
    print(
        "actual stored min forecast timestamp UTC: "
        f"{min(forecast_timestamps).isoformat() if forecast_timestamps else 'none'}"
    )
    print(
        "actual stored max forecast timestamp UTC: "
        f"{max(forecast_timestamps).isoformat() if forecast_timestamps else 'none'}"
    )
    print(f"source: {source}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch and cache Open-Meteo forecast weather.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--days-ahead", type=int, default=DEFAULT_DAYS_AHEAD)
    parser.add_argument("--forecast-hours", type=int, default=None)
    args = parser.parse_args()

    if args.days_ahead < 0:
        parser.error("--days-ahead must be 0 or greater")
    if args.forecast_hours is not None and args.forecast_hours <= 0:
        parser.error("--forecast-hours must be greater than 0")
    return args


def _resolve_forecast_date_window(
    station_timezone: ZoneInfo,
    days_ahead: int,
    now: datetime | None = None,
) -> tuple[date, date]:
    if days_ahead < 0:
        raise ValueError("days_ahead must be 0 or greater")
    local_now = (
        now.astimezone(station_timezone)
        if now
        else datetime.now(station_timezone)
    )
    start_date = local_now.date()
    end_date = date.fromordinal(start_date.toordinal() + days_ahead)
    return start_date, end_date


def _to_db_rows(
    forecasts: list[WeatherForecastData],
    station_id: str,
    fetched_at_utc: datetime,
) -> list[WeatherForecast]:
    return [
        WeatherForecast(
            station_id=station_id,
            fetched_at_utc=fetched_at_utc,
            forecast_timestamp_utc=forecast.forecast_timestamp_utc,
            forecast_timestamp_local=forecast.forecast_timestamp_local,
            weather_code=forecast.weather_code,
            cloud_cover_percent=forecast.cloud_cover_percent,
            precipitation_mm=forecast.precipitation_mm,
            rain_mm=forecast.rain_mm,
            snowfall_cm=forecast.snowfall_cm,
            shortwave_radiation_w_m2=forecast.shortwave_radiation_w_m2,
            direct_radiation_w_m2=forecast.direct_radiation_w_m2,
            diffuse_radiation_w_m2=forecast.diffuse_radiation_w_m2,
            source=forecast.source,
            resolution_minutes=forecast.resolution_minutes,
        )
        for forecast in forecasts
    ]


if __name__ == "__main__":
    main()
