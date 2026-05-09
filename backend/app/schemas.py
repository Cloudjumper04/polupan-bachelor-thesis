from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator


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


class StationConfig(BaseModel):
    id: str
    name: str
    description: str
    solar: SolarConfig


class AppConfig(BaseModel):
    station: StationConfig
