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
from app.simulation.load import (
    LoadContext,
    LoadSimulationSettings,
    LoadSimulator,
    load_settings_from_station_config,
)
from app.simulation.weather import map_weather_code_to_state
from tools.diagnostics.load_probe_support import SyntheticBatterySocProvider


DB_PATH = PROJECT_ROOT / "backend" / "data" / "smartenergy.db"
CONFIG_PATH = PROJECT_ROOT / "backend" / "config" / "station.default.yaml"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "load_probe"
SEED = 20260513
CAPACITY_WH = 2400.0
INITIAL_SOC_PERCENT = 100.0
GRID_CHARGE_W = 500.0
PLANNED_OUTAGE_LOOKAHEAD = timedelta(hours=2)
POST_OUTAGE_RECOVERY_WINDOW = timedelta(minutes=60)


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
    grid_available: bool
    grid_behavior: str
    synthetic_soc_percent: float
    battery_wh: float
    weather_state: str
    active_professor_count: int
    active_student_count: int
    active_event_tags: str


@dataclass(frozen=True)
class ProbeSummary:
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
    avg_load_grid_available_w: float | None
    avg_load_outage_w: float | None
    high_grid_minutes: int
    high_outage_minutes: int
    weather_fallback_minutes: int
    grid_fallback_minutes: int
    top_minutes: list[MinuteRecord]
    interpretation: list[str]


RANGES = [
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
    settings = load_settings_from_station_config(
        config,
        base_settings=LoadSimulationSettings(seed=SEED),
    )
    station_timezone = ZoneInfo(settings.timezone_name)
    station_id = config.station.id

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    summaries: list[ProbeSummary] = []
    with _open_read_only_db(DB_PATH) as connection:
        for probe_range in RANGES:
            summary = _run_range(
                connection=connection,
                probe_range=probe_range,
                settings=settings,
                station_timezone=station_timezone,
                station_id=station_id,
            )
            summaries.append(summary)
            print(_format_summary(summary))

    summary_text = "\n\n".join(_format_summary(summary) for summary in summaries)
    summary_path = OUTPUT_DIR / "summary.txt"
    summary_path.write_text(summary_text + "\n", encoding="utf-8")
    print(f"Summary written to {summary_path.relative_to(PROJECT_ROOT)}")


def _run_range(
    connection: sqlite3.Connection,
    probe_range: ProbeRange,
    settings: LoadSimulationSettings,
    station_timezone: ZoneInfo,
    station_id: str,
) -> ProbeSummary:
    local_start = datetime.combine(
        probe_range.start_date,
        time.min,
        tzinfo=station_timezone,
    )
    local_end = datetime.combine(
        probe_range.end_date_inclusive + timedelta(days=1),
        time.min,
        tzinfo=station_timezone,
    )
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc)
    query_start = start_utc - PLANNED_OUTAGE_LOOKAHEAD
    query_end = end_utc + PLANNED_OUTAGE_LOOKAHEAD

    grid_rows = _read_grid_rows(connection, query_start, query_end)
    weather_rows = _read_weather_rows(connection, station_id, query_start, query_end)
    grid_timestamps = [row.timestamp_utc for row in grid_rows]
    weather_timestamps = [row.timestamp_utc for row in weather_rows]

    simulator = LoadSimulator(settings)
    battery = SyntheticBatterySocProvider(
        capacity_wh=CAPACITY_WH,
        initial_soc_percent=INITIAL_SOC_PERCENT,
        grid_charge_w=GRID_CHARGE_W,
    )

    records: list[MinuteRecord] = []
    grid_fallback_minutes = 0
    weather_fallback_minutes = 0
    last_outage_end_utc = _latest_outage_end_before(start_utc, grid_rows)
    previous_grid_available: bool | None = None

    timestamp_utc = start_utc
    while timestamp_utc < end_utc:
        grid_row = _grid_row_for_minute(timestamp_utc, grid_rows, grid_timestamps)
        if grid_row is None:
            grid_available = True
            grid_behavior = "grid_normal"
            grid_fallback_minutes += 1
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
        if weather_row is None:
            weather_state = "clear"
            weather_fallback_minutes += 1
        else:
            weather_state = weather_row.weather_state

        context = LoadContext(
            grid_behavior=grid_behavior,
            grid_available=grid_available,
            soc_percent=battery.soc_percent,
            weather_state=weather_state,
        )
        point = simulator.build_point(timestamp_utc, context)
        snapshot = battery.update_after_minute(
            total_power_draw_w=point.total_power_draw_w,
            grid_available=grid_available,
            timestamp_local=point.timestamp_local,
        )

        records.append(
            MinuteRecord(
                timestamp_local=point.timestamp_local,
                total_power_draw_w=point.total_power_draw_w,
                grid_available=grid_available,
                grid_behavior=grid_behavior,
                synthetic_soc_percent=snapshot.soc_percent,
                battery_wh=snapshot.battery_wh,
                weather_state=weather_state,
                active_professor_count=point.active_professor_count,
                active_student_count=point.active_student_count,
                active_event_tags="|".join(point.active_event_tags),
            )
        )

        previous_grid_available = grid_available
        timestamp_utc += timedelta(minutes=1)

    csv_path = OUTPUT_DIR / f"load_probe_{probe_range.slug}.csv"
    png_path = OUTPUT_DIR / f"load_probe_{probe_range.slug}.png"
    _write_csv(csv_path, records)
    _write_chart(png_path, probe_range, records)

    return _build_summary(
        probe_range=probe_range,
        csv_path=csv_path,
        png_path=png_path,
        records=records,
        grid_fallback_minutes=grid_fallback_minutes,
        weather_fallback_minutes=weather_fallback_minutes,
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
                row["current_outage_window_end_utc"]
            ),
            next_outage_window_start_utc=_parse_optional_utc(
                row["next_outage_window_start_utc"]
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
    return min(
        candidates,
        key=lambda row: abs((row.timestamp_utc - timestamp_utc).total_seconds()),
    )


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


def _write_csv(path: Path, records: list[MinuteRecord]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "timestamp_local",
                "total_power_draw_w",
                "grid_available",
                "grid_behavior",
                "synthetic_soc_percent",
                "battery_wh",
                "weather_state",
                "active_professor_count",
                "active_student_count",
                "active_event_tags",
            ],
        )
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "timestamp_local": record.timestamp_local.isoformat(),
                    "total_power_draw_w": f"{record.total_power_draw_w:.4f}",
                    "grid_available": int(record.grid_available),
                    "grid_behavior": record.grid_behavior,
                    "synthetic_soc_percent": f"{record.synthetic_soc_percent:.4f}",
                    "battery_wh": f"{record.battery_wh:.4f}",
                    "weather_state": record.weather_state,
                    "active_professor_count": record.active_professor_count,
                    "active_student_count": record.active_student_count,
                    "active_event_tags": record.active_event_tags,
                }
            )


def _write_chart(
    path: Path,
    probe_range: ProbeRange,
    records: list[MinuteRecord],
) -> None:
    timestamps = [record.timestamp_local for record in records]
    power_w = [record.total_power_draw_w for record in records]
    grid_available = [1 if record.grid_available else 0 for record in records]
    soc_percent = [record.synthetic_soc_percent for record in records]
    battery_wh = [record.battery_wh for record in records]
    active_people = [
        record.active_professor_count + record.active_student_count for record in records
    ]

    figure, axes = plt.subplots(5, 1, figsize=(16, 10), sharex=True)
    figure.suptitle(f"Load behavior probe {probe_range.slug}: {probe_range.purpose}")

    axes[0].plot(timestamps, power_w)
    axes[0].set_ylabel("Load W")

    axes[1].step(timestamps, grid_available, where="post")
    axes[1].set_ylabel("Grid")
    axes[1].set_ylim(-0.1, 1.1)

    axes[2].plot(timestamps, soc_percent)
    axes[2].set_ylabel("SoC %")
    axes[2].set_ylim(-2, 102)

    axes[3].plot(timestamps, battery_wh)
    axes[3].set_ylabel("Battery Wh")
    axes[3].set_ylim(-50, CAPACITY_WH + 50)

    axes[4].step(timestamps, active_people, where="post")
    axes[4].set_ylabel("People")

    for axis in axes:
        axis.grid(True, linewidth=0.4, alpha=0.5)
    axes[-1].set_xlabel("Local time")

    figure.autofmt_xdate()
    figure.tight_layout(rect=(0, 0, 1, 0.97))
    figure.savefig(path, dpi=100)
    plt.close(figure)


def _build_summary(
    probe_range: ProbeRange,
    csv_path: Path,
    png_path: Path,
    records: list[MinuteRecord],
    grid_fallback_minutes: int,
    weather_fallback_minutes: int,
) -> ProbeSummary:
    if not records:
        raise RuntimeError(f"No records generated for {probe_range.slug}")

    powers = [record.total_power_draw_w for record in records]
    soc_values = [record.synthetic_soc_percent for record in records]
    grid_power = [
        record.total_power_draw_w for record in records if record.grid_available
    ]
    outage_power = [
        record.total_power_draw_w for record in records if not record.grid_available
    ]
    outage_minutes = len(outage_power)
    high_grid_minutes = sum(
        1
        for record in records
        if record.grid_available and record.total_power_draw_w > 2000.0
    )
    high_outage_minutes = sum(
        1
        for record in records
        if not record.grid_available and record.total_power_draw_w > 2000.0
    )
    top_minutes = sorted(
        records,
        key=lambda record: record.total_power_draw_w,
        reverse=True,
    )[:10]

    return ProbeSummary(
        probe_range=probe_range,
        csv_path=csv_path,
        png_path=png_path,
        minutes_simulated=len(records),
        outage_minutes=outage_minutes,
        min_power_w=min(powers),
        avg_power_w=mean(powers),
        max_power_w=max(powers),
        total_energy_kwh=sum(power / 60.0 for power in powers) / 1000.0,
        min_soc_percent=min(soc_values),
        final_soc_percent=soc_values[-1],
        avg_load_grid_available_w=mean(grid_power) if grid_power else None,
        avg_load_outage_w=mean(outage_power) if outage_power else None,
        high_grid_minutes=high_grid_minutes,
        high_outage_minutes=high_outage_minutes,
        weather_fallback_minutes=weather_fallback_minutes,
        grid_fallback_minutes=grid_fallback_minutes,
        top_minutes=top_minutes,
        interpretation=_interpret_records(
            records,
            high_grid_minutes=high_grid_minutes,
            high_outage_minutes=high_outage_minutes,
            outage_minutes=outage_minutes,
        ),
    )


def _interpret_records(
    records: list[MinuteRecord],
    high_grid_minutes: int,
    high_outage_minutes: int,
    outage_minutes: int,
) -> list[str]:
    active_loads = [
        record.total_power_draw_w
        for record in records
        if record.active_professor_count + record.active_student_count > 0
    ]
    inactive_loads = [
        record.total_power_draw_w
        for record in records
        if record.active_professor_count + record.active_student_count == 0
    ]
    interpretations: list[str] = []

    if active_loads and inactive_loads and mean(active_loads) > mean(inactive_loads):
        interpretations.append("load rises during occupied class/working periods")
    else:
        interpretations.append("occupation-driven load rise needs visual review")

    daily_loads: dict[date, list[float]] = {}
    for record in records:
        daily_loads.setdefault(record.timestamp_local.date(), []).append(
            record.total_power_draw_w
        )
    weekend_averages = [
        mean(loads)
        for day, loads in daily_loads.items()
        if day.weekday() >= 5 and loads
    ]
    weekday_averages = [
        mean(loads)
        for day, loads in daily_loads.items()
        if day.weekday() < 5 and loads
    ]
    if weekend_averages and weekday_averages:
        if mean(weekend_averages) < mean(weekday_averages):
            interpretations.append("weekend load is lower than weekday/class load")
        else:
            interpretations.append("weekend load is not clearly lower in this range")

    if high_grid_minutes > 0:
        interpretations.append(">2 kW load appears while grid is available")
    else:
        interpretations.append(">2 kW grid-pass-through cases did not occur in this range")

    if outage_minutes == 0:
        interpretations.append("no outage minutes in this range")
    elif high_outage_minutes == 0:
        interpretations.append(">2 kW outage load is absent")
    elif high_outage_minutes <= max(3, int(outage_minutes * 0.01)):
        interpretations.append(">2 kW outage load is rare")
    else:
        interpretations.append(">2 kW outage load should be reviewed")

    soc_values = [record.synthetic_soc_percent for record in records]
    if outage_minutes > 0 and min(soc_values) < max(soc_values):
        interpretations.append("synthetic SoC falls during outages and recovers on grid")
    else:
        interpretations.append("synthetic SoC behavior is flat or has no outage opportunity")

    return interpretations


def _format_summary(summary: ProbeSummary) -> str:
    avg_grid = _format_optional(summary.avg_load_grid_available_w, " W")
    avg_outage = _format_optional(summary.avg_load_outage_w, " W")
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
        f"Total energy consumed: {summary.total_energy_kwh:.3f} kWh",
        (
            "Synthetic SoC min/final: "
            f"{summary.min_soc_percent:.2f}% / {summary.final_soc_percent:.2f}%"
        ),
        f"Average load while grid available: {avg_grid}",
        f"Average load during outage: {avg_outage}",
        f"Minutes >2000 W while grid available: {summary.high_grid_minutes}",
        f"Minutes >2000 W during outage: {summary.high_outage_minutes}",
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
            f"soc={record.synthetic_soc_percent:.2f}% | "
            f"weather={record.weather_state} | "
            f"tags={record.active_event_tags or '-'}"
        )
    lines.append("Interpretation:")
    lines.extend(f"  {item}" for item in summary.interpretation)
    return "\n".join(lines)


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
