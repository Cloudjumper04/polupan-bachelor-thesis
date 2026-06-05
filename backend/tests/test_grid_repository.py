from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session

from app.config_loader import load_config
from app.main import get_grid_current
from app.storage.database import create_db_and_tables, get_engine
from app.storage.grid_repository import (
    GridAvailabilityPointRecord,
    find_missing_grid_availability_timestamps,
    list_grid_availability_points,
    save_grid_availability_points,
)


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import generate_grid_availability


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "station.default.yaml"
STATION_TIMEZONE = ZoneInfo("Europe/Kyiv")


def test_repository_inserts_and_reads_grid_availability_points(tmp_path: Path) -> None:
    engine = get_engine(_database_url(tmp_path))
    create_db_and_tables(engine)
    timestamp = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

    with Session(engine) as session:
        inserted = save_grid_availability_points(session, [_point(timestamp)])
        rows = list_grid_availability_points(
            session,
            timestamp,
            timestamp + timedelta(minutes=30),
        )

    assert inserted == 1
    assert len(rows) == 1
    assert rows[0].timestamp_utc == timestamp
    assert rows[0].outage_queue == "3.1"
    assert rows[0].local_grid_available is True


def test_repository_skips_duplicate_timestamp_rows(tmp_path: Path) -> None:
    engine = get_engine(_database_url(tmp_path))
    create_db_and_tables(engine)
    timestamp = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

    with Session(engine) as session:
        first_insert = save_grid_availability_points(
            session,
            [_point(timestamp), _point(timestamp, voltage=229.0)],
        )
        second_insert = save_grid_availability_points(session, [_point(timestamp)])
        rows = list_grid_availability_points(session)

    assert first_insert == 1
    assert second_insert == 0
    assert len(rows) == 1


def test_gap_detection_fills_missing_grid_availability_range(tmp_path: Path) -> None:
    engine = get_engine(_database_url(tmp_path))
    create_db_and_tables(engine)
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)

    with Session(engine) as session:
        save_grid_availability_points(
            session,
            [
                _point(start),
                _point(start + timedelta(hours=1)),
            ],
        )
        missing = find_missing_grid_availability_timestamps(
            session,
            start,
            end,
            cadence_minutes=30,
        )
        save_grid_availability_points(
            session,
            [_point(timestamp) for timestamp in missing],
        )
        rows = list_grid_availability_points(session, start, end)

    assert missing == [
        start + timedelta(minutes=30),
        start + timedelta(minutes=90),
    ]
    assert len(rows) == 4


def test_generate_grid_availability_cli_help_works() -> None:
    script_path = SCRIPTS_DIR / "generate_grid_availability.py"

    result = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Generate deterministic grid damage" in result.stdout


def test_generate_grid_availability_small_range_uses_temp_database(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)

    summary = generate_grid_availability.run_grid_availability_generation(
        config_path=CONFIG_PATH,
        database_url=database_url,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 1),
        days_ahead=0,
        seed=20260513,
        now=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )
    second_summary = generate_grid_availability.run_grid_availability_generation(
        config_path=CONFIG_PATH,
        database_url=database_url,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 1),
        days_ahead=0,
        seed=20260513,
        now=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )

    engine = get_engine(database_url)
    with Session(engine) as session:
        rows = list_grid_availability_points(session)

    assert summary.generated_points == 48
    assert summary.availability_rows_inserted == 48
    assert second_summary.availability_rows_inserted == 0
    assert len(rows) == 48


def test_grid_current_accepts_at_alias(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = _database_url(tmp_path)
    config = load_config(CONFIG_PATH)
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    timestamp = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    with Session(engine) as session:
        save_grid_availability_points(session, [_point(timestamp, voltage=221.0)])
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_grid_current(at=timestamp + timedelta(minutes=12))

    assert payload["status"] == "ok"
    assert payload["station"]["id"] == config.station.id
    assert payload["requested_at_utc"] == (
        timestamp + timedelta(minutes=12)
    ).isoformat()
    assert payload["resolved_at_utc"] == timestamp.isoformat()
    assert payload["current"]["grid_voltage_v"] == 221.0


def _point(
    timestamp_utc: datetime,
    voltage: float = 230.0,
) -> GridAvailabilityPointRecord:
    return GridAvailabilityPointRecord(
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc.astimezone(STATION_TIMEZONE),
        generation_health_percent=130.0,
        delivery_health_percent=130.0,
        effective_health_percent=130.0,
        deficit_percent=0.0,
        daily_outage_hours=0.0,
        outage_level="stable",
        outage_queue="3.1",
        local_grid_available=True,
        is_outage_now=False,
        grid_voltage_v=voltage,
        reason="no active damage",
        current_outage_window_start_utc=None,
        current_outage_window_end_utc=None,
        next_outage_window_start_utc=None,
        next_outage_window_end_utc=None,
    )


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'grid.db'}"
