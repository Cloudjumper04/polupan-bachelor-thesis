from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from math import isfinite


class BatteryChemistry(str, Enum):
    LEAD_ACID = "lead_acid"
    LIFEPO4 = "lifepo4"
    LI_ION = "li_ion"


class BatteryStatus(str, Enum):
    IDLE = "idle"
    CHARGING = "charging"
    DISCHARGING = "discharging"
    FULL = "full"
    EMPTY = "empty"
    CHARGE_LIMITED = "charge_limited"
    DISCHARGE_LIMITED = "discharge_limited"


@dataclass(frozen=True, slots=True)
class VoltageCurvePoint:
    soc_percent: float
    voltage_factor: float


@dataclass(frozen=True, slots=True)
class BatteryChemistryProfile:
    chemistry: BatteryChemistry
    usable_dod_fraction: float
    operational_dod_factor: float
    max_charge_c_rate: float
    charge_efficiency: float
    discharge_efficiency: float
    reference_cycle_life_to_80_soh: float
    voltage_curve: tuple[VoltageCurvePoint, ...]

    @property
    def operational_usable_fraction(self) -> float:
        return self.usable_dod_fraction * self.operational_dod_factor


@dataclass(frozen=True, slots=True)
class BatteryConfig:
    chemistry: BatteryChemistry | str
    nominal_voltage_v: int
    capacity_ah: float
    installation_date: date | str

    def __post_init__(self) -> None:
        try:
            chemistry = BatteryChemistry(self.chemistry)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in BatteryChemistry)
            raise ValueError(f"battery chemistry must be one of: {allowed}") from exc
        object.__setattr__(self, "chemistry", chemistry)

        voltage = int(self.nominal_voltage_v)
        if voltage != self.nominal_voltage_v or voltage not in {12, 24}:
            raise ValueError("battery nominal_voltage_v must be exactly 12 or 24")
        object.__setattr__(self, "nominal_voltage_v", voltage)

        capacity = float(self.capacity_ah)
        if not isfinite(capacity) or capacity <= 0.0:
            raise ValueError("battery capacity_ah must be a positive finite number")
        object.__setattr__(self, "capacity_ah", capacity)

        installation_date = self.installation_date
        if isinstance(installation_date, str):
            try:
                installation_date = date.fromisoformat(installation_date)
            except ValueError as exc:
                raise ValueError("battery installation_date must use YYYY-MM-DD") from exc
        if not isinstance(installation_date, date) or isinstance(installation_date, datetime):
            raise ValueError("battery installation_date must be a date")
        object.__setattr__(self, "installation_date", installation_date)


@dataclass(frozen=True, slots=True)
class BatteryState:
    energy_wh: float
    soc_percent: float
    soh_percent: float
    nominal_energy_wh: float
    usable_capacity_wh: float
    current_usable_capacity_wh: float
    voltage_v: float
    equivalent_cycles_today: float
    total_equivalent_cycles: float
    status: BatteryStatus


@dataclass(frozen=True, slots=True)
class BatteryStepInput:
    timestamp: datetime
    consumed_energy_wh_last_minute: float = 0.0
    battery_provides_energy: bool = False
    requested_charge_power_w: float = 0.0

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("battery step timestamp must be timezone-aware")
        if not _is_non_negative_finite(self.consumed_energy_wh_last_minute):
            raise ValueError("consumed_energy_wh_last_minute must be a non-negative finite number")
        if not _is_non_negative_finite(self.requested_charge_power_w):
            raise ValueError("requested_charge_power_w must be a non-negative finite number")


@dataclass(frozen=True, slots=True)
class BatteryStepResult:
    timestamp: datetime
    state: BatteryState
    requested_charge_power_w: float
    applied_charge_power_w: float
    stored_charge_energy_wh: float
    requested_discharge_energy_wh: float
    applied_discharge_energy_wh: float
    removed_discharge_energy_wh: float
    status: BatteryStatus

    @property
    def applied_discharge_power_w(self) -> float:
        return self.applied_discharge_energy_wh * 60.0


@dataclass(frozen=True, slots=True)
class BatteryDailyFinalizeResult:
    finalized_date: date
    equivalent_cycles: float
    soh_loss_percent: float
    state: BatteryState
    applied: bool = True


OPERATIONAL_DOD_FACTOR = 0.80

_VOLTAGE_CURVES: dict[BatteryChemistry, tuple[VoltageCurvePoint, ...]] = {
    BatteryChemistry.LEAD_ACID: (
        VoltageCurvePoint(0.0, 0.90),
        VoltageCurvePoint(10.0, 0.92),
        VoltageCurvePoint(20.0, 0.94),
        VoltageCurvePoint(30.0, 0.965),
        VoltageCurvePoint(40.0, 0.98),
        VoltageCurvePoint(50.0, 1.00),
        VoltageCurvePoint(60.0, 1.02),
        VoltageCurvePoint(70.0, 1.03),
        VoltageCurvePoint(80.0, 1.04),
        VoltageCurvePoint(90.0, 1.05),
        VoltageCurvePoint(100.0, 1.06),
    ),
    BatteryChemistry.LIFEPO4: (
        VoltageCurvePoint(0.0, 0.88),
        VoltageCurvePoint(10.0, 0.94),
        VoltageCurvePoint(20.0, 0.975),
        VoltageCurvePoint(30.0, 0.99),
        VoltageCurvePoint(40.0, 0.995),
        VoltageCurvePoint(50.0, 1.00),
        VoltageCurvePoint(60.0, 1.015),
        VoltageCurvePoint(70.0, 1.03),
        VoltageCurvePoint(80.0, 1.045),
        VoltageCurvePoint(90.0, 1.06),
        VoltageCurvePoint(100.0, 1.12),
    ),
    BatteryChemistry.LI_ION: (
        VoltageCurvePoint(0.0, 0.81),
        VoltageCurvePoint(10.0, 0.88),
        VoltageCurvePoint(20.0, 0.92),
        VoltageCurvePoint(30.0, 0.96),
        VoltageCurvePoint(40.0, 0.98),
        VoltageCurvePoint(50.0, 1.00),
        VoltageCurvePoint(60.0, 1.02),
        VoltageCurvePoint(70.0, 1.04),
        VoltageCurvePoint(80.0, 1.065),
        VoltageCurvePoint(90.0, 1.09),
        VoltageCurvePoint(100.0, 1.14),
    ),
}

CHEMISTRY_PROFILES: dict[BatteryChemistry, BatteryChemistryProfile] = {
    BatteryChemistry.LEAD_ACID: BatteryChemistryProfile(
        chemistry=BatteryChemistry.LEAD_ACID,
        usable_dod_fraction=0.50,
        operational_dod_factor=OPERATIONAL_DOD_FACTOR,
        max_charge_c_rate=0.20,
        charge_efficiency=0.85,
        discharge_efficiency=0.85,
        reference_cycle_life_to_80_soh=500.0,
        voltage_curve=_VOLTAGE_CURVES[BatteryChemistry.LEAD_ACID],
    ),
    BatteryChemistry.LIFEPO4: BatteryChemistryProfile(
        chemistry=BatteryChemistry.LIFEPO4,
        usable_dod_fraction=1.00,
        operational_dod_factor=OPERATIONAL_DOD_FACTOR,
        max_charge_c_rate=0.50,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        reference_cycle_life_to_80_soh=3000.0,
        voltage_curve=_VOLTAGE_CURVES[BatteryChemistry.LIFEPO4],
    ),
    BatteryChemistry.LI_ION: BatteryChemistryProfile(
        chemistry=BatteryChemistry.LI_ION,
        usable_dod_fraction=0.90,
        operational_dod_factor=OPERATIONAL_DOD_FACTOR,
        max_charge_c_rate=0.50,
        charge_efficiency=0.93,
        discharge_efficiency=0.93,
        reference_cycle_life_to_80_soh=800.0,
        voltage_curve=_VOLTAGE_CURVES[BatteryChemistry.LI_ION],
    ),
}


class BatterySimulator:
    def __init__(
        self,
        config: BatteryConfig,
        initial_state: BatteryState | None = None,
    ) -> None:
        self.config = config
        self.profile = chemistry_profile(config.chemistry)
        self._active_day = config.installation_date
        self._finalized_dates: set[date] = set()
        if initial_state is None:
            self._state = self._build_state(
                energy_wh=self.initial_energy_wh,
                soh_percent=100.0,
                equivalent_cycles_today=0.0,
                total_equivalent_cycles=0.0,
                status=BatteryStatus.IDLE,
            )
            self._discharged_wh_today = 0.0
        else:
            self._state = self._normalized_state(initial_state)
            self._discharged_wh_today = (
                self._state.equivalent_cycles_today
                * self._state.current_usable_capacity_wh
            )

    @classmethod
    def from_station_config(cls, station_or_app_config: object) -> "BatterySimulator":
        battery_config = _extract_battery_config(station_or_app_config)
        return cls(
            BatteryConfig(
                chemistry=battery_config.chemistry,
                nominal_voltage_v=battery_config.nominal_voltage_v,
                capacity_ah=battery_config.capacity_ah,
                installation_date=battery_config.installation_date,
            )
        )

    @property
    def state(self) -> BatteryState:
        return self._state

    @property
    def active_day(self) -> date:
        return self._active_day

    @property
    def nominal_energy_wh(self) -> float:
        return nominal_energy_wh(self.config)

    @property
    def usable_capacity_wh(self) -> float:
        return usable_capacity_wh(self.config, self.profile)

    @property
    def initial_energy_wh(self) -> float:
        return self.usable_capacity_wh * 0.50

    @property
    def max_charge_current_a(self) -> float:
        return self.config.capacity_ah * self.profile.max_charge_c_rate

    @property
    def max_charge_power_w(self) -> float:
        return self.config.nominal_voltage_v * self.max_charge_current_a

    def step(self, step_input: BatteryStepInput) -> BatteryStepResult:
        self._finalize_previous_days(step_input.timestamp.date())

        state = self._state
        energy_wh = state.energy_wh

        requested_discharge_energy_wh = (
            float(step_input.consumed_energy_wh_last_minute)
            if step_input.battery_provides_energy
            else 0.0
        )
        available_discharge_to_load_wh = energy_wh * self.profile.discharge_efficiency
        applied_discharge_energy_wh = min(
            requested_discharge_energy_wh,
            available_discharge_to_load_wh,
        )
        removed_discharge_energy_wh = (
            applied_discharge_energy_wh / self.profile.discharge_efficiency
            if applied_discharge_energy_wh > 0.0
            else 0.0
        )
        energy_wh -= removed_discharge_energy_wh
        if energy_wh <= 1e-9:
            energy_wh = 0.0

        if removed_discharge_energy_wh > 0.0:
            self._discharged_wh_today += removed_discharge_energy_wh

        requested_charge_power_w = float(step_input.requested_charge_power_w)
        remaining_capacity_wh = max(0.0, state.current_usable_capacity_wh - energy_wh)
        max_power_by_capacity_w = (
            remaining_capacity_wh * 60.0 / self.profile.charge_efficiency
            if remaining_capacity_wh > 0.0
            else 0.0
        )
        applied_charge_power_w = min(
            requested_charge_power_w,
            self.max_charge_power_w,
            max_power_by_capacity_w,
        )
        stored_charge_energy_wh = (
            applied_charge_power_w * self.profile.charge_efficiency / 60.0
            if applied_charge_power_w > 0.0
            else 0.0
        )
        energy_wh += stored_charge_energy_wh
        energy_wh = _clamp(energy_wh, 0.0, state.current_usable_capacity_wh)

        equivalent_cycles_today = self._equivalent_cycles_today(
            state.current_usable_capacity_wh,
        )
        status = self._step_status(
            requested_discharge_energy_wh=requested_discharge_energy_wh,
            applied_discharge_energy_wh=applied_discharge_energy_wh,
            requested_charge_power_w=requested_charge_power_w,
            applied_charge_power_w=applied_charge_power_w,
            energy_wh=energy_wh,
            capacity_wh=state.current_usable_capacity_wh,
        )
        self._state = self._build_state(
            energy_wh=energy_wh,
            soh_percent=state.soh_percent,
            equivalent_cycles_today=equivalent_cycles_today,
            total_equivalent_cycles=state.total_equivalent_cycles,
            status=status,
        )
        return BatteryStepResult(
            timestamp=step_input.timestamp,
            state=self._state,
            requested_charge_power_w=requested_charge_power_w,
            applied_charge_power_w=applied_charge_power_w,
            stored_charge_energy_wh=stored_charge_energy_wh,
            requested_discharge_energy_wh=requested_discharge_energy_wh,
            applied_discharge_energy_wh=applied_discharge_energy_wh,
            removed_discharge_energy_wh=removed_discharge_energy_wh,
            status=status,
        )

    def finalize_day(self, finalized_date: date | None = None) -> BatteryDailyFinalizeResult:
        finalized = self._finalize_date_or_raise(finalized_date)
        if finalized > self._active_day:
            raise ValueError(
                "cannot finalize a future battery day; advance the simulator with step(...)"
            )
        if finalized < self._active_day or finalized in self._finalized_dates:
            return BatteryDailyFinalizeResult(
                finalized_date=finalized,
                equivalent_cycles=0.0,
                soh_loss_percent=0.0,
                state=self._state,
                applied=False,
            )

        state = self._state
        equivalent_cycles = self._equivalent_cycles_today(state.current_usable_capacity_wh)
        soh_loss_percent = (
            equivalent_cycles * (20.0 / self.profile.reference_cycle_life_to_80_soh)
        )
        new_soh_percent = max(0.0, state.soh_percent - soh_loss_percent)
        self._discharged_wh_today = 0.0
        self._finalized_dates.add(finalized)
        self._state = self._build_state(
            energy_wh=state.energy_wh,
            soh_percent=new_soh_percent,
            equivalent_cycles_today=0.0,
            total_equivalent_cycles=state.total_equivalent_cycles + equivalent_cycles,
            status=self._capacity_status(state.energy_wh, current_usable_capacity_wh(new_soh_percent, self.usable_capacity_wh)),
        )
        return BatteryDailyFinalizeResult(
            finalized_date=finalized,
            equivalent_cycles=equivalent_cycles,
            soh_loss_percent=soh_loss_percent,
            state=self._state,
            applied=True,
        )

    def _finalize_previous_days(self, step_day: date) -> None:
        if step_day > self._active_day:
            self.finalize_day(self._active_day)
            self._active_day = step_day

    def _finalize_date_or_raise(self, finalized_date: date | None) -> date:
        if finalized_date is None:
            return self._active_day
        if isinstance(finalized_date, datetime):
            raise ValueError("finalized_date must be a date, not a datetime")
        if not isinstance(finalized_date, date):
            raise ValueError("finalized_date must be a date")
        return finalized_date

    def _build_state(
        self,
        *,
        energy_wh: float,
        soh_percent: float,
        equivalent_cycles_today: float,
        total_equivalent_cycles: float,
        status: BatteryStatus,
    ) -> BatteryState:
        nominal_wh = self.nominal_energy_wh
        usable_wh = self.usable_capacity_wh
        current_usable_wh = current_usable_capacity_wh(soh_percent, usable_wh)
        clamped_energy_wh = _clamp(energy_wh, 0.0, current_usable_wh)
        soc_percent = soc_percent_from_energy(clamped_energy_wh, current_usable_wh)
        return BatteryState(
            energy_wh=clamped_energy_wh,
            soc_percent=soc_percent,
            soh_percent=soh_percent,
            nominal_energy_wh=nominal_wh,
            usable_capacity_wh=usable_wh,
            current_usable_capacity_wh=current_usable_wh,
            voltage_v=display_voltage_v(
                self.config.nominal_voltage_v,
                self.profile,
                soc_percent,
            ),
            equivalent_cycles_today=equivalent_cycles_today,
            total_equivalent_cycles=total_equivalent_cycles,
            status=status,
        )

    def _normalized_state(self, state: BatteryState) -> BatteryState:
        return self._build_state(
            energy_wh=state.energy_wh,
            soh_percent=_clamp(state.soh_percent, 0.0, 100.0),
            equivalent_cycles_today=max(0.0, state.equivalent_cycles_today),
            total_equivalent_cycles=max(0.0, state.total_equivalent_cycles),
            status=BatteryStatus(state.status),
        )

    def _equivalent_cycles_today(self, capacity_wh: float) -> float:
        if capacity_wh <= 0.0:
            return 0.0
        return self._discharged_wh_today / capacity_wh

    def _step_status(
        self,
        *,
        requested_discharge_energy_wh: float,
        applied_discharge_energy_wh: float,
        requested_charge_power_w: float,
        applied_charge_power_w: float,
        energy_wh: float,
        capacity_wh: float,
    ) -> BatteryStatus:
        discharge_limited = (
            requested_discharge_energy_wh > applied_discharge_energy_wh + 1e-9
        )
        charge_limited = requested_charge_power_w > applied_charge_power_w + 1e-9
        if discharge_limited:
            return (
                BatteryStatus.EMPTY
                if energy_wh <= 1e-9
                else BatteryStatus.DISCHARGE_LIMITED
            )
        if charge_limited:
            return (
                BatteryStatus.FULL
                if energy_wh >= capacity_wh - 1e-9
                else BatteryStatus.CHARGE_LIMITED
            )
        if applied_charge_power_w > 0.0:
            return BatteryStatus.CHARGING
        if applied_discharge_energy_wh > 0.0:
            return BatteryStatus.DISCHARGING
        return self._capacity_status(energy_wh, capacity_wh)

    def _capacity_status(self, energy_wh: float, capacity_wh: float) -> BatteryStatus:
        if capacity_wh <= 0.0 or energy_wh <= 1e-9:
            return BatteryStatus.EMPTY
        if energy_wh >= capacity_wh - 1e-9:
            return BatteryStatus.FULL
        return BatteryStatus.IDLE


def chemistry_profile(
    chemistry: BatteryChemistry | str,
) -> BatteryChemistryProfile:
    return CHEMISTRY_PROFILES[BatteryChemistry(chemistry)]


def nominal_energy_wh(config: BatteryConfig) -> float:
    return float(config.nominal_voltage_v) * float(config.capacity_ah)


def usable_capacity_wh(
    config: BatteryConfig,
    profile: BatteryChemistryProfile | None = None,
) -> float:
    resolved_profile = profile or chemistry_profile(config.chemistry)
    return nominal_energy_wh(config) * resolved_profile.operational_usable_fraction


def current_usable_capacity_wh(soh_percent: float, usable_capacity_wh: float) -> float:
    return usable_capacity_wh * _clamp(soh_percent, 0.0, 100.0) / 100.0


def soc_percent_from_energy(energy_wh: float, current_usable_capacity_wh: float) -> float:
    if current_usable_capacity_wh <= 0.0:
        return 0.0
    return _clamp(energy_wh / current_usable_capacity_wh * 100.0, 0.0, 100.0)


def display_voltage_v(
    nominal_voltage_v: int,
    profile: BatteryChemistryProfile,
    soc_percent: float,
) -> float:
    return float(nominal_voltage_v) * interpolate_voltage_factor(
        profile.voltage_curve,
        soc_percent,
    )


def interpolate_voltage_factor(
    voltage_curve: tuple[VoltageCurvePoint, ...],
    soc_percent: float,
) -> float:
    soc = _clamp(float(soc_percent), 0.0, 100.0)
    if soc <= voltage_curve[0].soc_percent:
        return voltage_curve[0].voltage_factor
    for lower, upper in zip(voltage_curve, voltage_curve[1:]):
        if soc <= upper.soc_percent:
            span = upper.soc_percent - lower.soc_percent
            if span <= 0.0:
                return upper.voltage_factor
            ratio = (soc - lower.soc_percent) / span
            return lower.voltage_factor + ratio * (
                upper.voltage_factor - lower.voltage_factor
            )
    return voltage_curve[-1].voltage_factor


def _extract_battery_config(station_or_app_config: object) -> object:
    station = getattr(station_or_app_config, "station", station_or_app_config)
    battery_config = getattr(station, "battery", None)
    if battery_config is None:
        raise ValueError("station config does not contain battery settings")
    return battery_config


def _is_non_negative_finite(value: float) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(numeric) and numeric >= 0.0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
