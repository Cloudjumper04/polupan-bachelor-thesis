from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session

from app.config_loader import calculate_system_config_hash, load_config
from app.simulation.engine import SystemSimulationPersistSummary
from app.storage.battery_repository import list_battery_cache_points
from app.storage.database import get_engine
from app.storage.ems_repository import list_ems_cache_points
from app.storage.load_repository import list_load_cache_points


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import update_data_pipeline


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "station.default.yaml"
FIXED_NOW = datetime(2026, 1, 1, 10, 5, 30, tzinfo=timezone.utc)


def test_pipeline_dry_run_does_not_create_or_write_db(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"

    summary = update_data_pipeline.run_data_pipeline(
        config_path=CONFIG_PATH,
        database_url=_db_url(db_path),
        dry_run=True,
        now=FIXED_NOW,
    )

    assert summary.dry_run is True
    assert summary.source_maintenance is None
    assert summary.system_generation is None
    assert summary.system_plan.history_writes_enabled is False
    assert summary.system_plan.load_cache_end_utc == datetime(
        2026,
        1,
        3,
        10,
        6,
        tzinfo=timezone.utc,
    )
    assert not db_path.exists()


def test_full_history_dry_run_uses_history_start_override(tmp_path: Path) -> None:
    db_path = tmp_path / "history_override.db"
    config = load_config(CONFIG_PATH)
    history_start = date(2025, 12, 1)
    timezone_info = ZoneInfo(config.station.solar.installation.timezone)
    expected_start = datetime(
        2025,
        12,
        1,
        0,
        0,
        tzinfo=timezone_info,
    ).astimezone(timezone.utc)

    summary = update_data_pipeline.run_data_pipeline(
        config_path=CONFIG_PATH,
        database_url=_db_url(db_path),
        history_start=history_start,
        full_history=True,
        dry_run=True,
        now=FIXED_NOW,
    )

    assert summary.history_start == history_start
    assert summary.system_plan.history_writes_enabled is True
    assert summary.system_plan.start_utc == expected_start
    assert not db_path.exists()


def test_pipeline_schema_initialization_includes_system_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "schema.db"
    _mock_non_writing_pipeline(monkeypatch)

    update_data_pipeline.run_data_pipeline(
        config_path=CONFIG_PATH,
        database_url=_db_url(db_path),
        now=FIXED_NOW,
    )

    table_names = _sqlite_table_names(db_path)
    assert {
        "load_history_point",
        "load_cache_point",
        "battery_history_point",
        "battery_cache_point",
        "ems_history_point",
        "ems_cache_point",
    }.issubset(table_names)


def test_source_maintenance_failure_prevents_actual_system_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "source_failure.db"
    actual_system_calls = 0

    def fake_system_generation(*args: object, **kwargs: object):
        nonlocal actual_system_calls
        if kwargs.get("dry_run"):
            return _system_summary(dry_run=True)
        actual_system_calls += 1
        return _system_summary(dry_run=False)

    monkeypatch.setattr(
        update_data_pipeline.generate_system_simulation,
        "run_system_simulation_generation",
        fake_system_generation,
    )

    def fail_source(**kwargs: object) -> object:
        raise RuntimeError("weather maintenance failed")

    monkeypatch.setattr(update_data_pipeline, "run_source_maintenance", fail_source)

    with pytest.raises(RuntimeError, match="weather maintenance failed"):
        update_data_pipeline.run_data_pipeline(
            config_path=CONFIG_PATH,
            database_url=_db_url(db_path),
            now=FIXED_NOW,
        )

    assert actual_system_calls == 0


def test_grid_failure_is_fatal_in_source_maintenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "grid_failure.db"
    config = load_config(CONFIG_PATH)

    monkeypatch.setattr(
        update_data_pipeline.solar_data_scheduler,
        "ensure_ideal_solar_coverage",
        lambda **kwargs: SimpleNamespace(rows=1, regenerated=False),
    )
    monkeypatch.setattr(
        update_data_pipeline.solar_data_scheduler.update_weather_cache,
        "update_weather_cache",
        lambda **kwargs: SimpleNamespace(
            historical_rows_inserted=1,
            forecast_rows_inserted=1,
        ),
    )
    monkeypatch.setattr(
        update_data_pipeline.solar_data_scheduler,
        "ensure_historical_adjusted_solar_coverage",
        lambda **kwargs: SimpleNamespace(rows=1, regenerated=False),
    )
    monkeypatch.setattr(
        update_data_pipeline.solar_data_scheduler,
        "regenerate_forecast_adjusted_solar",
        lambda **kwargs: SimpleNamespace(rows=1, regenerated=True),
    )

    def fail_grid(**kwargs: object) -> object:
        raise RuntimeError("grid maintenance failed")

    monkeypatch.setattr(
        update_data_pipeline.generate_grid_availability,
        "run_grid_availability_generation",
        fail_grid,
    )

    update_data_pipeline.import_all_storage_models()
    update_data_pipeline.create_db_and_tables(get_engine(_db_url(db_path)))

    with pytest.raises(RuntimeError, match="grid maintenance failed"):
        update_data_pipeline.run_source_maintenance(
            config_path=CONFIG_PATH,
            database_url=_db_url(db_path),
            config=config,
            history_start=date(2025, 10, 6),
            source_days_ahead=2,
            grid_days_ahead=7,
            now=FIXED_NOW,
        )


def test_source_coverage_failure_prevents_actual_system_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "coverage_failure.db"
    actual_system_calls = 0

    def fake_system_generation(*args: object, **kwargs: object):
        nonlocal actual_system_calls
        if kwargs.get("dry_run"):
            return _system_summary(dry_run=True)
        actual_system_calls += 1
        return _system_summary(dry_run=False)

    monkeypatch.setattr(
        update_data_pipeline.generate_system_simulation,
        "run_system_simulation_generation",
        fake_system_generation,
    )
    monkeypatch.setattr(
        update_data_pipeline,
        "run_source_maintenance",
        lambda **kwargs: object(),
    )

    def fail_coverage(**kwargs: object) -> object:
        raise update_data_pipeline.SourceCoverageError("solar source missing")

    monkeypatch.setattr(
        update_data_pipeline,
        "validate_system_source_coverage",
        fail_coverage,
    )

    with pytest.raises(update_data_pipeline.SourceCoverageError, match="solar source"):
        update_data_pipeline.run_data_pipeline(
            config_path=CONFIG_PATH,
            database_url=_db_url(db_path),
            now=FIXED_NOW,
        )

    assert actual_system_calls == 0


def test_db_path_argument_targets_requested_database(tmp_path: Path) -> None:
    db_path = tmp_path / "target.db"
    args = update_data_pipeline.parse_args(["--db-path", str(db_path)])

    assert update_data_pipeline._database_url_from_args(args) == f"sqlite:///{db_path}"


def test_repeated_pipeline_run_replaces_system_cache_without_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repeated.db"
    db_url = _db_url(db_path)

    monkeypatch.setattr(
        update_data_pipeline,
        "run_source_maintenance",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        update_data_pipeline,
        "validate_system_source_coverage",
        lambda **kwargs: object(),
    )

    first = update_data_pipeline.run_data_pipeline(
        config_path=CONFIG_PATH,
        database_url=db_url,
        allow_fallbacks=True,
        now=FIXED_NOW,
    )
    second = update_data_pipeline.run_data_pipeline(
        config_path=CONFIG_PATH,
        database_url=db_url,
        allow_fallbacks=True,
        now=FIXED_NOW,
    )

    assert first.system_generation is not None
    assert second.system_generation is not None
    assert first.system_generation.persisted is not None
    assert second.system_generation.persisted is not None
    assert first.system_generation.persisted.load_cache_rows == (
        second.system_generation.persisted.load_cache_rows
    )

    config = load_config(CONFIG_PATH)
    config_hash = calculate_system_config_hash(config)
    engine = get_engine(db_url)
    with Session(engine) as session:
        load_rows = list_load_cache_points(session, config.station.id, config_hash)
        battery_rows = list_battery_cache_points(session, config.station.id, config_hash)
        ems_rows = list_ems_cache_points(session, config.station.id, config_hash)

    assert len(load_rows) == second.system_generation.persisted.load_cache_rows
    assert len(battery_rows) == second.system_generation.persisted.battery_cache_rows
    assert len(ems_rows) == second.system_generation.persisted.ems_cache_rows
    assert second.system_generation.load_cache_end_utc == datetime(
        2026,
        1,
        3,
        10,
        6,
        tzinfo=timezone.utc,
    )


def _mock_non_writing_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        update_data_pipeline,
        "run_source_maintenance",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        update_data_pipeline,
        "validate_system_source_coverage",
        lambda **kwargs: object(),
    )

    def fake_system_generation(*args: object, **kwargs: object):
        return _system_summary(dry_run=bool(kwargs.get("dry_run")))

    monkeypatch.setattr(
        update_data_pipeline.generate_system_simulation,
        "run_system_simulation_generation",
        fake_system_generation,
    )


def _system_summary(*, dry_run: bool):
    persisted = None
    if not dry_run:
        persisted = SystemSimulationPersistSummary(
            load_history_rows=0,
            load_cache_rows=0,
            battery_history_rows=0,
            battery_cache_rows=0,
            ems_history_rows=0,
            ems_cache_rows=0,
        )
    return update_data_pipeline.generate_system_simulation.SystemGenerationSummary(
        station_id="smartenergy-lab",
        config_hash="test-system-hash",
        start_utc=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 1, 3, 10, 6, tzinfo=timezone.utc),
        cache_only=True,
        history_writes_enabled=False,
        allow_fallbacks=False,
        dry_run=dry_run,
        load_cache_start_utc=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        load_cache_end_utc=datetime(2026, 1, 3, 10, 6, tzinfo=timezone.utc),
        battery_cache_start_utc=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        battery_cache_end_utc=datetime(2026, 1, 3, 10, 6, tzinfo=timezone.utc),
        ems_cache_start_utc=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        ems_cache_end_utc=datetime(2026, 1, 3, 10, 6, tzinfo=timezone.utc),
        persisted=persisted,
    )


def _sqlite_table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "select name from sqlite_master where type='table'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def _db_url(db_path: Path) -> str:
    return f"sqlite:///{db_path}"
