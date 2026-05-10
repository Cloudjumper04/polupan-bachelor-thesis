from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session

from app.storage.database import create_db_and_tables, get_engine
from app.storage.forecast_repository import (
    WeatherForecast,
    delete_forecast_for_station,
    get_forecast_range,
    get_latest_forecast_fetch_time,
    list_forecast_for_station,
    save_forecast_rows,
)


def test_forecast_repository_saves_lists_latest_and_deletes_rows() -> None:
    engine = get_engine("sqlite:///:memory:")
    create_db_and_tables(engine)
    station_timezone = ZoneInfo("Europe/Kyiv")
    fetched_at_utc = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    forecast_start_utc = datetime(2026, 5, 9, 11, 0, tzinfo=timezone.utc)
    rows = [
        _forecast_row(
            timestamp_utc=forecast_start_utc,
            fetched_at_utc=fetched_at_utc,
            station_timezone=station_timezone,
        ),
        _forecast_row(
            timestamp_utc=forecast_start_utc + timedelta(hours=1),
            fetched_at_utc=fetched_at_utc + timedelta(minutes=5),
            station_timezone=station_timezone,
        ),
    ]

    with Session(engine) as session:
        save_forecast_rows(session, rows)
        saved_rows = list_forecast_for_station(session, "smart_energy_lab")

        assert len(saved_rows) == 2
        assert saved_rows[0].forecast_timestamp_utc.tzinfo == timezone.utc
        assert saved_rows[0].forecast_timestamp_local.utcoffset().total_seconds() == 10800
        assert saved_rows[0].resolution_minutes == 60
        assert get_latest_forecast_fetch_time(
            session,
            "smart_energy_lab",
        ) == fetched_at_utc + timedelta(minutes=5)
        assert get_forecast_range(session, "smart_energy_lab") == (
            forecast_start_utc,
            forecast_start_utc + timedelta(hours=1),
        )

        filtered_rows = list_forecast_for_station(
            session,
            "smart_energy_lab",
            start_utc=forecast_start_utc + timedelta(minutes=30),
            end_utc=forecast_start_utc + timedelta(hours=2),
        )
        assert len(filtered_rows) == 1

        delete_forecast_for_station(session, "smart_energy_lab")
        assert list_forecast_for_station(session, "smart_energy_lab") == []
        assert get_latest_forecast_fetch_time(session, "smart_energy_lab") is None
        assert get_forecast_range(session, "smart_energy_lab") == (None, None)


def _forecast_row(
    timestamp_utc: datetime,
    fetched_at_utc: datetime,
    station_timezone: ZoneInfo,
) -> WeatherForecast:
    return WeatherForecast(
        station_id="smart_energy_lab",
        fetched_at_utc=fetched_at_utc,
        forecast_timestamp_utc=timestamp_utc,
        forecast_timestamp_local=timestamp_utc.astimezone(station_timezone),
        weather_code=3,
        cloud_cover_percent=80.0,
        precipitation_mm=0.2,
        rain_mm=0.2,
        snowfall_cm=0.0,
        shortwave_radiation_w_m2=50.0,
        direct_radiation_w_m2=10.0,
        diffuse_radiation_w_m2=40.0,
        source="open_meteo_forecast",
        resolution_minutes=60,
    )
