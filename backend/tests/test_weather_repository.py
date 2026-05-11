from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session

from app.storage.database import create_db_and_tables, get_engine
from app.storage.weather_repository import (
    WeatherObservation,
    delete_weather_observations,
    delete_weather_observations_for_range,
    get_latest_weather_observation_time,
    get_weather_observation_range,
    list_weather_observations,
    save_weather_observations,
)


def test_weather_repository_saves_lists_and_deletes_observations() -> None:
    engine = get_engine("sqlite:///:memory:")
    create_db_and_tables(engine)
    station_timezone = ZoneInfo("Europe/Kyiv")
    start_utc = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    observation = WeatherObservation(
        station_id="smart_energy_lab",
        timestamp_utc=start_utc,
        timestamp_local=start_utc.astimezone(station_timezone),
        weather_code=61,
        temperature_c=8.5,
        cloud_cover_percent=75.0,
        precipitation_mm=1.0,
        rain_mm=1.0,
        snowfall_cm=0.0,
        shortwave_radiation_w_m2=20.0,
        direct_radiation_w_m2=5.0,
        diffuse_radiation_w_m2=15.0,
        source="open-meteo-archive",
    )

    with Session(engine) as session:
        save_weather_observations(session, [observation])
        saved_observations = list_weather_observations(
            session,
            "smart_energy_lab",
            start_utc,
            start_utc + timedelta(hours=1),
        )

        assert len(saved_observations) == 1
        assert saved_observations[0].weather_code == 61
        assert saved_observations[0].temperature_c == 8.5
        assert saved_observations[0].timestamp_utc.tzinfo == timezone.utc
        assert saved_observations[0].timestamp_local.utcoffset().total_seconds() == 7200
        assert get_weather_observation_range(
            session,
            "smart_energy_lab",
        ) == (start_utc, start_utc)
        assert (
            get_latest_weather_observation_time(session, "smart_energy_lab")
            == start_utc
        )

        delete_weather_observations_for_range(
            session,
            "smart_energy_lab",
            start_utc,
            start_utc + timedelta(hours=1),
        )
        assert (
            list_weather_observations(
                session,
                "smart_energy_lab",
                start_utc,
                start_utc + timedelta(hours=1),
            )
            == []
        )
        assert get_weather_observation_range(session, "smart_energy_lab") == (
            None,
            None,
        )
        assert get_latest_weather_observation_time(session, "smart_energy_lab") is None

        replacement_observation = WeatherObservation(
            station_id="smart_energy_lab",
            timestamp_utc=start_utc,
            timestamp_local=start_utc.astimezone(station_timezone),
            weather_code=61,
            temperature_c=8.5,
            cloud_cover_percent=75.0,
            precipitation_mm=1.0,
            rain_mm=1.0,
            snowfall_cm=0.0,
            shortwave_radiation_w_m2=20.0,
            direct_radiation_w_m2=5.0,
            diffuse_radiation_w_m2=15.0,
            source="open-meteo-archive",
        )
        save_weather_observations(session, [replacement_observation])
        delete_weather_observations(
            session,
            "smart_energy_lab",
            start_utc,
            start_utc + timedelta(hours=1),
        )
        assert list_weather_observations(
            session,
            "smart_energy_lab",
            start_utc,
            start_utc + timedelta(hours=1),
        ) == []
