from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session

from app.config_loader import calculate_config_hash, load_config
from app.storage.database import create_db_and_tables, get_engine
from app.storage.forecast_repository import (
    WeatherForecast,
    delete_forecast_for_station,
    save_forecast_rows,
)
from app.storage.forecast_solar_repository import list_forecast_solar_for_config
from app.storage.simulated_solar_repository import list_simulated_solar_for_config
from app.storage.solar_repository import (
    IdealSolarProduction,
    list_ideal_solar_for_config,
    save_ideal_solar_points,
)
from app.storage.weather_repository import (
    WeatherObservation,
    delete_weather_observations_for_range,
    save_weather_observations,
)


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import solar_data_scheduler


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "station.default.yaml"
STATION_ID = "smart_energy_lab"
STATION_TIMEZONE = ZoneInfo("Europe/Kyiv")


def test_ideal_solar_maintenance_regenerates_incomplete_required_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        solar_data_scheduler,
        "IdealSolarGenerator",
        FakeIdealSolarGenerator,
    )
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = _database_url(tmp_path)
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    start_local = datetime(2025, 10, 6, tzinfo=STATION_TIMEZONE)
    end_local = datetime(2025, 10, 7, tzinfo=STATION_TIMEZONE)

    with Session(engine) as session:
        save_ideal_solar_points(
            session,
            [
                _ideal_row(
                    timestamp_utc=start_local.astimezone(timezone.utc),
                    ideal_power_w=100.0,
                    config_hash=config_hash,
                ),
            ],
        )

        summary = solar_data_scheduler.ensure_ideal_solar_coverage(
            session=session,
            config=config,
            station_id=STATION_ID,
            config_hash=config_hash,
            start_local=start_local,
            end_local=end_local,
        )
        rows = list_ideal_solar_for_config(
            session,
            STATION_ID,
            config_hash,
            start_utc=start_local.astimezone(timezone.utc),
            end_utc=end_local.astimezone(timezone.utc),
        )

    assert summary.regenerated is True
    assert summary.rows == 96
    assert len(rows) == 96
    assert rows[0].timestamp_local.isoformat() == "2025-10-06T00:00:00+03:00"
    assert rows[-1].timestamp_local.isoformat() == "2025-10-06T23:45:00+03:00"


def test_pipeline_calls_weather_cache_and_generates_historical_and_forecast_solar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        solar_data_scheduler,
        "IdealSolarGenerator",
        FakeIdealSolarGenerator,
    )
    calls: list[tuple[Path, str | None, date | None, int, datetime | None]] = []

    def fake_update_weather_cache(
        config_path: Path,
        database_url: str | None,
        history_start: date | None,
        days_ahead: int,
        now: datetime | None = None,
    ):
        calls.append((config_path, database_url, history_start, days_ahead, now))
        assert history_start is not None
        return _save_fake_weather_cache(
            config_path,
            database_url,
            history_start,
            days_ahead,
            now,
        )

    monkeypatch.setattr(
        solar_data_scheduler.update_weather_cache,
        "update_weather_cache",
        fake_update_weather_cache,
    )
    database_url = _database_url(tmp_path)
    current_now = datetime(2025, 10, 8, 12, 0, tzinfo=STATION_TIMEZONE)
    history_start = date(2025, 10, 6)

    summary = solar_data_scheduler.run_solar_data_maintenance(
        config_path=CONFIG_PATH,
        database_url=database_url,
        history_start=history_start,
        days_ahead=2,
        now=current_now,
    )

    assert calls == [(CONFIG_PATH, database_url, history_start, 2, current_now)]
    assert summary.weather_cache.historical_rows_inserted == 48
    assert summary.weather_cache.forecast_rows_inserted == 72
    assert summary.historical_adjusted_solar.rows == 192
    assert summary.forecast_adjusted_solar.rows == 288

    config_hash = calculate_config_hash(load_config(CONFIG_PATH))
    engine = get_engine(database_url)
    historical_start_utc, historical_end_utc = solar_data_scheduler._date_range_to_utc_bounds(
        history_start,
        date(2025, 10, 7),
        STATION_TIMEZONE,
    )
    with Session(engine) as session:
        historical_solar = list_simulated_solar_for_config(
            session,
            STATION_ID,
            config_hash,
            start_utc=historical_start_utc,
            end_utc=historical_end_utc,
        )
        forecast_solar = list_forecast_solar_for_config(
            session,
            STATION_ID,
            config_hash,
        )

    assert len(historical_solar) == 192
    assert historical_solar[-1].timestamp_local.date() == date(2025, 10, 7)
    assert historical_solar[-1].timestamp_local.time() == time(23, 45)
    assert len(forecast_solar) == 288
    assert all(point.forecast_power_w <= point.ideal_power_w for point in forecast_solar)


def test_historical_adjusted_maintenance_is_not_blocked_after_old_may_2026_limit(
    tmp_path: Path,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = _database_url(tmp_path)
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    start_utc, end_utc = solar_data_scheduler._date_range_to_utc_bounds(
        date(2026, 5, 9),
        date(2026, 5, 9),
        STATION_TIMEZONE,
    )

    with Session(engine) as session:
        save_ideal_solar_points(
            session,
            [
                _ideal_row(
                    timestamp_utc=start_utc + timedelta(minutes=15 * index),
                    ideal_power_w=100.0,
                    config_hash=config_hash,
                )
                for index in range(96)
            ],
        )
        save_weather_observations(
            session,
            [
                _weather_row(start_utc + timedelta(hours=index))
                for index in range(24)
            ],
        )

        summary = solar_data_scheduler.ensure_historical_adjusted_solar_coverage(
            session=session,
            station_id=STATION_ID,
            config_hash=config_hash,
            station_timezone=STATION_TIMEZONE,
            history_start=date(2026, 5, 9),
            current_local_date=date(2026, 5, 10),
        )
        rows = list_simulated_solar_for_config(
            session,
            STATION_ID,
            config_hash,
            start_utc=start_utc,
            end_utc=end_utc,
        )

    assert summary.regenerated is True
    assert summary.rows == 96
    assert len(rows) == 96
    assert rows[-1].timestamp_local.date() == date(2026, 5, 9)


def test_forecast_adjusted_solar_uses_previous_forecast_row_and_caps_power() -> None:
    ideal_points = [
        _ideal_row(
            datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
            ideal_power_w=100.0,
            config_hash="test_hash",
        ),
        _ideal_row(
            datetime(2026, 5, 10, 9, 15, tzinfo=timezone.utc),
            ideal_power_w=150.0,
            config_hash="test_hash",
        ),
        _ideal_row(
            datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
            ideal_power_w=0.0,
            config_hash="test_hash",
        ),
    ]
    forecasts = [
        _forecast_row(
            datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
            weather_code=0,
        ),
        _forecast_row(
            datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
            weather_code=61,
        ),
    ]

    points = solar_data_scheduler.generate_forecast_adjusted_solar(
        ideal_points,
        forecasts,
    )

    assert len(points) == 3
    assert points[1].weather_code == 0
    assert points[2].weather_code == 61
    assert points[2].forecast_power_w == 0.0
    assert all(point.forecast_power_w <= point.ideal_power_w for point in points)


def test_validate_complete_coverage_reports_first_timestamp_mismatch_with_equal_count() -> None:
    start_utc = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end_utc = start_utc + timedelta(hours=3)
    rows = [
        SimpleNamespace(timestamp_utc=start_utc + timedelta(hours=1)),
        SimpleNamespace(timestamp_utc=start_utc + timedelta(hours=2)),
        SimpleNamespace(timestamp_utc=start_utc + timedelta(hours=3)),
    ]

    with pytest.raises(RuntimeError, match="Historical weather data first timestamp mismatch"):
        solar_data_scheduler._validate_complete_coverage(
            rows,
            timestamp_attr="timestamp_utc",
            start_utc=start_utc,
            end_utc=end_utc,
            timestep_minutes=60,
            label="Historical weather data",
        )


def test_validate_complete_coverage_reports_last_timestamp_mismatch_with_equal_count() -> None:
    start_utc = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end_utc = start_utc + timedelta(hours=3)
    rows = [
        SimpleNamespace(timestamp_utc=start_utc),
        SimpleNamespace(timestamp_utc=start_utc + timedelta(hours=1)),
        SimpleNamespace(timestamp_utc=start_utc + timedelta(hours=3)),
    ]

    with pytest.raises(RuntimeError, match="Historical weather data last timestamp mismatch"):
        solar_data_scheduler._validate_complete_coverage(
            rows,
            timestamp_attr="timestamp_utc",
            start_utc=start_utc,
            end_utc=end_utc,
            timestep_minutes=60,
            label="Historical weather data",
        )


def test_validate_complete_coverage_reports_gap_with_equal_count_and_boundaries() -> None:
    start_utc = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end_utc = start_utc + timedelta(hours=4)
    rows = [
        SimpleNamespace(timestamp_utc=start_utc),
        SimpleNamespace(timestamp_utc=start_utc + timedelta(hours=1)),
        SimpleNamespace(timestamp_utc=start_utc + timedelta(hours=1, minutes=30)),
        SimpleNamespace(timestamp_utc=start_utc + timedelta(hours=3)),
    ]

    with pytest.raises(RuntimeError, match="non-continuous 60-minute timestep"):
        solar_data_scheduler._validate_complete_coverage(
            rows,
            timestamp_attr="timestamp_utc",
            start_utc=start_utc,
            end_utc=end_utc,
            timestep_minutes=60,
            label="Historical weather data",
        )


def test_validate_complete_coverage_normalizes_timestamps_to_utc() -> None:
    start_utc = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end_utc = start_utc + timedelta(hours=2)
    rows = [
        SimpleNamespace(timestamp_utc=start_utc.astimezone(STATION_TIMEZONE)),
        SimpleNamespace(
            timestamp_utc=(start_utc + timedelta(hours=1)).astimezone(
                STATION_TIMEZONE,
            ),
        ),
    ]

    solar_data_scheduler._validate_complete_coverage(
        rows,
        timestamp_attr="timestamp_utc",
        start_utc=start_utc,
        end_utc=end_utc,
        timestep_minutes=60,
        label="Historical weather data",
    )


def test_validate_complete_coverage_reports_empty_data() -> None:
    start_utc = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end_utc = start_utc + timedelta(hours=1)

    with pytest.raises(RuntimeError, match="Historical weather data is empty"):
        solar_data_scheduler._validate_complete_coverage(
            [],
            timestamp_attr="timestamp_utc",
            start_utc=start_utc,
            end_utc=end_utc,
            timestep_minutes=60,
            label="Historical weather data",
        )


def test_validate_complete_coverage_reports_row_count_mismatch() -> None:
    start_utc = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end_utc = start_utc + timedelta(hours=2)
    rows = [SimpleNamespace(timestamp_utc=start_utc)]

    with pytest.raises(RuntimeError, match="Historical weather data row count mismatch"):
        solar_data_scheduler._validate_complete_coverage(
            rows,
            timestamp_attr="timestamp_utc",
            start_utc=start_utc,
            end_utc=end_utc,
            timestep_minutes=60,
            label="Historical weather data",
        )


def test_validate_local_wall_time_coverage_accepts_kyiv_dst_fall_back_utc_gap() -> None:
    start_local = datetime(2025, 10, 26, 0, 0, tzinfo=STATION_TIMEZONE)
    end_local = datetime(2025, 10, 27, 0, 0, tzinfo=STATION_TIMEZONE)
    rows = _local_wall_rows(start_local, end_local, minutes=60)

    assert len(rows) == 24
    assert rows[3].timestamp_utc.isoformat() == "2025-10-26T00:00:00+00:00"
    assert rows[4].timestamp_utc.isoformat() == "2025-10-26T02:00:00+00:00"

    solar_data_scheduler._validate_complete_coverage(
        rows,
        timestamp_attr="timestamp_utc",
        local_timestamp_attr="timestamp_local",
        start_utc=start_local.astimezone(timezone.utc),
        end_utc=end_local.astimezone(timezone.utc),
        start_local=start_local,
        end_local=end_local,
        timestep_minutes=60,
        label="Historical weather data",
        cadence_mode="local_wall_time",
    )


def test_validate_local_wall_time_coverage_fails_on_missing_dst_day_local_hour() -> None:
    start_local = datetime(2025, 10, 26, 0, 0, tzinfo=STATION_TIMEZONE)
    end_local = datetime(2025, 10, 27, 0, 0, tzinfo=STATION_TIMEZONE)
    rows = _local_wall_rows(start_local, end_local, minutes=60)
    rows = rows[:4] + rows[5:] + [rows[-1]]

    with pytest.raises(
        RuntimeError,
        match="Historical weather data has a non-continuous local wall-clock",
    ):
        solar_data_scheduler._validate_complete_coverage(
            rows,
            timestamp_attr="timestamp_utc",
            local_timestamp_attr="timestamp_local",
            start_utc=start_local.astimezone(timezone.utc),
            end_utc=end_local.astimezone(timezone.utc),
            start_local=start_local,
            end_local=end_local,
            timestep_minutes=60,
            label="Historical weather data",
            cadence_mode="local_wall_time",
        )


def test_scheduler_runs_full_pipeline_before_first_sleep() -> None:
    settings = solar_data_scheduler.SchedulerSettings(
        config=Path("config.yaml"),
        database_url="sqlite:///cache.db",
        history_start=date(2025, 10, 6),
        days_ahead=2,
        interval_hours=12.0,
    )
    events: list[object] = []

    class StopScheduler(Exception):
        pass

    def fake_runner(
        config_path: Path,
        database_url: str | None,
        history_start: date,
        days_ahead: int,
    ):
        events.append((config_path, database_url, history_start, days_ahead))
        return _successful_scheduler_summary()

    def fake_sleep(seconds: float) -> None:
        events.append(seconds)
        raise StopScheduler

    with pytest.raises(StopScheduler):
        solar_data_scheduler.run_forever(
            settings,
            maintenance_runner=fake_runner,
            sleep=fake_sleep,
        )

    assert events == [
        (Path("config.yaml"), "sqlite:///cache.db", date(2025, 10, 6), 2),
        43200.0,
    ]


def test_scheduler_sleeps_and_retries_later_after_pipeline_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = solar_data_scheduler.SchedulerSettings(
        config=Path("config.yaml"),
        database_url="sqlite:///cache.db",
        history_start=date(2025, 10, 6),
        days_ahead=2,
        interval_hours=12.0,
    )
    events: list[object] = []

    class StopScheduler(Exception):
        pass

    def failing_runner(
        config_path: Path,
        database_url: str | None,
        history_start: date,
        days_ahead: int,
    ):
        events.append("runner")
        raise RuntimeError("temporary failure")

    def fake_sleep(seconds: float) -> None:
        events.append(seconds)
        raise StopScheduler

    with pytest.raises(StopScheduler):
        solar_data_scheduler.run_forever(
            settings,
            maintenance_runner=failing_runner,
            sleep=fake_sleep,
        )

    captured = capsys.readouterr()
    assert events == ["runner", 43200.0]
    assert "solar data maintenance failed: temporary failure" in captured.err


class FakeIdealSolarGenerator:
    def __init__(self, config) -> None:
        self.station_timezone = ZoneInfo(config.station.solar.installation.timezone)

    def generate(
        self,
        start: datetime,
        end: datetime,
        timestep_minutes: int,
    ) -> list[SimpleNamespace]:
        current = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        points: list[SimpleNamespace] = []
        while current < end_utc:
            ideal_power_w = 0.0 if current.hour < 3 else 100.0
            points.append(
                SimpleNamespace(
                    timestamp_utc=current,
                    timestamp_local=current.astimezone(self.station_timezone),
                    sun_elevation_deg=20.0,
                    sun_azimuth_deg=180.0,
                    incidence_factor=0.5,
                    ambient_factor=0.04,
                    direct_power_w=80.0,
                    ambient_power_w=20.0,
                    ideal_power_w=ideal_power_w,
                )
            )
            current += timedelta(minutes=timestep_minutes)
        return points


def _save_fake_weather_cache(
    config_path: Path,
    database_url: str | None,
    history_start: date,
    days_ahead: int,
    now: datetime | None,
):
    assert now is not None
    config = load_config(config_path)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    current_local_date = now.astimezone(station_timezone).date()
    yesterday = current_local_date - timedelta(days=1)
    forecast_end_date = current_local_date + timedelta(days=days_ahead)
    historical_start_utc, historical_end_utc = solar_data_scheduler._date_range_to_utc_bounds(
        history_start,
        yesterday,
        station_timezone,
    )
    forecast_start_utc, forecast_end_utc = solar_data_scheduler._date_range_to_utc_bounds(
        current_local_date,
        forecast_end_date,
        station_timezone,
    )
    historical_rows = [
        _weather_row(local_time.astimezone(timezone.utc))
        for local_time in _iter_local_hours(history_start, yesterday, station_timezone)
    ]
    forecast_rows = [
        _forecast_row(local_time.astimezone(timezone.utc), weather_code=3)
        for local_time in _iter_local_hours(
            current_local_date,
            forecast_end_date,
            station_timezone,
        )
    ]

    engine = get_engine(database_url)
    create_db_and_tables(engine)
    with Session(engine) as session:
        delete_weather_observations_for_range(
            session,
            config.station.id,
            historical_start_utc,
            historical_end_utc,
        )
        save_weather_observations(session, historical_rows)
        delete_forecast_for_station(session, config.station.id)
        save_forecast_rows(session, forecast_rows)

    return solar_data_scheduler.update_weather_cache.WeatherCacheSummary(
        station_id=config.station.id,
        timezone_name=station_timezone.key,
        current_local_date=current_local_date,
        historical_backfill_start=history_start,
        historical_backfill_end=yesterday,
        historical_rows_inserted=len(historical_rows),
        forecast_requested_start=current_local_date,
        forecast_requested_end=forecast_end_date,
        forecast_rows_inserted=len(forecast_rows),
        final_historical_start_utc=historical_start_utc,
        final_historical_end_utc=historical_end_utc - timedelta(hours=1),
        final_historical_count=len(historical_rows),
        final_forecast_start_utc=forecast_start_utc,
        final_forecast_end_utc=forecast_end_utc - timedelta(hours=1),
        final_forecast_count=len(forecast_rows),
        validation_result="ok",
    )


def _ideal_row(
    timestamp_utc: datetime,
    ideal_power_w: float,
    config_hash: str,
) -> IdealSolarProduction:
    return IdealSolarProduction(
        station_id=STATION_ID,
        config_hash=config_hash,
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc.astimezone(STATION_TIMEZONE),
        sun_elevation_deg=20.0,
        sun_azimuth_deg=180.0,
        incidence_factor=0.5,
        ambient_factor=0.04,
        direct_power_w=80.0,
        ambient_power_w=20.0,
        ideal_power_w=ideal_power_w,
    )


def _weather_row(timestamp_utc: datetime) -> WeatherObservation:
    return WeatherObservation(
        station_id=STATION_ID,
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc.astimezone(STATION_TIMEZONE),
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


def _forecast_row(timestamp_utc: datetime, weather_code: int) -> WeatherForecast:
    fetched_at_utc = datetime(2025, 10, 8, 9, 0, tzinfo=timezone.utc)
    return WeatherForecast(
        station_id=STATION_ID,
        fetched_at_utc=fetched_at_utc,
        forecast_timestamp_utc=timestamp_utc,
        forecast_timestamp_local=timestamp_utc.astimezone(STATION_TIMEZONE),
        weather_code=weather_code,
        cloud_cover_percent=80.0,
        precipitation_mm=0.0,
        rain_mm=0.0,
        snowfall_cm=0.0,
        shortwave_radiation_w_m2=100.0,
        direct_radiation_w_m2=80.0,
        diffuse_radiation_w_m2=20.0,
        source="open_meteo_forecast",
        resolution_minutes=60,
    )


def _iter_local_hours(
    start_date: date,
    end_date: date,
    station_timezone: ZoneInfo,
) -> list[datetime]:
    current = datetime.combine(start_date, time.min, tzinfo=station_timezone)
    end = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
        tzinfo=station_timezone,
    )
    timestamps: list[datetime] = []
    while current < end:
        timestamps.append(current)
        current += timedelta(hours=1)
    return timestamps


def _local_wall_rows(
    start_local: datetime,
    end_local: datetime,
    minutes: int,
) -> list[SimpleNamespace]:
    current = start_local
    rows: list[SimpleNamespace] = []
    while current < end_local:
        rows.append(
            SimpleNamespace(
                timestamp_utc=current.astimezone(timezone.utc),
                timestamp_local=current,
            )
        )
        current += timedelta(minutes=minutes)
    return rows


def _successful_scheduler_summary() -> SimpleNamespace:
    return SimpleNamespace(
        station_id=STATION_ID,
        current_local_date=date(2025, 10, 8),
        ideal_solar=SimpleNamespace(rows=96, regenerated=True),
        weather_cache=SimpleNamespace(
            historical_rows_inserted=24,
            forecast_rows_inserted=72,
        ),
        historical_adjusted_solar=SimpleNamespace(rows=96, regenerated=True),
        forecast_adjusted_solar=SimpleNamespace(rows=288, regenerated=True),
    )


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'solar_data.db'}"
