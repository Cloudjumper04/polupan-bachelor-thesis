from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session

from app.config_loader import calculate_config_hash, load_config
from app.storage.database import create_db_and_tables, get_engine
from app.storage.solar_repository import IdealSolarProduction, save_ideal_solar_points
from app.storage.weather_repository import WeatherObservation, save_weather_observations


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from generate_weather_adjusted_solar import main


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "station.default.yaml"


def test_script_prints_summary_after_saving_committed_simulated_rows(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    start_date = date(2026, 1, 1)
    start_utc = datetime(2025, 12, 31, 22, 0, tzinfo=timezone.utc)
    database_url = f"sqlite:///{tmp_path / 'smartenergy.db'}"
    engine = get_engine(database_url)
    create_db_and_tables(engine)

    with Session(engine) as session:
        save_ideal_solar_points(
            session,
            [
                _ideal_point(
                    station_id=config.station.id,
                    config_hash=config_hash,
                    timestamp_utc=start_utc + timedelta(minutes=15 * index),
                    station_timezone=station_timezone,
                    ideal_power_w=100.0,
                )
                for index in range(96)
            ],
        )
        save_weather_observations(
            session,
            [
                _weather_observation(
                    station_id=config.station.id,
                    timestamp_utc=start_utc + timedelta(hours=index),
                    station_timezone=station_timezone,
                )
                for index in range(24)
            ],
        )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_weather_adjusted_solar.py",
            "--config",
            str(CONFIG_PATH),
            "--database-url",
            database_url,
            "--start",
            start_date.isoformat(),
            "--end",
            start_date.isoformat(),
        ],
    )

    main()

    output = capsys.readouterr().out
    assert "simulated rows generated: 96" in output
    assert "min simulated power W:" in output
    assert "max simulated power W:" in output


def _ideal_point(
    station_id: str,
    config_hash: str,
    timestamp_utc: datetime,
    station_timezone: ZoneInfo,
    ideal_power_w: float,
) -> IdealSolarProduction:
    return IdealSolarProduction(
        station_id=station_id,
        config_hash=config_hash,
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc.astimezone(station_timezone),
        sun_elevation_deg=20.0,
        sun_azimuth_deg=180.0,
        incidence_factor=0.5,
        ambient_factor=0.04,
        direct_power_w=80.0,
        ambient_power_w=20.0,
        ideal_power_w=ideal_power_w,
    )


def _weather_observation(
    station_id: str,
    timestamp_utc: datetime,
    station_timezone: ZoneInfo,
) -> WeatherObservation:
    return WeatherObservation(
        station_id=station_id,
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc.astimezone(station_timezone),
        weather_code=0,
        cloud_cover_percent=10.0,
        precipitation_mm=0.0,
        rain_mm=0.0,
        snowfall_cm=0.0,
        shortwave_radiation_w_m2=100.0,
        direct_radiation_w_m2=80.0,
        diffuse_radiation_w_m2=20.0,
        source="open-meteo-archive",
    )
