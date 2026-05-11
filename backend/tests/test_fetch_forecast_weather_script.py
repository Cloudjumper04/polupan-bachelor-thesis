from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session

from app.simulation.weather import WeatherForecastData
from app.storage.database import get_engine
from app.storage.forecast_repository import list_forecast_for_station


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import fetch_forecast_weather


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "station.default.yaml"


def test_day_window_uses_current_station_local_date() -> None:
    station_timezone = ZoneInfo("Europe/Kyiv")
    fixed_now = datetime(2026, 5, 9, 22, 30, tzinfo=station_timezone)

    start_date, end_date = fetch_forecast_weather._resolve_forecast_date_window(
        station_timezone,
        days_ahead=2,
        now=fixed_now,
    )

    assert start_date == date(2026, 5, 9)
    assert end_date == date(2026, 5, 11)


def test_script_processes_mocked_forecast_data_in_temp_database(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'forecast.db'}"
    station_timezone = ZoneInfo("Europe/Kyiv")
    forecasts = [
        _forecast_data(
            datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc),
            station_timezone,
        ),
        _forecast_data(
            datetime(2026, 5, 9, 11, 0, tzinfo=timezone.utc),
            station_timezone,
        ),
    ]

    def fake_fetch_forecast(
        latitude: float,
        longitude: float,
        timezone: str,
        forecast_hours: int | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[WeatherForecastData]:
        assert forecast_hours is None
        assert start_date == date(2026, 5, 9)
        assert end_date == date(2026, 5, 11)
        assert start_date <= end_date
        assert timezone == "Europe/Kyiv"
        return forecasts

    def fake_resolve_date_window(
        station_timezone: ZoneInfo,
        days_ahead: int,
        now: datetime | None = None,
    ) -> tuple[date, date]:
        assert station_timezone.key == "Europe/Kyiv"
        assert days_ahead == 2
        return date(2026, 5, 9), date(2026, 5, 11)

    monkeypatch.setattr(
        fetch_forecast_weather,
        "fetch_open_meteo_forecast",
        fake_fetch_forecast,
    )
    monkeypatch.setattr(
        fetch_forecast_weather,
        "_resolve_forecast_date_window",
        fake_resolve_date_window,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fetch_forecast_weather.py",
            "--config",
            str(CONFIG_PATH),
            "--database-url",
            database_url,
            "--days-ahead",
            "2",
        ],
    )

    fetch_forecast_weather.main()

    output = capsys.readouterr().out
    assert "station id: smart_energy_lab" in output
    assert "requested start_date:" in output
    assert "requested end_date:" in output
    assert "rows fetched: 2" in output
    assert "source: open_meteo_forecast" in output

    engine = get_engine(database_url)
    with Session(engine) as session:
        rows = list_forecast_for_station(session, "smart_energy_lab")

    assert len(rows) == 2
    assert rows[0].forecast_timestamp_utc.isoformat() == "2026-05-09T10:00:00+00:00"
    assert rows[0].temperature_c == 16.5


def _forecast_data(
    timestamp_utc: datetime,
    station_timezone: ZoneInfo,
) -> WeatherForecastData:
    return WeatherForecastData(
        forecast_timestamp_utc=timestamp_utc,
        forecast_timestamp_local=timestamp_utc.astimezone(station_timezone),
        weather_code=0,
        temperature_c=16.5,
        cloud_cover_percent=10.0,
        precipitation_mm=0.0,
        rain_mm=0.0,
        snowfall_cm=0.0,
        shortwave_radiation_w_m2=100.0,
        direct_radiation_w_m2=80.0,
        diffuse_radiation_w_m2=20.0,
    )
