from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config_loader import load_config
from app.simulation.solar import IdealSolarGenerator, calculate_incidence_factor


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "station.default.yaml"


@pytest.fixture
def generator() -> IdealSolarGenerator:
    return IdealSolarGenerator(load_config(CONFIG_PATH))


def test_total_configured_solar_power_is_400w(generator: IdealSolarGenerator) -> None:
    assert generator.total_installed_power_w == 400.0
    assert generator.calculate_string_voltages()["series_1"] == 70.0


def test_incidence_factor_returns_zero_at_night() -> None:
    assert calculate_incidence_factor(-5.0, 180.0, 35.0, 180.0) == 0.0


def test_incidence_factor_is_high_when_sun_is_perpendicular_to_panel() -> None:
    factor = calculate_incidence_factor(55.0, 180.0, 35.0, 180.0)

    assert factor == pytest.approx(1.0)


def test_incidence_factor_is_zero_when_sun_is_behind_panel() -> None:
    factor = calculate_incidence_factor(35.0, 0.0, 55.0, 180.0)

    assert factor == 0.0


@pytest.mark.parametrize(
    ("sun_elevation", "sun_azimuth", "tilt", "panel_azimuth"),
    [
        (-10.0, 0.0, 0.0, 0.0),
        (0.0, 180.0, 35.0, 180.0),
        (15.0, 90.0, 45.0, 270.0),
        (45.0, 180.0, 35.0, 180.0),
        (90.0, 123.0, 0.0, 180.0),
    ],
)
def test_incidence_factor_is_always_clamped(
    sun_elevation: float,
    sun_azimuth: float,
    tilt: float,
    panel_azimuth: float,
) -> None:
    factor = calculate_incidence_factor(
        sun_elevation,
        sun_azimuth,
        tilt,
        panel_azimuth,
    )

    assert 0.0 <= factor <= 1.0


def test_ideal_generation_produces_zero_at_night(
    generator: IdealSolarGenerator,
) -> None:
    points = generator.generate(
        datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        15,
    )

    assert points
    assert all(point.ideal_power_w == 0.0 for point in points)


def test_ideal_generation_uses_station_timezone_for_naive_inputs(
    generator: IdealSolarGenerator,
) -> None:
    points = generator.generate(
        datetime(2026, 1, 1, 0, 0),
        datetime(2026, 1, 1, 0, 15),
        15,
    )

    point = points[0]
    assert point.timestamp_local.tzinfo == ZoneInfo("Europe/Kyiv")
    assert point.timestamp_local.isoformat() == "2026-01-01T00:00:00+02:00"
    assert point.timestamp_utc.tzinfo == timezone.utc
    assert point.timestamp_utc.isoformat() == "2025-12-31T22:00:00+00:00"


def test_ideal_generation_uses_kyiv_summer_offset(
    generator: IdealSolarGenerator,
) -> None:
    points = generator.generate(
        datetime(2026, 5, 11, 0, 0),
        datetime(2026, 5, 11, 0, 15),
        15,
    )

    point = points[0]
    assert point.timestamp_local.isoformat() == "2026-05-11T00:00:00+03:00"
    assert point.timestamp_utc.isoformat() == "2026-05-10T21:00:00+00:00"


def test_ideal_generation_starts_around_local_sunrise_not_one_hour_early(
    generator: IdealSolarGenerator,
) -> None:
    station_timezone = ZoneInfo("Europe/Kyiv")
    points = generator.generate(
        datetime(2026, 5, 11, 4, 30, tzinfo=station_timezone),
        datetime(2026, 5, 11, 6, 30, tzinfo=station_timezone),
        15,
    )

    first_positive = next(point for point in points if point.ideal_power_w > 0)

    assert first_positive.timestamp_local.isoformat() == "2026-05-11T05:30:00+03:00"
    assert all(
        point.ideal_power_w == 0.0
        for point in points
        if point.timestamp_local < datetime(2026, 5, 11, 5, 30, tzinfo=station_timezone)
    )


def test_ideal_generation_handles_kyiv_dst_spring_forward_explicitly(
    generator: IdealSolarGenerator,
) -> None:
    station_timezone = ZoneInfo("Europe/Kyiv")
    points = generator.generate(
        datetime(2026, 3, 29, 0, 0, tzinfo=station_timezone),
        datetime(2026, 3, 30, 0, 0, tzinfo=station_timezone),
        15,
    )

    assert len(points) == 23 * 4
    assert points[0].timestamp_local.isoformat() == "2026-03-29T00:00:00+02:00"
    assert points[-1].timestamp_local.isoformat() == "2026-03-29T23:45:00+03:00"
    assert not any(point.timestamp_local.hour == 3 for point in points)
    assert all(
        right.timestamp_utc - left.timestamp_utc == timedelta(minutes=15)
        for left, right in zip(points, points[1:])
    )


def test_ideal_generation_handles_kyiv_dst_fall_back_explicitly(
    generator: IdealSolarGenerator,
) -> None:
    station_timezone = ZoneInfo("Europe/Kyiv")
    points = generator.generate(
        datetime(2026, 10, 25, 0, 0, tzinfo=station_timezone),
        datetime(2026, 10, 26, 0, 0, tzinfo=station_timezone),
        15,
    )
    repeated_three_oclock = [
        point
        for point in points
        if point.timestamp_local.hour == 3
    ]

    assert len(points) == 25 * 4
    assert points[0].timestamp_local.isoformat() == "2026-10-25T00:00:00+03:00"
    assert points[-1].timestamp_local.isoformat() == "2026-10-25T23:45:00+02:00"
    assert len(repeated_three_oclock) == 8
    assert {point.timestamp_local.utcoffset() for point in repeated_three_oclock} == {
        timedelta(hours=2),
        timedelta(hours=3),
    }
    assert len({point.timestamp_utc for point in points}) == len(points)
    assert all(
        right.timestamp_utc - left.timestamp_utc == timedelta(minutes=15)
        for left, right in zip(points, points[1:])
    )


def test_ideal_generation_produces_positive_power_during_daytime(
    generator: IdealSolarGenerator,
) -> None:
    points = generator.generate(
        datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        15,
    )

    assert any(point.ideal_power_w > 0 for point in points)


def test_ideal_generation_never_exceeds_total_rated_power(
    generator: IdealSolarGenerator,
) -> None:
    points = generator.generate(
        datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc),
        60,
    )

    assert max(point.ideal_power_w for point in points) <= generator.total_installed_power_w
    assert len(points) == 8760


def test_ideal_generation_leap_year_uses_real_timestamps(
    generator: IdealSolarGenerator,
) -> None:
    points = generator.generate(
        datetime(2028, 1, 1, 0, 0),
        datetime(2029, 1, 1, 0, 0),
        60,
    )

    assert len(points) == 8784
