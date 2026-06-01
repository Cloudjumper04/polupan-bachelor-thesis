from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, time
from enum import Enum
from math import isfinite
from typing import Mapping


class EmsMode(str, Enum):
    AUTO = "auto"
    GRID_PRIORITY = "grid_priority"
    SOLAR_PRIORITY = "solar_priority"
    SELF_CONSUMPTION = "self_consumption"
    BATTERY_PRIORITY = "battery_priority"
    BACKUP_RESERVE = "backup_reserve"
    FORCE_CHARGE = "force_charge"
    OUTAGE_MODE = "outage_mode"
    BATTERY_PROTECTION = "battery_protection"
    INVERTER_PROTECTION_SHUTDOWN = "inverter_protection_shutdown"
    POST_OUTAGE_RECOVERY = "post_outage_recovery"


CONFIG_MODES = {
    EmsMode.AUTO,
    EmsMode.GRID_PRIORITY,
    EmsMode.SOLAR_PRIORITY,
    EmsMode.SELF_CONSUMPTION,
    EmsMode.BATTERY_PRIORITY,
    EmsMode.BACKUP_RESERVE,
    EmsMode.FORCE_CHARGE,
}


@dataclass(frozen=True, slots=True)
class EmsConfig:
    mode: EmsMode | str = EmsMode.AUTO
    inverter_output_limit_w: float = 2000.0
    critical_soc_percent: float = 10.0
    reserve_soc_percent: float = 30.0
    normal_target_soc_percent: float = 80.0
    backup_target_soc_percent: float = 100.0
    cheap_tariff_start: str = "23:00"
    cheap_tariff_end: str = "07:00"
    cheap_tariff_price_factor: float = 0.5
    allow_grid_charging: bool = True
    recent_outage_recovery_minutes: int = 60

    def __post_init__(self) -> None:
        try:
            mode = EmsMode(self.mode)
        except ValueError as exc:
            allowed = ", ".join(sorted(mode.value for mode in CONFIG_MODES))
            raise ValueError(f"EMS mode must be one of: {allowed}") from exc
        if mode not in CONFIG_MODES:
            allowed = ", ".join(sorted(item.value for item in CONFIG_MODES))
            raise ValueError(f"EMS mode must be one of: {allowed}")
        object.__setattr__(self, "mode", mode)

        _validate_positive_finite(
            "inverter_output_limit_w",
            self.inverter_output_limit_w,
        )
        _validate_percent("critical_soc_percent", self.critical_soc_percent)
        _validate_percent("reserve_soc_percent", self.reserve_soc_percent)
        _validate_percent("normal_target_soc_percent", self.normal_target_soc_percent)
        _validate_percent("backup_target_soc_percent", self.backup_target_soc_percent)
        if not (
            self.critical_soc_percent
            < self.reserve_soc_percent
            < self.normal_target_soc_percent
            <= self.backup_target_soc_percent
        ):
            raise ValueError(
                "EMS SoC thresholds must satisfy critical < reserve < normal <= backup"
            )
        object.__setattr__(
            self,
            "cheap_tariff_start",
            _normalize_clock_time(self.cheap_tariff_start),
        )
        object.__setattr__(
            self,
            "cheap_tariff_end",
            _normalize_clock_time(self.cheap_tariff_end),
        )
        _validate_positive_finite(
            "cheap_tariff_price_factor",
            self.cheap_tariff_price_factor,
        )
        if int(self.recent_outage_recovery_minutes) != self.recent_outage_recovery_minutes:
            raise ValueError("recent_outage_recovery_minutes must be an integer")
        if self.recent_outage_recovery_minutes < 0:
            raise ValueError("recent_outage_recovery_minutes must be non-negative")
        object.__setattr__(
            self,
            "recent_outage_recovery_minutes",
            int(self.recent_outage_recovery_minutes),
        )
        object.__setattr__(self, "allow_grid_charging", bool(self.allow_grid_charging))

    @classmethod
    def from_station_config(cls, station_or_app_config: object) -> "EmsConfig":
        station = getattr(station_or_app_config, "station", station_or_app_config)
        raw_config = getattr(station, "ems", station_or_app_config)
        return cls.from_raw(raw_config)

    @classmethod
    def from_raw(cls, raw_config: object) -> "EmsConfig":
        if isinstance(raw_config, cls):
            return raw_config
        if hasattr(raw_config, "model_dump"):
            return cls(**raw_config.model_dump())
        if hasattr(raw_config, "dict"):
            return cls(**raw_config.dict())
        if isinstance(raw_config, Mapping):
            return cls(**dict(raw_config))

        values: dict[str, object] = {}
        for field in fields(cls):
            if hasattr(raw_config, field.name):
                values[field.name] = getattr(raw_config, field.name)
        if values:
            return cls(**values)
        raise ValueError("station config does not contain EMS settings")


@dataclass(frozen=True, slots=True)
class EmsHistorySummary:
    outage_minutes_last_6h: float = 0.0
    outage_minutes_last_24h: float = 0.0
    outage_count_last_24h: int = 0
    outage_count_last_72h: int = 0
    hours_since_last_outage: float | None = None
    min_soc_last_24h: float | None = None
    battery_recovered_to_full_after_last_outage: bool = True

    def __post_init__(self) -> None:
        _validate_non_negative_finite(
            "outage_minutes_last_6h",
            self.outage_minutes_last_6h,
        )
        _validate_non_negative_finite(
            "outage_minutes_last_24h",
            self.outage_minutes_last_24h,
        )
        if self.outage_count_last_24h < 0 or self.outage_count_last_72h < 0:
            raise ValueError("outage counts must be non-negative")
        if self.hours_since_last_outage is not None:
            _validate_non_negative_finite(
                "hours_since_last_outage",
                self.hours_since_last_outage,
            )
        if self.min_soc_last_24h is not None:
            _validate_percent("min_soc_last_24h", self.min_soc_last_24h)
        object.__setattr__(
            self,
            "outage_count_last_24h",
            int(self.outage_count_last_24h),
        )
        object.__setattr__(
            self,
            "outage_count_last_72h",
            int(self.outage_count_last_72h),
        )
        object.__setattr__(
            self,
            "battery_recovered_to_full_after_last_outage",
            bool(self.battery_recovered_to_full_after_last_outage),
        )


@dataclass(frozen=True, slots=True)
class EmsInput:
    timestamp: datetime
    grid_available: bool
    solar_available_power_w: float
    load_power_w: float
    battery_soc_percent: float
    battery_soh_percent: float
    battery_energy_wh: float
    battery_current_usable_capacity_wh: float
    battery_voltage_v: float
    battery_status: str
    battery_max_charge_power_w: float
    grid_was_available_previous_minute: bool | None = None
    history_summary: EmsHistorySummary | None = None
    config: EmsConfig | None = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("EMS timestamp must be timezone-aware")
        _validate_non_negative_finite(
            "solar_available_power_w",
            self.solar_available_power_w,
        )
        _validate_non_negative_finite("load_power_w", self.load_power_w)
        _validate_percent("battery_soc_percent", self.battery_soc_percent)
        _validate_percent("battery_soh_percent", self.battery_soh_percent)
        _validate_non_negative_finite("battery_energy_wh", self.battery_energy_wh)
        _validate_non_negative_finite(
            "battery_current_usable_capacity_wh",
            self.battery_current_usable_capacity_wh,
        )
        _validate_non_negative_finite("battery_voltage_v", self.battery_voltage_v)
        _validate_non_negative_finite(
            "battery_max_charge_power_w",
            self.battery_max_charge_power_w,
        )
        object.__setattr__(self, "grid_available", bool(self.grid_available))
        object.__setattr__(
            self,
            "battery_status",
            str(self.battery_status).strip().lower(),
        )
        if self.history_summary is None:
            object.__setattr__(self, "history_summary", EmsHistorySummary())
        if self.config is not None and not isinstance(self.config, EmsConfig):
            object.__setattr__(self, "config", EmsConfig.from_raw(self.config))


@dataclass(frozen=True, slots=True)
class EmsDecision:
    selected_mode: EmsMode
    auto_risk_score: int
    reason: str
    effective_served_load_w: float
    inverter_output_enabled: bool
    battery_provides_energy: bool
    requested_battery_discharge_energy_wh_last_minute: float
    requested_charge_power_w: float
    solar_to_load_w: float
    solar_to_battery_w: float
    grid_to_load_w: float
    grid_to_battery_w: float
    battery_to_load_w: float
    protection_active: bool
    cheap_tariff_active: bool
    flags: tuple[str, ...] = ()


class EmsDecisionEngine:
    def __init__(self, config: EmsConfig | object | None = None) -> None:
        self.config = EmsConfig() if config is None else EmsConfig.from_raw(config)

    def decide(self, ems_input: EmsInput) -> EmsDecision:
        config = ems_input.config or self.config
        cheap_tariff_active = is_cheap_tariff_active(ems_input.timestamp, config)
        auto_risk_score = calculate_auto_risk_score(ems_input, config)

        if not ems_input.grid_available:
            return _decide_outage(
                ems_input,
                config,
                auto_risk_score=auto_risk_score,
                cheap_tariff_active=cheap_tariff_active,
            )

        selected_mode = _selected_mode(config, ems_input, auto_risk_score, cheap_tariff_active)
        return _decide_grid_available(
            ems_input,
            config,
            selected_mode=selected_mode,
            auto_risk_score=auto_risk_score,
            cheap_tariff_active=cheap_tariff_active,
        )


def decide_ems(ems_input: EmsInput, config: EmsConfig | object | None = None) -> EmsDecision:
    return EmsDecisionEngine(config).decide(ems_input)


def is_cheap_tariff_active(timestamp: datetime, config: EmsConfig) -> bool:
    current = timestamp.timetz().replace(tzinfo=None)
    start = _parse_clock_time(config.cheap_tariff_start)
    end = _parse_clock_time(config.cheap_tariff_end)
    if start < end:
        return start <= current < end
    return current >= start or current < end


def calculate_auto_risk_score(ems_input: EmsInput, config: EmsConfig) -> int:
    history = ems_input.history_summary or EmsHistorySummary()
    score = 0.0

    if not ems_input.grid_available:
        score += 40.0
    if history.outage_minutes_last_6h > 0.0 or _hours_since_at_most(history, 6.0):
        score += 35.0
    if history.outage_minutes_last_24h > 240.0:
        score += 25.0
    if history.outage_count_last_24h >= 2:
        score += 20.0
    elif history.outage_count_last_24h == 1:
        score += 8.0
    if history.outage_count_last_72h >= 5:
        score += 25.0
    elif history.outage_count_last_72h >= 3:
        score += 15.0

    critical_soc_seen = (
        history.min_soc_last_24h is not None
        and history.min_soc_last_24h < config.critical_soc_percent
    )
    reserve_soc_seen = (
        history.min_soc_last_24h is not None
        and history.min_soc_last_24h < config.reserve_soc_percent
    )
    if critical_soc_seen:
        score += 60.0
    elif reserve_soc_seen:
        score += 20.0

    recent_outage_seen = (
        history.outage_minutes_last_6h > 0.0
        or history.outage_minutes_last_24h > 0.0
        or history.outage_count_last_24h > 0
        or history.outage_count_last_72h > 0
        or _hours_since_at_most(history, 72.0)
    )
    if recent_outage_seen and not history.battery_recovered_to_full_after_last_outage:
        score += 20.0

    if ems_input.battery_soc_percent <= config.critical_soc_percent:
        score += 35.0
    elif ems_input.battery_soc_percent < config.reserve_soc_percent:
        score += 15.0

    if ems_input.grid_available and not critical_soc_seen:
        if history.hours_since_last_outage is None and not recent_outage_seen:
            score -= 35.0
        elif history.hours_since_last_outage is not None:
            if history.hours_since_last_outage >= 12.0:
                score -= 20.0
            if history.hours_since_last_outage >= 24.0:
                score -= 15.0
            if history.hours_since_last_outage >= 72.0:
                score -= 15.0
        if (
            history.outage_minutes_last_24h == 0.0
            and history.outage_count_last_24h == 0
            and history.outage_count_last_72h == 0
        ):
            score -= 10.0

    if ems_input.battery_soc_percent >= config.backup_target_soc_percent - 0.1:
        score -= 15.0
    elif ems_input.battery_soc_percent >= config.normal_target_soc_percent:
        score -= 10.0

    if critical_soc_seen:
        score = max(score, 80.0)

    return int(round(_clamp(score, 0.0, 100.0)))


def _selected_mode(
    config: EmsConfig,
    ems_input: EmsInput,
    auto_risk_score: int,
    cheap_tariff_active: bool,
) -> EmsMode:
    if config.mode != EmsMode.AUTO:
        return config.mode
    if auto_risk_score <= 20:
        if (
            cheap_tariff_active
            and config.allow_grid_charging
            and ems_input.battery_soc_percent < config.backup_target_soc_percent
        ):
            return EmsMode.GRID_PRIORITY
        return EmsMode.SELF_CONSUMPTION
    if auto_risk_score <= 50:
        return EmsMode.SOLAR_PRIORITY
    if auto_risk_score <= 75:
        return EmsMode.BACKUP_RESERVE
    if (
        config.allow_grid_charging
        and ems_input.battery_soc_percent < config.backup_target_soc_percent
    ):
        return EmsMode.FORCE_CHARGE
    return EmsMode.BACKUP_RESERVE


def _decide_grid_available(
    ems_input: EmsInput,
    config: EmsConfig,
    *,
    selected_mode: EmsMode,
    auto_risk_score: int,
    cheap_tariff_active: bool,
) -> EmsDecision:
    load_power_w = ems_input.load_power_w
    solar_power_w = ems_input.solar_available_power_w
    flags: list[str] = []
    battery_to_load_w = 0.0

    if _recent_post_outage(ems_input, config):
        flags.append(EmsMode.POST_OUTAGE_RECOVERY.value)

    if selected_mode in {EmsMode.BACKUP_RESERVE, EmsMode.FORCE_CHARGE}:
        solar_to_load_w = 0.0
        grid_to_load_w = load_power_w
        solar_surplus_w = solar_power_w
    else:
        solar_to_load_w = min(solar_power_w, load_power_w)
        remaining_load_w = load_power_w - solar_to_load_w
        if selected_mode == EmsMode.SELF_CONSUMPTION:
            battery_to_load_w = _battery_to_load_power(
                ems_input,
                config,
                remaining_load_w,
                config.reserve_soc_percent,
            )
        elif selected_mode == EmsMode.BATTERY_PRIORITY:
            battery_to_load_w = _battery_to_load_power(
                ems_input,
                config,
                remaining_load_w,
                config.critical_soc_percent,
            )
        elif selected_mode not in {EmsMode.GRID_PRIORITY, EmsMode.SOLAR_PRIORITY}:
            selected_mode = EmsMode.GRID_PRIORITY
        grid_to_load_w = max(0.0, remaining_load_w - battery_to_load_w)
        solar_surplus_w = max(0.0, solar_power_w - solar_to_load_w)

    if (
        battery_to_load_w == 0.0
        and selected_mode in {EmsMode.SELF_CONSUMPTION, EmsMode.BATTERY_PRIORITY}
        and _battery_discharge_block_reason(ems_input, config) is not None
    ):
        flags.append(EmsMode.BATTERY_PROTECTION.value)

    target_soc = _target_soc_for_mode(selected_mode, config)
    if (
        config.mode == EmsMode.AUTO
        and auto_risk_score <= 20
        and cheap_tariff_active
        and config.allow_grid_charging
    ):
        target_soc = config.backup_target_soc_percent
    grid_charging_allowed = _grid_charging_allowed(
        selected_mode,
        ems_input,
        config,
        cheap_tariff_active,
    )
    solar_to_battery_w, grid_to_battery_w = _charge_allocation(
        ems_input,
        config,
        target_soc_percent=target_soc,
        solar_surplus_w=solar_surplus_w,
        grid_charging_allowed=grid_charging_allowed,
    )

    requested_charge_power_w = solar_to_battery_w + grid_to_battery_w
    reason = _grid_reason(
        selected_mode,
        requested_charge_power_w=requested_charge_power_w,
        grid_charging_allowed=grid_charging_allowed,
        cheap_tariff_active=cheap_tariff_active,
    )
    return _decision(
        selected_mode=selected_mode,
        auto_risk_score=auto_risk_score,
        reason=reason,
        effective_served_load_w=load_power_w,
        inverter_output_enabled=True,
        battery_to_load_w=battery_to_load_w,
        requested_charge_power_w=requested_charge_power_w,
        solar_to_load_w=solar_to_load_w,
        solar_to_battery_w=solar_to_battery_w,
        grid_to_load_w=grid_to_load_w,
        grid_to_battery_w=grid_to_battery_w,
        protection_active=False,
        cheap_tariff_active=cheap_tariff_active,
        flags=tuple(flags),
    )


def _decide_outage(
    ems_input: EmsInput,
    config: EmsConfig,
    *,
    auto_risk_score: int,
    cheap_tariff_active: bool,
) -> EmsDecision:
    load_power_w = ems_input.load_power_w
    if load_power_w <= 0.0:
        solar_to_battery_w, _grid_to_battery_w = _charge_allocation(
            ems_input,
            config,
            target_soc_percent=config.backup_target_soc_percent,
            solar_surplus_w=ems_input.solar_available_power_w,
            grid_charging_allowed=False,
        )
        return _decision(
            selected_mode=EmsMode.OUTAGE_MODE,
            auto_risk_score=auto_risk_score,
            reason="outage mode: no current load; solar may charge the battery",
            effective_served_load_w=0.0,
            inverter_output_enabled=True,
            battery_to_load_w=0.0,
            requested_charge_power_w=solar_to_battery_w,
            solar_to_load_w=0.0,
            solar_to_battery_w=solar_to_battery_w,
            grid_to_load_w=0.0,
            grid_to_battery_w=0.0,
            protection_active=False,
            cheap_tariff_active=cheap_tariff_active,
            flags=(EmsMode.OUTAGE_MODE.value,),
        )

    if load_power_w > config.inverter_output_limit_w:
        return _shutdown_decision(
            ems_input,
            config,
            auto_risk_score=auto_risk_score,
            cheap_tariff_active=cheap_tariff_active,
            reason=(
                "inverter protection shutdown: outage load exceeds inverter "
                "output limit"
            ),
            flags=("outage_load_above_inverter_limit",),
        )

    solar_to_load_w = min(ems_input.solar_available_power_w, load_power_w)
    remaining_load_w = load_power_w - solar_to_load_w
    battery_to_load_w = 0.0
    if remaining_load_w > 0.0:
        block_reason = _battery_discharge_block_reason(ems_input, config)
        if block_reason is not None:
            return _shutdown_decision(
                ems_input,
                config,
                auto_risk_score=auto_risk_score,
                cheap_tariff_active=cheap_tariff_active,
                reason=f"battery protection shutdown: {block_reason}",
                flags=(EmsMode.BATTERY_PROTECTION.value,),
            )
        available_battery_power_w = _available_battery_output_power_w(
            ems_input,
            config,
            config.critical_soc_percent,
        )
        if remaining_load_w > available_battery_power_w + 1e-9:
            return _shutdown_decision(
                ems_input,
                config,
                auto_risk_score=auto_risk_score,
                cheap_tariff_active=cheap_tariff_active,
                reason=(
                    "inverter protection shutdown: battery/inverter capability "
                    "is insufficient for outage load"
                ),
                flags=("battery_capability_insufficient",),
            )
        battery_to_load_w = remaining_load_w

    solar_surplus_w = max(0.0, ems_input.solar_available_power_w - solar_to_load_w)
    solar_to_battery_w, _grid_to_battery_w = _charge_allocation(
        ems_input,
        config,
        target_soc_percent=config.backup_target_soc_percent,
        solar_surplus_w=solar_surplus_w,
        grid_charging_allowed=False,
    )
    requested_charge_power_w = solar_to_battery_w
    return _decision(
        selected_mode=EmsMode.OUTAGE_MODE,
        auto_risk_score=auto_risk_score,
        reason=_outage_reason(battery_to_load_w),
        effective_served_load_w=load_power_w,
        inverter_output_enabled=True,
        battery_to_load_w=battery_to_load_w,
        requested_charge_power_w=requested_charge_power_w,
        solar_to_load_w=solar_to_load_w,
        solar_to_battery_w=solar_to_battery_w,
        grid_to_load_w=0.0,
        grid_to_battery_w=0.0,
        protection_active=False,
        cheap_tariff_active=cheap_tariff_active,
        flags=(EmsMode.OUTAGE_MODE.value,),
    )


def _outage_reason(battery_to_load_w: float) -> str:
    if battery_to_load_w > 0.0:
        return "outage mode: solar serves load first and battery covers the rest"
    return "outage mode: solar fully serves load; battery discharge is disabled"


def _shutdown_decision(
    ems_input: EmsInput,
    config: EmsConfig,
    *,
    auto_risk_score: int,
    cheap_tariff_active: bool,
    reason: str,
    flags: tuple[str, ...],
) -> EmsDecision:
    solar_to_battery_w, _grid_to_battery_w = _charge_allocation(
        ems_input,
        config,
        target_soc_percent=config.backup_target_soc_percent,
        solar_surplus_w=ems_input.solar_available_power_w,
        grid_charging_allowed=False,
    )
    return _decision(
        selected_mode=EmsMode.INVERTER_PROTECTION_SHUTDOWN,
        auto_risk_score=auto_risk_score,
        reason=reason,
        effective_served_load_w=0.0,
        inverter_output_enabled=False,
        battery_to_load_w=0.0,
        requested_charge_power_w=solar_to_battery_w,
        solar_to_load_w=0.0,
        solar_to_battery_w=solar_to_battery_w,
        grid_to_load_w=0.0,
        grid_to_battery_w=0.0,
        protection_active=True,
        cheap_tariff_active=cheap_tariff_active,
        flags=(EmsMode.INVERTER_PROTECTION_SHUTDOWN.value, *flags),
    )


def _decision(
    *,
    selected_mode: EmsMode,
    auto_risk_score: int,
    reason: str,
    effective_served_load_w: float,
    inverter_output_enabled: bool,
    battery_to_load_w: float,
    requested_charge_power_w: float,
    solar_to_load_w: float,
    solar_to_battery_w: float,
    grid_to_load_w: float,
    grid_to_battery_w: float,
    protection_active: bool,
    cheap_tariff_active: bool,
    flags: tuple[str, ...],
) -> EmsDecision:
    battery_to_load_w = _round_power(battery_to_load_w)
    requested_discharge_wh = battery_to_load_w / 60.0 if battery_to_load_w > 0.0 else 0.0
    return EmsDecision(
        selected_mode=selected_mode,
        auto_risk_score=auto_risk_score,
        reason=reason,
        effective_served_load_w=_round_power(effective_served_load_w),
        inverter_output_enabled=inverter_output_enabled,
        battery_provides_energy=battery_to_load_w > 0.0,
        requested_battery_discharge_energy_wh_last_minute=round(
            requested_discharge_wh,
            6,
        ),
        requested_charge_power_w=_round_power(requested_charge_power_w),
        solar_to_load_w=_round_power(solar_to_load_w),
        solar_to_battery_w=_round_power(solar_to_battery_w),
        grid_to_load_w=_round_power(grid_to_load_w),
        grid_to_battery_w=_round_power(grid_to_battery_w),
        battery_to_load_w=battery_to_load_w,
        protection_active=protection_active,
        cheap_tariff_active=cheap_tariff_active,
        flags=flags,
    )


def _grid_reason(
    selected_mode: EmsMode,
    *,
    requested_charge_power_w: float,
    grid_charging_allowed: bool,
    cheap_tariff_active: bool,
) -> str:
    reason_by_mode = {
        EmsMode.GRID_PRIORITY: "grid priority: grid covers load while battery discharge is disabled",
        EmsMode.SOLAR_PRIORITY: "solar priority: solar serves load first and grid covers the rest",
        EmsMode.SELF_CONSUMPTION: "self-consumption: solar and battery reduce grid use while reserve is protected",
        EmsMode.BATTERY_PRIORITY: "battery priority: battery is used aggressively above critical reserve",
        EmsMode.BACKUP_RESERVE: "backup reserve: battery discharge is disabled while grid exists",
        EmsMode.FORCE_CHARGE: "force charge: battery discharge is disabled and charging is prioritized",
    }
    reason = reason_by_mode.get(selected_mode, "grid available: conservative routing")
    if requested_charge_power_w > 0.0:
        source = "grid/solar" if grid_charging_allowed else "solar"
        if cheap_tariff_active and grid_charging_allowed:
            source = "cheap-tariff grid/solar"
        reason = f"{reason}; requesting {source} battery charging"
    return reason


def _target_soc_for_mode(selected_mode: EmsMode, config: EmsConfig) -> float:
    if selected_mode in {EmsMode.BACKUP_RESERVE, EmsMode.FORCE_CHARGE}:
        return config.backup_target_soc_percent
    if selected_mode == EmsMode.BATTERY_PRIORITY:
        return config.reserve_soc_percent
    return config.normal_target_soc_percent


def _grid_charging_allowed(
    selected_mode: EmsMode,
    ems_input: EmsInput,
    config: EmsConfig,
    cheap_tariff_active: bool,
) -> bool:
    if not config.allow_grid_charging:
        return False
    if selected_mode in {
        EmsMode.GRID_PRIORITY,
        EmsMode.BACKUP_RESERVE,
        EmsMode.FORCE_CHARGE,
    }:
        return True
    if cheap_tariff_active and selected_mode == EmsMode.SOLAR_PRIORITY:
        return True
    if ems_input.battery_soc_percent < config.reserve_soc_percent:
        return True
    return False


def _charge_allocation(
    ems_input: EmsInput,
    config: EmsConfig,
    *,
    target_soc_percent: float,
    solar_surplus_w: float,
    grid_charging_allowed: bool,
) -> tuple[float, float]:
    desired_charge_power_w = _charge_power_to_target(
        ems_input,
        target_soc_percent,
    )
    solar_to_battery_w = min(
        max(0.0, solar_surplus_w),
        desired_charge_power_w,
    )
    remaining_charge_w = max(0.0, desired_charge_power_w - solar_to_battery_w)
    grid_to_battery_w = remaining_charge_w if grid_charging_allowed else 0.0
    if not config.allow_grid_charging:
        grid_to_battery_w = 0.0
    return solar_to_battery_w, grid_to_battery_w


def _charge_power_to_target(
    ems_input: EmsInput,
    target_soc_percent: float,
) -> float:
    if not ems_input.battery_status:
        return 0.0
    if ems_input.battery_current_usable_capacity_wh <= 0.0:
        return 0.0
    if ems_input.battery_max_charge_power_w <= 0.0:
        return 0.0
    target_energy_wh = (
        ems_input.battery_current_usable_capacity_wh
        * _clamp(target_soc_percent, 0.0, 100.0)
        / 100.0
    )
    headroom_wh = max(0.0, target_energy_wh - ems_input.battery_energy_wh)
    if headroom_wh <= 0.0:
        return 0.0
    return min(ems_input.battery_max_charge_power_w, headroom_wh * 60.0)


def _battery_to_load_power(
    ems_input: EmsInput,
    config: EmsConfig,
    remaining_load_w: float,
    floor_soc_percent: float,
) -> float:
    if remaining_load_w <= 0.0:
        return 0.0
    if _battery_discharge_block_reason(ems_input, config) is not None:
        return 0.0
    available_power_w = _available_battery_output_power_w(
        ems_input,
        config,
        floor_soc_percent,
    )
    return min(remaining_load_w, available_power_w)


def _available_battery_output_power_w(
    ems_input: EmsInput,
    config: EmsConfig,
    floor_soc_percent: float,
) -> float:
    if _battery_discharge_block_reason(ems_input, config) is not None:
        return 0.0
    floor_energy_wh = 0.0
    if ems_input.battery_current_usable_capacity_wh > 0.0:
        floor_energy_wh = (
            ems_input.battery_current_usable_capacity_wh
            * _clamp(floor_soc_percent, 0.0, 100.0)
            / 100.0
        )
    available_energy_wh = max(0.0, ems_input.battery_energy_wh - floor_energy_wh)
    return min(config.inverter_output_limit_w, available_energy_wh * 60.0)


def _battery_discharge_block_reason(
    ems_input: EmsInput,
    config: EmsConfig,
) -> str | None:
    if not ems_input.battery_status:
        return "battery status is unavailable"
    if ems_input.battery_status == "empty":
        return "battery status is empty"
    if ems_input.battery_current_usable_capacity_wh <= 0.0:
        return "battery usable capacity is unavailable"
    if ems_input.battery_energy_wh <= 0.0:
        return "battery has no usable energy"
    if ems_input.battery_soc_percent <= config.critical_soc_percent:
        return "battery SoC is at or below critical threshold"
    return None


def _recent_post_outage(ems_input: EmsInput, config: EmsConfig) -> bool:
    if ems_input.grid_was_available_previous_minute is False:
        return True
    history = ems_input.history_summary or EmsHistorySummary()
    if history.hours_since_last_outage is None:
        return False
    recovery_hours = config.recent_outage_recovery_minutes / 60.0
    return history.hours_since_last_outage <= recovery_hours


def _hours_since_at_most(history: EmsHistorySummary, hours: float) -> bool:
    return (
        history.hours_since_last_outage is not None
        and history.hours_since_last_outage <= hours
    )


def _normalize_clock_time(value: object) -> str:
    parsed = _parse_clock_time(str(value))
    return f"{parsed.hour:02d}:{parsed.minute:02d}"


def _parse_clock_time(value: str) -> time:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError("EMS tariff time must use HH:MM")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError("EMS tariff time must use HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("EMS tariff time must use HH:MM")
    return time(hour, minute)


def _validate_positive_finite(name: str, value: float) -> None:
    _validate_non_negative_finite(name, value)
    if float(value) <= 0.0:
        raise ValueError(f"{name} must be positive")


def _validate_non_negative_finite(name: str, value: float) -> None:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not isfinite(numeric) or numeric < 0.0:
        raise ValueError(f"{name} must be a non-negative finite number")


def _validate_percent(name: str, value: float) -> None:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite percent") from exc
    if not isfinite(numeric) or not (0.0 <= numeric <= 100.0):
        raise ValueError(f"{name} must be between 0 and 100")


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _round_power(value: float) -> float:
    return round(max(0.0, value), 6)
