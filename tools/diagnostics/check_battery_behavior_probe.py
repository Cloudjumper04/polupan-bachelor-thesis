from __future__ import annotations

import csv
import sqlite3
import sys
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config_loader import load_config
from app.simulation.battery import BatterySimulator, BatteryStepInput
from app.simulation.load import (
    LoadContext,
    LoadSimulationSettings,
    LoadSimulator,
    load_settings_from_station_config,
)
from app.simulation.weather import map_weather_code_to_state


DB_PATH = PROJECT_ROOT / "backend" / "data" / "smartenergy.db"
CONFIG_PATH = PROJECT_ROOT / "backend" / "config" / "station.default.yaml"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "battery_probe"
SEED = 20260513
PLANNED_OUTAGE_LOOKAHEAD = timedelta(hours=2)
POST_OUTAGE_RECOVERY_WINDOW = timedelta(minutes=60)
WEATHER_MAX_DISTANCE = timedelta(hours=3)


@dataclass(frozen=True)
class ProbeRange:
    slug: str
    start_date: date
    end_date_inclusive: date
    purpose: str


@dataclass(frozen=True)
class GridRow:
    timestamp_utc: datetime
    local_grid_available: bool
    is_outage_now: bool
    current_outage_window_end_utc: datetime | None
    next_outage_window_start_utc: datetime | None


@dataclass(frozen=True)
class WeatherRow:
    timestamp_utc: datetime
    weather_state: str


@dataclass(frozen=True)
class MinuteRecord:
    timestamp_local: datetime
    total_power_draw_w: float
    consumed_energy_wh_last_minute: float
    grid_available: bool
    grid_behavior: str
    weather_state: str
    battery_soc_percent: float
    battery_energy_wh: float
    battery_voltage_v: float
    battery_soh_percent: float
    battery_status: str
    battery_provides_energy: bool
    requested_charge_power_w: float
    applied_charge_power_w: float
    stored_charge_energy_wh: float
    applied_discharge_energy_wh: float
    removed_discharge_energy_wh: float
    active_professor_count: int
    active_student_count: int
    active_event_tags: str

    @property
    def applied_discharge_power_w(self) -> float:
        return self.applied_discharge_energy_wh * 60.0


@dataclass
class DailyAccumulator:
    day: date
    daily_energy_consumed_kwh: float = 0.0
    daily_discharge_wh: float = 0.0
    daily_charge_wh: float = 0.0
    soc_values: list[float] | None = None
    voltage_values: list[float] | None = None
    cumulative_equivalent_cycles: float = 0.0
    soh_percent: float = 100.0
    current_usable_capacity_wh: float = 0.0
    outage_minutes: int = 0
    empty_minutes: int = 0
    full_minutes: int = 0
    final_soc_percent: float = 0.0

    def __post_init__(self) -> None:
        if self.soc_values is None:
            self.soc_values = []
        if self.voltage_values is None:
            self.voltage_values = []

    def add(self, record: MinuteRecord, cumulative_cycles: float, capacity_wh: float) -> None:
        self.daily_energy_consumed_kwh += record.consumed_energy_wh_last_minute / 1000.0
        self.daily_discharge_wh += record.removed_discharge_energy_wh
        self.daily_charge_wh += record.stored_charge_energy_wh
        self.soc_values.append(record.battery_soc_percent)
        self.voltage_values.append(record.battery_voltage_v)
        self.cumulative_equivalent_cycles = cumulative_cycles
        self.soh_percent = record.battery_soh_percent
        self.current_usable_capacity_wh = capacity_wh
        self.final_soc_percent = record.battery_soc_percent
        if not record.grid_available:
            self.outage_minutes += 1
        if record.battery_status == "empty":
            self.empty_minutes += 1
        if record.battery_status == "full":
            self.full_minutes += 1

    def to_row(self) -> dict[str, str]:
        soc_values = self.soc_values or [0.0]
        voltage_values = self.voltage_values or [0.0]
        daily_equivalent_cycles = (
            self.daily_discharge_wh / self.current_usable_capacity_wh
            if self.current_usable_capacity_wh > 0.0
            else 0.0
        )
        return {
            "date": self.day.isoformat(),
            "daily_energy_consumed_kwh": f"{self.daily_energy_consumed_kwh:.6f}",
            "daily_discharge_wh": f"{self.daily_discharge_wh:.4f}",
            "daily_charge_wh": f"{self.daily_charge_wh:.4f}",
            "daily_min_soc_percent": f"{min(soc_values):.4f}",
            "daily_avg_soc_percent": f"{mean(soc_values):.4f}",
            "daily_final_soc_percent": f"{self.final_soc_percent:.4f}",
            "daily_min_voltage_v": f"{min(voltage_values):.4f}",
            "daily_equivalent_cycles": f"{daily_equivalent_cycles:.8f}",
            "cumulative_equivalent_cycles": f"{self.cumulative_equivalent_cycles:.8f}",
            "soh_percent": f"{self.soh_percent:.6f}",
            "current_usable_capacity_wh": f"{self.current_usable_capacity_wh:.4f}",
            "outage_minutes": str(self.outage_minutes),
        }


@dataclass(frozen=True)
class ShortRangeSummary:
    probe_range: ProbeRange
    csv_path: Path
    png_path: Path
    minutes_simulated: int
    outage_minutes: int
    min_power_w: float
    avg_power_w: float
    max_power_w: float
    total_energy_kwh: float
    min_soc_percent: float
    final_soc_percent: float
    min_energy_wh: float
    final_energy_wh: float
    min_soh_percent: float
    final_soh_percent: float
    min_voltage_v: float
    max_voltage_v: float
    avg_load_grid_available_w: float | None
    avg_load_outage_w: float | None
    high_grid_minutes: int
    high_outage_minutes: int
    empty_minutes: int
    full_minutes: int
    grid_fallback_minutes: int
    weather_fallback_minutes: int
    top_minutes: list[MinuteRecord]


@dataclass(frozen=True)
class LongRangeSummary:
    csv_path: Path
    png_path: Path
    start_date: date
    end_date_inclusive: date
    days_simulated: int
    total_load_energy_kwh: float
    total_discharged_wh: float
    total_charged_wh: float
    min_soc_percent: float
    final_soc_percent: float
    initial_soh_percent: float
    final_soh_percent: float
    total_equivalent_cycles: float
    final_current_usable_capacity_wh: float
    days_with_battery_empty: int
    days_with_outages: int
    worst_soc_day: date
    grid_fallback_minutes: int
    weather_fallback_minutes: int


SHORT_RANGES = [
    ProbeRange(
        slug="2026-01-14_to_2026-01-16",
        start_date=date(2026, 1, 14),
        end_date_inclusive=date(2026, 1, 16),
        purpose="severe outage period plus class behavior",
    ),
    ProbeRange(
        slug="2026-01-11_to_2026-01-13",
        start_date=date(2026, 1, 11),
        end_date_inclusive=date(2026, 1, 13),
        purpose="Sunday to Tuesday weekly pattern",
    ),
    ProbeRange(
        slug="2026-05-11_to_2026-05-13",
        start_date=date(2026, 5, 11),
        end_date_inclusive=date(2026, 5, 13),
        purpose="calmer baseline/recovery comparison",
    ),
]


def main() -> None:
    config = load_config(CONFIG_PATH)
    load_settings = load_settings_from_station_config(
        config,
        base_settings=LoadSimulationSettings(seed=SEED),
    )
    station_timezone = ZoneInfo(load_settings.timezone_name)
    station_id = config.station.id
    battery_config = config.station.battery

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with _open_read_only_db(DB_PATH) as connection:
        short_summaries = [
            _run_short_range(
                connection=connection,
                probe_range=probe_range,
                load_settings=load_settings,
                station_timezone=station_timezone,
                station_id=station_id,
                config=config,
            )
            for probe_range in SHORT_RANGES
        ]

        long_summary = _run_long_range(
            connection=connection,
            load_settings=load_settings,
            station_timezone=station_timezone,
            station_id=station_id,
            config=config,
        )

    summary_text = _format_full_summary(
        short_summaries=short_summaries,
        long_summary=long_summary,
        battery_config=battery_config,
    )
    summary_path = OUTPUT_DIR / "summary.txt"
    summary_path.write_text(summary_text + "\n", encoding="utf-8")
    print(summary_text)
    print(f"\nSummary written to {summary_path.relative_to(PROJECT_ROOT)}")


def _run_short_range(
    connection: sqlite3.Connection,
    probe_range: ProbeRange,
    load_settings: LoadSimulationSettings,
    station_timezone: ZoneInfo,
    station_id: str,
    config: object,
) -> ShortRangeSummary:
    local_start, local_end = _local_range(
        probe_range.start_date,
        probe_range.end_date_inclusive,
        station_timezone,
    )
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc)
    records, grid_fallback_minutes, weather_fallback_minutes = _simulate_minutes(
        connection=connection,
        start_utc=start_utc,
        end_utc=end_utc,
        load_settings=load_settings,
        station_timezone=station_timezone,
        station_id=station_id,
        config=config,
    )

    csv_path = OUTPUT_DIR / f"battery_probe_{probe_range.slug}.csv"
    png_path = OUTPUT_DIR / f"battery_probe_{probe_range.slug}.png"
    _write_short_csv(csv_path, records)
    _write_short_chart(png_path, probe_range, records)
    return _build_short_summary(
        probe_range=probe_range,
        csv_path=csv_path,
        png_path=png_path,
        records=records,
        grid_fallback_minutes=grid_fallback_minutes,
        weather_fallback_minutes=weather_fallback_minutes,
    )


def _run_long_range(
    connection: sqlite3.Connection,
    load_settings: LoadSimulationSettings,
    station_timezone: ZoneInfo,
    station_id: str,
    config: object,
) -> LongRangeSummary:
    battery_installation_date = date.fromisoformat(config.station.battery.installation_date)
    latest_grid_utc = _latest_grid_timestamp(connection)
    end_date_inclusive = latest_grid_utc.astimezone(station_timezone).date()
    local_start, local_end = _local_range(
        battery_installation_date,
        end_date_inclusive,
        station_timezone,
    )
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc)

    daily_rows, grid_fallback_minutes, weather_fallback_minutes = _simulate_daily(
        connection=connection,
        start_utc=start_utc,
        end_utc=end_utc,
        load_settings=load_settings,
        station_timezone=station_timezone,
        station_id=station_id,
        config=config,
    )

    csv_path = OUTPUT_DIR / (
        f"battery_probe_daily_{battery_installation_date.isoformat()}_to_"
        f"{end_date_inclusive.isoformat()}.csv"
    )
    png_path = OUTPUT_DIR / (
        f"battery_probe_daily_{battery_installation_date.isoformat()}_to_"
        f"{end_date_inclusive.isoformat()}.png"
    )
    _write_daily_csv(csv_path, daily_rows)
    _write_daily_chart(png_path, daily_rows)
    return _build_long_summary(
        csv_path=csv_path,
        png_path=png_path,
        start_date=battery_installation_date,
        end_date_inclusive=end_date_inclusive,
        daily_rows=daily_rows,
        grid_fallback_minutes=grid_fallback_minutes,
        weather_fallback_minutes=weather_fallback_minutes,
    )


def _simulate_minutes(
    connection: sqlite3.Connection,
    start_utc: datetime,
    end_utc: datetime,
    load_settings: LoadSimulationSettings,
    station_timezone: ZoneInfo,
    station_id: str,
    config: object,
) -> tuple[list[MinuteRecord], int, int]:
    query_start = start_utc - PLANNED_OUTAGE_LOOKAHEAD
    query_end = end_utc + PLANNED_OUTAGE_LOOKAHEAD
    grid_rows = _read_grid_rows(connection, query_start, query_end)
    weather_rows = _read_weather_rows(connection, station_id, query_start, query_end)
    grid_timestamps = [row.timestamp_utc for row in grid_rows]
    weather_timestamps = [row.timestamp_utc for row in weather_rows]

    load_simulator = LoadSimulator(load_settings)
    battery = BatterySimulator.from_station_config(config)
    records: list[MinuteRecord] = []
    grid_fallback_minutes = 0
    weather_fallback_minutes = 0
    last_outage_end_utc = _latest_outage_end_before(start_utc, grid_rows)
    previous_grid_available: bool | None = None

    timestamp_utc = start_utc
    while timestamp_utc < end_utc:
        record, grid_fallback, weather_fallback, previous_grid_available, last_outage_end_utc = (
            _simulate_one_minute(
                timestamp_utc=timestamp_utc,
                load_simulator=load_simulator,
                battery=battery,
                grid_rows=grid_rows,
                grid_timestamps=grid_timestamps,
                weather_rows=weather_rows,
                weather_timestamps=weather_timestamps,
                previous_grid_available=previous_grid_available,
                last_outage_end_utc=last_outage_end_utc,
            )
        )
        records.append(record)
        grid_fallback_minutes += int(grid_fallback)
        weather_fallback_minutes += int(weather_fallback)
        timestamp_utc += timedelta(minutes=1)

    return records, grid_fallback_minutes, weather_fallback_minutes


def _simulate_daily(
    connection: sqlite3.Connection,
    start_utc: datetime,
    end_utc: datetime,
    load_settings: LoadSimulationSettings,
    station_timezone: ZoneInfo,
    station_id: str,
    config: object,
) -> tuple[list[dict[str, str]], int, int]:
    query_start = start_utc - PLANNED_OUTAGE_LOOKAHEAD
    query_end = end_utc + PLANNED_OUTAGE_LOOKAHEAD
    grid_rows = _read_grid_rows(connection, query_start, query_end)
    weather_rows = _read_weather_rows(connection, station_id, query_start, query_end)
    grid_timestamps = [row.timestamp_utc for row in grid_rows]
    weather_timestamps = [row.timestamp_utc for row in weather_rows]

    load_simulator = LoadSimulator(load_settings)
    battery = BatterySimulator.from_station_config(config)
    daily: dict[date, DailyAccumulator] = {}
    grid_fallback_minutes = 0
    weather_fallback_minutes = 0
    last_outage_end_utc = _latest_outage_end_before(start_utc, grid_rows)
    previous_grid_available: bool | None = None
    previous_day: date | None = None

    timestamp_utc = start_utc
    while timestamp_utc < end_utc:
        record, grid_fallback, weather_fallback, previous_grid_available, last_outage_end_utc = (
            _simulate_one_minute(
                timestamp_utc=timestamp_utc,
                load_simulator=load_simulator,
                battery=battery,
                grid_rows=grid_rows,
                grid_timestamps=grid_timestamps,
                weather_rows=weather_rows,
                weather_timestamps=weather_timestamps,
                previous_grid_available=previous_grid_available,
                last_outage_end_utc=last_outage_end_utc,
            )
        )
        day = record.timestamp_local.astimezone(station_timezone).date()
        if previous_day is not None and day != previous_day and previous_day in daily:
            daily[previous_day].soh_percent = battery.state.soh_percent
            daily[previous_day].current_usable_capacity_wh = (
                battery.state.current_usable_capacity_wh
            )
            daily[previous_day].cumulative_equivalent_cycles = (
                battery.state.total_equivalent_cycles
            )
        accumulator = daily.setdefault(day, DailyAccumulator(day=day))
        state = battery.state
        accumulator.add(
            record,
            cumulative_cycles=state.total_equivalent_cycles + state.equivalent_cycles_today,
            capacity_wh=state.current_usable_capacity_wh,
        )
        grid_fallback_minutes += int(grid_fallback)
        weather_fallback_minutes += int(weather_fallback)
        previous_day = day
        timestamp_utc += timedelta(minutes=1)

    if previous_day is not None and previous_day in daily:
        battery.finalize_day(battery.active_day)
        daily[previous_day].soh_percent = battery.state.soh_percent
        daily[previous_day].current_usable_capacity_wh = (
            battery.state.current_usable_capacity_wh
        )
        daily[previous_day].cumulative_equivalent_cycles = (
            battery.state.total_equivalent_cycles
        )

    return (
        [daily[day].to_row() for day in sorted(daily)],
        grid_fallback_minutes,
        weather_fallback_minutes,
    )


def _simulate_one_minute(
    *,
    timestamp_utc: datetime,
    load_simulator: LoadSimulator,
    battery: BatterySimulator,
    grid_rows: list[GridRow],
    grid_timestamps: list[datetime],
    weather_rows: list[WeatherRow],
    weather_timestamps: list[datetime],
    previous_grid_available: bool | None,
    last_outage_end_utc: datetime | None,
) -> tuple[MinuteRecord, bool, bool, bool, datetime | None]:
    grid_row = _grid_row_for_minute(timestamp_utc, grid_rows, grid_timestamps)
    grid_fallback = grid_row is None
    if grid_row is None:
        grid_available = True
        grid_behavior = "grid_normal"
    else:
        grid_available = grid_row.local_grid_available and not grid_row.is_outage_now
        if previous_grid_available is False and grid_available:
            last_outage_end_utc = timestamp_utc
        grid_behavior = _derive_grid_behavior(
            timestamp_utc=timestamp_utc,
            grid_available=grid_available,
            grid_row=grid_row,
            last_outage_end_utc=last_outage_end_utc,
        )

    weather_row = _weather_row_for_minute(
        timestamp_utc,
        weather_rows,
        weather_timestamps,
    )
    weather_fallback = weather_row is None
    weather_state = "clear" if weather_row is None else weather_row.weather_state

    context = LoadContext(
        grid_behavior=grid_behavior,
        grid_available=grid_available,
        soc_percent=battery.state.soc_percent,
        weather_state=weather_state,
    )
    load_point = load_simulator.build_point(timestamp_utc, context)
    consumed_energy_wh = load_point.total_power_draw_w / 60.0

    battery_provides_energy = not grid_available
    requested_charge_power_w = battery.max_charge_power_w if grid_available else 0.0
    battery_result = battery.step(
        BatteryStepInput(
            timestamp=load_point.timestamp_local,
            consumed_energy_wh_last_minute=consumed_energy_wh,
            battery_provides_energy=battery_provides_energy,
            requested_charge_power_w=requested_charge_power_w,
        )
    )
    state = battery_result.state

    return (
        MinuteRecord(
            timestamp_local=load_point.timestamp_local,
            total_power_draw_w=load_point.total_power_draw_w,
            consumed_energy_wh_last_minute=consumed_energy_wh,
            grid_available=grid_available,
            grid_behavior=grid_behavior,
            weather_state=weather_state,
            battery_soc_percent=state.soc_percent,
            battery_energy_wh=state.energy_wh,
            battery_voltage_v=state.voltage_v,
            battery_soh_percent=state.soh_percent,
            battery_status=state.status.value,
            battery_provides_energy=battery_provides_energy,
            requested_charge_power_w=requested_charge_power_w,
            applied_charge_power_w=battery_result.applied_charge_power_w,
            stored_charge_energy_wh=battery_result.stored_charge_energy_wh,
            applied_discharge_energy_wh=battery_result.applied_discharge_energy_wh,
            removed_discharge_energy_wh=battery_result.removed_discharge_energy_wh,
            active_professor_count=load_point.active_professor_count,
            active_student_count=load_point.active_student_count,
            active_event_tags="|".join(load_point.active_event_tags),
        ),
        grid_fallback,
        weather_fallback,
        grid_available,
        last_outage_end_utc,
    )


def _open_read_only_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Runtime database not found: {db_path}")
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _read_grid_rows(
    connection: sqlite3.Connection,
    start_utc: datetime,
    end_utc: datetime,
) -> list[GridRow]:
    rows = connection.execute(
        """
        SELECT
            timestamp_utc,
            local_grid_available,
            is_outage_now,
            current_outage_window_end_utc,
            next_outage_window_start_utc
        FROM grid_availability_point
        WHERE timestamp_utc >= ? AND timestamp_utc < ?
        ORDER BY timestamp_utc
        """,
        (start_utc.isoformat(), end_utc.isoformat()),
    ).fetchall()
    return [
        GridRow(
            timestamp_utc=_parse_utc(row["timestamp_utc"]),
            local_grid_available=bool(row["local_grid_available"]),
            is_outage_now=bool(row["is_outage_now"]),
            current_outage_window_end_utc=_parse_optional_utc(
                row["current_outage_window_end_utc"],
            ),
            next_outage_window_start_utc=_parse_optional_utc(
                row["next_outage_window_start_utc"],
            ),
        )
        for row in rows
    ]


def _read_weather_rows(
    connection: sqlite3.Connection,
    station_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[WeatherRow]:
    weather_by_timestamp: dict[datetime, WeatherRow] = {}
    for table_name, timestamp_column in (
        ("weatherobservation", "timestamp_utc"),
        ("weatherforecast", "forecast_timestamp_utc"),
    ):
        rows = connection.execute(
            f"""
            SELECT {timestamp_column} AS timestamp_utc, weather_code
            FROM {table_name}
            WHERE station_id = ? AND {timestamp_column} >= ? AND {timestamp_column} < ?
            ORDER BY {timestamp_column}
            """,
            (station_id, start_utc.isoformat(), end_utc.isoformat()),
        ).fetchall()
        for row in rows:
            timestamp_utc = _parse_utc(row["timestamp_utc"])
            weather_by_timestamp.setdefault(
                timestamp_utc,
                WeatherRow(
                    timestamp_utc=timestamp_utc,
                    weather_state=map_weather_code_to_state(row["weather_code"]),
                ),
            )
    return sorted(weather_by_timestamp.values(), key=lambda row: row.timestamp_utc)


def _latest_grid_timestamp(connection: sqlite3.Connection) -> datetime:
    row = connection.execute(
        "SELECT MAX(timestamp_utc) AS timestamp_utc FROM grid_availability_point",
    ).fetchone()
    if row is None or row["timestamp_utc"] is None:
        raise RuntimeError("No grid availability rows found")
    return _parse_utc(row["timestamp_utc"])


def _grid_row_for_minute(
    timestamp_utc: datetime,
    rows: list[GridRow],
    timestamps: list[datetime],
) -> GridRow | None:
    if not rows:
        return None
    index = bisect_right(timestamps, timestamp_utc) - 1
    if index < 0:
        return rows[0]
    return rows[index]


def _weather_row_for_minute(
    timestamp_utc: datetime,
    rows: list[WeatherRow],
    timestamps: list[datetime],
) -> WeatherRow | None:
    if not rows:
        return None
    index = bisect_left(timestamps, timestamp_utc)
    candidates: list[WeatherRow] = []
    if index < len(rows):
        candidates.append(rows[index])
    if index > 0:
        candidates.append(rows[index - 1])
    closest = min(
        candidates,
        key=lambda row: abs((row.timestamp_utc - timestamp_utc).total_seconds()),
    )
    if abs(closest.timestamp_utc - timestamp_utc) > WEATHER_MAX_DISTANCE:
        return None
    return closest


def _derive_grid_behavior(
    timestamp_utc: datetime,
    grid_available: bool,
    grid_row: GridRow,
    last_outage_end_utc: datetime | None,
) -> str:
    if not grid_available:
        return "outage_active"

    if (
        grid_row.current_outage_window_end_utc is not None
        and timedelta(0)
        <= timestamp_utc - grid_row.current_outage_window_end_utc
        <= POST_OUTAGE_RECOVERY_WINDOW
    ):
        return "post_outage_recovery"

    if (
        last_outage_end_utc is not None
        and timedelta(0)
        <= timestamp_utc - last_outage_end_utc
        <= POST_OUTAGE_RECOVERY_WINDOW
    ):
        return "post_outage_recovery"

    if (
        grid_row.next_outage_window_start_utc is not None
        and timedelta(0)
        < grid_row.next_outage_window_start_utc - timestamp_utc
        <= PLANNED_OUTAGE_LOOKAHEAD
    ):
        return "planned_outage_soon"

    return "grid_normal"


def _latest_outage_end_before(
    timestamp_utc: datetime,
    rows: list[GridRow],
) -> datetime | None:
    outage_ends = [
        row.current_outage_window_end_utc
        for row in rows
        if row.current_outage_window_end_utc is not None
        and row.current_outage_window_end_utc <= timestamp_utc
    ]
    return max(outage_ends, default=None)


def _write_short_csv(path: Path, records: list[MinuteRecord]) -> None:
    fieldnames = [
        "timestamp_local",
        "total_power_draw_w",
        "consumed_energy_wh_last_minute",
        "grid_available",
        "grid_behavior",
        "weather_state",
        "battery_soc_percent",
        "battery_energy_wh",
        "battery_voltage_v",
        "battery_soh_percent",
        "battery_status",
        "battery_provides_energy",
        "requested_charge_power_w",
        "applied_charge_power_w",
        "stored_charge_energy_wh",
        "applied_discharge_energy_wh",
        "removed_discharge_energy_wh",
        "active_professor_count",
        "active_student_count",
        "active_event_tags",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "timestamp_local": record.timestamp_local.isoformat(),
                    "total_power_draw_w": f"{record.total_power_draw_w:.4f}",
                    "consumed_energy_wh_last_minute": f"{record.consumed_energy_wh_last_minute:.6f}",
                    "grid_available": int(record.grid_available),
                    "grid_behavior": record.grid_behavior,
                    "weather_state": record.weather_state,
                    "battery_soc_percent": f"{record.battery_soc_percent:.4f}",
                    "battery_energy_wh": f"{record.battery_energy_wh:.4f}",
                    "battery_voltage_v": f"{record.battery_voltage_v:.4f}",
                    "battery_soh_percent": f"{record.battery_soh_percent:.6f}",
                    "battery_status": record.battery_status,
                    "battery_provides_energy": int(record.battery_provides_energy),
                    "requested_charge_power_w": f"{record.requested_charge_power_w:.4f}",
                    "applied_charge_power_w": f"{record.applied_charge_power_w:.4f}",
                    "stored_charge_energy_wh": f"{record.stored_charge_energy_wh:.6f}",
                    "applied_discharge_energy_wh": f"{record.applied_discharge_energy_wh:.6f}",
                    "removed_discharge_energy_wh": f"{record.removed_discharge_energy_wh:.6f}",
                    "active_professor_count": record.active_professor_count,
                    "active_student_count": record.active_student_count,
                    "active_event_tags": record.active_event_tags,
                }
            )


def _write_daily_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "date",
        "daily_energy_consumed_kwh",
        "daily_discharge_wh",
        "daily_charge_wh",
        "daily_min_soc_percent",
        "daily_avg_soc_percent",
        "daily_final_soc_percent",
        "daily_min_voltage_v",
        "daily_equivalent_cycles",
        "cumulative_equivalent_cycles",
        "soh_percent",
        "current_usable_capacity_wh",
        "outage_minutes",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_short_chart(
    path: Path,
    probe_range: ProbeRange,
    records: list[MinuteRecord],
) -> None:
    timestamps = [record.timestamp_local for record in records]
    discharge_power = [-record.applied_discharge_power_w for record in records]

    figure, axes = plt.subplots(7, 1, figsize=(16, 14), sharex=True)
    figure.suptitle(f"Battery behavior probe {probe_range.slug}: {probe_range.purpose}")

    axes[0].plot(timestamps, [record.total_power_draw_w for record in records])
    axes[0].set_ylabel("Load W")

    axes[1].step(timestamps, [int(record.grid_available) for record in records], where="post")
    axes[1].set_ylabel("Grid")
    axes[1].set_ylim(-0.1, 1.1)

    axes[2].plot(timestamps, [record.battery_soc_percent for record in records])
    axes[2].set_ylabel("SoC %")
    axes[2].set_ylim(-2, 102)

    axes[3].plot(timestamps, [record.battery_energy_wh for record in records])
    axes[3].set_ylabel("Battery Wh")

    axes[4].plot(timestamps, [record.battery_voltage_v for record in records])
    axes[4].set_ylabel("Voltage V")

    axes[5].plot(timestamps, [record.applied_charge_power_w for record in records], label="charge")
    axes[5].plot(timestamps, discharge_power, label="discharge")
    axes[5].set_ylabel("Battery W")
    axes[5].legend(loc="upper right")

    axes[6].step(
        timestamps,
        [
            record.active_professor_count + record.active_student_count
            for record in records
        ],
        where="post",
    )
    axes[6].set_ylabel("People")
    axes[6].set_xlabel("Local time")

    for axis in axes:
        axis.grid(True, linewidth=0.4, alpha=0.5)

    figure.autofmt_xdate()
    figure.tight_layout(rect=(0, 0, 1, 0.97))
    figure.savefig(path, dpi=100)
    plt.close(figure)


def _write_daily_chart(path: Path, rows: list[dict[str, str]]) -> None:
    dates = [date.fromisoformat(row["date"]) for row in rows]
    figure, axes = plt.subplots(6, 1, figsize=(16, 14), sharex=True)
    figure.suptitle("Battery behavior probe daily aggregation")

    axes[0].plot(dates, [float(row["daily_min_soc_percent"]) for row in rows])
    axes[0].set_ylabel("Min SoC %")
    axes[0].set_ylim(-2, 102)

    axes[1].plot(dates, [float(row["daily_discharge_wh"]) for row in rows], label="discharge")
    axes[1].plot(dates, [float(row["daily_charge_wh"]) for row in rows], label="charge")
    axes[1].set_ylabel("Wh/day")
    axes[1].legend(loc="upper right")

    axes[2].plot(dates, [float(row["daily_equivalent_cycles"]) for row in rows])
    axes[2].set_ylabel("Cycles/day")

    axes[3].plot(dates, [float(row["cumulative_equivalent_cycles"]) for row in rows])
    axes[3].set_ylabel("Total cycles")

    axes[4].plot(dates, [float(row["soh_percent"]) for row in rows])
    axes[4].set_ylabel("SoH %")

    axes[5].plot(dates, [float(row["current_usable_capacity_wh"]) for row in rows])
    axes[5].set_ylabel("Usable Wh")
    axes[5].set_xlabel("Date")

    for axis in axes:
        axis.grid(True, linewidth=0.4, alpha=0.5)

    figure.autofmt_xdate()
    figure.tight_layout(rect=(0, 0, 1, 0.97))
    figure.savefig(path, dpi=100)
    plt.close(figure)


def _build_short_summary(
    probe_range: ProbeRange,
    csv_path: Path,
    png_path: Path,
    records: list[MinuteRecord],
    grid_fallback_minutes: int,
    weather_fallback_minutes: int,
) -> ShortRangeSummary:
    if not records:
        raise RuntimeError(f"No records generated for {probe_range.slug}")

    powers = [record.total_power_draw_w for record in records]
    soc_values = [record.battery_soc_percent for record in records]
    energy_values = [record.battery_energy_wh for record in records]
    soh_values = [record.battery_soh_percent for record in records]
    voltage_values = [record.battery_voltage_v for record in records]
    grid_power = [
        record.total_power_draw_w for record in records if record.grid_available
    ]
    outage_power = [
        record.total_power_draw_w for record in records if not record.grid_available
    ]
    top_minutes = sorted(
        records,
        key=lambda record: record.total_power_draw_w,
        reverse=True,
    )[:10]
    return ShortRangeSummary(
        probe_range=probe_range,
        csv_path=csv_path,
        png_path=png_path,
        minutes_simulated=len(records),
        outage_minutes=len(outage_power),
        min_power_w=min(powers),
        avg_power_w=mean(powers),
        max_power_w=max(powers),
        total_energy_kwh=sum(record.consumed_energy_wh_last_minute for record in records) / 1000.0,
        min_soc_percent=min(soc_values),
        final_soc_percent=soc_values[-1],
        min_energy_wh=min(energy_values),
        final_energy_wh=energy_values[-1],
        min_soh_percent=min(soh_values),
        final_soh_percent=soh_values[-1],
        min_voltage_v=min(voltage_values),
        max_voltage_v=max(voltage_values),
        avg_load_grid_available_w=mean(grid_power) if grid_power else None,
        avg_load_outage_w=mean(outage_power) if outage_power else None,
        high_grid_minutes=sum(
            1
            for record in records
            if record.grid_available and record.total_power_draw_w > 2000.0
        ),
        high_outage_minutes=sum(
            1
            for record in records
            if not record.grid_available and record.total_power_draw_w > 2000.0
        ),
        empty_minutes=sum(1 for record in records if record.battery_status == "empty"),
        full_minutes=sum(1 for record in records if record.battery_status == "full"),
        grid_fallback_minutes=grid_fallback_minutes,
        weather_fallback_minutes=weather_fallback_minutes,
        top_minutes=top_minutes,
    )


def _build_long_summary(
    csv_path: Path,
    png_path: Path,
    start_date: date,
    end_date_inclusive: date,
    daily_rows: list[dict[str, str]],
    grid_fallback_minutes: int,
    weather_fallback_minutes: int,
) -> LongRangeSummary:
    if not daily_rows:
        raise RuntimeError("No daily records generated")
    min_soc_by_day = {
        date.fromisoformat(row["date"]): float(row["daily_min_soc_percent"])
        for row in daily_rows
    }
    worst_soc_day = min(min_soc_by_day, key=min_soc_by_day.get)
    return LongRangeSummary(
        csv_path=csv_path,
        png_path=png_path,
        start_date=start_date,
        end_date_inclusive=end_date_inclusive,
        days_simulated=len(daily_rows),
        total_load_energy_kwh=sum(
            float(row["daily_energy_consumed_kwh"]) for row in daily_rows
        ),
        total_discharged_wh=sum(float(row["daily_discharge_wh"]) for row in daily_rows),
        total_charged_wh=sum(float(row["daily_charge_wh"]) for row in daily_rows),
        min_soc_percent=min(float(row["daily_min_soc_percent"]) for row in daily_rows),
        final_soc_percent=float(daily_rows[-1]["daily_final_soc_percent"]),
        initial_soh_percent=100.0,
        final_soh_percent=float(daily_rows[-1]["soh_percent"]),
        total_equivalent_cycles=float(daily_rows[-1]["cumulative_equivalent_cycles"]),
        final_current_usable_capacity_wh=float(daily_rows[-1]["current_usable_capacity_wh"]),
        days_with_battery_empty=sum(
            1 for row in daily_rows if float(row["daily_min_soc_percent"]) <= 0.0001
        ),
        days_with_outages=sum(1 for row in daily_rows if int(row["outage_minutes"]) > 0),
        worst_soc_day=worst_soc_day,
        grid_fallback_minutes=grid_fallback_minutes,
        weather_fallback_minutes=weather_fallback_minutes,
    )


def _format_full_summary(
    short_summaries: list[ShortRangeSummary],
    long_summary: LongRangeSummary,
    battery_config: object,
) -> str:
    sections = [
        "Battery behavior diagnostic probe",
        "",
        "Battery config read from YAML:",
        f"- chemistry: {battery_config.chemistry}",
        f"- nominal_voltage_v: {battery_config.nominal_voltage_v}",
        f"- capacity_ah: {battery_config.capacity_ah}",
        f"- installation_date: {battery_config.installation_date}",
        "",
        "Short ranges:",
    ]
    for summary in short_summaries:
        sections.append(_format_short_summary(summary))
        sections.append("")
    sections.append("Long range:")
    sections.append(_format_long_summary(long_summary))
    sections.append("")
    sections.append("Interpretation:")
    sections.extend(f"- {item}" for item in _interpret_results(short_summaries, long_summary))
    return "\n".join(sections)


def _format_short_summary(summary: ShortRangeSummary) -> str:
    lines = [
        f"Range: {summary.probe_range.slug}",
        f"Purpose: {summary.probe_range.purpose}",
        f"CSV: {summary.csv_path.relative_to(PROJECT_ROOT)}",
        f"PNG: {summary.png_path.relative_to(PROJECT_ROOT)}",
        f"Minutes simulated: {summary.minutes_simulated}",
        f"Outage minutes: {summary.outage_minutes}",
        (
            "Power W min/avg/max: "
            f"{summary.min_power_w:.2f} / {summary.avg_power_w:.2f} / {summary.max_power_w:.2f}"
        ),
        f"Total load energy: {summary.total_energy_kwh:.3f} kWh",
        (
            "Battery SoC min/final: "
            f"{summary.min_soc_percent:.2f}% / {summary.final_soc_percent:.2f}%"
        ),
        (
            "Battery energy Wh min/final: "
            f"{summary.min_energy_wh:.2f} / {summary.final_energy_wh:.2f}"
        ),
        (
            "Battery SoH min/final: "
            f"{summary.min_soh_percent:.6f}% / {summary.final_soh_percent:.6f}%"
        ),
        f"Battery voltage V min/max: {summary.min_voltage_v:.3f} / {summary.max_voltage_v:.3f}",
        f"Average load while grid available: {_format_optional(summary.avg_load_grid_available_w, ' W')}",
        f"Average load during outage: {_format_optional(summary.avg_load_outage_w, ' W')}",
        f"Minutes >2000 W while grid available: {summary.high_grid_minutes}",
        f"Minutes >2000 W during outage: {summary.high_outage_minutes}",
        f"Minutes battery empty: {summary.empty_minutes}",
        f"Minutes battery full: {summary.full_minutes}",
        f"Grid fallback minutes: {summary.grid_fallback_minutes}",
        f"Weather fallback minutes: {summary.weather_fallback_minutes}",
        "Top 10 highest-load minutes:",
    ]
    for record in summary.top_minutes:
        lines.append(
            "  "
            f"{record.timestamp_local.isoformat()} | "
            f"{record.total_power_draw_w:.2f} W | "
            f"grid={int(record.grid_available)} {record.grid_behavior} | "
            f"soc={record.battery_soc_percent:.2f}% | "
            f"voltage={record.battery_voltage_v:.2f} V | "
            f"weather={record.weather_state} | "
            f"status={record.battery_status} | "
            f"tags={record.active_event_tags or '-'}"
        )
    return "\n".join(lines)


def _format_long_summary(summary: LongRangeSummary) -> str:
    return "\n".join(
        [
            f"Date range: {summary.start_date.isoformat()} to {summary.end_date_inclusive.isoformat()}",
            f"CSV: {summary.csv_path.relative_to(PROJECT_ROOT)}",
            f"PNG: {summary.png_path.relative_to(PROJECT_ROOT)}",
            f"Days simulated: {summary.days_simulated}",
            f"Total load energy: {summary.total_load_energy_kwh:.3f} kWh",
            f"Total discharged Wh: {summary.total_discharged_wh:.2f}",
            f"Total charged Wh: {summary.total_charged_wh:.2f}",
            f"Min SoC over range: {summary.min_soc_percent:.2f}%",
            f"Final SoC: {summary.final_soc_percent:.2f}%",
            f"Initial/final SoH: {summary.initial_soh_percent:.6f}% / {summary.final_soh_percent:.6f}%",
            f"Total equivalent cycles: {summary.total_equivalent_cycles:.6f}",
            f"Final current usable capacity Wh: {summary.final_current_usable_capacity_wh:.2f}",
            f"Days with battery empty: {summary.days_with_battery_empty}",
            f"Days with outages: {summary.days_with_outages}",
            f"Worst SoC day: {summary.worst_soc_day.isoformat()}",
            f"Grid fallback minutes: {summary.grid_fallback_minutes}",
            f"Weather fallback minutes: {summary.weather_fallback_minutes}",
        ]
    )


def _interpret_results(
    short_summaries: list[ShortRangeSummary],
    long_summary: LongRangeSummary,
) -> list[str]:
    january = [
        summary
        for summary in short_summaries
        if summary.probe_range.start_date.month == 1
    ]
    may = next(
        (
            summary
            for summary in short_summaries
            if summary.probe_range.start_date.month == 5
        ),
        None,
    )
    interpretations = [
        "Lead-acid 12 V 200 Ah has 2400 Wh nominal energy and about 960 Wh operational usable capacity with the current lead-acid profile.",
        "Grid-available minutes request maximum profile charging and do not subtract load from the battery.",
        "Outage minutes set battery_provides_energy=true and discharge the battery through the battery module limits.",
    ]
    if january and may is not None and min(s.min_soc_percent for s in january) < may.min_soc_percent:
        interpretations.append("January outage ranges drop SoC more than the calmer May range.")
    if any(summary.high_outage_minutes == 0 for summary in short_summaries):
        interpretations.append("At least one outage range has no >2 kW outage supply minutes.")
    if long_summary.total_equivalent_cycles > 0.0 and long_summary.final_soh_percent < long_summary.initial_soh_percent:
        interpretations.append("Long-history cycling produces small but nonzero SoH degradation.")
    if long_summary.days_with_battery_empty > 0:
        interpretations.append("The small lead-acid usable capacity reaches empty on outage days; this is expected for the diagnostic EMS.")
    return interpretations


def _local_range(
    start_date: date,
    end_date_inclusive: date,
    timezone_info: ZoneInfo,
) -> tuple[datetime, datetime]:
    return (
        datetime.combine(start_date, time.min, tzinfo=timezone_info),
        datetime.combine(end_date_inclusive + timedelta(days=1), time.min, tzinfo=timezone_info),
    )


def _format_optional(value: float | None, suffix: str) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}{suffix}"


def _parse_optional_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    return _parse_utc(value)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


if __name__ == "__main__":
    main()
