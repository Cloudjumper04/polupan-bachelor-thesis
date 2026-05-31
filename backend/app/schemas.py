from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class StationConfig(BaseModel):
    id: str
    name: str
    description: str
    solar: SolarConfig
    grid: GridConfig = Field(default_factory=GridConfig)
    battery: BatteryConfig


class AppConfig(BaseModel):
    station: StationConfig
