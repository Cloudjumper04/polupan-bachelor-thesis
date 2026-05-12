from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import cos, isfinite, radians, sin
from zoneinfo import ZoneInfo

import pandas as pd
import pvlib

from app.schemas import AppConfig, SolarPanelType, SolarSeriesConnection


@dataclass(frozen=True)
class IdealSolarPoint:
    timestamp_utc: datetime
    timestamp_local: datetime
    sun_elevation_deg: float
    sun_azimuth_deg: float
    incidence_factor: float
    ambient_factor: float
    direct_power_w: float
    ambient_power_w: float
    ideal_power_w: float


@dataclass(frozen=True)
class PVArrayOperatingPoint:
    voltage_v: float
    current_a: float


AXIOMA_200W_OPEN_CIRCUIT_VOLTAGE_V = 35.4
AXIOMA_200W_MAX_POWER_VOLTAGE_V = 33.0
DEFAULT_MAX_POWER_VOLTAGE_RATIO = 0.94


def estimate_pv_array_operating_point(
    power_w: float,
    config: AppConfig,
) -> PVArrayOperatingPoint:
    """Derive display-only PV voltage/current from simulated array power."""
    panel_types = {panel.id: panel for panel in config.station.solar.panel_types}
    connection_specs: list[tuple[float, float, float]] = []
    for connection in config.station.solar.array.series_connections:
        panel = panel_types[connection.panel_type_id]
        series_count = connection.panels_in_series
        connection_specs.append(
            (
                panel.max_power_w * series_count,
                _panel_max_power_voltage_v(panel) * series_count,
                _panel_open_circuit_voltage_v(panel) * series_count,
            )
        )

    array_max_power_w = sum(max_power_w for max_power_w, _, _ in connection_specs)
    try:
        numeric_power_w = float(power_w)
    except (TypeError, ValueError):
        return PVArrayOperatingPoint(voltage_v=0.0, current_a=0.0)
    if not isfinite(numeric_power_w) or array_max_power_w <= 0:
        return PVArrayOperatingPoint(voltage_v=0.0, current_a=0.0)

    clamped_power_w = _clamp(numeric_power_w, 0.0, array_max_power_w)
    if clamped_power_w <= 1.0:
        return PVArrayOperatingPoint(voltage_v=0.0, current_a=0.0)

    power_ratio = clamped_power_w / array_max_power_w
    array_vmpp_v = _weighted_connection_voltage(connection_specs, voltage_index=1)
    array_voc_v = _weighted_connection_voltage(connection_specs, voltage_index=2)
    voltage_factor = 0.80 + 0.20 * min(1.0, power_ratio / 0.20)
    high_power_derate = 1.0 - 0.015 * max(0.0, power_ratio - 0.80) / 0.20
    array_voltage_v = array_vmpp_v * voltage_factor * high_power_derate
    array_voltage_v = _clamp(array_voltage_v, 0.0, array_voc_v * 0.98)
    if array_voltage_v <= 0:
        return PVArrayOperatingPoint(voltage_v=0.0, current_a=0.0)

    return PVArrayOperatingPoint(
        voltage_v=array_voltage_v,
        current_a=clamped_power_w / array_voltage_v,
    )


def calculate_incidence_factor(
    sun_elevation_deg: float,
    sun_azimuth_deg: float,
    panel_tilt_deg: float,
    panel_azimuth_deg: float,
) -> float:
    if sun_elevation_deg <= 0:
        return 0.0

    elevation_rad = radians(sun_elevation_deg)
    sun_azimuth_rad = radians(sun_azimuth_deg)
    panel_tilt_rad = radians(panel_tilt_deg)
    panel_azimuth_rad = radians(panel_azimuth_deg)

    sun_x = cos(elevation_rad) * sin(sun_azimuth_rad)
    sun_y = cos(elevation_rad) * cos(sun_azimuth_rad)
    sun_z = sin(elevation_rad)

    panel_x = sin(panel_tilt_rad) * sin(panel_azimuth_rad)
    panel_y = sin(panel_tilt_rad) * cos(panel_azimuth_rad)
    panel_z = cos(panel_tilt_rad)

    incidence_factor = sun_x * panel_x + sun_y * panel_y + sun_z * panel_z
    return _clamp(incidence_factor, 0.0, 1.0)


class IdealSolarGenerator:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.station_timezone = ZoneInfo(config.station.solar.installation.timezone)
        self._panel_types = {
            panel.id: panel for panel in config.station.solar.panel_types
        }

    @property
    def total_installed_power_w(self) -> float:
        return sum(
            self._connection_power_w(connection)
            for connection in self.config.station.solar.array.series_connections
        )

    def calculate_string_voltages(self) -> dict[str, float]:
        return {
            connection.id: self._connection_voltage_v(connection)
            for connection in self.config.station.solar.array.series_connections
        }

    def generate(
        self,
        start: datetime,
        end: datetime,
        timestep_minutes: int,
    ) -> list[IdealSolarPoint]:
        if timestep_minutes <= 0:
            raise ValueError("timestep_minutes must be greater than 0")
        start_local = _as_station_time(start, self.station_timezone)
        end_local = _as_station_time(end, self.station_timezone)
        if end_local <= start_local:
            raise ValueError("end must be later than start")

        timestamps = pd.date_range(
            start=start_local,
            end=end_local,
            freq=f"{timestep_minutes}min",
            inclusive="left",
        )
        if len(timestamps) == 0:
            return []

        installation = self.config.station.solar.installation
        solar_position = pvlib.solarposition.get_solarposition(
            time=timestamps,
            latitude=installation.latitude,
            longitude=installation.longitude,
        )

        points: list[IdealSolarPoint] = []
        total_power_w = self.total_installed_power_w
        for timestamp, position in solar_position.iterrows():
            elevation = float(position["apparent_elevation"])
            azimuth = float(position["azimuth"])
            incidence_factor = calculate_incidence_factor(
                sun_elevation_deg=elevation,
                sun_azimuth_deg=azimuth,
                panel_tilt_deg=installation.tilt_deg,
                panel_azimuth_deg=installation.azimuth_deg,
            )
            ambient_factor = _calculate_ambient_factor(elevation)
            direct_power_w = total_power_w * incidence_factor
            ambient_power_w = total_power_w * ambient_factor
            ideal_power_w = min(total_power_w, direct_power_w + ambient_power_w)

            if elevation <= 0:
                direct_power_w = 0.0
                ambient_power_w = 0.0
                ideal_power_w = 0.0

            timestamp_local = timestamp.to_pydatetime()
            timestamp_utc = timestamp.tz_convert(timezone.utc).to_pydatetime()
            points.append(
                IdealSolarPoint(
                    timestamp_utc=timestamp_utc,
                    timestamp_local=timestamp_local,
                    sun_elevation_deg=elevation,
                    sun_azimuth_deg=azimuth,
                    incidence_factor=incidence_factor,
                    ambient_factor=ambient_factor,
                    direct_power_w=direct_power_w,
                    ambient_power_w=ambient_power_w,
                    ideal_power_w=ideal_power_w,
                )
            )

        return points

    def _connection_power_w(self, connection: SolarSeriesConnection) -> float:
        panel = self._get_panel_type(connection.panel_type_id)
        return panel.max_power_w * connection.panels_in_series

    def _connection_voltage_v(self, connection: SolarSeriesConnection) -> float:
        panel = self._get_panel_type(connection.panel_type_id)
        return panel.nominal_voltage_v * connection.panels_in_series

    def _get_panel_type(self, panel_type_id: str) -> SolarPanelType:
        return self._panel_types[panel_type_id]


def _calculate_ambient_factor(sun_elevation_deg: float) -> float:
    if sun_elevation_deg <= 0:
        return 0.0
    normalized_elevation = _clamp(sun_elevation_deg / 90.0, 0.0, 1.0)
    return _clamp(0.03 + 0.05 * normalized_elevation, 0.0, 0.08)


def _panel_open_circuit_voltage_v(panel: SolarPanelType) -> float:
    configured_voltage = _optional_positive_float(
        getattr(panel, "open_circuit_voltage_v", None)
    )
    if configured_voltage is not None:
        return configured_voltage
    if _is_axioma_200w(panel):
        return AXIOMA_200W_OPEN_CIRCUIT_VOLTAGE_V
    return panel.nominal_voltage_v


def _panel_max_power_voltage_v(panel: SolarPanelType) -> float:
    open_circuit_voltage_v = _panel_open_circuit_voltage_v(panel)
    configured_voltage = _optional_positive_float(
        getattr(panel, "max_power_voltage_v", None)
    )
    if configured_voltage is None:
        configured_voltage = (
            AXIOMA_200W_MAX_POWER_VOLTAGE_V
            if _is_axioma_200w(panel)
            else open_circuit_voltage_v * DEFAULT_MAX_POWER_VOLTAGE_RATIO
        )
    return _clamp(configured_voltage, 0.0, open_circuit_voltage_v * 0.98)


def _weighted_connection_voltage(
    connection_specs: list[tuple[float, float, float]],
    voltage_index: int,
) -> float:
    total_power_w = sum(max_power_w for max_power_w, _, _ in connection_specs)
    if total_power_w <= 0:
        return 0.0
    return (
        sum(spec[0] * spec[voltage_index] for spec in connection_specs)
        / total_power_w
    )


def _optional_positive_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(numeric_value) or numeric_value <= 0:
        return None
    return numeric_value


def _is_axioma_200w(panel: SolarPanelType) -> bool:
    panel_id = panel.id.lower()
    panel_name = panel.name.lower()
    return (
        ("axioma" in panel_id or "axioma" in panel_name)
        and 190.0 <= panel.max_power_w <= 220.0
    )


def _as_station_time(value: datetime, station_timezone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=station_timezone)
    return value.astimezone(station_timezone)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
