from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from math import isnan
from random import Random
from typing import Any
from zoneinfo import ZoneInfo

import requests

from app.storage.simulated_solar_repository import SimulatedSolarProduction
from app.storage.solar_repository import IdealSolarProduction
from app.storage.weather_repository import WeatherObservation


OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_SOURCE = "open-meteo-archive"
OPEN_METEO_FORECAST_SOURCE = "open_meteo_forecast"

HOURLY_VARIABLES = [
    "cloud_cover",
    "precipitation",
    "rain",
    "snowfall",
    "weather_code",
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
]

WEATHER_STATE_RANGES: dict[str, tuple[float, float]] = {
    "clear": (0.85, 1.00),
    "partly_cloudy": (0.55, 0.85),
    "cloudy": (0.25, 0.55),
    "fog": (0.10, 0.35),
    "drizzle": (0.20, 0.50),
    "rain": (0.10, 0.40),
    "snow": (0.05, 0.30),
    "thunderstorm": (0.05, 0.25),
    "unknown": (0.50, 0.80),
}

PRECIPITATION_STATES = {"fog", "drizzle", "rain", "snow", "thunderstorm"}
CLOUD_COVER_STATES = {"clear", "partly_cloudy", "cloudy"}


@dataclass(frozen=True)
class WeatherObservationData:
    timestamp_utc: datetime
    timestamp_local: datetime
    weather_code: int | None
    cloud_cover_percent: float | None
    precipitation_mm: float | None
    rain_mm: float | None
    snowfall_cm: float | None
    shortwave_radiation_w_m2: float | None
    direct_radiation_w_m2: float | None
    diffuse_radiation_w_m2: float | None
    source: str = OPEN_METEO_SOURCE


@dataclass(frozen=True)
class WeatherForecastData:
    forecast_timestamp_utc: datetime
    forecast_timestamp_local: datetime
    weather_code: int | None
    cloud_cover_percent: float | None
    precipitation_mm: float | None
    rain_mm: float | None
    snowfall_cm: float | None
    shortwave_radiation_w_m2: float | None
    direct_radiation_w_m2: float | None
    diffuse_radiation_w_m2: float | None
    source: str = OPEN_METEO_FORECAST_SOURCE
    resolution_minutes: int = 60


def map_weather_code_to_state(weather_code: int | None) -> str:
    if weather_code == 0:
        return "clear"
    if weather_code in {1, 2}:
        return "partly_cloudy"
    if weather_code == 3:
        return "cloudy"
    if weather_code in {45, 48}:
        return "fog"
    if weather_code in {51, 53, 55, 56, 57}:
        return "drizzle"
    if weather_code in {61, 63, 65, 66, 67, 80, 81, 82}:
        return "rain"
    if weather_code in {71, 73, 75, 77, 85, 86}:
        return "snow"
    if weather_code in {95, 96, 99}:
        return "thunderstorm"
    return "unknown"


def calculate_weather_factor(
    weather_code: int | None,
    cloud_cover_percent: float | None,
    timestamp_key: str,
    seed: int = 42,
) -> float:
    weather_state = map_weather_code_to_state(weather_code)
    rng = _build_deterministic_rng(
        seed=seed,
        timestamp_key=timestamp_key,
        weather_code=weather_code,
        cloud_cover_percent=cloud_cover_percent,
    )

    if weather_state in PRECIPITATION_STATES:
        return _clamp(_random_in_range(rng, WEATHER_STATE_RANGES[weather_state]), 0.05, 1.0)

    if weather_state in CLOUD_COVER_STATES and cloud_cover_percent is not None:
        cloud_probability = _clamp(float(cloud_cover_percent) / 100.0, 0.0, 1.0)
        draw = rng.random()
        if draw < cloud_probability * 0.9:
            selected_range = WEATHER_STATE_RANGES["cloudy"]
        elif draw < cloud_probability:
            selected_range = (0.55, 0.75)
        else:
            selected_range = WEATHER_STATE_RANGES["clear"]
        return _clamp(_random_in_range(rng, selected_range), 0.05, 1.0)

    return _clamp(_random_in_range(rng, WEATHER_STATE_RANGES[weather_state]), 0.05, 1.0)


def fetch_open_meteo_historical_weather(
    latitude: float,
    longitude: float,
    timezone: str,
    start_date: date,
    end_date: date,
) -> list[WeatherObservationData]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "hourly": ",".join(HOURLY_VARIABLES),
        "timezone": timezone,
    }
    try:
        response = requests.get(OPEN_METEO_ARCHIVE_URL, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch Open-Meteo historical weather: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError("Open-Meteo returned invalid JSON") from exc

    if payload.get("error"):
        reason = payload.get("reason", "unknown API error")
        raise RuntimeError(f"Open-Meteo historical weather error: {reason}")

    return _parse_open_meteo_hourly_response(payload, timezone)


def fetch_open_meteo_forecast(
    latitude: float,
    longitude: float,
    timezone: str,
    forecast_hours: int | None = 48,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[WeatherForecastData]:
    if (start_date is None) != (end_date is None):
        raise ValueError("start_date and end_date must be provided together")
    if start_date is not None and end_date is not None and end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if start_date is None and (forecast_hours is None or forecast_hours <= 0):
        raise ValueError("forecast_hours must be greater than 0")

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": ",".join(HOURLY_VARIABLES),
        "timezone": timezone,
    }
    if start_date is not None and end_date is not None:
        params["start_date"] = start_date.isoformat()
        params["end_date"] = end_date.isoformat()
    else:
        params["forecast_hours"] = forecast_hours
    try:
        response = requests.get(OPEN_METEO_FORECAST_URL, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch Open-Meteo forecast weather: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError("Open-Meteo returned invalid JSON") from exc

    if payload.get("error"):
        reason = payload.get("reason", "unknown API error")
        raise RuntimeError(f"Open-Meteo forecast weather error: {reason}")

    return _parse_open_meteo_forecast_response(payload, timezone)


def generate_weather_adjusted_solar(
    ideal_points: list[IdealSolarProduction],
    weather_observations: list[WeatherObservation],
    seed: int = 42,
) -> list[SimulatedSolarProduction]:
    if not ideal_points:
        return []
    if not weather_observations:
        raise ValueError("weather_observations must not be empty")

    sorted_ideal_points = sorted(ideal_points, key=lambda point: point.timestamp_utc)
    sorted_weather = sorted(
        weather_observations,
        key=lambda observation: observation.timestamp_utc,
    )

    weather_index = 0
    simulated_points: list[SimulatedSolarProduction] = []
    for ideal_point in sorted_ideal_points:
        while (
            weather_index + 1 < len(sorted_weather)
            and sorted_weather[weather_index + 1].timestamp_utc
            <= ideal_point.timestamp_utc
        ):
            weather_index += 1

        weather = sorted_weather[weather_index]
        if weather.timestamp_utc > ideal_point.timestamp_utc:
            raise ValueError(
                "weather observations do not cover the first ideal solar timestamp"
            )

        timestamp_key = ideal_point.timestamp_utc.isoformat()
        weather_factor = calculate_weather_factor(
            weather_code=weather.weather_code,
            cloud_cover_percent=weather.cloud_cover_percent,
            timestamp_key=timestamp_key,
            seed=seed,
        )
        simulated_power_w = (
            0.0
            if ideal_point.ideal_power_w <= 0
            else ideal_point.ideal_power_w * weather_factor
        )
        weather_state = map_weather_code_to_state(weather.weather_code)

        simulated_points.append(
            SimulatedSolarProduction(
                station_id=ideal_point.station_id,
                config_hash=ideal_point.config_hash,
                timestamp_utc=ideal_point.timestamp_utc,
                timestamp_local=ideal_point.timestamp_local,
                ideal_power_w=ideal_point.ideal_power_w,
                weather_code=weather.weather_code,
                weather_state=weather_state,
                cloud_cover_percent=weather.cloud_cover_percent,
                weather_factor=weather_factor,
                simulated_power_w=simulated_power_w,
            )
        )

    return simulated_points


def _parse_open_meteo_hourly_response(
    payload: dict[str, Any],
    timezone_name: str,
) -> list[WeatherObservationData]:
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise RuntimeError("Open-Meteo response is missing hourly data")

    times = hourly.get("time")
    if not isinstance(times, list):
        raise RuntimeError("Open-Meteo response is missing hourly time data")

    values_by_variable: dict[str, list[Any]] = {}
    for variable in HOURLY_VARIABLES:
        values = hourly.get(variable)
        if not isinstance(values, list):
            raise RuntimeError(f"Open-Meteo response is missing hourly {variable} data")
        if len(values) != len(times):
            raise RuntimeError(
                f"Open-Meteo hourly {variable} length does not match time length"
            )
        values_by_variable[variable] = values

    station_timezone = ZoneInfo(timezone_name)
    observations: list[WeatherObservationData] = []
    for index, timestamp_value in enumerate(times):
        timestamp_local = _parse_open_meteo_timestamp(
            timestamp_value,
            station_timezone,
        )
        observations.append(
            WeatherObservationData(
                timestamp_utc=timestamp_local.astimezone(timezone.utc),
                timestamp_local=timestamp_local,
                weather_code=_optional_int(values_by_variable["weather_code"][index]),
                cloud_cover_percent=_optional_float(
                    values_by_variable["cloud_cover"][index]
                ),
                precipitation_mm=_optional_float(
                    values_by_variable["precipitation"][index]
                ),
                rain_mm=_optional_float(values_by_variable["rain"][index]),
                snowfall_cm=_optional_float(values_by_variable["snowfall"][index]),
                shortwave_radiation_w_m2=_optional_float(
                    values_by_variable["shortwave_radiation"][index]
                ),
                direct_radiation_w_m2=_optional_float(
                    values_by_variable["direct_radiation"][index]
                ),
                diffuse_radiation_w_m2=_optional_float(
                    values_by_variable["diffuse_radiation"][index]
                ),
            )
        )

    return observations


def _parse_open_meteo_forecast_response(
    payload: dict[str, Any],
    timezone_name: str,
) -> list[WeatherForecastData]:
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise RuntimeError("Open-Meteo response is missing hourly data")

    times = hourly.get("time")
    if not isinstance(times, list):
        raise RuntimeError("Open-Meteo response is missing hourly time data")

    values_by_variable: dict[str, list[Any]] = {}
    for variable in HOURLY_VARIABLES:
        values = hourly.get(variable)
        if not isinstance(values, list):
            raise RuntimeError(f"Open-Meteo response is missing hourly {variable} data")
        if len(values) != len(times):
            raise RuntimeError(
                f"Open-Meteo hourly {variable} length does not match time length"
            )
        values_by_variable[variable] = values

    station_timezone = ZoneInfo(timezone_name)
    forecasts: list[WeatherForecastData] = []
    for index, timestamp_value in enumerate(times):
        forecast_timestamp_local = _parse_open_meteo_timestamp(
            timestamp_value,
            station_timezone,
        )
        forecasts.append(
            WeatherForecastData(
                forecast_timestamp_utc=forecast_timestamp_local.astimezone(
                    timezone.utc
                ),
                forecast_timestamp_local=forecast_timestamp_local,
                weather_code=_optional_int(values_by_variable["weather_code"][index]),
                cloud_cover_percent=_optional_float(
                    values_by_variable["cloud_cover"][index]
                ),
                precipitation_mm=_optional_float(
                    values_by_variable["precipitation"][index]
                ),
                rain_mm=_optional_float(values_by_variable["rain"][index]),
                snowfall_cm=_optional_float(values_by_variable["snowfall"][index]),
                shortwave_radiation_w_m2=_optional_float(
                    values_by_variable["shortwave_radiation"][index]
                ),
                direct_radiation_w_m2=_optional_float(
                    values_by_variable["direct_radiation"][index]
                ),
                diffuse_radiation_w_m2=_optional_float(
                    values_by_variable["diffuse_radiation"][index]
                ),
            )
        )

    return forecasts


def _parse_open_meteo_timestamp(
    value: str,
    station_timezone: ZoneInfo,
) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=station_timezone)
    return parsed.astimezone(station_timezone)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    if isnan(parsed):
        return None
    return parsed


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _build_deterministic_rng(
    seed: int,
    timestamp_key: str,
    weather_code: int | None,
    cloud_cover_percent: float | None,
) -> Random:
    cloud_value = (
        "None"
        if cloud_cover_percent is None
        else f"{float(cloud_cover_percent):.3f}"
    )
    key = f"{seed}|{timestamp_key}|{weather_code}|{cloud_value}"
    digest = sha256(key.encode("utf-8")).hexdigest()
    return Random(int(digest[:16], 16))


def _random_in_range(rng: Random, value_range: tuple[float, float]) -> float:
    minimum, maximum = value_range
    return rng.uniform(minimum, maximum)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
