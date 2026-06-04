from __future__ import annotations

from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session

from app.config_loader import calculate_config_hash, calculate_system_config_hash
from app.schemas import AppConfig
from app.simulation.battery import (
    BatteryConfig as SimulationBatteryConfig,
    BatterySimulator,
    BatteryState,
    BatteryStatus,
    BatteryStepInput,
)
from app.simulation.ems import EmsDecision, EmsDecisionEngine, EmsHistorySummary, EmsInput
from app.simulation.load import (
    LoadContext,
    LoadSimulator,
    effective_grid_behavior,
    load_settings_from_station_config,
)
from app.simulation.solar_interpolation import interpolate_power
from app.storage.battery_repository import (
    BatteryCachePoint,
    BatteryHistoryPoint,
    delete_battery_cache_points,
    delete_battery_history_points,
    get_latest_battery_cache_point,
    get_latest_battery_history_point,
    save_battery_cache_points,
    save_battery_history_points,
)
from app.storage.ems_repository import (
    EmsCachePoint,
    EmsHistoryPoint,
    delete_ems_cache_points,
    delete_ems_history_points,
    frontend_mode_id,
    save_ems_cache_points,
    save_ems_history_points,
)
from app.storage.forecast_solar_repository import list_forecast_solar_for_config
from app.storage.grid_repository import GridAvailabilityPointRecord, list_grid_availability_points
from app.storage.load_repository import (
    LoadCachePoint,
    LoadHistoryPoint,
    delete_load_cache_points,
    delete_load_history_points,
    encode_event_tags,
    save_load_cache_points,
    save_load_history_points,
)
from app.storage.simulated_solar_repository import list_simulated_solar_for_config


HISTORY_MINUTES = {0, 15, 30, 45}
SECONDS_PER_MINUTE = 60.0


@dataclass(frozen=True, slots=True)
class SystemSimulationWindows:
    start_utc: datetime
    end_utc: datetime
    history_end_utc: datetime | None
    load_cache_start_utc: datetime
    load_cache_end_utc: datetime
    battery_cache_start_utc: datetime
    battery_cache_end_utc: datetime
    ems_cache_start_utc: datetime
    ems_cache_end_utc: datetime


@dataclass(frozen=True, slots=True)
class SystemSimulationResult:
    station_id: str
    config_hash: str
    start_utc: datetime
    end_utc: datetime
    load_history: list[LoadHistoryPoint]
    load_cache: list[LoadCachePoint]
    battery_history: list[BatteryHistoryPoint]
    battery_cache: list[BatteryCachePoint]
    ems_history: list[EmsHistoryPoint]
    ems_cache: list[EmsCachePoint]
    fallbacks: SystemSimulationFallbackSummary
    seed: SystemSimulationSeedSummary


@dataclass(frozen=True, slots=True)
class SystemSimulationPersistSummary:
    load_history_rows: int
    load_cache_rows: int
    battery_history_rows: int
    battery_cache_rows: int
    ems_history_rows: int
    ems_cache_rows: int


@dataclass(frozen=True, slots=True)
class SystemSimulationFallbackSummary:
    solar_fallback_minutes: int = 0
    grid_fallback_minutes: int = 0
    weather_fallback_minutes: int = 0

    @property
    def has_fallbacks(self) -> bool:
        return (
            self.solar_fallback_minutes > 0
            or self.grid_fallback_minutes > 0
            or self.weather_fallback_minutes > 0
        )


@dataclass(frozen=True, slots=True)
class SystemSimulationSeedSummary:
    battery_seed_source: str
    battery_seed_timestamp_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class BatterySeed:
    state: BatteryState | None
    source: str
    timestamp_utc: datetime | None = None


class SimulationFallbackError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SolarSample:
    power_w: float
    weather_state: str
    solar_fallback_used: bool = False
    weather_fallback_used: bool = False


@dataclass(frozen=True, slots=True)
class SolarSourcePoint:
    timestamp_utc: datetime
    power_w: float
    weather_state: str | None


@dataclass(frozen=True, slots=True)
class GridSample:
    available: bool
    is_outage_now: bool
    outage_level: str
    next_outage_window_start_utc: datetime | None
    fallback_used: bool = False


@dataclass(slots=True)
class RollingEmsHistory:
    outage_minutes: deque[datetime]
    outage_starts: deque[datetime]
    soc_points: deque[tuple[datetime, float]]
    last_grid_available: bool | None = None
    last_outage_time: datetime | None = None
    recovered_to_full_after_last_outage: bool = True

    def append(
        self,
        timestamp_utc: datetime,
        *,
        grid_available: bool,
        soc_percent: float,
        backup_target_soc_percent: float,
    ) -> None:
        cutoff_72h = timestamp_utc - timedelta(hours=72)
        cutoff_24h = timestamp_utc - timedelta(hours=24)
        while self.outage_minutes and self.outage_minutes[0] < cutoff_24h:
            self.outage_minutes.popleft()
        while self.outage_starts and self.outage_starts[0] < cutoff_72h:
            self.outage_starts.popleft()
        while self.soc_points and self.soc_points[0][0] < cutoff_24h:
            self.soc_points.popleft()

        if not grid_available:
            self.outage_minutes.append(timestamp_utc)
            self.last_outage_time = timestamp_utc
            self.recovered_to_full_after_last_outage = False
            if self.last_grid_available is not False:
                self.outage_starts.append(timestamp_utc)
        elif soc_percent >= backup_target_soc_percent - 0.1:
            self.recovered_to_full_after_last_outage = True

        self.soc_points.append((timestamp_utc, soc_percent))
        self.last_grid_available = grid_available

    def summary(self, timestamp_utc: datetime) -> EmsHistorySummary:
        cutoff_6h = timestamp_utc - timedelta(hours=6)
        cutoff_24h = timestamp_utc - timedelta(hours=24)
        outage_minutes_6h = sum(1 for value in self.outage_minutes if value >= cutoff_6h)
        outage_minutes_24h = sum(1 for value in self.outage_minutes if value >= cutoff_24h)
        outage_count_24h = sum(1 for value in self.outage_starts if value >= cutoff_24h)
        outage_count_72h = len(self.outage_starts)
        min_soc = min((soc for _, soc in self.soc_points), default=None)
        hours_since_last_outage = (
            None
            if self.last_outage_time is None
            else max(0.0, (timestamp_utc - self.last_outage_time).total_seconds() / 3600.0)
        )
        return EmsHistorySummary(
            outage_minutes_last_6h=float(outage_minutes_6h),
            outage_minutes_last_24h=float(outage_minutes_24h),
            outage_count_last_24h=outage_count_24h,
            outage_count_last_72h=outage_count_72h,
            hours_since_last_outage=hours_since_last_outage,
            min_soc_last_24h=min_soc,
            battery_recovered_to_full_after_last_outage=(
                self.recovered_to_full_after_last_outage
            ),
        )


def build_default_system_simulation_windows(
    now: datetime,
    station_timezone: ZoneInfo,
    *,
    load_days_ahead: int = 2,
    battery_days_ahead: int = 2,
    ems_days_ahead: int = 2,
    history_enabled: bool = False,
) -> SystemSimulationWindows:
    now_utc = _as_utc(now)
    local_now = now_utc.astimezone(station_timezone)
    local_midnight = datetime.combine(local_now.date(), time.min, tzinfo=station_timezone)
    start_utc = local_midnight.astimezone(timezone.utc)
    load_cache_end = now_utc + timedelta(days=load_days_ahead)
    battery_cache_end = now_utc + timedelta(days=battery_days_ahead)
    ems_cache_end = now_utc + timedelta(days=ems_days_ahead)
    end_utc = max(load_cache_end, battery_cache_end, ems_cache_end)
    return SystemSimulationWindows(
        start_utc=start_utc,
        end_utc=_ceil_to_minute(end_utc),
        history_end_utc=now_utc if history_enabled else None,
        load_cache_start_utc=start_utc,
        load_cache_end_utc=_ceil_to_minute(load_cache_end),
        battery_cache_start_utc=start_utc,
        battery_cache_end_utc=_ceil_to_minute(battery_cache_end),
        ems_cache_start_utc=start_utc,
        ems_cache_end_utc=_ceil_to_minute(ems_cache_end),
    )


def simulate_integrated_system_window(
    session: Session,
    config: AppConfig,
    windows: SystemSimulationWindows,
    *,
    allow_fallbacks: bool = False,
) -> SystemSimulationResult:
    start_utc = _floor_to_minute(windows.start_utc)
    end_utc = _floor_to_minute(windows.end_utc)
    if end_utc <= start_utc:
        raise ValueError("system simulation end must be later than start")

    station_id = config.station.id
    system_config_hash = calculate_system_config_hash(config)
    solar_config_hash = calculate_config_hash(config)
    station_timezone = ZoneInfo(config.station.solar.installation.timezone)
    load_settings = load_settings_from_station_config(config)
    load_simulator = LoadSimulator(load_settings)
    battery_seed = _load_battery_seed(
        session,
        config,
        station_id,
        system_config_hash,
        start_utc,
    )
    battery_simulator = _battery_simulator_from_seed(config, battery_seed)
    ems_engine = EmsDecisionEngine(config.station.ems)
    rolling_history = RollingEmsHistory(deque(), deque(), deque())
    solar_sources = _load_solar_source_points(
        session,
        station_id,
        solar_config_hash,
        start_utc - timedelta(minutes=15),
        end_utc + timedelta(minutes=15),
    )
    grid_sources = list_grid_availability_points(
        session,
        start_utc=start_utc - timedelta(minutes=30),
        end_utc=end_utc + timedelta(minutes=30),
    )
    grid_timestamps = [point.timestamp_utc for point in grid_sources]

    load_history: list[LoadHistoryPoint] = []
    load_cache: list[LoadCachePoint] = []
    battery_history: list[BatteryHistoryPoint] = []
    battery_cache: list[BatteryCachePoint] = []
    ems_history: list[EmsHistoryPoint] = []
    ems_cache: list[EmsCachePoint] = []
    previous_grid_available: bool | None = None
    daily_totals: dict[date, dict[str, float]] = {}
    solar_fallback_minutes = 0
    grid_fallback_minutes = 0
    weather_fallback_minutes = 0

    current_utc = start_utc
    while current_utc < end_utc:
        timestamp_local = current_utc.astimezone(station_timezone)
        battery_state_before = battery_simulator.state
        solar = _sample_solar(solar_sources, current_utc)
        grid = _sample_grid(grid_sources, grid_timestamps, current_utc)
        if solar.solar_fallback_used:
            solar_fallback_minutes += 1
        if solar.weather_fallback_used:
            weather_fallback_minutes += 1
        if grid.fallback_used:
            grid_fallback_minutes += 1
        if (
            not allow_fallbacks
            and (
                solar.solar_fallback_used
                or solar.weather_fallback_used
                or grid.fallback_used
            )
        ):
            raise SimulationFallbackError(
                "system simulation source fallback used at "
                f"{current_utc.isoformat()}; rerun with allow_fallbacks=True "
                "or --allow-fallbacks after generating solar/grid source data"
            )
        load_context = LoadContext(
            grid_behavior=_load_grid_behavior(
                grid,
                current_utc,
                previous_grid_available,
            ),
            grid_available=grid.available,
            soc_percent=battery_state_before.soc_percent,
            weather_state=solar.weather_state,
        )
        load_point = load_simulator.build_point(current_utc, load_context)
        history_summary = rolling_history.summary(current_utc)
        ems_input = EmsInput(
            timestamp=timestamp_local,
            grid_available=grid.available,
            solar_available_power_w=solar.power_w,
            load_power_w=load_point.total_power_draw_w,
            battery_soc_percent=battery_state_before.soc_percent,
            battery_soh_percent=battery_state_before.soh_percent,
            battery_energy_wh=battery_state_before.energy_wh,
            battery_current_usable_capacity_wh=(
                battery_state_before.current_usable_capacity_wh
            ),
            battery_voltage_v=battery_state_before.voltage_v,
            battery_status=battery_state_before.status.value,
            battery_max_charge_power_w=battery_simulator.max_charge_power_w,
            grid_was_available_previous_minute=previous_grid_available,
            history_summary=history_summary,
            config=config.station.ems,
        )
        decision = ems_engine.decide(ems_input)
        battery_result = battery_simulator.step(
            BatteryStepInput(
                timestamp=current_utc,
                consumed_energy_wh_last_minute=(
                    decision.requested_battery_discharge_energy_wh_last_minute
                ),
                battery_provides_energy=decision.battery_provides_energy,
                requested_charge_power_w=decision.requested_charge_power_w,
            )
        )
        load_values = _update_daily_load_values(
            daily_totals,
            timestamp_local.date(),
            load_point.total_power_draw_w,
            decision,
            config.station.economics.grid_tariff_uah_per_kwh,
        )
        load_cut_w = max(
            0.0,
            load_point.total_power_draw_w - decision.effective_served_load_w,
        )
        cycle_increment = (
            0.0
            if battery_state_before.current_usable_capacity_wh <= 0.0
            else battery_result.removed_discharge_energy_wh
            / battery_state_before.current_usable_capacity_wh
        )
        target_soc_percent = _target_soc_for_decision(decision, config)
        inverter_state = _inverter_state(decision)

        load_kwargs = {
            "station_id": station_id,
            "config_hash": system_config_hash,
            "timestamp_utc": current_utc,
            "timestamp_local": timestamp_local,
            "total_load_power_w": load_point.total_power_draw_w,
            "effective_served_load_w": decision.effective_served_load_w,
            "load_cut_by_ems_w": round(load_cut_w, 6),
            "daily_energy_wh_so_far": load_values["daily_energy_wh"],
            "solar_covered_percent": load_values["solar_covered_percent"],
            "money_saved_uah": load_values["money_saved_uah"],
            "active_student_count": load_point.active_student_count,
            "active_professor_count": load_point.active_professor_count,
            "active_event_tags_json": encode_event_tags(load_point.active_event_tags),
            "lighting_active": "dark_condition" in load_point.active_event_tags,
            "high_power_active": any(
                tag in load_point.active_event_tags
                for tag in ("kettle", "heat_gun", "hand_drill")
            ),
        }
        battery_kwargs = {
            "station_id": station_id,
            "config_hash": system_config_hash,
            "timestamp_utc": current_utc,
            "timestamp_local": timestamp_local,
            "soc_percent": battery_result.state.soc_percent,
            "soh_percent": battery_result.state.soh_percent,
            "voltage_v": battery_result.state.voltage_v,
            "energy_wh": battery_result.state.energy_wh,
            "usable_capacity_wh": battery_result.state.usable_capacity_wh,
            "current_usable_capacity_wh": (
                battery_result.state.current_usable_capacity_wh
            ),
            "applied_charge_power_w": battery_result.applied_charge_power_w,
            "applied_discharge_power_w": battery_result.applied_discharge_power_w,
            "net_battery_power_w": round(
                battery_result.applied_charge_power_w
                - battery_result.applied_discharge_power_w,
                6,
            ),
            "cycle_fraction_increment": round(cycle_increment, 10),
            "soh_loss_percent": max(
                0.0,
                battery_state_before.soh_percent - battery_result.state.soh_percent,
            ),
            "status": battery_result.status.value,
        }
        ems_kwargs = {
            "station_id": station_id,
            "config_hash": system_config_hash,
            "timestamp_utc": current_utc,
            "timestamp_local": timestamp_local,
            "control_mode": (
                "auto" if str(config.station.ems.mode) == "auto" else "manual"
            ),
            "selected_mode": decision.selected_mode.value,
            "selected_mode_frontend_id": frontend_mode_id(decision.selected_mode.value),
            "auto_risk_score": decision.auto_risk_score,
            "protection_active": decision.protection_active,
            "inverter_output_enabled": decision.inverter_output_enabled,
            "inverter_state": inverter_state,
            "target_soc_percent": target_soc_percent,
            "cutoff_soc_percent": config.station.ems.critical_soc_percent,
            "requested_charge_power_w": decision.requested_charge_power_w,
            "grid_to_load_w": decision.grid_to_load_w,
            "grid_to_battery_w": decision.grid_to_battery_w,
            "solar_to_load_w": decision.solar_to_load_w,
            "solar_to_battery_w": decision.solar_to_battery_w,
            "battery_to_load_w": decision.battery_to_load_w,
            "applied_charge_power_w": battery_result.applied_charge_power_w,
            "effective_load_power_w": decision.effective_served_load_w,
            "curtailed_or_cut_load_w": round(load_cut_w, 6),
        }

        if (
            windows.history_end_utc is not None
            and _is_history_timestamp(current_utc)
            and current_utc <= windows.history_end_utc
        ):
            load_history.append(LoadHistoryPoint(**load_kwargs))
            battery_history.append(BatteryHistoryPoint(**battery_kwargs))
            ems_history.append(EmsHistoryPoint(**ems_kwargs))
        if windows.load_cache_start_utc <= current_utc < windows.load_cache_end_utc:
            load_cache.append(LoadCachePoint(**load_kwargs))
        if windows.battery_cache_start_utc <= current_utc < windows.battery_cache_end_utc:
            battery_cache.append(BatteryCachePoint(**battery_kwargs))
        if windows.ems_cache_start_utc <= current_utc < windows.ems_cache_end_utc:
            ems_cache.append(EmsCachePoint(**ems_kwargs))

        rolling_history.append(
            current_utc,
            grid_available=grid.available,
            soc_percent=battery_result.state.soc_percent,
            backup_target_soc_percent=config.station.ems.backup_target_soc_percent,
        )
        previous_grid_available = grid.available
        current_utc += timedelta(minutes=1)

    return SystemSimulationResult(
        station_id=station_id,
        config_hash=system_config_hash,
        start_utc=start_utc,
        end_utc=end_utc,
        load_history=load_history,
        load_cache=load_cache,
        battery_history=battery_history,
        battery_cache=battery_cache,
        ems_history=ems_history,
        ems_cache=ems_cache,
        fallbacks=SystemSimulationFallbackSummary(
            solar_fallback_minutes=solar_fallback_minutes,
            grid_fallback_minutes=grid_fallback_minutes,
            weather_fallback_minutes=weather_fallback_minutes,
        ),
        seed=SystemSimulationSeedSummary(
            battery_seed_source=battery_seed.source,
            battery_seed_timestamp_utc=battery_seed.timestamp_utc,
        ),
    )


def persist_integrated_system_result(
    session: Session,
    result: SystemSimulationResult,
    *,
    replace_existing: bool = True,
) -> SystemSimulationPersistSummary:
    if replace_existing:
        delete_load_history_points(
            session,
            result.station_id,
            result.config_hash,
            result.start_utc,
            result.end_utc,
        )
        delete_load_cache_points(
            session,
            result.station_id,
            result.config_hash,
            result.start_utc,
            result.end_utc,
        )
        delete_battery_history_points(
            session,
            result.station_id,
            result.config_hash,
            result.start_utc,
            result.end_utc,
        )
        delete_battery_cache_points(
            session,
            result.station_id,
            result.config_hash,
            result.start_utc,
            result.end_utc,
        )
        delete_ems_history_points(
            session,
            result.station_id,
            result.config_hash,
            result.start_utc,
            result.end_utc,
        )
        delete_ems_cache_points(
            session,
            result.station_id,
            result.config_hash,
            result.start_utc,
            result.end_utc,
        )

    return SystemSimulationPersistSummary(
        load_history_rows=save_load_history_points(session, result.load_history),
        load_cache_rows=save_load_cache_points(session, result.load_cache),
        battery_history_rows=save_battery_history_points(session, result.battery_history),
        battery_cache_rows=save_battery_cache_points(session, result.battery_cache),
        ems_history_rows=save_ems_history_points(session, result.ems_history),
        ems_cache_rows=save_ems_cache_points(session, result.ems_cache),
    )


def _load_battery_seed(
    session: Session,
    config: AppConfig,
    station_id: str,
    config_hash: str,
    start_utc: datetime,
) -> BatterySeed:
    seed_cutoff_utc = start_utc - timedelta(seconds=1)
    cache_seed = get_latest_battery_cache_point(
        session,
        station_id,
        config_hash,
        seed_cutoff_utc,
    )
    history_seed = get_latest_battery_history_point(
        session,
        station_id,
        config_hash,
        seed_cutoff_utc,
    )
    candidates = [
        ("cache", cache_seed),
        ("history", history_seed),
    ]
    selected_source, selected_row = max(
        candidates,
        key=lambda item: (
            item[1].timestamp_utc if item[1] is not None else datetime.min.replace(tzinfo=timezone.utc)
        ),
    )
    if selected_row is None:
        return BatterySeed(state=None, source="default", timestamp_utc=None)
    return BatterySeed(
        state=_battery_state_from_persisted_row(config, selected_row),
        source=selected_source,
        timestamp_utc=selected_row.timestamp_utc,
    )


def _battery_simulator_from_seed(
    config: AppConfig,
    seed: BatterySeed,
) -> BatterySimulator:
    return BatterySimulator(_simulation_battery_config(config), initial_state=seed.state)


def _battery_state_from_persisted_row(
    config: AppConfig,
    row: BatteryCachePoint | BatteryHistoryPoint,
) -> BatteryState:
    try:
        status = BatteryStatus(row.status)
    except ValueError:
        status = BatteryStatus.IDLE
    return BatteryState(
        energy_wh=row.energy_wh,
        soc_percent=row.soc_percent,
        soh_percent=row.soh_percent,
        nominal_energy_wh=float(config.station.battery.nominal_voltage_v)
        * float(config.station.battery.capacity_ah),
        usable_capacity_wh=row.usable_capacity_wh,
        current_usable_capacity_wh=row.current_usable_capacity_wh,
        voltage_v=row.voltage_v,
        equivalent_cycles_today=max(0.0, row.cycle_fraction_increment),
        total_equivalent_cycles=0.0,
        status=status,
    )


def _simulation_battery_config(config: AppConfig) -> SimulationBatteryConfig:
    battery_config = config.station.battery
    return SimulationBatteryConfig(
        chemistry=battery_config.chemistry,
        nominal_voltage_v=battery_config.nominal_voltage_v,
        capacity_ah=battery_config.capacity_ah,
        installation_date=battery_config.installation_date,
    )


def _load_solar_source_points(
    session: Session,
    station_id: str,
    config_hash: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[SolarSourcePoint]:
    points_by_timestamp: dict[datetime, SolarSourcePoint] = {}
    for row in list_forecast_solar_for_config(
        session,
        station_id,
        config_hash,
        start_utc=start_utc,
        end_utc=end_utc,
    ):
        points_by_timestamp[row.timestamp_utc] = SolarSourcePoint(
            timestamp_utc=row.timestamp_utc,
            power_w=row.forecast_power_w,
            weather_state=getattr(row, "weather_state", None),
        )
    for row in list_simulated_solar_for_config(
        session,
        station_id,
        config_hash,
        start_utc=start_utc,
        end_utc=end_utc,
    ):
        points_by_timestamp[row.timestamp_utc] = SolarSourcePoint(
            timestamp_utc=row.timestamp_utc,
            power_w=row.simulated_power_w,
            weather_state=getattr(row, "weather_state", None),
        )
    return [points_by_timestamp[key] for key in sorted(points_by_timestamp)]


def _sample_solar(
    source_points: list[SolarSourcePoint],
    timestamp_utc: datetime,
) -> SolarSample:
    if not source_points:
        return SolarSample(
            power_w=0.0,
            weather_state="clear",
            solar_fallback_used=True,
            weather_fallback_used=True,
        )
    timestamps = [point.timestamp_utc for point in source_points]
    index = bisect_right(timestamps, timestamp_utc)
    lower = source_points[index - 1] if index > 0 else None
    upper = source_points[index] if index < len(source_points) else None
    if lower is not None and lower.timestamp_utc == timestamp_utc:
        weather_state, weather_fallback = _resolved_weather_state(lower.weather_state)
        return SolarSample(
            lower.power_w,
            weather_state,
            weather_fallback_used=weather_fallback,
        )
    if lower is not None and upper is not None:
        if upper.timestamp_utc - lower.timestamp_utc <= timedelta(minutes=30):
            power_w, _ = interpolate_power(
                timestamp_utc,
                lower.timestamp_utc,
                lower.power_w,
                upper.timestamp_utc,
                upper.power_w,
            )
            weather_state, weather_fallback = _resolved_weather_state(lower.weather_state)
            return SolarSample(
                power_w=round(power_w, 6),
                weather_state=weather_state,
                weather_fallback_used=weather_fallback,
            )
    if lower is not None and timestamp_utc - lower.timestamp_utc < timedelta(minutes=15):
        weather_state, weather_fallback = _resolved_weather_state(lower.weather_state)
        return SolarSample(
            lower.power_w,
            weather_state,
            weather_fallback_used=weather_fallback,
        )
    if upper is not None and upper.timestamp_utc - timestamp_utc < timedelta(minutes=15):
        weather_state, weather_fallback = _resolved_weather_state(upper.weather_state)
        return SolarSample(
            upper.power_w,
            weather_state,
            weather_fallback_used=weather_fallback,
        )
    return SolarSample(
        power_w=0.0,
        weather_state="clear",
        solar_fallback_used=True,
        weather_fallback_used=True,
    )


def _sample_grid(
    grid_points: list[GridAvailabilityPointRecord],
    timestamps: list[datetime],
    timestamp_utc: datetime,
) -> GridSample:
    if not grid_points:
        return GridSample(
            available=True,
            is_outage_now=False,
            outage_level="stable",
            next_outage_window_start_utc=None,
            fallback_used=True,
        )
    index = bisect_right(timestamps, timestamp_utc) - 1
    if index < 0:
        index = 0
    row = grid_points[index]
    return GridSample(
        available=row.local_grid_available and not row.is_outage_now,
        is_outage_now=row.is_outage_now,
        outage_level=row.outage_level,
        next_outage_window_start_utc=row.next_outage_window_start_utc,
    )


def _resolved_weather_state(value: str | None) -> tuple[str, bool]:
    if value is None or not str(value).strip():
        return "clear", True
    return str(value), False


def _load_grid_behavior(
    grid: GridSample,
    timestamp_utc: datetime,
    previous_grid_available: bool | None,
) -> str:
    if not grid.available or grid.is_outage_now:
        return "outage_active"
    if previous_grid_available is False:
        return "post_outage_recovery"
    if grid.next_outage_window_start_utc is not None:
        minutes_until_outage = (
            grid.next_outage_window_start_utc - timestamp_utc
        ).total_seconds() / SECONDS_PER_MINUTE
        if 0.0 <= minutes_until_outage <= 60.0:
            return "planned_outage_soon"
    if grid.outage_level in {"partial_outage", "severe_outage", "blackout"}:
        return "planned_outage_soon"
    return effective_grid_behavior(LoadContext(grid_available=True))


def _update_daily_load_values(
    daily_totals: dict[date, dict[str, float]],
    local_date: date,
    load_power_w: float,
    decision: EmsDecision,
    tariff_uah_per_kwh: float,
) -> dict[str, float]:
    totals = daily_totals.setdefault(
        local_date,
        {"load_wh": 0.0, "solar_covered_wh": 0.0},
    )
    totals["load_wh"] += max(0.0, load_power_w) / SECONDS_PER_MINUTE
    totals["solar_covered_wh"] += (
        max(0.0, decision.solar_to_load_w)
        + max(0.0, decision.solar_to_battery_w)
    ) / SECONDS_PER_MINUTE
    solar_covered_percent = (
        0.0
        if totals["load_wh"] <= 0.0
        else _clamp(totals["solar_covered_wh"] / totals["load_wh"] * 100.0, 0.0, 100.0)
    )
    return {
        "daily_energy_wh": round(totals["load_wh"], 6),
        "solar_covered_percent": round(solar_covered_percent, 6),
        "money_saved_uah": round(totals["solar_covered_wh"] / 1000.0 * tariff_uah_per_kwh, 6),
    }


def _target_soc_for_decision(decision: EmsDecision, config: AppConfig) -> float:
    selected = decision.selected_mode.value
    if selected in {
        "backup_reserve",
        "force_charge",
        "outage_mode",
        "battery_protection",
        "inverter_protection_shutdown",
    }:
        return config.station.ems.backup_target_soc_percent
    if selected == "battery_priority":
        return config.station.ems.reserve_soc_percent
    return config.station.ems.normal_target_soc_percent


def _inverter_state(decision: EmsDecision) -> str:
    if not decision.inverter_output_enabled:
        return "protection_shutdown"
    if decision.protection_active:
        return "protection_active"
    if decision.battery_to_load_w > 0.0:
        return "battery_support"
    if decision.grid_to_load_w > 0.0:
        return "pass_through"
    if decision.requested_charge_power_w > 0.0:
        return "charging"
    return "active"


def _is_history_timestamp(value: datetime) -> bool:
    return value.second == 0 and value.microsecond == 0 and value.minute in HISTORY_MINUTES


def _floor_to_minute(value: datetime) -> datetime:
    return _as_utc(value).replace(second=0, microsecond=0)


def _ceil_to_minute(value: datetime) -> datetime:
    floored = _floor_to_minute(value)
    if floored == _as_utc(value):
        return floored
    return floored + timedelta(minutes=1)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime is required")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
