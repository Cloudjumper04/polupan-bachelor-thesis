from __future__ import annotations

import csv
import sqlite3
import sys
from bisect import bisect_left, bisect_right
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean
from time import perf_counter
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
from app.simulation.ems import EmsConfig, EmsDecisionEngine, EmsHistorySummary, EmsInput
from app.simulation.load import (
    LoadContext,
    LoadSimulationSettings,
    LoadSimulator,
    load_settings_from_station_config,
)


DB_PATH = PROJECT_ROOT / "backend" / "data" / "smartenergy.db"
CONFIG_PATH = PROJECT_ROOT / "backend" / "config" / "station.default.yaml"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "ems_probe"
SEED = 20260513
WEATHER_MAX_DISTANCE = timedelta(hours=3)
SOLAR_MAX_INTERPOLATION_GAP = timedelta(hours=2)
PROGRESS_INTERVAL_MINUTES = 25000
MAX_EXPECTED_MINUTES = 600000
ALLOW_HUGE_SIMULATION = False


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


@dataclass(frozen=True)
class WeatherRow:
    timestamp_utc: datetime
    weather_state: str


@dataclass(frozen=True)
class SolarRow:
    timestamp_utc: datetime
    power_w: float


@dataclass(frozen=True)
class MinuteRecord:
    timestamp_local: datetime
    grid_available: bool
    grid_behavior: str
    solar_available_power_w: float
    weather_state: str
    requested_load_power_w: float
    effective_served_load_w: float
    ems_selected_mode: str
    ems_reason: str
    auto_risk_score: int
    cheap_tariff_active: bool
    protection_active: bool
    inverter_output_enabled: bool
    solar_to_load_w: float
    solar_to_battery_w: float
    grid_to_load_w: float
    grid_to_battery_w: float
    battery_to_load_w: float
    requested_charge_power_w: float
    requested_battery_discharge_energy_wh_last_minute: float
    battery_soc_percent: float
    battery_energy_wh: float
    battery_voltage_v: float
    battery_soh_percent: float
    battery_status: str
    applied_charge_power_w: float
    stored_charge_energy_wh: float
    applied_discharge_energy_wh: float
    removed_discharge_energy_wh: float
    active_professor_count: int
    active_student_count: int
    active_event_tags: str
    cumulative_equivalent_cycles: float
    current_usable_capacity_wh: float

    @property
    def applied_discharge_power_w(self) -> float:
        return self.applied_discharge_energy_wh * 60.0


@dataclass
class DailyAccumulator:
    day: date
    daily_requested_load_kwh: float = 0.0
    daily_served_load_kwh: float = 0.0
    daily_solar_available_kwh: float = 0.0
    daily_solar_to_load_kwh: float = 0.0
    daily_solar_to_battery_kwh: float = 0.0
    daily_grid_to_load_kwh: float = 0.0
    daily_grid_to_battery_kwh: float = 0.0
    daily_battery_to_load_kwh: float = 0.0
    daily_charge_wh: float = 0.0
    daily_discharge_wh: float = 0.0
    soc_values: list[float] = field(default_factory=list)
    voltage_values: list[float] = field(default_factory=list)
    risk_scores: list[int] = field(default_factory=list)
    mode_counts: Counter[str] = field(default_factory=Counter)
    cumulative_equivalent_cycles: float = 0.0
    soh_percent: float = 100.0
    current_usable_capacity_wh: float = 0.0
    outage_minutes: int = 0
    protection_minutes: int = 0
    final_soc_percent: float = 0.0

    def add(self, record: MinuteRecord) -> None:
        self.daily_requested_load_kwh += record.requested_load_power_w / 60.0 / 1000.0
        self.daily_served_load_kwh += record.effective_served_load_w / 60.0 / 1000.0
        self.daily_solar_available_kwh += record.solar_available_power_w / 60.0 / 1000.0
        self.daily_solar_to_load_kwh += record.solar_to_load_w / 60.0 / 1000.0
        self.daily_solar_to_battery_kwh += record.solar_to_battery_w / 60.0 / 1000.0
        self.daily_grid_to_load_kwh += record.grid_to_load_w / 60.0 / 1000.0
        self.daily_grid_to_battery_kwh += record.grid_to_battery_w / 60.0 / 1000.0
        self.daily_battery_to_load_kwh += record.battery_to_load_w / 60.0 / 1000.0
        self.daily_charge_wh += record.stored_charge_energy_wh
        self.daily_discharge_wh += record.removed_discharge_energy_wh
        self.soc_values.append(record.battery_soc_percent)
        self.voltage_values.append(record.battery_voltage_v)
        self.risk_scores.append(record.auto_risk_score)
        self.mode_counts.update([record.ems_selected_mode])
        self.cumulative_equivalent_cycles = record.cumulative_equivalent_cycles
        self.soh_percent = record.battery_soh_percent
        self.current_usable_capacity_wh = record.current_usable_capacity_wh
        self.final_soc_percent = record.battery_soc_percent
        if not record.grid_available:
            self.outage_minutes += 1
        if record.protection_active:
            self.protection_minutes += 1

    def to_row(self) -> dict[str, str]:
        soc_values = self.soc_values or [0.0]
        voltage_values = self.voltage_values or [0.0]
        risk_scores = self.risk_scores or [0]
        daily_equivalent_cycles = (
            self.daily_discharge_wh / self.current_usable_capacity_wh
            if self.current_usable_capacity_wh > 0.0
            else 0.0
        )
        return {
            "date": self.day.isoformat(),
            "daily_requested_load_kwh": f"{self.daily_requested_load_kwh:.6f}",
            "daily_served_load_kwh": f"{self.daily_served_load_kwh:.6f}",
            "daily_solar_available_kwh": f"{self.daily_solar_available_kwh:.6f}",
            "daily_solar_to_load_kwh": f"{self.daily_solar_to_load_kwh:.6f}",
            "daily_solar_to_battery_kwh": f"{self.daily_solar_to_battery_kwh:.6f}",
            "daily_grid_to_load_kwh": f"{self.daily_grid_to_load_kwh:.6f}",
            "daily_grid_to_battery_kwh": f"{self.daily_grid_to_battery_kwh:.6f}",
            "daily_battery_to_load_kwh": f"{self.daily_battery_to_load_kwh:.6f}",
            "daily_charge_wh": f"{self.daily_charge_wh:.6f}",
            "daily_discharge_wh": f"{self.daily_discharge_wh:.6f}",
            "daily_min_soc_percent": f"{min(soc_values):.6f}",
            "daily_avg_soc_percent": f"{mean(soc_values):.6f}",
            "daily_final_soc_percent": f"{self.final_soc_percent:.6f}",
            "daily_min_voltage_v": f"{min(voltage_values):.6f}",
            "daily_equivalent_cycles": f"{daily_equivalent_cycles:.8f}",
            "cumulative_equivalent_cycles": f"{self.cumulative_equivalent_cycles:.8f}",
            "soh_percent": f"{self.soh_percent:.6f}",
            "current_usable_capacity_wh": f"{self.current_usable_capacity_wh:.6f}",
            "outage_minutes": str(self.outage_minutes),
            "protection_minutes": str(self.protection_minutes),
            "average_auto_risk_score": f"{mean(risk_scores):.4f}",
            "dominant_ems_mode": _dominant_mode(self.mode_counts),
        }


@dataclass
class HistoryTracker:
    outage_minutes_6h: deque[datetime] = field(default_factory=deque)
    outage_minutes_24h: deque[datetime] = field(default_factory=deque)
    outage_minutes_72h: deque[datetime] = field(default_factory=deque)
    outage_starts_24h: deque[datetime] = field(default_factory=deque)
    outage_starts_72h: deque[datetime] = field(default_factory=deque)
    soc_history_24h: deque[tuple[datetime, float]] = field(default_factory=deque)
    soc_min_24h: deque[tuple[datetime, float]] = field(default_factory=deque)
    last_outage_end_utc: datetime | None = None
    battery_recovered_to_full_after_last_outage: bool = True

    def summary(self, timestamp_utc: datetime) -> EmsHistorySummary:
        self.trim(timestamp_utc)
        min_soc_last_24h = self.soc_min_24h[0][1] if self.soc_min_24h else None
        hours_since_last_outage = (
            (timestamp_utc - self.last_outage_end_utc).total_seconds() / 3600.0
            if self.last_outage_end_utc is not None
            else None
        )
        return EmsHistorySummary(
            outage_minutes_last_6h=float(len(self.outage_minutes_6h)),
            outage_minutes_last_24h=float(len(self.outage_minutes_24h)),
            outage_count_last_24h=len(self.outage_starts_24h),
            outage_count_last_72h=len(self.outage_starts_72h),
            hours_since_last_outage=hours_since_last_outage,
            min_soc_last_24h=min_soc_last_24h,
            battery_recovered_to_full_after_last_outage=(
                self.battery_recovered_to_full_after_last_outage
            ),
        )

    def trim(self, timestamp_utc: datetime) -> None:
        _trim_datetime_queue(self.outage_minutes_6h, timestamp_utc - timedelta(hours=6))
        _trim_datetime_queue(self.outage_minutes_24h, timestamp_utc - timedelta(hours=24))
        _trim_datetime_queue(self.outage_minutes_72h, timestamp_utc - timedelta(hours=72))
        _trim_datetime_queue(self.outage_starts_24h, timestamp_utc - timedelta(hours=24))
        _trim_datetime_queue(self.outage_starts_72h, timestamp_utc - timedelta(hours=72))
        soc_cutoff = timestamp_utc - timedelta(hours=24)
        while self.soc_history_24h and self.soc_history_24h[0][0] < soc_cutoff:
            self.soc_history_24h.popleft()
        while self.soc_min_24h and self.soc_min_24h[0][0] < soc_cutoff:
            self.soc_min_24h.popleft()

    def record_after_minute(
        self,
        *,
        timestamp_utc: datetime,
        grid_available: bool,
        previous_grid_available: bool | None,
        soc_percent: float,
        recovery_soc_target_percent: float,
    ) -> None:
        if not grid_available:
            self.outage_minutes_6h.append(timestamp_utc)
            self.outage_minutes_24h.append(timestamp_utc)
            self.outage_minutes_72h.append(timestamp_utc)
            if previous_grid_available is not False:
                self.outage_starts_24h.append(timestamp_utc)
                self.outage_starts_72h.append(timestamp_utc)
        elif previous_grid_available is False:
            self.last_outage_end_utc = timestamp_utc
            self.battery_recovered_to_full_after_last_outage = False

        if (
            grid_available
            and not self.battery_recovered_to_full_after_last_outage
            and soc_percent >= recovery_soc_target_percent
        ):
            self.battery_recovered_to_full_after_last_outage = True

        self.soc_history_24h.append((timestamp_utc, soc_percent))
        while self.soc_min_24h and self.soc_min_24h[-1][1] >= soc_percent:
            self.soc_min_24h.pop()
        self.soc_min_24h.append((timestamp_utc, soc_percent))
        self.trim(timestamp_utc)


@dataclass
class DataFallbackCounts:
    grid_fallback_minutes: int = 0
    weather_fallback_minutes: int = 0
    solar_fallback_minutes: int = 0


@dataclass(frozen=True)
class ShortRangeOutput:
    probe_range: ProbeRange
    records: list[MinuteRecord]
    csv_path: Path
    png_path: Path
    summary: dict[str, object]


@dataclass(frozen=True)
class SimulationOutput:
    start_utc: datetime
    end_utc_exclusive: datetime
    short_outputs: list[ShortRangeOutput]
    daily_rows: list[dict[str, str]]
    daily_csv_path: Path
    daily_png_path: Path
    fallback_counts: DataFallbackCounts
    total_minutes: int
    runtime_seconds: float


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
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    config = load_config(CONFIG_PATH)
    load_settings = load_settings_from_station_config(
        config,
        base_settings=LoadSimulationSettings(seed=SEED),
    )
    station_timezone = ZoneInfo(load_settings.timezone_name)
    ems_config = EmsConfig.from_station_config(config)
    battery_installation_date = _as_date(config.station.battery.installation_date)

    with _open_read_only_db(DB_PATH) as connection:
        grid_bounds = _table_bounds(connection, "grid_availability_point", "timestamp_utc")
        solar_bounds = _combined_solar_bounds(connection, config.station.id)
        start_utc, end_utc_exclusive = _simulation_bounds(
            installation_date=battery_installation_date,
            station_timezone=station_timezone,
            grid_bounds=grid_bounds,
            solar_bounds=solar_bounds,
        )
        grid_rows = _read_grid_rows(connection, start_utc, end_utc_exclusive)
        weather_rows = _read_weather_rows(
            connection,
            config.station.id,
            start_utc - WEATHER_MAX_DISTANCE,
            end_utc_exclusive + WEATHER_MAX_DISTANCE,
        )
        solar_rows = _read_solar_rows(
            connection,
            config.station.id,
            start_utc - SOLAR_MAX_INTERPOLATION_GAP,
            end_utc_exclusive + SOLAR_MAX_INTERPOLATION_GAP,
        )

    output = _run_continuous_simulation(
        config=config,
        load_settings=load_settings,
        ems_config=ems_config,
        station_timezone=station_timezone,
        start_utc=start_utc,
        end_utc_exclusive=end_utc_exclusive,
        grid_rows=grid_rows,
        weather_rows=weather_rows,
        solar_rows=solar_rows,
    )
    summary_text = _format_summary(
        config=config,
        load_settings=load_settings,
        ems_config=ems_config,
        output=output,
    )
    summary_path = OUTPUT_DIR / "summary.txt"
    summary_path.write_text(summary_text + "\n", encoding="utf-8")
    print(summary_text)
    print(f"\nSummary written to {summary_path.relative_to(PROJECT_ROOT)}")


def _run_continuous_simulation(
    *,
    config: object,
    load_settings: LoadSimulationSettings,
    ems_config: EmsConfig,
    station_timezone: ZoneInfo,
    start_utc: datetime,
    end_utc_exclusive: datetime,
    grid_rows: list[GridRow],
    weather_rows: list[WeatherRow],
    solar_rows: list[SolarRow],
) -> SimulationOutput:
    run_started = perf_counter()
    expected_minutes = int((end_utc_exclusive - start_utc).total_seconds() // 60)
    print(
        "Starting EMS continuous simulation: "
        f"{start_utc.isoformat()} to {end_utc_exclusive.isoformat()} "
        f"exclusive ({expected_minutes} expected minutes)",
        flush=True,
    )
    if expected_minutes > MAX_EXPECTED_MINUTES and not ALLOW_HUGE_SIMULATION:
        raise RuntimeError(
            "EMS probe range is unexpectedly large: "
            f"{expected_minutes} minutes exceeds {MAX_EXPECTED_MINUTES}. "
            "Set ALLOW_HUGE_SIMULATION=True in the diagnostic script to run it."
        )
    if expected_minutes > MAX_EXPECTED_MINUTES:
        print(
            "Warning: huge EMS probe range allowed by ALLOW_HUGE_SIMULATION=True",
            flush=True,
        )

    load_simulator = LoadSimulator(load_settings)
    battery = BatterySimulator.from_station_config(config)
    ems_engine = EmsDecisionEngine(ems_config)

    grid_timestamps = [row.timestamp_utc for row in grid_rows]
    weather_timestamps = [row.timestamp_utc for row in weather_rows]
    solar_timestamps = [row.timestamp_utc for row in solar_rows]
    history = HistoryTracker()
    fallback_counts = DataFallbackCounts()
    previous_grid_available: bool | None = None
    total_minutes = 0

    short_windows = {
        item.slug: _local_range(item.start_date, item.end_date_inclusive, station_timezone)
        for item in SHORT_RANGES
    }
    short_records: dict[str, list[MinuteRecord]] = {
        item.slug: [] for item in SHORT_RANGES
    }
    daily: dict[date, DailyAccumulator] = {}
    previous_day: date | None = None

    timestamp_utc = start_utc
    while timestamp_utc < end_utc_exclusive:
        grid_available, grid_fallback = _grid_available_for_minute(
            timestamp_utc,
            grid_rows,
            grid_timestamps,
        )
        fallback_counts.grid_fallback_minutes += int(grid_fallback)
        if previous_grid_available is False and grid_available:
            history.last_outage_end_utc = timestamp_utc
            history.battery_recovered_to_full_after_last_outage = False
        grid_behavior = _grid_behavior_for_minute(
            timestamp_utc=timestamp_utc,
            grid_available=grid_available,
            recovery_minutes=ems_config.recent_outage_recovery_minutes,
            last_outage_end_utc=history.last_outage_end_utc,
        )

        weather_state, weather_fallback = _weather_state_for_minute(
            timestamp_utc,
            weather_rows,
            weather_timestamps,
        )
        fallback_counts.weather_fallback_minutes += int(weather_fallback)
        solar_power_w, solar_fallback = _solar_power_for_minute(
            timestamp_utc,
            solar_rows,
            solar_timestamps,
        )
        fallback_counts.solar_fallback_minutes += int(solar_fallback)

        state_before = battery.state
        load_point = load_simulator.build_point(
            timestamp_utc,
            LoadContext(
                grid_behavior=grid_behavior,
                grid_available=grid_available,
                soc_percent=state_before.soc_percent,
                weather_state=weather_state,
            ),
        )
        history_summary = history.summary(timestamp_utc)
        decision = ems_engine.decide(
            EmsInput(
                timestamp=load_point.timestamp_local,
                grid_available=grid_available,
                grid_was_available_previous_minute=previous_grid_available,
                solar_available_power_w=solar_power_w,
                load_power_w=load_point.total_power_draw_w,
                battery_soc_percent=state_before.soc_percent,
                battery_soh_percent=state_before.soh_percent,
                battery_energy_wh=state_before.energy_wh,
                battery_current_usable_capacity_wh=(
                    state_before.current_usable_capacity_wh
                ),
                battery_voltage_v=state_before.voltage_v,
                battery_status=state_before.status.value,
                battery_max_charge_power_w=battery.max_charge_power_w,
                history_summary=history_summary,
            )
        )
        battery_result = battery.step(
            BatteryStepInput(
                timestamp=load_point.timestamp_local,
                consumed_energy_wh_last_minute=(
                    decision.requested_battery_discharge_energy_wh_last_minute
                ),
                battery_provides_energy=decision.battery_provides_energy,
                requested_charge_power_w=decision.requested_charge_power_w,
            )
        )
        state_after = battery_result.state
        cumulative_cycles = (
            state_after.total_equivalent_cycles + state_after.equivalent_cycles_today
        )
        record = MinuteRecord(
            timestamp_local=load_point.timestamp_local,
            grid_available=grid_available,
            grid_behavior=grid_behavior,
            solar_available_power_w=solar_power_w,
            weather_state=weather_state,
            requested_load_power_w=load_point.total_power_draw_w,
            effective_served_load_w=decision.effective_served_load_w,
            ems_selected_mode=decision.selected_mode.value,
            ems_reason=decision.reason,
            auto_risk_score=decision.auto_risk_score,
            cheap_tariff_active=decision.cheap_tariff_active,
            protection_active=decision.protection_active,
            inverter_output_enabled=decision.inverter_output_enabled,
            solar_to_load_w=decision.solar_to_load_w,
            solar_to_battery_w=decision.solar_to_battery_w,
            grid_to_load_w=decision.grid_to_load_w,
            grid_to_battery_w=decision.grid_to_battery_w,
            battery_to_load_w=decision.battery_to_load_w,
            requested_charge_power_w=decision.requested_charge_power_w,
            requested_battery_discharge_energy_wh_last_minute=(
                decision.requested_battery_discharge_energy_wh_last_minute
            ),
            battery_soc_percent=state_after.soc_percent,
            battery_energy_wh=state_after.energy_wh,
            battery_voltage_v=state_after.voltage_v,
            battery_soh_percent=state_after.soh_percent,
            battery_status=state_after.status.value,
            applied_charge_power_w=battery_result.applied_charge_power_w,
            stored_charge_energy_wh=battery_result.stored_charge_energy_wh,
            applied_discharge_energy_wh=battery_result.applied_discharge_energy_wh,
            removed_discharge_energy_wh=battery_result.removed_discharge_energy_wh,
            active_professor_count=load_point.active_professor_count,
            active_student_count=load_point.active_student_count,
            active_event_tags="|".join(load_point.active_event_tags),
            cumulative_equivalent_cycles=cumulative_cycles,
            current_usable_capacity_wh=state_after.current_usable_capacity_wh,
        )

        local_day = record.timestamp_local.date()
        if previous_day is not None and local_day != previous_day and previous_day in daily:
            daily[previous_day].cumulative_equivalent_cycles = (
                state_after.total_equivalent_cycles
            )
            daily[previous_day].soh_percent = state_after.soh_percent
            daily[previous_day].current_usable_capacity_wh = (
                state_after.current_usable_capacity_wh
            )
        daily.setdefault(local_day, DailyAccumulator(day=local_day)).add(record)
        previous_day = local_day

        for probe_range in SHORT_RANGES:
            local_start, local_end = short_windows[probe_range.slug]
            if local_start <= record.timestamp_local < local_end:
                short_records[probe_range.slug].append(record)

        history.record_after_minute(
            timestamp_utc=timestamp_utc,
            grid_available=grid_available,
            previous_grid_available=previous_grid_available,
            soc_percent=state_after.soc_percent,
            recovery_soc_target_percent=ems_config.normal_target_soc_percent,
        )
        previous_grid_available = grid_available
        total_minutes += 1
        if (
            total_minutes % PROGRESS_INTERVAL_MINUTES == 0
            or total_minutes == expected_minutes
        ):
            percent = (total_minutes / expected_minutes * 100.0) if expected_minutes else 100.0
            print(
                "EMS simulation progress: "
                f"{total_minutes}/{expected_minutes} minutes "
                f"({percent:.1f}%) at {timestamp_utc.isoformat()}",
                flush=True,
            )
        timestamp_utc += timedelta(minutes=1)

    if previous_day is not None and previous_day in daily:
        battery.finalize_day(battery.active_day)
        final_state = battery.state
        daily[previous_day].cumulative_equivalent_cycles = (
            final_state.total_equivalent_cycles
        )
        daily[previous_day].soh_percent = final_state.soh_percent
        daily[previous_day].current_usable_capacity_wh = (
            final_state.current_usable_capacity_wh
        )

    short_outputs: list[ShortRangeOutput] = []
    for probe_range in SHORT_RANGES:
        records = short_records[probe_range.slug]
        csv_path = OUTPUT_DIR / f"ems_probe_{probe_range.slug}.csv"
        png_path = OUTPUT_DIR / f"ems_probe_{probe_range.slug}.png"
        print(f"Writing short CSV: {csv_path.relative_to(PROJECT_ROOT)}", flush=True)
        _write_short_csv(csv_path, records)
        print(f"Wrote short CSV: {csv_path.relative_to(PROJECT_ROOT)}", flush=True)
        print(f"Writing short PNG: {png_path.relative_to(PROJECT_ROOT)}", flush=True)
        _write_short_chart(png_path, probe_range, records)
        print(f"Wrote short PNG: {png_path.relative_to(PROJECT_ROOT)}", flush=True)
        short_outputs.append(
            ShortRangeOutput(
                probe_range=probe_range,
                records=records,
                csv_path=csv_path,
                png_path=png_path,
                summary=_build_short_summary(records),
            )
        )

    daily_rows = [daily[day].to_row() for day in sorted(daily)]
    start_date = start_utc.astimezone(station_timezone).date()
    end_date = (end_utc_exclusive - timedelta(minutes=1)).astimezone(
        station_timezone
    ).date()
    daily_csv_path = OUTPUT_DIR / (
        f"ems_probe_daily_{start_date.isoformat()}_to_{end_date.isoformat()}.csv"
    )
    daily_png_path = OUTPUT_DIR / (
        f"ems_probe_daily_{start_date.isoformat()}_to_{end_date.isoformat()}.png"
    )
    print(f"Writing daily CSV: {daily_csv_path.relative_to(PROJECT_ROOT)}", flush=True)
    _write_daily_csv(daily_csv_path, daily_rows)
    print(f"Wrote daily CSV: {daily_csv_path.relative_to(PROJECT_ROOT)}", flush=True)
    print(f"Writing daily PNG: {daily_png_path.relative_to(PROJECT_ROOT)}", flush=True)
    _write_daily_chart(daily_png_path, daily_rows)
    print(f"Wrote daily PNG: {daily_png_path.relative_to(PROJECT_ROOT)}", flush=True)
    runtime_seconds = perf_counter() - run_started
    print(f"EMS probe runtime: {runtime_seconds:.2f} seconds", flush=True)
    return SimulationOutput(
        start_utc=start_utc,
        end_utc_exclusive=end_utc_exclusive,
        short_outputs=short_outputs,
        daily_rows=daily_rows,
        daily_csv_path=daily_csv_path,
        daily_png_path=daily_png_path,
        fallback_counts=fallback_counts,
        total_minutes=total_minutes,
        runtime_seconds=runtime_seconds,
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
        SELECT timestamp_utc, local_grid_available, is_outage_now
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
            WHERE station_id = ?
              AND {timestamp_column} >= ?
              AND {timestamp_column} < ?
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
                    weather_state=_map_weather_code_to_state(row["weather_code"]),
                ),
            )
    return sorted(weather_by_timestamp.values(), key=lambda row: row.timestamp_utc)


def _read_solar_rows(
    connection: sqlite3.Connection,
    station_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[SolarRow]:
    rows_by_timestamp: dict[datetime, SolarRow] = {}
    for table_name, timestamp_column, power_column in (
        ("simulatedsolarproduction", "timestamp_utc", "simulated_power_w"),
        ("forecastsolarproduction", "timestamp_utc", "forecast_power_w"),
    ):
        rows = connection.execute(
            f"""
            SELECT {timestamp_column} AS timestamp_utc, {power_column} AS power_w
            FROM {table_name}
            WHERE station_id = ?
              AND {timestamp_column} >= ?
              AND {timestamp_column} < ?
            ORDER BY {timestamp_column}
            """,
            (station_id, start_utc.isoformat(), end_utc.isoformat()),
        ).fetchall()
        for row in rows:
            timestamp_utc = _parse_utc(row["timestamp_utc"])
            rows_by_timestamp.setdefault(
                timestamp_utc,
                SolarRow(
                    timestamp_utc=timestamp_utc,
                    power_w=max(0.0, float(row["power_w"] or 0.0)),
                ),
            )
    return sorted(rows_by_timestamp.values(), key=lambda row: row.timestamp_utc)


def _table_bounds(
    connection: sqlite3.Connection,
    table_name: str,
    timestamp_column: str,
) -> tuple[datetime, datetime]:
    row = connection.execute(
        f"SELECT MIN({timestamp_column}) AS min_ts, MAX({timestamp_column}) AS max_ts FROM {table_name}"
    ).fetchone()
    if row is None or row["min_ts"] is None or row["max_ts"] is None:
        raise RuntimeError(f"No timestamp bounds available for {table_name}")
    return _parse_utc(row["min_ts"]), _parse_utc(row["max_ts"])


def _combined_solar_bounds(
    connection: sqlite3.Connection,
    station_id: str,
) -> tuple[datetime, datetime]:
    bounds: list[tuple[datetime, datetime]] = []
    for table_name, timestamp_column in (
        ("simulatedsolarproduction", "timestamp_utc"),
        ("forecastsolarproduction", "timestamp_utc"),
    ):
        row = connection.execute(
            f"""
            SELECT MIN({timestamp_column}) AS min_ts, MAX({timestamp_column}) AS max_ts
            FROM {table_name}
            WHERE station_id = ?
            """,
            (station_id,),
        ).fetchone()
        if row is not None and row["min_ts"] is not None and row["max_ts"] is not None:
            bounds.append((_parse_utc(row["min_ts"]), _parse_utc(row["max_ts"])))
    if not bounds:
        raise RuntimeError("No generated solar production rows found")
    return min(item[0] for item in bounds), max(item[1] for item in bounds)


def _simulation_bounds(
    *,
    installation_date: date,
    station_timezone: ZoneInfo,
    grid_bounds: tuple[datetime, datetime],
    solar_bounds: tuple[datetime, datetime],
) -> tuple[datetime, datetime]:
    install_start_local = datetime.combine(
        installation_date,
        time.min,
        tzinfo=station_timezone,
    )
    start_utc = max(
        install_start_local.astimezone(timezone.utc),
        grid_bounds[0],
        solar_bounds[0],
    ).replace(second=0, microsecond=0)
    latest_common_utc = min(grid_bounds[1], solar_bounds[1]).replace(
        second=0,
        microsecond=0,
    )
    end_utc_exclusive = latest_common_utc + timedelta(minutes=1)
    if end_utc_exclusive <= start_utc:
        raise RuntimeError("No overlapping grid/solar range available for EMS probe")
    return start_utc, end_utc_exclusive


def _grid_available_for_minute(
    timestamp_utc: datetime,
    rows: list[GridRow],
    timestamps: list[datetime],
) -> tuple[bool, bool]:
    row = _row_at_or_before(timestamp_utc, rows, timestamps)
    if row is None:
        return True, True
    return row.local_grid_available and not row.is_outage_now, False


def _weather_state_for_minute(
    timestamp_utc: datetime,
    rows: list[WeatherRow],
    timestamps: list[datetime],
) -> tuple[str, bool]:
    if not rows:
        return "clear", True
    index = bisect_left(timestamps, timestamp_utc)
    candidates: list[WeatherRow] = []
    if index < len(rows):
        candidates.append(rows[index])
    if index > 0:
        candidates.append(rows[index - 1])
    if not candidates:
        return "clear", True
    closest = min(
        candidates,
        key=lambda row: abs((row.timestamp_utc - timestamp_utc).total_seconds()),
    )
    if abs(closest.timestamp_utc - timestamp_utc) > WEATHER_MAX_DISTANCE:
        return "clear", True
    return closest.weather_state, False


def _solar_power_for_minute(
    timestamp_utc: datetime,
    rows: list[SolarRow],
    timestamps: list[datetime],
) -> tuple[float, bool]:
    if not rows:
        return 0.0, True
    index = bisect_left(timestamps, timestamp_utc)
    if index < len(rows) and rows[index].timestamp_utc == timestamp_utc:
        return rows[index].power_w, False
    lower = rows[index - 1] if index > 0 else None
    upper = rows[index] if index < len(rows) else None
    if lower is not None and upper is not None:
        gap = upper.timestamp_utc - lower.timestamp_utc
        if timedelta(0) < gap <= SOLAR_MAX_INTERPOLATION_GAP:
            ratio = (timestamp_utc - lower.timestamp_utc).total_seconds() / gap.total_seconds()
            power_w = lower.power_w + (upper.power_w - lower.power_w) * ratio
            return max(0.0, power_w), False
    if lower is not None and timestamp_utc - lower.timestamp_utc <= SOLAR_MAX_INTERPOLATION_GAP:
        return lower.power_w, False
    return 0.0, True


def _row_at_or_before(
    timestamp_utc: datetime,
    rows: list[GridRow],
    timestamps: list[datetime],
) -> GridRow | None:
    if not rows:
        return None
    index = bisect_right(timestamps, timestamp_utc) - 1
    if index < 0:
        return None
    return rows[index]


def _grid_behavior_for_minute(
    *,
    timestamp_utc: datetime,
    grid_available: bool,
    recovery_minutes: int,
    last_outage_end_utc: datetime | None,
) -> str:
    if not grid_available:
        return "outage_active"
    if last_outage_end_utc is None:
        return "grid_normal"
    if timedelta(0) <= timestamp_utc - last_outage_end_utc <= timedelta(
        minutes=recovery_minutes,
    ):
        return "post_outage_recovery"
    return "grid_normal"


def _write_short_csv(path: Path, records: list[MinuteRecord]) -> None:
    fieldnames = [
        "timestamp_local",
        "grid_available",
        "grid_behavior",
        "solar_available_power_w",
        "weather_state",
        "requested_load_power_w",
        "effective_served_load_w",
        "ems_selected_mode",
        "ems_reason",
        "auto_risk_score",
        "cheap_tariff_active",
        "protection_active",
        "inverter_output_enabled",
        "solar_to_load_w",
        "solar_to_battery_w",
        "grid_to_load_w",
        "grid_to_battery_w",
        "battery_to_load_w",
        "requested_charge_power_w",
        "requested_battery_discharge_energy_wh_last_minute",
        "battery_soc_percent",
        "battery_energy_wh",
        "battery_voltage_v",
        "battery_soh_percent",
        "battery_status",
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
            writer.writerow(_record_to_csv_row(record))


def _record_to_csv_row(record: MinuteRecord) -> dict[str, object]:
    return {
        "timestamp_local": record.timestamp_local.isoformat(),
        "grid_available": int(record.grid_available),
        "grid_behavior": record.grid_behavior,
        "solar_available_power_w": f"{record.solar_available_power_w:.6f}",
        "weather_state": record.weather_state,
        "requested_load_power_w": f"{record.requested_load_power_w:.6f}",
        "effective_served_load_w": f"{record.effective_served_load_w:.6f}",
        "ems_selected_mode": record.ems_selected_mode,
        "ems_reason": record.ems_reason,
        "auto_risk_score": record.auto_risk_score,
        "cheap_tariff_active": int(record.cheap_tariff_active),
        "protection_active": int(record.protection_active),
        "inverter_output_enabled": int(record.inverter_output_enabled),
        "solar_to_load_w": f"{record.solar_to_load_w:.6f}",
        "solar_to_battery_w": f"{record.solar_to_battery_w:.6f}",
        "grid_to_load_w": f"{record.grid_to_load_w:.6f}",
        "grid_to_battery_w": f"{record.grid_to_battery_w:.6f}",
        "battery_to_load_w": f"{record.battery_to_load_w:.6f}",
        "requested_charge_power_w": f"{record.requested_charge_power_w:.6f}",
        "requested_battery_discharge_energy_wh_last_minute": (
            f"{record.requested_battery_discharge_energy_wh_last_minute:.8f}"
        ),
        "battery_soc_percent": f"{record.battery_soc_percent:.6f}",
        "battery_energy_wh": f"{record.battery_energy_wh:.6f}",
        "battery_voltage_v": f"{record.battery_voltage_v:.6f}",
        "battery_soh_percent": f"{record.battery_soh_percent:.6f}",
        "battery_status": record.battery_status,
        "applied_charge_power_w": f"{record.applied_charge_power_w:.6f}",
        "stored_charge_energy_wh": f"{record.stored_charge_energy_wh:.8f}",
        "applied_discharge_energy_wh": f"{record.applied_discharge_energy_wh:.8f}",
        "removed_discharge_energy_wh": f"{record.removed_discharge_energy_wh:.8f}",
        "active_professor_count": record.active_professor_count,
        "active_student_count": record.active_student_count,
        "active_event_tags": record.active_event_tags,
    }


def _write_daily_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "date",
        "daily_requested_load_kwh",
        "daily_served_load_kwh",
        "daily_solar_available_kwh",
        "daily_solar_to_load_kwh",
        "daily_solar_to_battery_kwh",
        "daily_grid_to_load_kwh",
        "daily_grid_to_battery_kwh",
        "daily_battery_to_load_kwh",
        "daily_charge_wh",
        "daily_discharge_wh",
        "daily_min_soc_percent",
        "daily_avg_soc_percent",
        "daily_final_soc_percent",
        "daily_min_voltage_v",
        "daily_equivalent_cycles",
        "cumulative_equivalent_cycles",
        "soh_percent",
        "current_usable_capacity_wh",
        "outage_minutes",
        "protection_minutes",
        "average_auto_risk_score",
        "dominant_ems_mode",
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
    if not records:
        return
    timestamps = [record.timestamp_local for record in records]
    charge_power = [record.applied_charge_power_w for record in records]
    discharge_power = [-record.applied_discharge_power_w for record in records]
    mode_names = sorted({record.ems_selected_mode for record in records})
    mode_codes = {name: index for index, name in enumerate(mode_names)}

    figure, axes = plt.subplots(7, 1, figsize=(16, 16), sharex=True)
    figure.suptitle(f"EMS probe {probe_range.slug}: {probe_range.purpose}")

    axes[0].plot(
        timestamps,
        [record.requested_load_power_w for record in records],
        label="requested",
    )
    axes[0].plot(
        timestamps,
        [record.effective_served_load_w for record in records],
        label="served",
    )
    axes[0].set_ylabel("Load W")
    axes[0].legend(loc="upper right")

    axes[1].step(
        timestamps,
        [int(record.grid_available) for record in records],
        where="post",
    )
    axes[1].set_ylabel("Grid")
    axes[1].set_ylim(-0.1, 1.1)

    axes[2].plot(timestamps, [record.solar_available_power_w for record in records])
    axes[2].set_ylabel("Solar W")

    axes[3].plot(timestamps, [record.battery_soc_percent for record in records])
    axes[3].set_ylabel("SoC %")
    axes[3].set_ylim(-2, 102)

    axes[4].plot(timestamps, charge_power, label="charge")
    axes[4].plot(timestamps, discharge_power, label="discharge")
    axes[4].set_ylabel("Battery W")
    axes[4].legend(loc="upper right")

    mode_values = [mode_codes[record.ems_selected_mode] for record in records]
    axes[5].step(timestamps, mode_values, where="post", label="mode")
    axes[5].set_yticks(list(mode_codes.values()))
    axes[5].set_yticklabels(list(mode_codes.keys()), fontsize=8)
    axes[5].set_ylabel("EMS mode")
    risk_axis = axes[5].twinx()
    risk_axis.plot(
        timestamps,
        [record.auto_risk_score for record in records],
        color="tab:orange",
        label="risk",
    )
    risk_axis.step(
        timestamps,
        [100 if record.protection_active else 0 for record in records],
        where="post",
        color="tab:red",
        alpha=0.45,
        label="protection",
    )
    risk_axis.set_ylabel("Risk/protection")
    risk_axis.set_ylim(-5, 105)

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
    if not rows:
        return
    dates = [date.fromisoformat(row["date"]) for row in rows]
    figure, axes = plt.subplots(7, 1, figsize=(16, 17), sharex=True)
    figure.suptitle("EMS probe daily aggregation")

    axes[0].plot(
        dates,
        [float(row["daily_requested_load_kwh"]) for row in rows],
        label="requested",
    )
    axes[0].plot(
        dates,
        [float(row["daily_served_load_kwh"]) for row in rows],
        label="served",
    )
    axes[0].set_ylabel("Load kWh")
    axes[0].legend(loc="upper right")

    for column, label in (
        ("daily_solar_to_load_kwh", "solar-load"),
        ("daily_solar_to_battery_kwh", "solar-battery"),
        ("daily_grid_to_load_kwh", "grid-load"),
        ("daily_grid_to_battery_kwh", "grid-battery"),
        ("daily_battery_to_load_kwh", "battery-load"),
    ):
        axes[1].plot(dates, [float(row[column]) for row in rows], label=label)
    axes[1].set_ylabel("Routing kWh")
    axes[1].legend(loc="upper right", fontsize=8)

    axes[2].plot(
        dates,
        [float(row["daily_min_soc_percent"]) for row in rows],
        label="min",
    )
    axes[2].plot(
        dates,
        [float(row["daily_final_soc_percent"]) for row in rows],
        label="final",
    )
    axes[2].set_ylabel("SoC %")
    axes[2].set_ylim(-2, 102)
    axes[2].legend(loc="upper right")

    axes[3].plot(
        dates,
        [int(row["outage_minutes"]) for row in rows],
        label="outage",
    )
    axes[3].plot(
        dates,
        [int(row["protection_minutes"]) for row in rows],
        label="protection",
    )
    axes[3].set_ylabel("Minutes")
    axes[3].legend(loc="upper right")

    axes[4].plot(dates, [float(row["average_auto_risk_score"]) for row in rows])
    axes[4].set_ylabel("Avg risk")
    axes[4].set_ylim(-5, 105)

    axes[5].plot(dates, [float(row["cumulative_equivalent_cycles"]) for row in rows])
    axes[5].set_ylabel("Total cycles")

    soh_axis = axes[6]
    capacity_axis = soh_axis.twinx()
    soh_axis.plot(
        dates,
        [float(row["soh_percent"]) for row in rows],
        color="tab:blue",
        label="SoH",
    )
    capacity_axis.plot(
        dates,
        [float(row["current_usable_capacity_wh"]) for row in rows],
        color="tab:green",
        label="usable Wh",
    )
    soh_axis.set_ylabel("SoH %")
    capacity_axis.set_ylabel("Usable Wh")
    soh_axis.set_xlabel("Date")

    for axis in axes:
        axis.grid(True, linewidth=0.4, alpha=0.5)
    figure.autofmt_xdate()
    figure.tight_layout(rect=(0, 0, 1, 0.97))
    figure.savefig(path, dpi=100)
    plt.close(figure)


def _build_short_summary(records: list[MinuteRecord]) -> dict[str, object]:
    if not records:
        return {}
    requested_loads = [record.requested_load_power_w for record in records]
    soc_values = [record.battery_soc_percent for record in records]
    soh_values = [record.battery_soh_percent for record in records]
    voltage_values = [record.battery_voltage_v for record in records]
    risk_scores = [record.auto_risk_score for record in records]
    mode_counts = Counter(record.ems_selected_mode for record in records)
    protection_records = [record for record in records if record.protection_active]
    top_protection = sorted(
        protection_records,
        key=lambda record: (
            record.requested_load_power_w,
            record.auto_risk_score,
        ),
        reverse=True,
    )[:10]
    return {
        "minutes_simulated": len(records),
        "outage_minutes": sum(1 for record in records if not record.grid_available),
        "protection_minutes": len(protection_records),
        "min_requested_load_power_w": min(requested_loads),
        "avg_requested_load_power_w": mean(requested_loads),
        "max_requested_load_power_w": max(requested_loads),
        "total_requested_load_kwh": _sum_power_kwh(
            record.requested_load_power_w for record in records
        ),
        "total_served_load_kwh": _sum_power_kwh(
            record.effective_served_load_w for record in records
        ),
        "total_solar_available_kwh": _sum_power_kwh(
            record.solar_available_power_w for record in records
        ),
        "total_solar_to_load_kwh": _sum_power_kwh(
            record.solar_to_load_w for record in records
        ),
        "total_solar_to_battery_kwh": _sum_power_kwh(
            record.solar_to_battery_w for record in records
        ),
        "total_grid_to_load_kwh": _sum_power_kwh(
            record.grid_to_load_w for record in records
        ),
        "total_grid_to_battery_kwh": _sum_power_kwh(
            record.grid_to_battery_w for record in records
        ),
        "total_battery_to_load_kwh": _sum_power_kwh(
            record.battery_to_load_w for record in records
        ),
        "min_battery_soc_percent": min(soc_values),
        "final_battery_soc_percent": soc_values[-1],
        "min_battery_soh_percent": min(soh_values),
        "final_battery_soh_percent": soh_values[-1],
        "min_voltage_v": min(voltage_values),
        "max_voltage_v": max(voltage_values),
        "average_auto_risk_score": mean(risk_scores),
        "maximum_auto_risk_score": max(risk_scores),
        "ems_mode_counts": dict(mode_counts),
        "cheap_tariff_minutes": sum(
            1 for record in records if record.cheap_tariff_active
        ),
        "top_protection_moments": top_protection,
    }


def _long_summary(rows: list[dict[str, str]]) -> dict[str, object]:
    if not rows:
        return {}
    risk_by_day = sorted(
        (
            (row["date"], float(row["average_auto_risk_score"]))
            for row in rows
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    modes = Counter(row["dominant_ems_mode"] for row in rows)
    return {
        "date_range": f"{rows[0]['date']} to {rows[-1]['date']}",
        "days_simulated": len(rows),
        "total_requested_load_kwh": sum(
            float(row["daily_requested_load_kwh"]) for row in rows
        ),
        "total_served_load_kwh": sum(
            float(row["daily_served_load_kwh"]) for row in rows
        ),
        "total_solar_available_kwh": sum(
            float(row["daily_solar_available_kwh"]) for row in rows
        ),
        "total_solar_to_load_kwh": sum(
            float(row["daily_solar_to_load_kwh"]) for row in rows
        ),
        "total_solar_to_battery_kwh": sum(
            float(row["daily_solar_to_battery_kwh"]) for row in rows
        ),
        "total_grid_to_load_kwh": sum(
            float(row["daily_grid_to_load_kwh"]) for row in rows
        ),
        "total_grid_to_battery_kwh": sum(
            float(row["daily_grid_to_battery_kwh"]) for row in rows
        ),
        "total_battery_to_load_kwh": sum(
            float(row["daily_battery_to_load_kwh"]) for row in rows
        ),
        "total_protection_minutes": sum(int(row["protection_minutes"]) for row in rows),
        "days_with_protection_events": sum(
            1 for row in rows if int(row["protection_minutes"]) > 0
        ),
        "days_with_outages": sum(1 for row in rows if int(row["outage_minutes"]) > 0),
        "min_soc_over_range": min(float(row["daily_min_soc_percent"]) for row in rows),
        "final_soc_percent": float(rows[-1]["daily_final_soc_percent"]),
        "initial_soh_percent": 100.0,
        "final_soh_percent": float(rows[-1]["soh_percent"]),
        "total_equivalent_cycles": float(rows[-1]["cumulative_equivalent_cycles"]),
        "final_current_usable_capacity_wh": float(rows[-1]["current_usable_capacity_wh"]),
        "highest_risk_days": risk_by_day[:10],
        "dominant_ems_modes": dict(modes),
    }


def _format_summary(
    *,
    config: object,
    load_settings: LoadSimulationSettings,
    ems_config: EmsConfig,
    output: SimulationOutput,
) -> str:
    long_summary = _long_summary(output.daily_rows)
    lines = [
        "EMS integrated diagnostic probe",
        "",
        "Config:",
        f"- YAML: {CONFIG_PATH.relative_to(PROJECT_ROOT)}",
        f"- station_id: {config.station.id}",
        f"- load seed: {load_settings.seed}",
        f"- timezone: {load_settings.timezone_name}",
        f"- EMS mode: {ems_config.mode.value}",
        f"- inverter_output_limit_w: {ems_config.inverter_output_limit_w}",
        f"- cheap tariff: {ems_config.cheap_tariff_start}-{ems_config.cheap_tariff_end}",
        "",
        "Simulation range:",
        f"- start UTC: {output.start_utc.isoformat()}",
        f"- end UTC exclusive: {output.end_utc_exclusive.isoformat()}",
        f"- total minutes: {output.total_minutes}",
        f"- runtime seconds: {output.runtime_seconds:.2f}",
        "",
        "Data fallbacks:",
        f"- grid fallback minutes: {output.fallback_counts.grid_fallback_minutes}",
        f"- weather fallback minutes: {output.fallback_counts.weather_fallback_minutes}",
        f"- solar fallback minutes: {output.fallback_counts.solar_fallback_minutes}",
        "",
        "Generated files:",
    ]
    for short_output in output.short_outputs:
        lines.append(f"- {short_output.csv_path.relative_to(PROJECT_ROOT)}")
        lines.append(f"- {short_output.png_path.relative_to(PROJECT_ROOT)}")
    lines.append(f"- {output.daily_csv_path.relative_to(PROJECT_ROOT)}")
    lines.append(f"- {output.daily_png_path.relative_to(PROJECT_ROOT)}")
    lines.append(f"- {OUTPUT_DIR.relative_to(PROJECT_ROOT) / 'summary.txt'}")
    lines.append("")
    lines.append("Short ranges:")
    for short_output in output.short_outputs:
        lines.extend(_format_short_output(short_output))
        lines.append("")
    lines.append("Long range:")
    lines.extend(_format_long_output(long_summary))
    lines.append("")
    lines.append("Interpretation:")
    lines.extend(f"- {item}" for item in _interpret_output(output, long_summary))
    return "\n".join(lines)


def _format_short_output(short_output: ShortRangeOutput) -> list[str]:
    summary = short_output.summary
    if not summary:
        return [
            f"Range: {short_output.probe_range.slug}",
            "No records generated for this range.",
        ]
    lines = [
        f"Range: {short_output.probe_range.slug}",
        f"Purpose: {short_output.probe_range.purpose}",
        f"CSV: {short_output.csv_path.relative_to(PROJECT_ROOT)}",
        f"PNG: {short_output.png_path.relative_to(PROJECT_ROOT)}",
        f"Minutes simulated: {summary['minutes_simulated']}",
        f"Outage minutes: {summary['outage_minutes']}",
        f"Protection/shutdown minutes: {summary['protection_minutes']}",
        (
            "Requested load W min/avg/max: "
            f"{summary['min_requested_load_power_w']:.2f} / "
            f"{summary['avg_requested_load_power_w']:.2f} / "
            f"{summary['max_requested_load_power_w']:.2f}"
        ),
        f"Total requested load: {summary['total_requested_load_kwh']:.3f} kWh",
        f"Total served load: {summary['total_served_load_kwh']:.3f} kWh",
        f"Total solar available: {summary['total_solar_available_kwh']:.3f} kWh",
        f"Total solar_to_load: {summary['total_solar_to_load_kwh']:.3f} kWh",
        f"Total solar_to_battery: {summary['total_solar_to_battery_kwh']:.3f} kWh",
        f"Total grid_to_load: {summary['total_grid_to_load_kwh']:.3f} kWh",
        f"Total grid_to_battery: {summary['total_grid_to_battery_kwh']:.3f} kWh",
        f"Total battery_to_load: {summary['total_battery_to_load_kwh']:.3f} kWh",
        (
            "Battery SoC min/final: "
            f"{summary['min_battery_soc_percent']:.2f}% / "
            f"{summary['final_battery_soc_percent']:.2f}%"
        ),
        (
            "Battery SoH min/final: "
            f"{summary['min_battery_soh_percent']:.6f}% / "
            f"{summary['final_battery_soh_percent']:.6f}%"
        ),
        f"Battery voltage V min/max: {summary['min_voltage_v']:.3f} / {summary['max_voltage_v']:.3f}",
        (
            "Auto risk score avg/max: "
            f"{summary['average_auto_risk_score']:.2f} / "
            f"{summary['maximum_auto_risk_score']}"
        ),
        f"EMS mode counts: {_format_counter(summary['ems_mode_counts'])}",
        f"Cheap tariff minutes: {summary['cheap_tariff_minutes']}",
        "Top protection/shutdown moments:",
    ]
    top_protection = summary["top_protection_moments"]
    if not top_protection:
        lines.append("  none")
    else:
        for record in top_protection:
            lines.append(
                "  "
                f"{record.timestamp_local.isoformat()} | "
                f"load={record.requested_load_power_w:.2f} W | "
                f"solar={record.solar_available_power_w:.2f} W | "
                f"soc={record.battery_soc_percent:.2f}% | "
                f"mode={record.ems_selected_mode} | "
                f"reason={record.ems_reason}"
            )
    return lines


def _format_long_output(summary: dict[str, object]) -> list[str]:
    if not summary:
        return ["No daily rows generated."]
    return [
        f"Date range: {summary['date_range']}",
        f"Days simulated: {summary['days_simulated']}",
        f"Total requested load: {summary['total_requested_load_kwh']:.3f} kWh",
        f"Total served load: {summary['total_served_load_kwh']:.3f} kWh",
        f"Total solar available: {summary['total_solar_available_kwh']:.3f} kWh",
        f"Total solar_to_load: {summary['total_solar_to_load_kwh']:.3f} kWh",
        f"Total solar_to_battery: {summary['total_solar_to_battery_kwh']:.3f} kWh",
        f"Total grid_to_load: {summary['total_grid_to_load_kwh']:.3f} kWh",
        f"Total grid_to_battery: {summary['total_grid_to_battery_kwh']:.3f} kWh",
        f"Total battery_to_load: {summary['total_battery_to_load_kwh']:.3f} kWh",
        f"Total protection minutes: {summary['total_protection_minutes']}",
        f"Days with protection events: {summary['days_with_protection_events']}",
        f"Days with outages: {summary['days_with_outages']}",
        f"Min SoC over range: {summary['min_soc_over_range']:.2f}%",
        f"Final SoC: {summary['final_soc_percent']:.2f}%",
        (
            "Initial/final SoH: "
            f"{summary['initial_soh_percent']:.6f}% / "
            f"{summary['final_soh_percent']:.6f}%"
        ),
        f"Total equivalent cycles: {summary['total_equivalent_cycles']:.6f}",
        f"Final current usable capacity Wh: {summary['final_current_usable_capacity_wh']:.2f}",
        f"Highest risk days: {_format_risk_days(summary['highest_risk_days'])}",
        f"Dominant EMS modes: {_format_counter(summary['dominant_ems_modes'])}",
    ]


def _interpret_output(
    output: SimulationOutput,
    long_summary: dict[str, object],
) -> list[str]:
    interpretations = [
        "The probe uses one continuous battery and EMS-history stream, then slices the requested chart windows.",
        "Grid behavior is derived from current/past grid rows only; no next outage windows or future schedule fields are read.",
        "Weather is passed only to LoadContext.weather_state; solar power is passed only to EMS solar_available_power_w.",
        "Generated solar is linearly interpolated between existing simulated/forecast solar rows; zero fallback is used only outside a safe generated-data gap.",
        "EMS requested discharge energy is passed to BatteryStepInput.consumed_energy_wh_last_minute, and requested_charge_power_w is passed through directly.",
        "The CSV schema intentionally contains no unmet_load_w or unused_solar_w fields.",
    ]
    if long_summary:
        if long_summary["total_protection_minutes"] > 0:
            interpretations.append(
                "Protection/shutdown minutes are present; inspect the top moments and charts for inverter-limit or battery-protection behavior."
            )
        else:
            interpretations.append(
                "No protection/shutdown minutes appeared in the simulated range."
            )
        if long_summary["days_with_outages"] > 0:
            interpretations.append(
                "Outage days are present and should show elevated risk scores after recent instability."
            )
        if long_summary["total_grid_to_battery_kwh"] > 0:
            interpretations.append(
                "Grid-to-battery charging is visible, including cheap-tariff and risk-driven charge requests."
            )
        if long_summary["final_soh_percent"] < long_summary["initial_soh_percent"]:
            interpretations.append(
                "Battery SoH declines slightly from accumulated equivalent cycles."
            )
    if any(
        int(row["protection_minutes"]) > 0 and float(row["daily_served_load_kwh"]) == 0.0
        for row in output.daily_rows
    ):
        interpretations.append(
            "Some days with protection have fully blocked served load; verify whether this is expected for the outage/load mix."
        )
    return interpretations


def _sum_power_kwh(values: object) -> float:
    return sum(values) / 60.0 / 1000.0


def _trim_datetime_queue(values: deque[datetime], cutoff: datetime) -> None:
    while values and values[0] < cutoff:
        values.popleft()


def _dominant_mode(counter: Counter[str]) -> str:
    if not counter:
        return "none"
    return counter.most_common(1)[0][0]


def _format_counter(value: object) -> str:
    if isinstance(value, Counter):
        items = value.items()
    elif isinstance(value, dict):
        items = value.items()
    else:
        return str(value)
    return ", ".join(f"{key}={count}" for key, count in sorted(items)) or "none"


def _format_risk_days(value: object) -> str:
    days = value if isinstance(value, list) else []
    return ", ".join(f"{day}:{risk:.1f}" for day, risk in days[:5]) or "none"


def _local_range(
    start_date: date,
    end_date_inclusive: date,
    timezone_info: ZoneInfo,
) -> tuple[datetime, datetime]:
    return (
        datetime.combine(start_date, time.min, tzinfo=timezone_info),
        datetime.combine(
            end_date_inclusive + timedelta(days=1),
            time.min,
            tzinfo=timezone_info,
        ),
    )


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(second=0, microsecond=0)


def _map_weather_code_to_state(value: object) -> str:
    code = int(value)
    if code == 0:
        return "clear"
    if code in {1, 2}:
        return "partly_cloudy"
    if code == 3:
        return "cloudy"
    if code in {45, 48}:
        return "fog"
    if code in {51, 53, 55, 56, 57}:
        return "drizzle"
    if code in {61, 63, 65, 66, 67, 80, 81, 82}:
        return "rain"
    if code in {71, 73, 75, 77, 85, 86}:
        return "snow"
    if code in {95, 96, 99}:
        return "thunderstorm"
    return "unknown"


if __name__ == "__main__":
    main()
