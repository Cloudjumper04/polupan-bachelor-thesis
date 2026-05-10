from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.simulation.solar_interpolation import (
    apply_interpolation_variation,
    calculate_deterministic_variation_factor,
    interpolate_power,
)


def test_interpolate_power_boundary_lower() -> None:
    lower = datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc)
    upper = lower + timedelta(minutes=15)

    baseline, ratio = interpolate_power(lower, lower, 100.0, upper, 200.0)

    assert baseline == 100.0
    assert ratio == 0.0


def test_interpolate_power_boundary_upper() -> None:
    lower = datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc)
    upper = lower + timedelta(minutes=15)

    baseline, ratio = interpolate_power(upper, lower, 100.0, upper, 200.0)

    assert baseline == 200.0
    assert ratio == 1.0


def test_interpolate_power_midpoint() -> None:
    lower = datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc)
    upper = lower + timedelta(minutes=15)
    midpoint = lower + timedelta(minutes=7, seconds=30)

    baseline, ratio = interpolate_power(midpoint, lower, 100.0, upper, 200.0)

    assert baseline == 150.0
    assert ratio == 0.5


def test_interpolate_power_rejects_out_of_range_timestamp() -> None:
    lower = datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc)
    upper = lower + timedelta(minutes=15)

    with pytest.raises(ValueError, match="within the source timestamp bounds"):
        interpolate_power(
            lower - timedelta(seconds=1),
            lower,
            100.0,
            upper,
            200.0,
        )


def test_deterministic_variation_returns_same_value_for_same_inputs() -> None:
    timestamp = datetime(2026, 5, 10, 9, 3, 12, tzinfo=timezone.utc)

    first = calculate_deterministic_variation_factor(
        timestamp,
        "station",
        "hash",
        "forecast",
        100.0,
        120.0,
        1,
        weather_state="partly_cloudy",
    )
    second = calculate_deterministic_variation_factor(
        timestamp,
        "station",
        "hash",
        "forecast",
        100.0,
        120.0,
        1,
        weather_state="partly_cloudy",
    )

    assert first == second


def test_deterministic_variation_changes_smoothly_second_to_second() -> None:
    timestamp = datetime(2026, 5, 10, 9, 3, 12, tzinfo=timezone.utc)
    factors = [
        calculate_deterministic_variation_factor(
            timestamp + timedelta(seconds=offset),
            "station",
            "hash",
            "forecast",
            100.0,
            120.0,
            1,
            weather_state="partly_cloudy",
        )
        for offset in range(10)
    ]

    assert max(
        abs(current - previous)
        for previous, current in zip(factors, factors[1:])
    ) < 0.05


def test_clear_variation_is_smaller_than_partly_cloudy_variation() -> None:
    timestamp = datetime(2026, 5, 10, 9, 3, 12, tzinfo=timezone.utc)
    clear = [
        calculate_deterministic_variation_factor(
            timestamp + timedelta(seconds=offset),
            "station",
            "hash",
            "forecast",
            100.0,
            120.0,
            1,
            weather_state="clear",
        )
        for offset in range(0, 300, 10)
    ]
    partly_cloudy = [
        calculate_deterministic_variation_factor(
            timestamp + timedelta(seconds=offset),
            "station",
            "hash",
            "forecast",
            100.0,
            120.0,
            1,
            weather_state="partly_cloudy",
        )
        for offset in range(0, 300, 10)
    ]

    assert max(clear) - min(clear) < max(partly_cloudy) - min(partly_cloudy)


def test_damped_weather_variation_is_smaller_than_partly_cloudy() -> None:
    timestamp = datetime(2026, 5, 10, 9, 3, 12, tzinfo=timezone.utc)
    rain = [
        calculate_deterministic_variation_factor(
            timestamp + timedelta(seconds=offset),
            "station",
            "hash",
            "forecast",
            80.0,
            90.0,
            1,
            weather_state="rain",
        )
        for offset in range(0, 600, 30)
    ]
    partly_cloudy = [
        calculate_deterministic_variation_factor(
            timestamp + timedelta(seconds=offset),
            "station",
            "hash",
            "forecast",
            80.0,
            90.0,
            1,
            weather_state="partly_cloudy",
        )
        for offset in range(0, 600, 30)
    ]

    assert max(rain) - min(rain) < max(partly_cloudy) - min(partly_cloudy)
    assert max(rain) <= 1.04


def test_apply_interpolation_variation_keeps_power_within_nearby_bounds() -> None:
    power = apply_interpolation_variation(
        baseline_power_w=100.0,
        variation_factor=1.5,
        lower_power_w=100.0,
        upper_power_w=120.0,
        weather_state="partly_cloudy",
    )

    assert power <= 120.0 * 1.15
