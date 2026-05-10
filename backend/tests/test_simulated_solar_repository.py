from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session

from app.storage.database import create_db_and_tables, get_engine
from app.storage.simulated_solar_repository import (
    SimulatedSolarProduction,
    delete_simulated_solar_for_config,
    get_simulated_solar_range,
    list_simulated_solar_for_config,
    save_simulated_solar_points,
)


def test_simulated_solar_repository_saves_lists_and_deletes_points() -> None:
    engine = get_engine("sqlite:///:memory:")
    create_db_and_tables(engine)
    station_timezone = ZoneInfo("Europe/Kyiv")
    start_utc = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    point = SimulatedSolarProduction(
        station_id="smart_energy_lab",
        config_hash="test_hash",
        timestamp_utc=start_utc,
        timestamp_local=start_utc.astimezone(station_timezone),
        ideal_power_w=200.0,
        weather_code=3,
        weather_state="cloudy",
        cloud_cover_percent=90.0,
        weather_factor=0.4,
        simulated_power_w=80.0,
    )

    with Session(engine) as session:
        save_simulated_solar_points(session, [point])
        saved_points = list_simulated_solar_for_config(
            session,
            "smart_energy_lab",
            "test_hash",
        )

        assert len(saved_points) == 1
        assert saved_points[0].simulated_power_w == 80.0
        assert saved_points[0].weather_state == "cloudy"
        assert saved_points[0].timestamp_utc.tzinfo == timezone.utc
        assert saved_points[0].timestamp_local.utcoffset().total_seconds() == 7200
        assert get_simulated_solar_range(session, "smart_energy_lab", "test_hash") == (
            start_utc,
            start_utc,
        )

        delete_simulated_solar_for_config(
            session,
            "smart_energy_lab",
            "test_hash",
            start_utc,
            start_utc + timedelta(minutes=15),
        )
        assert (
            list_simulated_solar_for_config(
                session,
                "smart_energy_lab",
                "test_hash",
            )
            == []
        )
        assert get_simulated_solar_range(session, "smart_energy_lab", "test_hash") == (
            None,
            None,
        )
