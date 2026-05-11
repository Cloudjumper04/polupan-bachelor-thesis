from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session

from app.config_loader import calculate_config_hash, load_config
from app.storage.database import create_db_and_tables, get_engine
from app.storage.forecast_solar_repository import (
    ForecastSolarProduction,
    save_forecast_solar_points,
)
from app.storage.interpolated_solar_repository import (
    InterpolatedSolarProduction,
    list_interpolated_solar_for_config,
    save_interpolated_solar_points,
)
from app.storage.simulated_solar_repository import (
    SimulatedSolarProduction,
    save_simulated_solar_points,
)


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import solar_data_scheduler


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "station.default.yaml"
STATION_ID = "smart_energy_lab"
CONFIG_HASH = "test_hash"
UTC = ZoneInfo("UTC")


def test_cache_generator_switches_from_historical_to_forecast_at_transition(
    tmp_path: Path,
) -> None:
    engine = _create_engine(tmp_path)
    generated_at = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    window = solar_data_scheduler.InterpolationWindow(
        start_utc=datetime(2026, 5, 9, 23, 59, 50, tzinfo=timezone.utc),
        end_utc=datetime(2026, 5, 10, 0, 0, 10, tzinfo=timezone.utc),
        resolution_seconds=5,
    )

    with Session(engine) as session:
        save_simulated_solar_points(
            session,
            [
                _simulated_row(
                    datetime(2026, 5, 9, 23, 45, tzinfo=timezone.utc),
                    power_w=80.0,
                )
            ],
        )
        save_forecast_solar_points(
            session,
            [
                _forecast_solar_row(
                    datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc),
                    power_w=100.0,
                ),
                _forecast_solar_row(
                    datetime(2026, 5, 10, 0, 15, tzinfo=timezone.utc),
                    power_w=120.0,
                ),
            ],
        )

        rows = solar_data_scheduler.generate_interpolated_solar_cache_points(
            session=session,
            station_id=STATION_ID,
            config_hash=CONFIG_HASH,
            station_timezone=UTC,
            windows=[window],
            generated_at_utc=generated_at,
        )

    assert [row.source_type for row in rows] == [
        "historical",
        "historical",
        "forecast",
        "forecast",
    ]
    assert rows[2].timestamp_utc == datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc)
    assert rows[2].power_w == 100.0


def test_cache_generator_avoids_duplicate_boundary_timestamps(
    tmp_path: Path,
) -> None:
    engine = _create_engine(tmp_path)
    generated_at = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    windows = [
        solar_data_scheduler.InterpolationWindow(
            start_utc=datetime(2026, 5, 10, 1, 0, tzinfo=timezone.utc),
            end_utc=datetime(2026, 5, 10, 1, 0, 10, tzinfo=timezone.utc),
            resolution_seconds=5,
        ),
        solar_data_scheduler.InterpolationWindow(
            start_utc=datetime(2026, 5, 10, 1, 0, 10, tzinfo=timezone.utc),
            end_utc=datetime(2026, 5, 10, 1, 0, 20, tzinfo=timezone.utc),
            resolution_seconds=5,
        ),
    ]

    with Session(engine) as session:
        _save_forecast_source_range(
            session,
            datetime(2026, 5, 10, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 10, 1, 15, tzinfo=timezone.utc),
        )
        rows = solar_data_scheduler.generate_interpolated_solar_cache_points(
            session=session,
            station_id=STATION_ID,
            config_hash=CONFIG_HASH,
            station_timezone=UTC,
            windows=windows,
            generated_at_utc=generated_at,
        )

    timestamps = [row.timestamp_utc for row in rows]
    assert timestamps == [
        datetime(2026, 5, 10, 1, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 10, 1, 0, 5, tzinfo=timezone.utc),
        datetime(2026, 5, 10, 1, 0, 10, tzinfo=timezone.utc),
        datetime(2026, 5, 10, 1, 0, 15, tzinfo=timezone.utc),
    ]
    assert len(timestamps) == len(set(timestamps))
    assert {row.resolution_seconds for row in rows} == {5}


def test_default_interpolation_windows_are_tier_exclusive() -> None:
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    windows = solar_data_scheduler.build_default_interpolation_windows(now)

    row_count = sum(
        int((window.end_utc - window.start_utc).total_seconds())
        // window.resolution_seconds
        for window in windows
    )

    assert [window.resolution_seconds for window in windows] == [
        300,
        60,
        30,
        5,
        1,
        5,
    ]
    assert row_count == 10728
    assert [(window.start_utc, window.end_utc) for window in windows] == [
        (now - timedelta(days=7), now - timedelta(hours=24)),
        (now - timedelta(hours=24), now - timedelta(hours=12)),
        (now - timedelta(hours=12), now - timedelta(hours=3)),
        (now - timedelta(hours=3), now - timedelta(minutes=30)),
        (now - timedelta(minutes=30), now + timedelta(minutes=30)),
        (now + timedelta(minutes=30), now + timedelta(hours=3)),
    ]
    assert all(
        current.end_utc <= following.start_utc
        for current, following in zip(windows, windows[1:])
    )


def test_missing_historical_bracketing_data_raises_clear_error(tmp_path: Path) -> None:
    engine = _create_engine(tmp_path)
    window = solar_data_scheduler.InterpolationWindow(
        start_utc=datetime(2026, 5, 9, 23, 30, tzinfo=timezone.utc),
        end_utc=datetime(2026, 5, 9, 23, 31, tzinfo=timezone.utc),
        resolution_seconds=30,
    )

    with Session(engine) as session:
        with pytest.raises(RuntimeError, match="Missing historical bracketing solar data"):
            solar_data_scheduler.generate_interpolated_solar_cache_points(
                session=session,
                station_id=STATION_ID,
                config_hash=CONFIG_HASH,
                station_timezone=UTC,
                windows=[window],
                generated_at_utc=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
            )


def test_missing_forecast_bracketing_data_raises_clear_error(tmp_path: Path) -> None:
    engine = _create_engine(tmp_path)
    window = solar_data_scheduler.InterpolationWindow(
        start_utc=datetime(2026, 5, 10, 1, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 5, 10, 1, 1, tzinfo=timezone.utc),
        resolution_seconds=30,
    )

    with Session(engine) as session:
        with pytest.raises(RuntimeError, match="Missing forecast bracketing solar data"):
            solar_data_scheduler.generate_interpolated_solar_cache_points(
                session=session,
                station_id=STATION_ID,
                config_hash=CONFIG_HASH,
                station_timezone=UTC,
                windows=[window],
                generated_at_utc=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
            )


def test_fast_cache_refresh_deletes_only_near_now_one_second_rows(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    config_hash = calculate_config_hash(load_config(CONFIG_PATH))
    now = datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc)
    near_start = now - timedelta(minutes=30)
    near_end = now + timedelta(minutes=30)

    with Session(engine) as session:
        _save_forecast_source_range(
            session,
            near_start,
            near_end,
            config_hash=config_hash,
        )
        save_interpolated_solar_points(
            session,
            [
                _cache_row(
                    near_start + timedelta(seconds=10),
                    resolution_seconds=1,
                    power_w=1.0,
                    config_hash=config_hash,
                ),
                _cache_row(
                    near_start + timedelta(seconds=10),
                    resolution_seconds=5,
                    power_w=5.0,
                    config_hash=config_hash,
                ),
                _cache_row(
                    near_start + timedelta(seconds=10),
                    resolution_seconds=30,
                    power_w=30.0,
                    config_hash=config_hash,
                ),
                _cache_row(
                    near_start + timedelta(seconds=10),
                    resolution_seconds=60,
                    power_w=60.0,
                    config_hash=config_hash,
                ),
                _cache_row(
                    near_start + timedelta(seconds=10),
                    resolution_seconds=300,
                    power_w=300.0,
                    config_hash=config_hash,
                ),
                _cache_row(
                    near_start - timedelta(hours=1),
                    resolution_seconds=1,
                    power_w=9.0,
                    config_hash=config_hash,
                ),
            ],
        )

    summary = solar_data_scheduler.run_fast_interpolated_solar_cache_refresh(
        CONFIG_PATH,
        database_url,
        now=now,
    )

    with Session(engine) as session:
        one_second_rows = list_interpolated_solar_for_config(
            session,
            STATION_ID,
            config_hash,
            resolution_seconds=1,
        )
        five_second_rows = list_interpolated_solar_for_config(
            session,
            STATION_ID,
            config_hash,
            resolution_seconds=5,
        )
        preserved_non_fast_rows = {
            resolution: list_interpolated_solar_for_config(
                session,
                STATION_ID,
                config_hash,
                resolution_seconds=resolution,
            )
            for resolution in (30, 60, 300)
        }

    assert summary.fast_only is True
    assert summary.rows == 3600
    assert len(
        [
            row
            for row in one_second_rows
            if near_start <= row.timestamp_utc < near_end
        ]
    ) == 3600
    assert any(row.power_w == 9.0 for row in one_second_rows)
    assert [row.power_w for row in five_second_rows] == [5.0]
    assert {
        resolution: [row.power_w for row in rows]
        for resolution, rows in preserved_non_fast_rows.items()
    } == {30: [30.0], 60: [60.0], 300: [300.0]}


def test_full_cache_refresh_rebuilds_all_configured_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    config_hash = calculate_config_hash(load_config(CONFIG_PATH))
    now = datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc)
    windows = [
        solar_data_scheduler.InterpolationWindow(
            start_utc=now,
            end_utc=now + timedelta(minutes=10),
            resolution_seconds=300,
        ),
        solar_data_scheduler.InterpolationWindow(
            start_utc=now + timedelta(minutes=10),
            end_utc=now + timedelta(minutes=12),
            resolution_seconds=60,
        ),
        solar_data_scheduler.InterpolationWindow(
            start_utc=now + timedelta(minutes=12),
            end_utc=now + timedelta(minutes=13),
            resolution_seconds=30,
        ),
        solar_data_scheduler.InterpolationWindow(
            start_utc=now + timedelta(minutes=13),
            end_utc=now + timedelta(minutes=13, seconds=10),
            resolution_seconds=5,
        ),
        solar_data_scheduler.InterpolationWindow(
            start_utc=now + timedelta(minutes=13, seconds=10),
            end_utc=now + timedelta(minutes=13, seconds=20),
            resolution_seconds=1,
        ),
        solar_data_scheduler.InterpolationWindow(
            start_utc=now + timedelta(minutes=13, seconds=20),
            end_utc=now + timedelta(minutes=13, seconds=30),
            resolution_seconds=5,
        ),
    ]
    monkeypatch.setattr(
        solar_data_scheduler,
        "build_default_interpolation_windows",
        lambda current_now: windows,
    )

    with Session(engine) as session:
        _save_forecast_source_range(
            session,
            now,
            now + timedelta(minutes=30),
            config_hash=config_hash,
        )
        save_interpolated_solar_points(
            session,
            [
                _cache_row(
                    now - timedelta(hours=1),
                    resolution_seconds=300,
                    power_w=99.0,
                    config_hash=config_hash,
                )
            ],
        )

    summary = solar_data_scheduler.run_full_interpolated_solar_cache_refresh(
        CONFIG_PATH,
        database_url,
        now=now,
    )

    with Session(engine) as session:
        rows = list_interpolated_solar_for_config(session, STATION_ID, config_hash)
        rows_by_resolution = {
            resolution: list_interpolated_solar_for_config(
                session,
                STATION_ID,
                config_hash,
                resolution_seconds=resolution,
            )
            for resolution in (1, 5, 30, 60, 300)
        }

    assert summary.fast_only is False
    assert summary.rows == 20
    assert summary.windows == 6
    assert len(rows) == 20
    assert all(row.power_w != 99.0 for row in rows)
    assert {row.resolution_seconds for row in rows} == {1, 5, 30, 60, 300}
    assert {
        resolution: len(resolution_rows)
        for resolution, resolution_rows in rows_by_resolution.items()
    } == {1: 10, 5: 4, 30: 2, 60: 2, 300: 2}
    assert len({row.timestamp_utc for row in rows}) == len(rows)


def test_scheduler_startup_runs_stable_maintenance_then_full_cache() -> None:
    settings = _settings()
    events: list[str] = []

    class StopScheduler(Exception):
        pass

    def fake_maintenance(
        config_path: Path,
        database_url: str | None,
        history_start: date,
        days_ahead: int,
    ):
        events.append("maintenance")
        return _successful_scheduler_summary()

    def fake_full_cache(config_path: Path, database_url: str | None):
        events.append("full-cache")
        return solar_data_scheduler.InterpolatedCacheSummary(
            rows=1,
            windows=1,
            start_utc=None,
            end_utc=None,
            fast_only=False,
        )

    def fake_sleep(seconds: float) -> None:
        events.append(f"sleep:{seconds:g}")
        raise StopScheduler

    with pytest.raises(StopScheduler):
        solar_data_scheduler.run_forever(
            settings,
            maintenance_runner=fake_maintenance,
            full_cache_runner=fake_full_cache,
            fast_cache_runner=fake_full_cache,
            sleep=fake_sleep,
            monotonic=lambda: 0.0,
        )

    assert events == ["maintenance", "full-cache", "sleep:5"]


def test_scheduler_loop_can_run_fast_cache_without_stable_maintenance() -> None:
    settings = _settings()
    events: list[str] = []
    clock_values = iter([0.0, 61.0])

    class StopScheduler(Exception):
        pass

    def fake_maintenance(
        config_path: Path,
        database_url: str | None,
        history_start: date,
        days_ahead: int,
    ):
        events.append("maintenance")
        return _successful_scheduler_summary()

    def fake_full_cache(config_path: Path, database_url: str | None):
        events.append("full-cache")
        return _cache_summary(fast_only=False)

    def fake_fast_cache(config_path: Path, database_url: str | None):
        events.append("fast-cache")
        return _cache_summary(fast_only=True)

    def fake_sleep(seconds: float) -> None:
        events.append(f"sleep:{seconds:g}")
        raise StopScheduler

    with pytest.raises(StopScheduler):
        solar_data_scheduler.run_forever(
            settings,
            maintenance_runner=fake_maintenance,
            full_cache_runner=fake_full_cache,
            fast_cache_runner=fake_fast_cache,
            sleep=fake_sleep,
            monotonic=lambda: next(clock_values),
        )

    assert events == ["maintenance", "full-cache", "fast-cache", "sleep:5"]


def test_scheduler_continues_after_interpolation_refresh_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings()
    events: list[str] = []

    class StopScheduler(Exception):
        pass

    def fake_maintenance(
        config_path: Path,
        database_url: str | None,
        history_start: date,
        days_ahead: int,
    ):
        events.append("maintenance")
        return _successful_scheduler_summary()

    def failing_full_cache(config_path: Path, database_url: str | None):
        events.append("full-cache")
        raise RuntimeError("cache failure")

    def fake_sleep(seconds: float) -> None:
        events.append(f"sleep:{seconds:g}")
        raise StopScheduler

    with pytest.raises(StopScheduler):
        solar_data_scheduler.run_forever(
            settings,
            maintenance_runner=fake_maintenance,
            full_cache_runner=failing_full_cache,
            fast_cache_runner=failing_full_cache,
            sleep=fake_sleep,
            monotonic=lambda: 0.0,
        )

    captured = capsys.readouterr()
    assert events == ["maintenance", "full-cache", "sleep:5"]
    assert "full interpolation cache refresh failed: cache failure" in captured.err


def _create_engine(tmp_path: Path):
    engine = get_engine(_database_url(tmp_path))
    create_db_and_tables(engine)
    return engine


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'interpolated_solar.db'}"


def _save_forecast_source_range(
    session: Session,
    start_utc: datetime,
    end_utc: datetime,
    config_hash: str = CONFIG_HASH,
) -> None:
    current = start_utc
    rows: list[ForecastSolarProduction] = []
    while current <= end_utc:
        rows.append(_forecast_solar_row(current, power_w=100.0, config_hash=config_hash))
        current += timedelta(minutes=15)
    save_forecast_solar_points(session, rows)


def _simulated_row(
    timestamp_utc: datetime,
    power_w: float,
    config_hash: str = CONFIG_HASH,
) -> SimulatedSolarProduction:
    return SimulatedSolarProduction(
        station_id=STATION_ID,
        config_hash=config_hash,
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc.astimezone(UTC),
        ideal_power_w=power_w,
        weather_code=0,
        weather_state="clear",
        cloud_cover_percent=10.0,
        weather_factor=1.0,
        simulated_power_w=power_w,
    )


def _forecast_solar_row(
    timestamp_utc: datetime,
    power_w: float,
    config_hash: str = CONFIG_HASH,
) -> ForecastSolarProduction:
    return ForecastSolarProduction(
        station_id=STATION_ID,
        config_hash=config_hash,
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc.astimezone(UTC),
        ideal_power_w=power_w,
        weather_code=2,
        weather_state="partly_cloudy",
        cloud_cover_percent=50.0,
        weather_factor=1.0,
        forecast_power_w=power_w,
    )


def _cache_row(
    timestamp_utc: datetime,
    resolution_seconds: int,
    power_w: float,
    config_hash: str,
) -> InterpolatedSolarProduction:
    return InterpolatedSolarProduction(
        station_id=STATION_ID,
        config_hash=config_hash,
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc.astimezone(UTC),
        source_type="forecast",
        resolution_seconds=resolution_seconds,
        lower_source_timestamp_utc=timestamp_utc,
        upper_source_timestamp_utc=timestamp_utc,
        lower_power_w=power_w,
        upper_power_w=power_w,
        interpolation_ratio=0.0,
        baseline_power_w=power_w,
        variation_factor=1.0,
        power_w=power_w,
        generated_at_utc=timestamp_utc,
    )


def _settings() -> solar_data_scheduler.SchedulerSettings:
    return solar_data_scheduler.SchedulerSettings(
        config=Path("config.yaml"),
        database_url="sqlite:///cache.db",
        history_start=date(2025, 10, 6),
        days_ahead=2,
        maintenance_interval_hours=12.0,
        fast_cache_interval_seconds=60.0,
        full_cache_interval_minutes=45.0,
    )


def _cache_summary(fast_only: bool) -> solar_data_scheduler.InterpolatedCacheSummary:
    return solar_data_scheduler.InterpolatedCacheSummary(
        rows=1,
        windows=1,
        start_utc=None,
        end_utc=None,
        fast_only=fast_only,
    )


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
