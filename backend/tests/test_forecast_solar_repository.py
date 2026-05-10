from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session

from app.storage.database import create_db_and_tables, get_engine
from app.storage.forecast_solar_repository import (
    ForecastSolarProduction,
    delete_forecast_solar_for_config,
    get_forecast_solar_range,
    list_forecast_solar_for_config,
    save_forecast_solar_points,
)


def test_forecast_solar_repository_saves_lists_ranges_and_deletes_points() -> None:
    engine = get_engine("sqlite:///:memory:")
    create_db_and_tables(engine)
    station_timezone = ZoneInfo("Europe/Kyiv")
    start_utc = datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc)
    points = [
        _forecast_solar_point(start_utc, station_timezone, forecast_power_w=80.0),
        _forecast_solar_point(
            start_utc + timedelta(minutes=15),
            station_timezone,
            forecast_power_w=90.0,
        ),
    ]

    with Session(engine) as session:
        save_forecast_solar_points(session, points)

        saved_points = list_forecast_solar_for_config(
            session,
            "smart_energy_lab",
            "test_hash",
        )
        assert len(saved_points) == 2
        assert saved_points[0].timestamp_utc.tzinfo == timezone.utc
        assert saved_points[0].timestamp_local.utcoffset().total_seconds() == 10800
        assert saved_points[1].forecast_power_w == 90.0
        assert get_forecast_solar_range(session, "smart_energy_lab", "test_hash") == (
            start_utc,
            start_utc + timedelta(minutes=15),
        )

        filtered_points = list_forecast_solar_for_config(
            session,
            "smart_energy_lab",
            "test_hash",
            start_utc=start_utc + timedelta(minutes=15),
            end_utc=start_utc + timedelta(minutes=30),
        )
        assert len(filtered_points) == 1

        delete_forecast_solar_for_config(
            session,
            "smart_energy_lab",
            "test_hash",
            start_utc,
            start_utc + timedelta(minutes=30),
        )
        assert (
            list_forecast_solar_for_config(
                session,
                "smart_energy_lab",
                "test_hash",
            )
            == []
        )
        assert get_forecast_solar_range(session, "smart_energy_lab", "test_hash") == (
            None,
            None,
        )


def _forecast_solar_point(
    timestamp_utc: datetime,
    station_timezone: ZoneInfo,
    forecast_power_w: float,
) -> ForecastSolarProduction:
    return ForecastSolarProduction(
        station_id="smart_energy_lab",
        config_hash="test_hash",
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc.astimezone(station_timezone),
        ideal_power_w=100.0,
        weather_code=3,
        weather_state="cloudy",
        cloud_cover_percent=80.0,
        weather_factor=0.8,
        forecast_power_w=forecast_power_w,
    )
