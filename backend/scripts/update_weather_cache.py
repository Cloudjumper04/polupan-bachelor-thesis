from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config_loader import load_config
from app.simulation.weather import (
    WeatherForecastData,
    WeatherObservationData,
    fetch_open_meteo_forecast,
    fetch_open_meteo_historical_weather,
)
from app.storage.database import create_db_and_tables, get_engine
from app.storage.forecast_repository import (
    WeatherForecast,
    delete_forecast_for_station,
    get_forecast_range,
    save_forecast_rows,
)
from app.storage.weather_repository import (
    WeatherObservation,
    delete_weather_observations_for_range,
    get_latest_weather_observation_time,
    get_weather_observation_range,
    save_weather_observations,
)


DEFAULT_CONFIG_PATH = BACKEND_DIR / "config" / "station.default.yaml"
DEFAULT_DAYS_AHEAD = 2
MAX_UTC = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


@dataclass(frozen=True)
class WeatherCacheSummary:
    station_id: str
    timezone_name: str
    current_local_date: date
    historical_backfill_start: date | None
    historical_backfill_end: date | None
    historical_rows_inserted: int
    forecast_requested_start: date
    forecast_requested_end: date
    forecast_rows_inserted: int
    final_historical_start_utc: datetime | None
    final_historical_end_utc: datetime | None
    final_historical_count: int
    final_forecast_start_utc: datetime | None
    final_forecast_end_utc: datetime | None
    final_forecast_count: int
    validation_result: str


def main() -> None:
    args = _parse_args()
    summary = update_weather_cache(
        config_path=args.config,
        database_url=args.database_url,
        history_start=args.history_start,
        days_ahead=args.days_ahead,
    )
    _print_summary(summary)


def update_weather_cache(
    config_path: Path,
    database_url: str | None,
    history_start: date | None,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    now: datetime | None = None,
) -> WeatherCacheSummary:
    if days_ahead < 0:
        raise ValueError("days_ahead must be 0 or greater")

    config = load_config(config_path)
    installation = config.station.solar.installation
    station_timezone = ZoneInfo(installation.timezone)
    current_local_date = _resolve_current_local_date(station_timezone, now)
    yesterday = current_local_date - timedelta(days=1)
    forecast_end_date = current_local_date + timedelta(days=days_ahead)
    station_id = config.station.id

    engine = get_engine(database_url)
    create_db_and_tables(engine)
    with Session(engine) as session:
        _delete_current_and_future_observations(
            session,
            station_id,
            current_local_date,
            station_timezone,
        )

        backfill_start = _resolve_historical_backfill_start(
            session=session,
            station_id=station_id,
            station_timezone=station_timezone,
            yesterday=yesterday,
            history_start=history_start,
        )
        historical_rows_inserted = 0
        if backfill_start is not None:
            historical_rows = _fetch_historical_rows(
                latitude=installation.latitude,
                longitude=installation.longitude,
                timezone_name=installation.timezone,
                station_id=station_id,
                start_date=backfill_start,
                end_date=yesterday,
            )
            start_utc, end_utc = _date_range_to_utc_bounds(
                backfill_start,
                yesterday,
                station_timezone,
            )
            delete_weather_observations_for_range(
                session,
                station_id,
                start_utc,
                end_utc,
            )
            save_weather_observations(session, historical_rows)
            historical_rows_inserted = len(historical_rows)

        forecast_rows = _fetch_forecast_rows(
            latitude=installation.latitude,
            longitude=installation.longitude,
            timezone_name=installation.timezone,
            station_id=station_id,
            start_date=current_local_date,
            end_date=forecast_end_date,
        )
        delete_forecast_for_station(session, station_id)
        save_forecast_rows(session, forecast_rows)

        validate_weather_cache(
            session=session,
            station_id=station_id,
            station_timezone=station_timezone,
            current_local_date=current_local_date,
            days_ahead=days_ahead,
        )

        final_historical_start, final_historical_end = get_weather_observation_range(
            session,
            station_id,
        )
        final_forecast_start, final_forecast_end = get_forecast_range(
            session,
            station_id,
        )
        return WeatherCacheSummary(
            station_id=station_id,
            timezone_name=installation.timezone,
            current_local_date=current_local_date,
            historical_backfill_start=backfill_start,
            historical_backfill_end=yesterday if backfill_start else None,
            historical_rows_inserted=historical_rows_inserted,
            forecast_requested_start=current_local_date,
            forecast_requested_end=forecast_end_date,
            forecast_rows_inserted=len(forecast_rows),
            final_historical_start_utc=final_historical_start,
            final_historical_end_utc=final_historical_end,
            final_historical_count=_count_weather_observations(session, station_id),
            final_forecast_start_utc=final_forecast_start,
            final_forecast_end_utc=final_forecast_end,
            final_forecast_count=_count_forecast_rows(session, station_id),
            validation_result="ok",
        )


def validate_weather_cache(
    session: Session,
    station_id: str,
    station_timezone: ZoneInfo,
    current_local_date: date,
    days_ahead: int,
) -> None:
    yesterday = current_local_date - timedelta(days=1)
    forecast_end_date = current_local_date + timedelta(days=days_ahead)
    historical_start_utc, historical_end_utc = get_weather_observation_range(
        session,
        station_id,
    )
    forecast_start_utc, forecast_end_utc = get_forecast_range(session, station_id)

    if historical_end_utc is None:
        raise RuntimeError("Historical weather cache is empty after maintenance")
    if forecast_start_utc is None or forecast_end_utc is None:
        raise RuntimeError("Forecast weather cache is empty after maintenance")

    historical_end_local = historical_end_utc.astimezone(station_timezone)
    forecast_start_local = forecast_start_utc.astimezone(station_timezone)
    forecast_end_local = forecast_end_utc.astimezone(station_timezone)

    if historical_start_utc is not None:
        historical_start_local = historical_start_utc.astimezone(station_timezone)
        if historical_start_local.date() >= current_local_date:
            raise RuntimeError(
                "Historical weather cache starts in the forecast period: "
                f"{historical_start_local.isoformat()}"
            )

    if historical_end_local.date() != yesterday:
        raise RuntimeError(
            "Historical weather cache must end on yesterday "
            f"({yesterday.isoformat()}), found "
            f"{historical_end_local.date().isoformat()}"
        )
    if historical_end_local.timetz().replace(tzinfo=None) != time(23, 0):
        raise RuntimeError(
            "Historical weather cache must end at 23:00 local time, found "
            f"{historical_end_local.isoformat()}"
        )
    if forecast_start_local.date() != current_local_date:
        raise RuntimeError(
            "Forecast weather cache must start on the current local date "
            f"({current_local_date.isoformat()}), found "
            f"{forecast_start_local.date().isoformat()}"
        )
    if forecast_start_local.timetz().replace(tzinfo=None) != time(0, 0):
        raise RuntimeError(
            "Forecast weather cache must start at 00:00 local time, found "
            f"{forecast_start_local.isoformat()}"
        )
    if forecast_end_local.date() != forecast_end_date:
        raise RuntimeError(
            "Forecast weather cache must end on "
            f"{forecast_end_date.isoformat()}, found "
            f"{forecast_end_local.date().isoformat()}"
        )
    if forecast_end_local.timetz().replace(tzinfo=None) != time(23, 0):
        raise RuntimeError(
            "Forecast weather cache must end at 23:00 local time, found "
            f"{forecast_end_local.isoformat()}"
        )
    if historical_end_local.date() + timedelta(days=1) != forecast_start_local.date():
        raise RuntimeError(
            "Weather cache is not continuous by date: historical ends on "
            f"{historical_end_local.date().isoformat()} and forecast starts on "
            f"{forecast_start_local.date().isoformat()}"
        )
    if historical_end_local.date() >= forecast_start_local.date():
        raise RuntimeError(
            "Weather cache overlaps by date: historical ends on "
            f"{historical_end_local.date().isoformat()} and forecast starts on "
            f"{forecast_start_local.date().isoformat()}"
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Maintain historical and forecast weather cache windows.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--history-start", type=_parse_date, default=None)
    parser.add_argument("--days-ahead", type=int, default=DEFAULT_DAYS_AHEAD)
    args = parser.parse_args()

    if args.days_ahead < 0:
        parser.error("--days-ahead must be 0 or greater")
    return args


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _resolve_current_local_date(
    station_timezone: ZoneInfo,
    now: datetime | None = None,
) -> date:
    if now is None:
        return datetime.now(station_timezone).date()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(station_timezone).date()


def _delete_current_and_future_observations(
    session: Session,
    station_id: str,
    current_local_date: date,
    station_timezone: ZoneInfo,
) -> None:
    start_utc, _ = _date_range_to_utc_bounds(
        current_local_date,
        current_local_date,
        station_timezone,
    )
    delete_weather_observations_for_range(session, station_id, start_utc, MAX_UTC)


def _resolve_historical_backfill_start(
    session: Session,
    station_id: str,
    station_timezone: ZoneInfo,
    yesterday: date,
    history_start: date | None,
) -> date | None:
    latest_observation_utc = get_latest_weather_observation_time(session, station_id)
    if latest_observation_utc is None:
        if history_start is None:
            raise RuntimeError(
                "Historical weather cache is empty; provide --history-start "
                "YYYY-MM-DD to initialize it"
            )
        if history_start > yesterday:
            raise RuntimeError(
                "--history-start must be on or before yesterday "
                f"({yesterday.isoformat()})"
            )
        return history_start

    latest_complete_date = _latest_complete_historical_date(
        latest_observation_utc,
        station_timezone,
    )
    if latest_complete_date >= yesterday:
        return None
    return latest_complete_date + timedelta(days=1)


def _latest_complete_historical_date(
    latest_observation_utc: datetime,
    station_timezone: ZoneInfo,
) -> date:
    latest_local = latest_observation_utc.astimezone(station_timezone)
    if latest_local.timetz().replace(tzinfo=None) >= time(23, 0):
        return latest_local.date()
    return latest_local.date() - timedelta(days=1)


def _fetch_historical_rows(
    latitude: float,
    longitude: float,
    timezone_name: str,
    station_id: str,
    start_date: date,
    end_date: date,
) -> list[WeatherObservation]:
    observations = fetch_open_meteo_historical_weather(
        latitude=latitude,
        longitude=longitude,
        timezone=timezone_name,
        start_date=start_date,
        end_date=end_date,
    )
    station_timezone = ZoneInfo(timezone_name)
    _validate_observation_data_window(
        observations,
        station_timezone,
        start_date,
        end_date,
    )
    return [
        WeatherObservation(
            station_id=station_id,
            timestamp_utc=observation.timestamp_utc,
            timestamp_local=observation.timestamp_local,
            weather_code=observation.weather_code,
            temperature_c=observation.temperature_c,
            cloud_cover_percent=observation.cloud_cover_percent,
            precipitation_mm=observation.precipitation_mm,
            rain_mm=observation.rain_mm,
            snowfall_cm=observation.snowfall_cm,
            shortwave_radiation_w_m2=observation.shortwave_radiation_w_m2,
            direct_radiation_w_m2=observation.direct_radiation_w_m2,
            diffuse_radiation_w_m2=observation.diffuse_radiation_w_m2,
            source=observation.source,
        )
        for observation in observations
    ]


def _fetch_forecast_rows(
    latitude: float,
    longitude: float,
    timezone_name: str,
    station_id: str,
    start_date: date,
    end_date: date,
) -> list[WeatherForecast]:
    forecasts = fetch_open_meteo_forecast(
        latitude=latitude,
        longitude=longitude,
        timezone=timezone_name,
        forecast_hours=None,
        start_date=start_date,
        end_date=end_date,
    )
    station_timezone = ZoneInfo(timezone_name)
    _validate_forecast_data_window(forecasts, station_timezone, start_date, end_date)
    fetched_at_utc = datetime.now(timezone.utc)
    return [
        WeatherForecast(
            station_id=station_id,
            fetched_at_utc=fetched_at_utc,
            forecast_timestamp_utc=forecast.forecast_timestamp_utc,
            forecast_timestamp_local=forecast.forecast_timestamp_local,
            weather_code=forecast.weather_code,
            temperature_c=forecast.temperature_c,
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


def _validate_observation_data_window(
    observations: list[WeatherObservationData],
    station_timezone: ZoneInfo,
    start_date: date,
    end_date: date,
) -> None:
    timestamps = [
        observation.timestamp_utc.astimezone(station_timezone)
        for observation in observations
    ]
    _validate_hourly_window(timestamps, start_date, end_date, "Historical weather")


def _validate_forecast_data_window(
    forecasts: list[WeatherForecastData],
    station_timezone: ZoneInfo,
    start_date: date,
    end_date: date,
) -> None:
    timestamps = [
        forecast.forecast_timestamp_utc.astimezone(station_timezone)
        for forecast in forecasts
    ]
    _validate_hourly_window(timestamps, start_date, end_date, "Forecast weather")


def _validate_hourly_window(
    timestamps: list[datetime],
    start_date: date,
    end_date: date,
    label: str,
) -> None:
    if not timestamps:
        raise RuntimeError(
            f"{label} fetch returned no rows for "
            f"{start_date.isoformat()} through {end_date.isoformat()}"
        )
    sorted_timestamps = sorted(timestamps)
    expected_start = datetime.combine(
        start_date,
        time.min,
        tzinfo=sorted_timestamps[0].tzinfo,
    )
    expected_end = datetime.combine(
        end_date,
        time(23, 0),
        tzinfo=sorted_timestamps[0].tzinfo,
    )
    if sorted_timestamps[0] != expected_start:
        raise RuntimeError(
            f"{label} fetch must start at {expected_start.isoformat()}, found "
            f"{sorted_timestamps[0].isoformat()}"
        )
    if sorted_timestamps[-1] != expected_end:
        raise RuntimeError(
            f"{label} fetch must end at {expected_end.isoformat()}, found "
            f"{sorted_timestamps[-1].isoformat()}"
        )

    start_utc, end_utc = _date_range_to_utc_bounds(
        start_date,
        end_date,
        sorted_timestamps[0].tzinfo,
    )
    expected_count = int((end_utc - start_utc).total_seconds() // 3600)
    if len(sorted_timestamps) != expected_count:
        raise RuntimeError(
            f"{label} fetch returned {len(sorted_timestamps)} rows; expected "
            f"{expected_count} hourly rows"
        )
    unique_timestamps = {
        timestamp.astimezone(timezone.utc) for timestamp in sorted_timestamps
    }
    if len(unique_timestamps) != len(sorted_timestamps):
        raise RuntimeError(f"{label} fetch returned duplicate hourly timestamps")

    expected_dates = set(_iter_dates(start_date, end_date))
    actual_dates = {timestamp.date() for timestamp in sorted_timestamps}
    if actual_dates != expected_dates:
        missing_dates = sorted(expected_dates - actual_dates)
        extra_dates = sorted(actual_dates - expected_dates)
        raise RuntimeError(
            f"{label} fetch returned wrong local dates: "
            f"missing={_format_dates(missing_dates)}, "
            f"extra={_format_dates(extra_dates)}"
        )


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


def _iter_dates(start_date: date, end_date: date) -> list[date]:
    return [
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    ]


def _count_weather_observations(session: Session, station_id: str) -> int:
    statement = select(WeatherObservation).where(
        WeatherObservation.station_id == station_id
    )
    return len(session.exec(statement).all())


def _count_forecast_rows(session: Session, station_id: str) -> int:
    statement = select(WeatherForecast).where(WeatherForecast.station_id == station_id)
    return len(session.exec(statement).all())


def _format_dates(dates: list[date]) -> str:
    if not dates:
        return "none"
    return ",".join(day.isoformat() for day in dates)


def _print_summary(summary: WeatherCacheSummary) -> None:
    station_timezone = ZoneInfo(summary.timezone_name)
    print(f"station id: {summary.station_id}")
    print(f"timezone: {summary.timezone_name}")
    print(f"current local date: {summary.current_local_date.isoformat()}")
    print(
        "historical backfill range: "
        f"{_format_date_range(summary.historical_backfill_start, summary.historical_backfill_end)}"
    )
    print(f"historical rows inserted: {summary.historical_rows_inserted}")
    print(
        "forecast requested range: "
        f"{summary.forecast_requested_start.isoformat()} through "
        f"{summary.forecast_requested_end.isoformat()}"
    )
    print(f"forecast rows inserted: {summary.forecast_rows_inserted}")
    print(
        "final historical range/count: "
        f"{_format_datetime_range(summary.final_historical_start_utc, summary.final_historical_end_utc, station_timezone)} "
        f"/ {summary.final_historical_count}"
    )
    print(
        "final forecast range/count: "
        f"{_format_datetime_range(summary.final_forecast_start_utc, summary.final_forecast_end_utc, station_timezone)} "
        f"/ {summary.final_forecast_count}"
    )
    print(f"validation result: {summary.validation_result}")


def _format_date_range(start_date: date | None, end_date: date | None) -> str:
    if start_date is None or end_date is None:
        return "none"
    return f"{start_date.isoformat()} through {end_date.isoformat()}"


def _format_datetime_range(
    start_utc: datetime | None,
    end_utc: datetime | None,
    station_timezone: ZoneInfo,
) -> str:
    if start_utc is None or end_utc is None:
        return "none"
    start_local = start_utc.astimezone(station_timezone)
    end_local = end_utc.astimezone(station_timezone)
    return f"{start_local.isoformat()} through {end_local.isoformat()}"


if __name__ == "__main__":
    main()
