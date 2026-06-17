from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session

from app.simulation.weather import WeatherForecastData, WeatherObservationData
from app.storage.database import create_db_and_tables, get_engine
from app.storage.forecast_repository import (
    WeatherForecast,
    list_forecast_for_station,
    save_forecast_rows,
)
from app.storage.weather_repository import (
    WeatherObservation,
    list_weather_observations,
    save_weather_observations,
)


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import update_weather_cache


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "station.default.yaml"
STATION_ID = "smart_energy_lab"
STATION_TIMEZONE = ZoneInfo("Europe/Kyiv")
CURRENT_NOW = datetime(2026, 5, 10, 12, 0, tzinfo=STATION_TIMEZONE)
TODAY = date(2026, 5, 10)
YESTERDAY = date(2026, 5, 9)


def test_detects_missing_historical_days_and_fetches_through_yesterday(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    _save_historical_days(database_url, date(2026, 5, 7), date(2026, 5, 7))
    captured_history_range: dict[str, date] = {}

    def fake_fetch_history(
        latitude: float,
        longitude: float,
        timezone: str,
        start_date: date,
        end_date: date,
    ) -> list[WeatherObservationData]:
        captured_history_range["start"] = start_date
        captured_history_range["end"] = end_date
        return _historical_data(start_date, end_date)

    monkeypatch.setattr(
        update_weather_cache,
        "fetch_open_meteo_historical_weather",
        fake_fetch_history,
    )
    monkeypatch.setattr(
        update_weather_cache,
        "fetch_open_meteo_forecast",
        _forecast_fetcher(TODAY, date(2026, 5, 12)),
    )

    summary = update_weather_cache.update_weather_cache(
        config_path=CONFIG_PATH,
        database_url=database_url,
        history_start=None,
        days_ahead=2,
        now=CURRENT_NOW,
    )

    assert captured_history_range == {
        "start": date(2026, 5, 8),
        "end": YESTERDAY,
    }
    assert summary.historical_backfill_start == date(2026, 5, 8)
    assert summary.historical_backfill_end == YESTERDAY
    assert summary.historical_rows_inserted == 48
    assert summary.forecast_rows_inserted == 72
    assert summary.validation_result == "ok"


def test_no_historical_backfill_when_archive_data_is_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    _save_historical_days(database_url, YESTERDAY, YESTERDAY)

    def fail_fetch_history(*args: object, **kwargs: object) -> list[WeatherObservationData]:
        raise AssertionError("historical fetch should not be called")

    monkeypatch.setattr(
        update_weather_cache,
        "fetch_open_meteo_historical_weather",
        fail_fetch_history,
    )
    monkeypatch.setattr(
        update_weather_cache,
        "fetch_open_meteo_forecast",
        _forecast_fetcher(TODAY, date(2026, 5, 12)),
    )

    summary = update_weather_cache.update_weather_cache(
        config_path=CONFIG_PATH,
        database_url=database_url,
        history_start=None,
        days_ahead=2,
        now=CURRENT_NOW,
    )

    assert summary.historical_backfill_start is None
    assert summary.historical_backfill_end is None
    assert summary.historical_rows_inserted == 0
    assert summary.forecast_rows_inserted == 72


def test_empty_historical_table_requires_history_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fail_fetch(*args: object, **kwargs: object) -> list[object]:
        raise AssertionError("fetch should not be called")

    monkeypatch.setattr(
        update_weather_cache,
        "fetch_open_meteo_historical_weather",
        fail_fetch,
    )
    monkeypatch.setattr(update_weather_cache, "fetch_open_meteo_forecast", fail_fetch)

    with pytest.raises(RuntimeError, match="provide --history-start"):
        update_weather_cache.update_weather_cache(
            config_path=CONFIG_PATH,
            database_url=database_url,
            history_start=None,
            days_ahead=2,
            now=CURRENT_NOW,
        )


def test_forecast_refresh_replaces_old_forecast_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    _save_historical_days(database_url, YESTERDAY, YESTERDAY)
    _save_forecast_days(
        database_url,
        date(2026, 5, 8),
        date(2026, 5, 8),
        source="old_forecast",
    )
    monkeypatch.setattr(
        update_weather_cache,
        "fetch_open_meteo_historical_weather",
        _unused_historical_fetcher,
    )
    monkeypatch.setattr(
        update_weather_cache,
        "fetch_open_meteo_forecast",
        _forecast_fetcher(TODAY, date(2026, 5, 12)),
    )

    update_weather_cache.update_weather_cache(
        config_path=CONFIG_PATH,
        database_url=database_url,
        history_start=None,
        days_ahead=2,
        now=CURRENT_NOW,
    )

    engine = get_engine(database_url)
    with Session(engine) as session:
        rows = list_forecast_for_station(session, STATION_ID)

    assert len(rows) == 72
    assert {row.source for row in rows} == {"open_meteo_forecast"}
    assert rows[0].forecast_timestamp_local.date() == TODAY
    assert rows[-1].forecast_timestamp_local.date() == date(2026, 5, 12)


def test_final_ranges_are_continuous_by_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    monkeypatch.setattr(
        update_weather_cache,
        "fetch_open_meteo_historical_weather",
        _historical_fetcher(date(2026, 5, 8), YESTERDAY),
    )
    monkeypatch.setattr(
        update_weather_cache,
        "fetch_open_meteo_forecast",
        _forecast_fetcher(TODAY, date(2026, 5, 12)),
    )

    summary = update_weather_cache.update_weather_cache(
        config_path=CONFIG_PATH,
        database_url=database_url,
        history_start=date(2026, 5, 8),
        days_ahead=2,
        now=CURRENT_NOW,
    )

    assert summary.final_historical_end_utc is not None
    assert summary.final_forecast_start_utc is not None
    assert summary.final_forecast_end_utc is not None
    historical_end = summary.final_historical_end_utc.astimezone(STATION_TIMEZONE)
    forecast_start = summary.final_forecast_start_utc.astimezone(STATION_TIMEZONE)
    forecast_end = summary.final_forecast_end_utc.astimezone(STATION_TIMEZONE)

    assert historical_end.date() == YESTERDAY
    assert historical_end.time() == time(23, 0)
    assert forecast_start.date() == TODAY
    assert forecast_start.time() == time(0, 0)
    assert forecast_end.date() == date(2026, 5, 12)
    assert forecast_end.time() == time(23, 0)


def test_validate_hourly_window_accepts_dst_fall_back_repeated_hour() -> None:
    timestamps = _iter_real_hours(date(2025, 10, 26), date(2025, 10, 26))

    assert len(timestamps) == 25
    assert timestamps[3].isoformat() == "2025-10-26T03:00:00+03:00"
    assert timestamps[4].isoformat() == "2025-10-26T03:00:00+02:00"

    update_weather_cache._validate_hourly_window(
        timestamps,
        date(2025, 10, 26),
        date(2025, 10, 26),
        "Historical weather",
    )


def test_validate_hourly_window_rejects_missing_dst_repeated_hour() -> None:
    timestamps = _iter_real_hours(date(2025, 10, 26), date(2025, 10, 26))
    timestamps = timestamps[:4] + timestamps[5:]

    with pytest.raises(RuntimeError, match="returned 24 rows; expected 25"):
        update_weather_cache._validate_hourly_window(
            timestamps,
            date(2025, 10, 26),
            date(2025, 10, 26),
            "Historical weather",
        )


def test_validate_hourly_window_accepts_dst_spring_forward_missing_local_hour() -> None:
    timestamps = _iter_real_hours(date(2026, 3, 29), date(2026, 3, 29))

    assert len(timestamps) == 23
    assert len({timestamp.astimezone(timezone.utc) for timestamp in timestamps}) == 23
    assert {timestamp.date() for timestamp in timestamps} == {date(2026, 3, 29)}
    assert 3 not in {timestamp.hour for timestamp in timestamps}

    update_weather_cache._validate_hourly_window(
        timestamps,
        date(2026, 3, 29),
        date(2026, 3, 29),
        "Historical weather",
    )


def test_validate_hourly_window_rejects_duplicate_spring_forward_utc_collapse() -> None:
    timestamps = _iter_real_hours(date(2026, 3, 29), date(2026, 3, 29))
    timestamps = timestamps[:4] + [timestamps[3]] + timestamps[5:]

    with pytest.raises(RuntimeError, match="duplicate hourly timestamps"):
        update_weather_cache._validate_hourly_window(
            timestamps,
            date(2026, 3, 29),
            date(2026, 3, 29),
            "Historical weather",
        )


def test_validation_fails_if_historical_end_is_not_yesterday(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    _save_historical_days(database_url, date(2026, 5, 8), date(2026, 5, 8))
    _save_forecast_days(database_url, TODAY, date(2026, 5, 12))

    engine = get_engine(database_url)
    with Session(engine) as session:
        with pytest.raises(RuntimeError, match="must end on yesterday"):
            update_weather_cache.validate_weather_cache(
                session=session,
                station_id=STATION_ID,
                station_timezone=STATION_TIMEZONE,
                current_local_date=TODAY,
                days_ahead=2,
            )


def test_validation_fails_if_forecast_does_not_start_today(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    _save_historical_days(database_url, YESTERDAY, YESTERDAY)
    _save_forecast_days(database_url, date(2026, 5, 11), date(2026, 5, 12))

    engine = get_engine(database_url)
    with Session(engine) as session:
        with pytest.raises(RuntimeError, match="must start on the current local date"):
            update_weather_cache.validate_weather_cache(
                session=session,
                station_id=STATION_ID,
                station_timezone=STATION_TIMEZONE,
                current_local_date=TODAY,
                days_ahead=2,
            )


def test_cli_works_with_mocked_fetches_against_temp_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = _database_url(tmp_path)
    monkeypatch.setattr(
        update_weather_cache,
        "_resolve_current_local_date",
        lambda station_timezone, now=None: TODAY,
    )
    monkeypatch.setattr(
        update_weather_cache,
        "fetch_open_meteo_historical_weather",
        _historical_fetcher(YESTERDAY, YESTERDAY),
    )
    monkeypatch.setattr(
        update_weather_cache,
        "fetch_open_meteo_forecast",
        _forecast_fetcher(TODAY, date(2026, 5, 12)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "update_weather_cache.py",
            "--config",
            str(CONFIG_PATH),
            "--database-url",
            database_url,
            "--history-start",
            YESTERDAY.isoformat(),
            "--days-ahead",
            "2",
        ],
    )

    update_weather_cache.main()

    output = capsys.readouterr().out
    assert "station id: smart_energy_lab" in output
    assert "historical backfill range: 2026-05-09 through 2026-05-09" in output
    assert "historical rows inserted: 24" in output
    assert "forecast rows inserted: 72" in output
    assert "validation result: ok" in output

    engine = get_engine(database_url)
    with Session(engine) as session:
        forecast_rows = list_forecast_for_station(session, STATION_ID)
        observation_rows = list_weather_observations(
            session,
            STATION_ID,
            datetime(2026, 5, 8, 21, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 9, 21, 0, tzinfo=timezone.utc),
        )

    assert len(observation_rows) == 24
    assert len(forecast_rows) == 72
    assert observation_rows[0].temperature_c == 9.5
    assert forecast_rows[0].temperature_c == 16.5


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'weather_cache.db'}"


def _historical_fetcher(
    expected_start_date: date,
    expected_end_date: date,
):
    def fake_fetch_history(
        latitude: float,
        longitude: float,
        timezone: str,
        start_date: date,
        end_date: date,
    ) -> list[WeatherObservationData]:
        assert start_date == expected_start_date
        assert end_date == expected_end_date
        assert timezone == "Europe/Kyiv"
        return _historical_data(start_date, end_date)

    return fake_fetch_history


def _forecast_fetcher(
    expected_start_date: date,
    expected_end_date: date,
):
    def fake_fetch_forecast(
        latitude: float,
        longitude: float,
        timezone: str,
        forecast_hours: int | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[WeatherForecastData]:
        assert forecast_hours is None
        assert start_date == expected_start_date
        assert end_date == expected_end_date
        assert timezone == "Europe/Kyiv"
        return _forecast_data(start_date, end_date)

    return fake_fetch_forecast


def _unused_historical_fetcher(
    *args: object,
    **kwargs: object,
) -> list[WeatherObservationData]:
    raise AssertionError("historical fetch should not be called")


def _save_historical_days(
    database_url: str,
    start_date: date,
    end_date: date,
) -> None:
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    with Session(engine) as session:
        save_weather_observations(
            session,
            _historical_rows(start_date, end_date),
        )


def _save_forecast_days(
    database_url: str,
    start_date: date,
    end_date: date,
    source: str = "open_meteo_forecast",
) -> None:
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    fetched_at_utc = datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc)
    with Session(engine) as session:
        save_forecast_rows(
            session,
            [
                _forecast_row(local_time, fetched_at_utc, source)
                for local_time in _iter_local_hours(start_date, end_date)
            ],
        )


def _historical_data(
    start_date: date,
    end_date: date,
) -> list[WeatherObservationData]:
    return [
        WeatherObservationData(
            timestamp_utc=local_time.astimezone(timezone.utc),
            timestamp_local=local_time,
            weather_code=0,
            temperature_c=9.5,
            cloud_cover_percent=10.0,
            precipitation_mm=0.0,
            rain_mm=0.0,
            snowfall_cm=0.0,
            shortwave_radiation_w_m2=100.0,
            direct_radiation_w_m2=80.0,
            diffuse_radiation_w_m2=20.0,
        )
        for local_time in _iter_local_hours(start_date, end_date)
    ]


def _forecast_data(
    start_date: date,
    end_date: date,
) -> list[WeatherForecastData]:
    return [
        WeatherForecastData(
            forecast_timestamp_utc=local_time.astimezone(timezone.utc),
            forecast_timestamp_local=local_time,
            weather_code=1,
            temperature_c=16.5,
            cloud_cover_percent=30.0,
            precipitation_mm=0.0,
            rain_mm=0.0,
            snowfall_cm=0.0,
            shortwave_radiation_w_m2=120.0,
            direct_radiation_w_m2=90.0,
            diffuse_radiation_w_m2=30.0,
        )
        for local_time in _iter_local_hours(start_date, end_date)
    ]


def _historical_rows(
    start_date: date,
    end_date: date,
) -> list[WeatherObservation]:
    return [
        WeatherObservation(
            station_id=STATION_ID,
            timestamp_utc=local_time.astimezone(timezone.utc),
            timestamp_local=local_time,
            weather_code=0,
            temperature_c=9.5,
            cloud_cover_percent=10.0,
            precipitation_mm=0.0,
            rain_mm=0.0,
            snowfall_cm=0.0,
            shortwave_radiation_w_m2=100.0,
            direct_radiation_w_m2=80.0,
            diffuse_radiation_w_m2=20.0,
            source="open-meteo-archive",
        )
        for local_time in _iter_local_hours(start_date, end_date)
    ]


def _forecast_row(
    local_time: datetime,
    fetched_at_utc: datetime,
    source: str,
) -> WeatherForecast:
    return WeatherForecast(
        station_id=STATION_ID,
        fetched_at_utc=fetched_at_utc,
        forecast_timestamp_utc=local_time.astimezone(timezone.utc),
        forecast_timestamp_local=local_time,
        weather_code=1,
        temperature_c=16.5,
        cloud_cover_percent=30.0,
        precipitation_mm=0.0,
        rain_mm=0.0,
        snowfall_cm=0.0,
        shortwave_radiation_w_m2=120.0,
        direct_radiation_w_m2=90.0,
        diffuse_radiation_w_m2=30.0,
        source=source,
        resolution_minutes=60,
    )


def _iter_local_hours(start_date: date, end_date: date) -> list[datetime]:
    current = datetime.combine(start_date, time.min, tzinfo=STATION_TIMEZONE)
    end = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
        tzinfo=STATION_TIMEZONE,
    )
    timestamps: list[datetime] = []
    while current < end:
        timestamps.append(current)
        current += timedelta(hours=1)
    return timestamps


def _iter_real_hours(start_date: date, end_date: date) -> list[datetime]:
    start_local = datetime.combine(start_date, time.min, tzinfo=STATION_TIMEZONE)
    end_local = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
        tzinfo=STATION_TIMEZONE,
    )
    current_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    timestamps: list[datetime] = []
    while current_utc < end_utc:
        timestamps.append(current_utc.astimezone(STATION_TIMEZONE))
        current_utc += timedelta(hours=1)
    return timestamps
