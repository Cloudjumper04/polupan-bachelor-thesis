from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from math import cos, pi


DAMPED_WEATHER_STATES = {"rain", "snow", "fog", "drizzle", "thunderstorm"}


def interpolate_power(
    timestamp_utc: datetime,
    lower_timestamp_utc: datetime,
    lower_power_w: float,
    upper_timestamp_utc: datetime,
    upper_power_w: float,
) -> tuple[float, float]:
    timestamp = _as_utc(timestamp_utc)
    lower_timestamp = _as_utc(lower_timestamp_utc)
    upper_timestamp = _as_utc(upper_timestamp_utc)

    if lower_timestamp == upper_timestamp:
        if timestamp != lower_timestamp:
            raise ValueError("timestamp must equal the source timestamp when bounds match")
        return max(0.0, float(lower_power_w)), 0.0
    if timestamp < lower_timestamp or timestamp > upper_timestamp:
        raise ValueError("timestamp must be within the source timestamp bounds")

    total_seconds = (upper_timestamp - lower_timestamp).total_seconds()
    elapsed_seconds = (timestamp - lower_timestamp).total_seconds()
    ratio = elapsed_seconds / total_seconds
    baseline = float(lower_power_w) + (float(upper_power_w) - float(lower_power_w)) * ratio
    return max(0.0, baseline), ratio


def calculate_deterministic_variation_factor(
    timestamp_utc: datetime,
    station_id: str,
    config_hash: str,
    source_type: str,
    lower_power_w: float,
    upper_power_w: float,
    resolution_seconds: int,
    weather_state: str | None = None,
) -> float:
    timestamp = _as_utc(timestamp_utc)
    lower_power = max(0.0, float(lower_power_w))
    upper_power = max(0.0, float(upper_power_w))
    if lower_power <= 0.0 and upper_power <= 0.0:
        return 1.0

    profile = _variation_profile(
        weather_state,
        lower_power=lower_power,
        upper_power=upper_power,
    )
    control_seconds = max(profile["control_seconds"], resolution_seconds * 4)
    timestamp_seconds = int(timestamp.timestamp())
    lower_bucket = timestamp_seconds // control_seconds
    upper_bucket = lower_bucket + 1
    bucket_ratio = (timestamp_seconds % control_seconds) / control_seconds
    smooth_ratio = _smoothstep(bucket_ratio)

    base_key = f"{station_id}|{config_hash}|{source_type}|{weather_state}|{resolution_seconds}"
    lower_noise = _noise_value(base_key, lower_bucket)
    upper_noise = _noise_value(base_key, upper_bucket)
    noise = lower_noise + (upper_noise - lower_noise) * smooth_ratio

    # A secondary slower component prevents the curve from looking like repeated
    # identical ramps while still staying deterministic and smooth.
    slow_control_seconds = control_seconds * 4
    slow_bucket = timestamp_seconds // slow_control_seconds
    slow_ratio = (timestamp_seconds % slow_control_seconds) / slow_control_seconds
    slow_noise = _noise_value(f"{base_key}|slow", slow_bucket)
    next_slow_noise = _noise_value(f"{base_key}|slow", slow_bucket + 1)
    slow_component = slow_noise + (next_slow_noise - slow_noise) * _smoothstep(
        slow_ratio
    )

    amplitude = profile["amplitude"]
    factor = 1.0 + noise * amplitude + slow_component * amplitude * 0.35
    return _clamp(factor, profile["minimum"], profile["maximum"])


def apply_interpolation_variation(
    baseline_power_w: float,
    variation_factor: float,
    lower_power_w: float,
    upper_power_w: float,
    weather_state: str | None = None,
) -> float:
    baseline = max(0.0, float(baseline_power_w))
    if baseline <= 0.0:
        return 0.0

    lower_power = max(0.0, float(lower_power_w))
    upper_power = max(0.0, float(upper_power_w))
    nearby_max = max(lower_power, upper_power, baseline)
    weather = weather_state or "unknown"
    if weather == "partly_cloudy":
        upper_bound = nearby_max * 1.15
    elif weather == "clear":
        upper_bound = nearby_max * 1.03
    elif weather == "cloudy":
        upper_bound = nearby_max * 1.08
    elif weather in DAMPED_WEATHER_STATES:
        upper_bound = nearby_max * 1.04
    else:
        upper_bound = nearby_max * 1.10

    varied_power = baseline * variation_factor
    return _clamp(varied_power, 0.0, upper_bound)


def _variation_profile(
    weather_state: str | None,
    lower_power: float,
    upper_power: float,
) -> dict[str, float]:
    weather = weather_state or "unknown"
    if weather == "clear":
        return {
            "amplitude": 0.015,
            "minimum": 0.97,
            "maximum": 1.03,
            "control_seconds": 180.0,
        }
    if weather == "partly_cloudy":
        return {
            "amplitude": 0.18,
            "minimum": 0.72,
            "maximum": 1.15,
            "control_seconds": 45.0,
        }
    if weather == "cloudy":
        return {
            "amplitude": 0.07,
            "minimum": 0.88,
            "maximum": 1.08,
            "control_seconds": 120.0,
        }
    if weather in DAMPED_WEATHER_STATES:
        return {
            "amplitude": 0.05,
            "minimum": 0.90,
            "maximum": 1.04,
            "control_seconds": 180.0,
        }

    nearby_max = max(lower_power, upper_power, 1.0)
    slope_ratio = abs(upper_power - lower_power) / nearby_max
    return {
        "amplitude": _clamp(0.02 + 0.10 * slope_ratio, 0.02, 0.16),
        "minimum": 0.82,
        "maximum": 1.10,
        "control_seconds": 90.0,
    }


def _noise_value(base_key: str, bucket: int) -> float:
    digest = sha256(f"{base_key}|{bucket}".encode("utf-8")).hexdigest()
    raw = int(digest[:16], 16) / float(0xFFFFFFFFFFFFFFFF)
    return raw * 2.0 - 1.0


def _smoothstep(value: float) -> float:
    value = _clamp(value, 0.0, 1.0)
    cosine = (1.0 - cos(value * pi)) / 2.0
    return cosine


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime is required")
    return value.astimezone(timezone.utc)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
