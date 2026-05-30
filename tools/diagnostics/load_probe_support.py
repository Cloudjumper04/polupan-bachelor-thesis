from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite


@dataclass(frozen=True)
class SyntheticBatterySnapshot:
    timestamp_local: datetime | None
    grid_available: bool
    total_power_draw_w: float
    battery_wh: float
    soc_percent: float
    charge_energy_wh: float
    discharge_energy_wh: float


@dataclass
class SyntheticBatterySocProvider:
    """Diagnostic-only minute-step SoC provider for load visual probing."""

    capacity_wh: float = 2400.0
    initial_soc_percent: float = 100.0
    grid_charge_w: float = 500.0
    battery_wh: float = field(init=False)

    def __post_init__(self) -> None:
        self.capacity_wh = _positive_finite(self.capacity_wh, "capacity_wh")
        self.grid_charge_w = _non_negative_finite(self.grid_charge_w, "grid_charge_w")
        initial_soc = _clamp(
            _finite_float(self.initial_soc_percent, "initial_soc_percent"),
            0.0,
            100.0,
        )
        self.battery_wh = self.capacity_wh * initial_soc / 100.0

    @property
    def soc_percent(self) -> float:
        return self.battery_wh / self.capacity_wh * 100.0

    def update_after_minute(
        self,
        total_power_draw_w: float,
        grid_available: bool,
        timestamp_local: datetime | None = None,
    ) -> SyntheticBatterySnapshot:
        power_w = _non_negative_finite(total_power_draw_w, "total_power_draw_w")
        charge_energy_wh = 0.0
        discharge_energy_wh = 0.0

        if grid_available:
            charge_energy_wh = self.grid_charge_w / 60.0
            self.battery_wh += charge_energy_wh
        else:
            discharge_energy_wh = power_w / 60.0
            self.battery_wh -= discharge_energy_wh

        self.battery_wh = _clamp(self.battery_wh, 0.0, self.capacity_wh)
        return self.snapshot(
            timestamp_local=timestamp_local,
            grid_available=grid_available,
            total_power_draw_w=power_w,
            charge_energy_wh=charge_energy_wh,
            discharge_energy_wh=discharge_energy_wh,
        )

    def snapshot(
        self,
        timestamp_local: datetime | None = None,
        grid_available: bool = True,
        total_power_draw_w: float = 0.0,
        charge_energy_wh: float = 0.0,
        discharge_energy_wh: float = 0.0,
    ) -> SyntheticBatterySnapshot:
        return SyntheticBatterySnapshot(
            timestamp_local=timestamp_local,
            grid_available=grid_available,
            total_power_draw_w=total_power_draw_w,
            battery_wh=self.battery_wh,
            soc_percent=self.soc_percent,
            charge_energy_wh=charge_energy_wh,
            discharge_energy_wh=discharge_energy_wh,
        )


def _finite_float(value: float, name: str) -> float:
    numeric_value = float(value)
    if not isfinite(numeric_value):
        raise ValueError(f"{name} must be finite")
    return numeric_value


def _positive_finite(value: float, name: str) -> float:
    numeric_value = _finite_float(value, name)
    if numeric_value <= 0.0:
        raise ValueError(f"{name} must be greater than zero")
    return numeric_value


def _non_negative_finite(value: float, name: str) -> float:
    numeric_value = _finite_float(value, name)
    if numeric_value < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return numeric_value


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
