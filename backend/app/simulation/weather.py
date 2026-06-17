from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from math import isnan
from random import Random
from typing import Any
from zoneinfo import ZoneInfo

import requests

from app.storage.simulated_solar_repository import SimulatedSolarProduction
from app.storage.solar_repository import IdealSolarProduction
from app.storage.weather_repository import WeatherObservation


LOGGER = logging.getLogger(__name__)

OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_CANONICAL_TIMEZONE = "UTC"
OPEN_METEO_SOURCE = "open-meteo-archive"
OPEN_METEO_FORECAST_SOURCE = "open_meteo_forecast"

HOURLY_VARIABLES = [
    "temperature_2m",
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
    temperature_c: float | None = None
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
    temperature_c: float | None = None
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

    station_timezone = ZoneInfo(timezone)
    start_utc, end_utc = _local_date_range_to_utc_bounds(
        start_date,
        end_date,
        station_timezone,
    )
    request_start_date, request_end_date = _utc_request_dates_for_local_range(
        start_utc,
        end_utc,
    )
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": request_start_date.isoformat(),
        "end_date": request_end_date.isoformat(),
        "hourly": ",".join(HOURLY_VARIABLES),
        "timezone": OPEN_METEO_CANONICAL_TIMEZONE,
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

    observations = _parse_open_meteo_hourly_response(
        payload,
        timezone,
        provider_timezone_name=OPEN_METEO_CANONICAL_TIMEZONE,
    )
    return _filter_observations_by_utc_range(observations, start_utc, end_utc)


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

    station_timezone = ZoneInfo(timezone)
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": ",".join(HOURLY_VARIABLES),
        "timezone": OPEN_METEO_CANONICAL_TIMEZONE,
    }
    if start_date is not None and end_date is not None:
        start_utc, end_utc = _local_date_range_to_utc_bounds(
            start_date,
            end_date,
            station_timezone,
        )
        request_start_date, request_end_date = _utc_request_dates_for_local_range(
            start_utc,
            end_utc,
        )
        params["start_date"] = request_start_date.isoformat()
        params["end_date"] = request_end_date.isoformat()
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

    forecasts = _parse_open_meteo_forecast_response(
        payload,
        timezone,
        provider_timezone_name=OPEN_METEO_CANONICAL_TIMEZONE,
    )
    if start_utc is None or end_utc is None:
        return forecasts
    return _filter_forecasts_by_utc_range(forecasts, start_utc, end_utc)


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
    provider_timezone_name: str | None = None,
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
    provider_timezone = ZoneInfo(provider_timezone_name or timezone_name)
    parsed_timestamps = _parse_open_meteo_timestamps(
        times,
        provider_timezone,
        "Open-Meteo historical weather",
    )
    observations: list[WeatherObservationData] = []
    for index, provider_timestamp in enumerate(parsed_timestamps):
        timestamp_utc = provider_timestamp.astimezone(timezone.utc)
        timestamp_local = timestamp_utc.astimezone(station_timezone)
        observations.append(
            WeatherObservationData(
                timestamp_utc=timestamp_utc,
                timestamp_local=timestamp_local,
                weather_code=_optional_int(values_by_variable["weather_code"][index]),
                temperature_c=_optional_float(
                    values_by_variable["temperature_2m"][index]
                ),
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
    provider_timezone_name: str | None = None,
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
    provider_timezone = ZoneInfo(provider_timezone_name or timezone_name)
    parsed_timestamps = _parse_open_meteo_timestamps(
        times,
        provider_timezone,
        "Open-Meteo forecast weather",
    )
    forecasts: list[WeatherForecastData] = []
    for index, provider_timestamp in enumerate(parsed_timestamps):
        forecast_timestamp_utc = provider_timestamp.astimezone(timezone.utc)
        forecast_timestamp_local = forecast_timestamp_utc.astimezone(station_timezone)
        forecasts.append(
            WeatherForecastData(
                forecast_timestamp_utc=forecast_timestamp_utc,
                forecast_timestamp_local=forecast_timestamp_local,
                weather_code=_optional_int(values_by_variable["weather_code"][index]),
                temperature_c=_optional_float(
                    values_by_variable["temperature_2m"][index]
                ),
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


def _local_date_range_to_utc_bounds(
    start_date: date,
    end_date: date,
    station_timezone: ZoneInfo,
) -> tuple[datetime, datetime]:
    start_local = datetime.combine(start_date, datetime.min.time(), tzinfo=station_timezone)
    end_local = datetime.combine(
        end_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=station_timezone,
    )
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _utc_request_dates_for_local_range(
    start_utc: datetime,
    end_utc: datetime,
) -> tuple[date, date]:
    if end_utc <= start_utc:
        raise ValueError("end_utc must be later than start_utc")
    last_requested_utc = end_utc - timedelta(seconds=1)
    return start_utc.date(), last_requested_utc.date()


def _filter_observations_by_utc_range(
    observations: list[WeatherObservationData],
    start_utc: datetime,
    end_utc: datetime,
) -> list[WeatherObservationData]:
    return [
        observation
        for observation in observations
        if start_utc <= observation.timestamp_utc < end_utc
    ]


def _filter_forecasts_by_utc_range(
    forecasts: list[WeatherForecastData],
    start_utc: datetime,
    end_utc: datetime,
) -> list[WeatherForecastData]:
    return [
        forecast
        for forecast in forecasts
        if start_utc <= forecast.forecast_timestamp_utc < end_utc
    ]


def _parse_open_meteo_timestamps(
    values: list[Any],
    station_timezone: ZoneInfo,
    label: str,
) -> list[datetime]:
    parsed_timestamps: list[datetime] = []
    naive_occurrences: dict[datetime, int] = {}
    utc_indexes: dict[datetime, int] = {}

    for index, value in enumerate(values):
        parsed = _parse_open_meteo_iso_datetime(value, label, index)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            occurrence = naive_occurrences.get(parsed, 0)
            timestamp_local = _resolve_naive_open_meteo_timestamp(
                parsed,
                station_timezone,
                occurrence,
                label,
                index,
            )
            naive_occurrences[parsed] = occurrence + 1
        else:
            timestamp_local = parsed.astimezone(station_timezone)

        timestamp_utc = timestamp_local.astimezone(timezone.utc)
        existing_index = utc_indexes.get(timestamp_utc)
        if existing_index is not None:
            raise RuntimeError(
                f"{label} returned duplicate UTC timestamp "
                f"{timestamp_utc.isoformat()} at indexes {existing_index} and {index}"
            )
        utc_indexes[timestamp_utc] = index
        parsed_timestamps.append(timestamp_local)

    return parsed_timestamps


def _parse_open_meteo_iso_datetime(
    value: Any,
    label: str,
    index: int,
) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError(
            f"{label} returned non-string hourly timestamp at index {index}"
        )
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(
            f"{label} returned invalid hourly timestamp at index {index}: {value}"
        ) from exc


def _resolve_naive_open_meteo_timestamp(
    parsed: datetime,
    station_timezone: ZoneInfo,
    occurrence: int,
    label: str,
    index: int,
) -> datetime:
    if occurrence == 0:
        return parsed.replace(tzinfo=station_timezone, fold=0)
    if occurrence > 1:
        raise RuntimeError(
            f"{label} returned more than two rows for local timestamp "
            f"{parsed.isoformat()} in {station_timezone.key}"
        )

    first_fold = parsed.replace(tzinfo=station_timezone, fold=0)
    second_fold = parsed.replace(tzinfo=station_timezone, fold=1)
    first_utc = first_fold.astimezone(timezone.utc)
    second_utc = second_fold.astimezone(timezone.utc)
    if first_utc == second_utc:
        raise RuntimeError(
            f"{label} returned duplicate non-ambiguous local timestamp "
            f"{parsed.isoformat()} in {station_timezone.key} at index {index}"
        )

    LOGGER.warning(
        "%s returned duplicate ambiguous local timestamp %s in %s; "
        "assigned fold=1 to occurrence 2 so UTC becomes %s",
        label,
        parsed.isoformat(),
        station_timezone.key,
        second_utc.isoformat(),
    )
    return second_fold


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
