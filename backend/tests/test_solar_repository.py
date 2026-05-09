from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session

from app.storage.database import create_db_and_tables, get_engine
from app.storage.solar_repository import (
    IdealSolarProduction,
    delete_ideal_solar_for_config,
    list_ideal_solar_for_config,
    save_ideal_solar_points,
)


def test_repository_saves_and_retrieves_points_from_temp_database() -> None:
    engine = get_engine("sqlite:///:memory:")
    create_db_and_tables(engine)
    point = IdealSolarProduction(
        station_id="smart_energy_lab",
        config_hash="test_hash",
        timestamp_utc=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        timestamp_local=datetime(2026, 1, 1, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv")),
        sun_elevation_deg=20.0,
        sun_azimuth_deg=180.0,
        incidence_factor=0.5,
        ambient_factor=0.04,
        direct_power_w=200.0,
        ambient_power_w=16.0,
        ideal_power_w=216.0,
    )

    with Session(engine) as session:
        save_ideal_solar_points(session, [point])
        saved_points = list_ideal_solar_for_config(
            session,
            "smart_energy_lab",
            "test_hash",
        )

        assert len(saved_points) == 1
        assert saved_points[0].ideal_power_w == 216.0
        assert saved_points[0].timestamp_utc.tzinfo == timezone.utc
        assert saved_points[0].timestamp_local.utcoffset().total_seconds() == 7200

        delete_ideal_solar_for_config(session, "smart_energy_lab", "test_hash")
        assert list_ideal_solar_for_config(session, "smart_energy_lab", "test_hash") == []
