from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import app.simulation.grid as grid_module
from app.simulation.grid import (
    GridDamageEvent,
    GridSimulationSettings,
    OutageWindow,
    calculate_grid_health,
    daily_outage_hours_from_deficit,
    generate_rolling_outage_schedule,
    outage_level_for_deficit,
    outage_window_context,
    remaining_damage,
    simulate_energy_damage_from_local_elements,
)


def test_health_formula_and_reasons_are_stable() -> None:
    stable = calculate_grid_health(0.0, 0.0)
    assert stable.generation_health_percent == 130.0
    assert stable.delivery_health_percent == 130.0
    assert stable.deficit_percent == 0.0
    assert stable.outage_level == "stable"
    assert stable.reason == "no active damage"

    generation_limited = calculate_grid_health(50.0, 0.0)
    assert generation_limited.generation_health_percent == 80.0
    assert generation_limited.delivery_health_percent == 130.0
    assert generation_limited.deficit_percent == 20.0
    assert generation_limited.reason == "generation bottleneck"

    delivery_limited = calculate_grid_health(0.0, 60.0)
    assert delivery_limited.generation_health_percent == 130.0
    assert delivery_limited.delivery_health_percent == 70.0
    assert delivery_limited.deficit_percent == 30.0
    assert delivery_limited.reason == "delivery bottleneck"

    combined = calculate_grid_health(50.0, 60.0)
    assert combined.generation_health_percent == 80.0
    assert combined.delivery_health_percent == 70.0
    assert combined.deficit_percent == 30.0
    assert combined.reason == "combined generation and delivery bottleneck"


@pytest.mark.parametrize(
    ("deficit_percent", "expected_level"),
    [
        (0.0, "stable"),
        (5.0, "strained"),
        (25.0, "partial_outage"),
        (60.0, "severe_outage"),
        (90.0, "blackout"),
    ],
)
def test_outage_level_thresholds(
    deficit_percent: float,
    expected_level: str,
) -> None:
    assert outage_level_for_deficit(deficit_percent) == expected_level


@pytest.mark.parametrize(
    ("deficit_percent", "expected_hours"),
    [
        (30.0, 7.5),
        (10.0, 2.5),
        (0.0, 0.0),
    ],
)
def test_outage_hours_round_up_to_half_hour(
    deficit_percent: float,
    expected_hours: float,
) -> None:
    assert daily_outage_hours_from_deficit(deficit_percent) == expected_hours


def test_rolling_schedule_splits_moderate_deficit_and_is_deterministic() -> None:
    local_date = date(2026, 1, 15)
    windows = generate_rolling_outage_schedule(
        local_date,
        deficit_percent=30.0,
        queue="3.1",
        seed=12345,
    )
    same_windows = generate_rolling_outage_schedule(
        local_date,
        deficit_percent=30.0,
        queue="3.1",
        seed=12345,
    )
    shifted_windows = generate_rolling_outage_schedule(
        local_date,
        deficit_percent=30.0,
        queue="4.1",
        seed=12345,
    )

    assert len(windows) > 1
    assert windows == same_windows
    assert windows != shifted_windows
    assert max(_duration_hours(window) for window in windows) <= 4.0


def test_energy_targeting_is_sampled_before_defence_filtering() -> None:
    event_date = date(2026, 1, 10)
    local_counts = {"uav": 12, "cruise": 0, "ballistic": 0}

    targeted_but_neutralized = simulate_energy_damage_from_local_elements(
        local_counts=local_counts,
        event_date=event_date,
        seed=101,
        energy_target_probability_override=1.0,
        defence_efficiency_override={"uav": 1.0},
    )
    assert targeted_but_neutralized.total_applied_damage_percent == 0.0

    leaking_non_energy = simulate_energy_damage_from_local_elements(
        local_counts=local_counts,
        event_date=event_date,
        seed=101,
        energy_target_probability_override=0.0,
        defence_efficiency_override={"uav": 0.0},
    )
    assert leaking_non_energy.total_applied_damage_percent == 0.0

    targeted_and_leaking = simulate_energy_damage_from_local_elements(
        local_counts=local_counts,
        event_date=event_date,
        seed=101,
        energy_target_probability_override=1.0,
        defence_efficiency_override={"uav": 0.0},
    )
    assert targeted_and_leaking.total_applied_damage_percent > 0.0


def test_recovery_curves_decrease_damage_and_fast_recovers_faster() -> None:
    event_timestamp = datetime(2026, 1, 10, 3, 0, tzinfo=timezone.utc)
    fast_event = _damage_event(
        damage_class="fast",
        event_timestamp=event_timestamp,
        recovery_days=7.0,
    )
    structural_event = _damage_event(
        damage_class="structural",
        event_timestamp=event_timestamp,
        recovery_days=180.0,
    )

    fast_day0 = remaining_damage(fast_event, event_timestamp)[0]
    fast_day1 = remaining_damage(fast_event, event_timestamp + timedelta(days=1))[0]
    fast_day3 = remaining_damage(fast_event, event_timestamp + timedelta(days=3))[0]
    structural_day1 = remaining_damage(
        structural_event,
        event_timestamp + timedelta(days=1),
    )[0]
    structural_day3 = remaining_damage(
        structural_event,
        event_timestamp + timedelta(days=3),
    )[0]

    assert fast_day0 == 10.0
    assert fast_day1 < fast_day0
    assert fast_day3 < fast_day1
    assert fast_day3 < structural_day3
    assert structural_day1 > 9.9


def test_availability_generation_uses_padded_cross_midnight_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_date = date(2026, 1, 15)
    requested_date = previous_date + timedelta(days=1)
    timezone_info = ZoneInfo("Europe/Kyiv")
    generated_schedule_dates: list[date] = []

    def fixed_health(*args: object, **kwargs: object):
        return grid_module.derive_grid_health_status(
            generation_health_percent=70.0,
            delivery_health_percent=130.0,
            has_active_damage=True,
        )

    def fixed_schedule(
        local_date: date,
        deficit_percent: float,
        queue: str,
        seed: int,
        timezone_name: str,
    ) -> list[OutageWindow]:
        generated_schedule_dates.append(local_date)
        if local_date != previous_date:
            return []
        return [
            OutageWindow(
                start_local=datetime.combine(
                    previous_date,
                    time(23, 30),
                    tzinfo=timezone_info,
                ),
                end_local=datetime.combine(
                    requested_date,
                    time(1, 30),
                    tzinfo=timezone_info,
                ),
            )
        ]

    monkeypatch.setattr(grid_module, "calculate_health_from_events", fixed_health)
    monkeypatch.setattr(
        grid_module,
        "generate_rolling_outage_schedule",
        fixed_schedule,
    )

    points, _ = grid_module.generate_grid_availability_points(
        requested_date,
        requested_date,
        settings=GridSimulationSettings(
            outage_schedule_seed=20260513,
            outage_queue="3.1",
            local_timezone="Europe/Kyiv",
        ),
    )
    midnight_utc = datetime.combine(
        requested_date,
        time.min,
        tzinfo=timezone_info,
    ).astimezone(timezone.utc)
    midnight_point = next(
        point for point in points if point.timestamp_utc == midnight_utc
    )

    assert generated_schedule_dates == [
        previous_date,
        requested_date,
        requested_date + timedelta(days=1),
    ]
    assert midnight_point.is_outage_now is True
    assert midnight_point.local_grid_available is False
    assert midnight_point.grid_voltage_v == 0.0
    assert midnight_point.current_outage_window_start_utc == datetime.combine(
        previous_date,
        time(23, 30),
        tzinfo=timezone_info,
    ).astimezone(timezone.utc)
    assert midnight_point.current_outage_window_end_utc == datetime.combine(
        requested_date,
        time(1, 30),
        tzinfo=timezone_info,
    ).astimezone(timezone.utc)
    assert midnight_point.daily_outage_hours == 7.5


def test_outage_window_context_merges_overlapping_current_windows() -> None:
    timezone_info = ZoneInfo("Europe/Kyiv")
    local_date = date(2026, 1, 15)
    windows = [
        OutageWindow(
            start_local=datetime.combine(
                local_date,
                time(23, 0),
                tzinfo=timezone_info,
            ),
            end_local=datetime.combine(
                local_date + timedelta(days=1),
                time(0, 30),
                tzinfo=timezone_info,
            ),
        ),
        OutageWindow(
            start_local=datetime.combine(
                local_date + timedelta(days=1),
                time(0, 15),
                tzinfo=timezone_info,
            ),
            end_local=datetime.combine(
                local_date + timedelta(days=1),
                time(2, 0),
                tzinfo=timezone_info,
            ),
        ),
        OutageWindow(
            start_local=datetime.combine(
                local_date + timedelta(days=1),
                time(4, 0),
                tzinfo=timezone_info,
            ),
            end_local=datetime.combine(
                local_date + timedelta(days=1),
                time(5, 0),
                tzinfo=timezone_info,
            ),
        ),
    ]
    timestamp = datetime.combine(
        local_date + timedelta(days=1),
        time(0, 20),
        tzinfo=timezone_info,
    ).astimezone(timezone.utc)

    current_window, next_window = outage_window_context(timestamp, windows)

    assert current_window is not None
    assert current_window.start_local == windows[0].start_local
    assert current_window.end_local == windows[1].end_local
    assert next_window == windows[2]


def _damage_event(
    damage_class: str,
    event_timestamp: datetime,
    recovery_days: float,
) -> GridDamageEvent:
    return GridDamageEvent(
        event_key=f"test-{damage_class}",
        event_date=event_timestamp.date(),
        event_timestamp_utc=event_timestamp,
        attack_state="combined",
        kyiv_focus_mode="primary",
        element_type="cruise",
        damage_class=damage_class,
        raw_damage_percent=10.0,
        applied_generation_damage_percent=10.0,
        applied_delivery_damage_percent=0.0,
        recovery_days=recovery_days,
        seed=1,
        metadata={},
    )


def _duration_hours(window) -> float:
    return (window.end_utc - window.start_utc).total_seconds() / 3600.0
