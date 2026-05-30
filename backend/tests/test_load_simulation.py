from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.simulation import load as load_module
from app.simulation.load import (
    LoadDaylightConfigurationError,
    LoadContext,
    LoadSimulationSettings,
    LoadSimulator,
    aggregate_to_15_minute_candidates,
    build_professor_year_calendar,
    critical_internet_baseline_w,
    generate_one_minute_load_points,
    holiday_day_count,
    load_settings_from_station_config,
)


STATION_TIMEZONE = ZoneInfo("Europe/Kyiv")


def test_empty_lab_returns_critical_internet_baseline_only() -> None:
    simulator = LoadSimulator(_quiet_settings())

    point = simulator.build_point(_local(2026, 1, 5, 10, 0), LoadContext())

    assert point.total_power_draw_w == critical_internet_baseline_w()
    assert point.active_professor_count == 0
    assert point.active_student_count == 0
    assert point.active_event_tags == ("critical_internet",)


def test_absent_professor_does_not_activate_workstation_or_events() -> None:
    settings = _quiet_settings(
        force_professor_daily_states={index: "absent_ill" for index in range(5)},
    )
    simulator = LoadSimulator(settings)

    point = simulator.build_point(_local(2026, 1, 5, 10, 0), LoadContext())

    assert point.total_power_draw_w == critical_internet_baseline_w()
    assert point.active_professor_count == 0
    assert "professor_workstation" not in point.active_event_tags
    assert "professor_soldering" not in point.active_event_tags
    assert "kettle" not in point.active_event_tags


def test_professor_holiday_total_is_fourteen_days_per_year() -> None:
    calendar = build_professor_year_calendar(2026, LoadSimulationSettings(seed=12345))

    assert {
        professor_index: holiday_day_count(days)
        for professor_index, days in calendar.items()
    } == {
        0: 14,
        1: 14,
        2: 14,
        3: 14,
        4: 14,
    }


def test_scheduled_student_class_produces_higher_load_than_empty_baseline() -> None:
    settings = _quiet_settings(enable_student_classes=True)
    simulator = LoadSimulator(settings)

    point = simulator.build_point(_local(2026, 1, 6, 14, 15), LoadContext())

    assert point.active_student_count == 6
    assert point.total_power_draw_w > critical_internet_baseline_w()
    assert "student_class" in point.active_event_tags


def test_kettle_cannot_occur_without_people() -> None:
    simulator = LoadSimulator(_quiet_settings(enable_kettle_events=False))

    point = simulator.build_point(
        _local(2026, 1, 5, 10, 0),
        LoadContext(force_kettle_active=True),
    )

    assert point.total_power_draw_w == critical_internet_baseline_w()
    assert "kettle" not in point.active_event_tags


def test_forced_kettle_adds_1200_w_when_people_are_present() -> None:
    simulator = LoadSimulator(_quiet_settings(enable_kettle_events=False))
    timestamp = _local(2026, 1, 5, 10, 0)
    without_kettle = simulator.build_point(
        timestamp,
        LoadContext(force_present_professors=1),
    )
    with_kettle = simulator.build_point(
        timestamp,
        LoadContext(force_present_professors=1, force_kettle_active=True),
    )

    assert with_kettle.total_power_draw_w - without_kettle.total_power_draw_w == 1200.0
    assert "kettle" in with_kettle.active_event_tags


def test_grid_available_can_exceed_2000_w_when_high_power_events_overlap() -> None:
    simulator = LoadSimulator(_quiet_settings(enable_kettle_events=False))

    point = simulator.build_point(
        _local(2026, 1, 5, 10, 0),
        LoadContext(
            grid_available=True,
            grid_behavior="grid_normal",
            soc_percent=90.0,
            force_present_professors=1,
            force_high_power_events=True,
            force_kettle_active=True,
        ),
    )

    assert point.total_power_draw_w > 2000.0
    assert {"heat_gun", "hand_drill", "kettle"} <= set(point.active_event_tags)


def test_active_outage_low_soc_reduces_high_power_optional_load() -> None:
    simulator = LoadSimulator(_quiet_settings(enable_kettle_events=False))
    timestamp = _local(2026, 1, 5, 10, 0)

    grid_available = simulator.build_point(
        timestamp,
        LoadContext(
            grid_available=True,
            grid_behavior="grid_normal",
            soc_percent=90.0,
            force_present_professors=1,
            force_high_power_events=True,
            force_kettle_active=True,
        ),
    )
    low_soc_outage = simulator.build_point(
        timestamp,
        LoadContext(
            grid_available=False,
            grid_behavior="outage_active",
            soc_percent=20.0,
            force_present_professors=1,
            force_high_power_events=True,
            force_kettle_active=True,
        ),
    )

    assert grid_available.total_power_draw_w > low_soc_outage.total_power_draw_w
    assert "heat_gun" not in low_soc_outage.active_event_tags
    assert "kettle" not in low_soc_outage.active_event_tags
    assert "hand_drill" not in low_soc_outage.active_event_tags


def test_ceiling_lamps_disabled_for_workstation_only_work_during_outage() -> None:
    simulator = LoadSimulator(_quiet_settings())

    point = simulator.build_point(
        _local(2026, 1, 5, 10, 0),
        LoadContext(
            grid_available=False,
            grid_behavior="outage_active",
            soc_percent=90.0,
            is_dark=True,
            force_present_professors=1,
            force_workstation_only=True,
        ),
    )

    assert "workstation_lamps" in point.active_event_tags
    assert "ceiling_lamp" not in point.active_event_tags


def test_clear_midday_with_people_does_not_behave_like_full_darkness() -> None:
    simulator = LoadSimulator(_quiet_settings())

    midday = simulator.build_point(
        _local(2026, 6, 1, 12, 0),
        _sun_context(2026, 6, 1, force_present_professors=1),
    )
    after_sunset = simulator.build_point(
        _local(2026, 6, 1, 19, 0),
        _sun_context(2026, 6, 1, force_present_professors=1),
    )

    assert "workstation_lamps" not in midday.active_event_tags
    assert "ceiling_lamp" not in midday.active_event_tags
    assert "workstation_lamps" in after_sunset.active_event_tags
    assert "ceiling_lamp" in after_sunset.active_event_tags
    assert after_sunset.total_power_draw_w > midday.total_power_draw_w


def test_clear_before_sunset_increases_lighting_versus_midday() -> None:
    simulator = LoadSimulator(_quiet_settings())

    midday = simulator.build_point(
        _local(2026, 6, 1, 12, 0),
        _sun_context(2026, 6, 1, force_present_professors=1),
    )
    before_sunset = simulator.build_point(
        _local(2026, 6, 1, 17, 30),
        _sun_context(2026, 6, 1, force_present_professors=1),
    )

    assert "workstation_lamps" not in midday.active_event_tags
    assert "workstation_lamps" in before_sunset.active_event_tags
    assert before_sunset.total_power_draw_w > midday.total_power_draw_w


def test_obstructing_weather_before_sunset_increases_lighting_versus_midday() -> None:
    simulator = LoadSimulator(_quiet_settings())

    clear_midday = simulator.build_point(
        _local(2026, 6, 1, 12, 0),
        _sun_context(2026, 6, 1, force_present_professors=1),
    )
    rainy_before_sunset = simulator.build_point(
        _local(2026, 6, 1, 16, 30),
        _sun_context(
            2026,
            6,
            1,
            weather_state="rain",
            force_present_professors=1,
        ),
    )

    assert "workstation_lamps" in rainy_before_sunset.active_event_tags
    assert rainy_before_sunset.total_power_draw_w > clear_midday.total_power_draw_w


def test_obstructing_weather_at_noon_is_not_equivalent_to_after_sunset() -> None:
    simulator = LoadSimulator(_quiet_settings())

    rainy_noon = simulator.build_point(
        _local(2026, 6, 1, 12, 0),
        _sun_context(
            2026,
            6,
            1,
            weather_state="rain",
            force_present_professors=1,
        ),
    )
    after_sunset = simulator.build_point(
        _local(2026, 6, 1, 19, 0),
        _sun_context(2026, 6, 1, force_present_professors=1),
    )

    assert "workstation_lamps" in rainy_noon.active_event_tags
    assert "ceiling_lamp" not in rainy_noon.active_event_tags
    assert "ceiling_lamp" in after_sunset.active_event_tags
    assert after_sunset.total_power_draw_w > rainy_noon.total_power_draw_w


def test_after_sunset_with_people_prefers_lamps() -> None:
    simulator = LoadSimulator(_quiet_settings())

    point = simulator.build_point(
        _local(2026, 6, 1, 19, 0),
        _sun_context(2026, 6, 1, force_present_professors=1),
    )

    assert "dark_condition" in point.active_event_tags
    assert "workstation_lamps" in point.active_event_tags
    assert "ceiling_lamp" in point.active_event_tags


def test_clear_after_sunrise_allows_lamps_then_decreases_after_window() -> None:
    simulator = LoadSimulator(_quiet_settings())

    shortly_after_sunrise = simulator.build_point(
        _local(2026, 6, 1, 6, 30),
        _sun_context(2026, 6, 1, force_present_professors=1),
    )
    after_window = simulator.build_point(
        _local(2026, 6, 1, 7, 30),
        _sun_context(2026, 6, 1, force_present_professors=1),
    )

    assert "workstation_lamps" in shortly_after_sunrise.active_event_tags
    assert "workstation_lamps" not in after_window.active_event_tags
    assert shortly_after_sunrise.total_power_draw_w > after_window.total_power_draw_w


def test_obstructing_weather_after_sunrise_stays_elevated_longer_than_clear() -> None:
    simulator = LoadSimulator(_quiet_settings())

    clear_after_window = simulator.build_point(
        _local(2026, 6, 1, 7, 30),
        _sun_context(2026, 6, 1, force_present_professors=1),
    )
    rainy_after_sunrise = simulator.build_point(
        _local(2026, 6, 1, 7, 30),
        _sun_context(
            2026,
            6,
            1,
            weather_state="rain",
            force_present_professors=1,
        ),
    )

    assert "workstation_lamps" not in clear_after_window.active_event_tags
    assert "workstation_lamps" in rainy_after_sunrise.active_event_tags
    assert rainy_after_sunrise.total_power_draw_w > clear_after_window.total_power_draw_w


def test_no_people_produces_no_lamps_regardless_darkness_or_weather() -> None:
    simulator = LoadSimulator(_quiet_settings())

    point = simulator.build_point(
        _local(2026, 6, 1, 19, 0),
        _sun_context(2026, 6, 1, weather_state="rain", is_dark=True),
    )

    assert point.total_power_draw_w == critical_internet_baseline_w()
    assert "workstation_lamps" not in point.active_event_tags
    assert "soldering_lamps" not in point.active_event_tags
    assert "ceiling_lamp" not in point.active_event_tags


def test_explicit_sunrise_sunset_overrides_work_without_coordinates() -> None:
    simulator = LoadSimulator(_quiet_settings_without_daylight_coordinates())

    overridden = simulator.build_point(
        _local(2026, 1, 5, 17, 30),
        _sun_context(
            2026,
            1,
            5,
            sunrise_hour=4,
            sunset_hour=23,
            force_present_professors=1,
        ),
    )

    assert "workstation_lamps" not in overridden.active_event_tags


def test_missing_sun_times_and_missing_coordinates_fail_clearly() -> None:
    simulator = LoadSimulator(_quiet_settings_without_daylight_coordinates())

    with pytest.raises(LoadDaylightConfigurationError, match="requires station latitude and longitude"):
        simulator.build_point(
            _local(2026, 6, 1, 12, 0),
            LoadContext(force_present_professors=1),
        )


def test_missing_sun_times_and_invalid_coordinates_fail_clearly() -> None:
    simulator = LoadSimulator(
        _quiet_settings(
            station_latitude=120.0,
            station_longitude=30.464642,
        ),
    )

    with pytest.raises(LoadDaylightConfigurationError, match="outside valid ranges"):
        simulator.build_point(
            _local(2026, 6, 1, 12, 0),
            LoadContext(force_present_professors=1),
        )


def test_invalid_daylight_timezone_fails_clearly() -> None:
    with pytest.raises(LoadDaylightConfigurationError, match="invalid station timezone"):
        LoadSimulator(
            _quiet_settings(
                timezone_name="Invalid/Timezone",
                station_latitude=50.448997,
                station_longitude=30.464642,
            ),
        )


def test_config_derived_coordinates_allow_internal_sun_times() -> None:
    config = SimpleNamespace(
        station=SimpleNamespace(
            solar=SimpleNamespace(
                installation=SimpleNamespace(
                    latitude=50.448997,
                    longitude=30.464642,
                    timezone="Europe/Kyiv",
                ),
            ),
        ),
    )
    settings = load_settings_from_station_config(
        config,
        base_settings=_quiet_settings_without_daylight_coordinates(),
    )
    simulator = LoadSimulator(settings)

    point = simulator.build_point(
        _local(2026, 6, 1, 18, 30),
        LoadContext(force_present_professors=1),
    )

    assert settings.station_latitude == 50.448997
    assert settings.station_longitude == 30.464642
    assert settings.timezone_name == "Europe/Kyiv"
    assert "workstation_lamps" not in point.active_event_tags


def test_missing_sun_times_use_internal_calculation_not_fixed_hour_fallback() -> None:
    simulator = LoadSimulator(_quiet_settings())

    point = simulator.build_point(
        _local(2026, 6, 1, 18, 30),
        LoadContext(force_present_professors=1),
    )

    assert "workstation_lamps" not in point.active_event_tags
    assert "ceiling_lamp" not in point.active_event_tags


def test_internal_sun_times_drive_winter_morning_and_evening_lighting() -> None:
    simulator = LoadSimulator(_quiet_settings())

    before_sunrise = simulator.build_point(
        _local(2026, 1, 5, 7, 30),
        LoadContext(force_present_professors=1),
    )
    after_sunrise_window = simulator.build_point(
        _local(2026, 1, 5, 9, 30),
        LoadContext(force_present_professors=1),
    )
    before_sunset = simulator.build_point(
        _local(2026, 1, 5, 15, 45),
        LoadContext(force_present_professors=1),
    )
    after_sunset = simulator.build_point(
        _local(2026, 1, 5, 16, 30),
        LoadContext(force_present_professors=1),
    )

    assert "workstation_lamps" in before_sunrise.active_event_tags
    assert "workstation_lamps" not in after_sunrise_window.active_event_tags
    assert "workstation_lamps" in before_sunset.active_event_tags
    assert "workstation_lamps" in after_sunset.active_event_tags
    assert before_sunrise.total_power_draw_w > after_sunrise_window.total_power_draw_w
    assert after_sunset.total_power_draw_w > after_sunrise_window.total_power_draw_w


def test_obstructing_weather_widens_computed_sunset_transition() -> None:
    simulator = LoadSimulator(_quiet_settings())

    clear = simulator.build_point(
        _local(2026, 6, 1, 19, 15),
        LoadContext(force_present_professors=1),
    )
    rainy = simulator.build_point(
        _local(2026, 6, 1, 19, 15),
        LoadContext(force_present_professors=1, weather_state="rain"),
    )

    assert "workstation_lamps" not in clear.active_event_tags
    assert "workstation_lamps" in rainy.active_event_tags
    assert rainy.total_power_draw_w > clear.total_power_draw_w


def test_load_module_does_not_use_solar_generation_values() -> None:
    source = inspect.getsource(load_module)

    assert "IdealSolarGenerator" not in source
    assert "ideal_power_w" not in source
    assert "simulated_power_w" not in source
    assert "forecast_power_w" not in source
    assert "InterpolatedSolarProduction" not in source
    assert "_fallback_sun_times" not in source


def test_15_minute_aggregation_computes_energy_when_load_exists() -> None:
    start = _local(2026, 1, 5, 10, 0)
    end = start + timedelta(minutes=16)
    points = generate_one_minute_load_points(
        start,
        end,
        settings=_quiet_settings(),
        context_provider=LoadContext(force_present_professors=1),
    )

    aggregates = aggregate_to_15_minute_candidates(points)
    assert [aggregate.timestamp_local.minute for aggregate in aggregates] == [15]
    aggregate_at_15 = next(
        aggregate
        for aggregate in aggregates
        if aggregate.timestamp_local.minute == 15
    )

    expected_energy = sum(point.total_power_draw_w / 60.0 for point in points[:15])
    assert aggregate_at_15.energy_wh_last_15m == pytest.approx(expected_energy)
    assert aggregate_at_15.energy_wh_last_15m > 0.0
    assert aggregate_at_15.momentary_power_w == points[15].total_power_draw_w

    points_with_gap = [
        point
        for point in points
        if point.timestamp_local.minute != 7
    ]
    assert aggregate_to_15_minute_candidates(points_with_gap) == []


def _quiet_settings(**overrides: object) -> LoadSimulationSettings:
    values = {
        "seed": 20260529,
        "timezone_name": "Europe/Kyiv",
        "station_latitude": 50.448997,
        "station_longitude": 30.464642,
        "enable_professors": False,
        "enable_student_classes": False,
        "enable_random_student_visits": False,
        "enable_kettle_events": False,
    }
    values.update(overrides)
    return LoadSimulationSettings(**values)


def _quiet_settings_without_daylight_coordinates(**overrides: object) -> LoadSimulationSettings:
    values = {
        "seed": 20260529,
        "timezone_name": "Europe/Kyiv",
        "station_latitude": None,
        "station_longitude": None,
        "enable_professors": False,
        "enable_student_classes": False,
        "enable_random_student_visits": False,
        "enable_kettle_events": False,
    }
    values.update(overrides)
    return LoadSimulationSettings(**values)


def _sun_context(
    year: int,
    month: int,
    day: int,
    sunrise_hour: int = 6,
    sunset_hour: int = 18,
    **overrides: object,
) -> LoadContext:
    values = {
        "sunrise_local": datetime(year, month, day, sunrise_hour, 0, tzinfo=STATION_TIMEZONE),
        "sunset_local": datetime(year, month, day, sunset_hour, 0, tzinfo=STATION_TIMEZONE),
    }
    values.update(overrides)
    return LoadContext(**values)


def _local(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=STATION_TIMEZONE).astimezone(
        timezone.utc,
    )
