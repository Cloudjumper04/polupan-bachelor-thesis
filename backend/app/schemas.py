from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SolarPanelType(BaseModel):
    id: str
    name: str
    nominal_voltage_v: float = Field(gt=0)
    max_power_w: float = Field(gt=0)


class SolarSeriesConnection(BaseModel):
    id: str
    panel_type_id: str
    panels_in_series: int = Field(ge=1)


class SolarArrayConfig(BaseModel):
    series_connections: list[SolarSeriesConnection]


class SolarInstallationConfig(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str
    azimuth_deg: float = Field(ge=0, le=360)
    tilt_deg: float = Field(ge=0, le=90)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Invalid timezone '{value}'") from exc
        return value


class SolarConfig(BaseModel):
    panel_types: list[SolarPanelType]
    array: SolarArrayConfig
    installation: SolarInstallationConfig


class GridConfig(BaseModel):
    base_delivery_health_percent: float = Field(default=130.0, ge=0, le=200)
    base_generation_health_percent: float = Field(default=130.0, ge=0, le=200)
    regeneration_cap_percent: float = Field(default=150.0, ge=0, le=200)
    minimum_health_percent: float = Field(default=0.0, ge=0, le=200)
    outage_queue: str = "3.1"
    outage_schedule_seed: int = 20260513
    local_timezone: str = "Europe/Kyiv"

    @field_validator("local_timezone")
    @classmethod
    def validate_local_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Invalid timezone '{value}'") from exc
        return value


class EconomicsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grid_tariff_uah_per_kwh: float = Field(default=4.32, gt=0)


class BatteryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chemistry: Literal["lead_acid", "lifepo4", "li_ion"]
    nominal_voltage_v: int
    capacity_ah: float = Field(gt=0)
    installation_date: str

    @field_validator("nominal_voltage_v")
    @classmethod
    def validate_nominal_voltage(cls, value: int) -> int:
        if value not in {12, 24}:
            raise ValueError("Battery nominal voltage must be either 12 or 24 V")
        return value

    @field_validator("installation_date", mode="before")
    @classmethod
    def validate_installation_date(cls, value: object) -> str:
        if isinstance(value, datetime):
            raise ValueError("Battery installation date must be a date, not a datetime")
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, str):
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("Battery installation date must use YYYY-MM-DD") from exc
            return value
        raise ValueError("Battery installation date must use YYYY-MM-DD")


class EmsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal[
        "auto",
        "grid_priority",
        "solar_priority",
        "self_consumption",
        "battery_priority",
        "backup_reserve",
        "force_charge",
    ] = "auto"
    inverter_output_limit_w: float = Field(default=2000.0, gt=0)
    critical_soc_percent: float = Field(default=10.0, ge=0, le=100)
    reserve_soc_percent: float = Field(default=30.0, ge=0, le=100)
    normal_target_soc_percent: float = Field(default=80.0, ge=0, le=100)
    backup_target_soc_percent: float = Field(default=100.0, ge=0, le=100)
    cheap_tariff_start: str = "23:00"
    cheap_tariff_end: str = "07:00"
    cheap_tariff_price_factor: float = Field(default=0.5, gt=0)
    allow_grid_charging: bool = True
    recent_outage_recovery_minutes: int = Field(default=60, ge=0)

    @field_validator("cheap_tariff_start", "cheap_tariff_end")
    @classmethod
    def validate_clock_time(cls, value: str) -> str:
        parts = str(value).split(":")
        if len(parts) != 2:
            raise ValueError("EMS tariff time must use HH:MM")
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except ValueError as exc:
            raise ValueError("EMS tariff time must use HH:MM") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("EMS tariff time must use HH:MM")
        return f"{hour:02d}:{minute:02d}"

    @model_validator(mode="after")
    def validate_soc_thresholds(self) -> "EmsConfig":
        if not (
            self.critical_soc_percent
            < self.reserve_soc_percent
            < self.normal_target_soc_percent
            <= self.backup_target_soc_percent
        ):
            raise ValueError(
                "EMS SoC thresholds must satisfy critical < reserve < normal <= backup"
            )
        return self


class StationConfig(BaseModel):
    id: str
    name: str
    description: str
    installation_date: str
    solar: SolarConfig
    grid: GridConfig = Field(default_factory=GridConfig)
    economics: EconomicsConfig = Field(default_factory=EconomicsConfig)
    battery: BatteryConfig
    ems: EmsConfig = Field(default_factory=EmsConfig)

    @field_validator("installation_date", mode="before")
    @classmethod
    def validate_installation_date(cls, value: object) -> str:
        if isinstance(value, datetime):
            raise ValueError("Station installation date must be a date, not a datetime")
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, str):
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("Station installation date must use YYYY-MM-DD") from exc
            return value
        raise ValueError("Station installation date must use YYYY-MM-DD")


class AppConfig(BaseModel):
    station: StationConfig
