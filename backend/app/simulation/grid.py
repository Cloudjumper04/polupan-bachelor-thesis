from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo


AttackState = Literal["quiet", "routine_uav", "dense_uav", "combined", "cooldown"]
KyivFocusMode = Literal["no_kyiv", "spillover", "secondary", "primary"]
ElementType = Literal["uav", "cruise", "ballistic"]
OutageLevel = Literal[
    "stable",
    "strained",
    "partial_outage",
    "severe_outage",
    "blackout",
]


DEFAULT_GRID_SIMULATION_SEED = 20260513
DEFAULT_GRID_HISTORY_START = date(2025, 10, 6)
GRID_ATTACK_ANCHOR_DATE = date(2025, 1, 1)
GRID_AVAILABILITY_CADENCE_MINUTES = 30
DEFAULT_LOCAL_TIMEZONE = "Europe/Kyiv"
DEFAULT_OUTAGE_QUEUE = "3.1"


@dataclass(frozen=True)
class GridSimulationSettings:
    base_delivery_health_percent: float = 130.0
    base_generation_health_percent: float = 130.0
    regeneration_cap_percent: float = 150.0
    minimum_health_percent: float = 0.0
    outage_queue: str = DEFAULT_OUTAGE_QUEUE
    outage_schedule_seed: int = DEFAULT_GRID_SIMULATION_SEED
    local_timezone: str = DEFAULT_LOCAL_TIMEZONE


@dataclass(frozen=True)
class GridHealthStatus:
    generation_health_percent: float
    delivery_health_percent: float
    effective_health_percent: float
    deficit_percent: float
    availability_ratio: float
    outage_level: OutageLevel
    reason: str


@dataclass(frozen=True)
class GridDamageEvent:
    event_key: str
    event_date: date
    event_timestamp_utc: datetime
    attack_state: AttackState
    kyiv_focus_mode: KyivFocusMode
    element_type: ElementType
    damage_class: str
    raw_damage_percent: float
    applied_generation_damage_percent: float
    applied_delivery_damage_percent: float
    recovery_days: float
    seed: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GridCycleDamage:
    events: list[GridDamageEvent]
    local_counts: dict[ElementType, int]
    energy_hit_counts: dict[ElementType, int]

    @property
    def total_raw_damage_percent(self) -> float:
        return sum(event.raw_damage_percent for event in self.events)

    @property
    def total_applied_generation_damage_percent(self) -> float:
        return sum(event.applied_generation_damage_percent for event in self.events)

    @property
    def total_applied_delivery_damage_percent(self) -> float:
        return sum(event.applied_delivery_damage_percent for event in self.events)

    @property
    def total_applied_damage_percent(self) -> float:
        return (
            self.total_applied_generation_damage_percent
            + self.total_applied_delivery_damage_percent
        )


@dataclass(frozen=True)
class DailyGridSimulation:
    local_date: date
    attack_state: AttackState
    kyiv_focus_mode: KyivFocusMode
    national_counts: dict[ElementType, int]
    local_counts: dict[ElementType, int]
    damage: GridCycleDamage


@dataclass(frozen=True)
class OutageWindow:
    start_local: datetime
    end_local: datetime

    @property
    def start_utc(self) -> datetime:
        return self.start_local.astimezone(timezone.utc)

    @property
    def end_utc(self) -> datetime:
        return self.end_local.astimezone(timezone.utc)


@dataclass(frozen=True)
class GridAvailabilityPoint:
    timestamp_utc: datetime
    timestamp_local: datetime
    generation_health_percent: float
    delivery_health_percent: float
    effective_health_percent: float
    deficit_percent: float
    daily_outage_hours: float
    outage_level: OutageLevel
    outage_queue: str
    local_grid_available: bool
    is_outage_now: bool
    grid_voltage_v: float
    reason: str
    current_outage_window_start_utc: datetime | None
    current_outage_window_end_utc: datetime | None
    next_outage_window_start_utc: datetime | None
    next_outage_window_end_utc: datetime | None


# TODO: Add Ukrainian Victory Day mode after war_end_date is known.
# def attacks_disabled_after_war_end(current_date: date, war_end_date: date | None) -> bool:
#     """Return True after the configured Ukrainian victory / war end date.
#     When enabled in the future, attack generation should return zero new damage.
#     Existing damage may continue recovering normally.
#     """
#     return war_end_date is not None and current_date > war_end_date


def calculate_grid_health(
    active_generation_damage_percent: float,
    active_delivery_damage_percent: float,
    settings: GridSimulationSettings | None = None,
    recovered_generation_margin_percent: float = 0.0,
    recovered_delivery_margin_percent: float = 0.0,
) -> GridHealthStatus:
    settings = settings or GridSimulationSettings()
    generation_health = _clamp(
        settings.base_generation_health_percent
        - active_generation_damage_percent
        + recovered_generation_margin_percent,
        settings.minimum_health_percent,
        settings.regeneration_cap_percent,
    )
    delivery_health = _clamp(
        settings.base_delivery_health_percent
        - active_delivery_damage_percent
        + recovered_delivery_margin_percent,
        settings.minimum_health_percent,
        settings.regeneration_cap_percent,
    )
    return derive_grid_health_status(
        generation_health,
        delivery_health,
        has_active_damage=(
            active_generation_damage_percent > 0.0
            or active_delivery_damage_percent > 0.0
        ),
    )


def derive_grid_health_status(
    generation_health_percent: float,
    delivery_health_percent: float,
    has_active_damage: bool = False,
) -> GridHealthStatus:
    effective_health = min(generation_health_percent, delivery_health_percent)
    deficit = max(0.0, 100.0 - effective_health)
    availability_ratio = _clamp(1.0 - deficit / 100.0, 0.0, 1.0)
    return GridHealthStatus(
        generation_health_percent=round(generation_health_percent, 4),
        delivery_health_percent=round(delivery_health_percent, 4),
        effective_health_percent=round(effective_health, 4),
        deficit_percent=round(deficit, 4),
        availability_ratio=round(availability_ratio, 4),
        outage_level=outage_level_for_deficit(deficit),
        reason=reason_for_health(
            generation_health_percent,
            delivery_health_percent,
            deficit,
            has_active_damage,
        ),
    )


def outage_level_for_deficit(deficit_percent: float) -> OutageLevel:
    if deficit_percent <= 0.0:
        return "stable"
    if deficit_percent <= 10.0:
        return "strained"
    if deficit_percent <= 40.0:
        return "partial_outage"
    if deficit_percent <= 80.0:
        return "severe_outage"
    return "blackout"


def reason_for_health(
    generation_health_percent: float,
    delivery_health_percent: float,
    deficit_percent: float,
    has_active_damage: bool,
) -> str:
    if deficit_percent <= 0.0:
        return "recovery phase after damage" if has_active_damage else "no active damage"
    generation_limited = generation_health_percent < 100.0
    delivery_limited = delivery_health_percent < 100.0
    if generation_limited and delivery_limited:
        return "combined generation and delivery bottleneck"
    if generation_limited or generation_health_percent < delivery_health_percent:
        return "generation bottleneck"
    if delivery_limited or delivery_health_percent < generation_health_percent:
        return "delivery bottleneck"
    return "combined generation and delivery bottleneck"


def daily_outage_hours_from_deficit(deficit_percent: float) -> float:
    raw_hours = 24.0 * max(0.0, deficit_percent) / 100.0
    return math.ceil(raw_hours * 2.0) / 2.0


def generate_rolling_outage_schedule(
    local_date: date,
    deficit_percent: float,
    queue: str = DEFAULT_OUTAGE_QUEUE,
    seed: int = DEFAULT_GRID_SIMULATION_SEED,
    timezone_name: str = DEFAULT_LOCAL_TIMEZONE,
) -> list[OutageWindow]:
    outage_hours = daily_outage_hours_from_deficit(deficit_percent)
    total_units = int(round(outage_hours * 2.0))
    if total_units <= 0:
        return []

    timezone_info = ZoneInfo(timezone_name)
    block_units = _split_outage_units(total_units)
    gap_units = _split_powered_gap_units(48 - total_units, len(block_units))
    queue_index = _queue_index(queue)
    rng = _rng(seed, "outage-schedule", local_date.isoformat(), queue, total_units)
    start_unit = ((queue_index + rng.randrange(12)) % 12) * 4

    windows: list[OutageWindow] = []
    cursor_units = start_unit
    for index, block_unit_count in enumerate(block_units):
        start_units_in_day = cursor_units % 48
        target_start = datetime.combine(
            local_date,
            time.min,
            tzinfo=timezone_info,
        ) + timedelta(minutes=30 * start_units_in_day)
        target_end = target_start + timedelta(minutes=30 * block_unit_count)
        start_jitter_minutes = rng.randint(-5, 5)
        end_jitter_minutes = rng.randint(-5, 15)
        windows.append(
            OutageWindow(
                start_local=target_start + timedelta(minutes=start_jitter_minutes),
                end_local=target_end + timedelta(minutes=end_jitter_minutes),
            )
        )
        cursor_units += block_unit_count + gap_units[index]

    return sorted(windows, key=lambda window: window.start_utc)


def grid_voltage_visual(
    timestamp_utc: datetime,
    deficit_percent: float,
    is_outage_now: bool,
    seed: int = DEFAULT_GRID_SIMULATION_SEED,
) -> float:
    if is_outage_now:
        return 0.0

    timestamp = _as_utc(timestamp_utc).replace(second=0, microsecond=0)
    rng = _rng(seed, "grid-voltage", timestamp.isoformat(), round(deficit_percent, 2))
    deficit = max(0.0, deficit_percent)
    if deficit <= 0.0:
        return round(230.0 + rng.uniform(-1.5, 1.5), 1)
    if deficit <= 10.0:
        return round(rng.uniform(225.0, 235.0), 1)
    if deficit <= 40.0:
        if rng.random() < 0.12 + deficit / 250.0:
            return round(rng.uniform(190.0, 212.0), 1)
        return round(rng.uniform(210.0, 235.0), 1)
    if deficit <= 80.0:
        if rng.random() < 0.28 + deficit / 180.0:
            return round(rng.uniform(185.0, 207.0), 1)
        return round(rng.uniform(207.0, 230.0), 1)
    if rng.random() < 0.70:
        return 0.0
    return round(rng.uniform(160.0, 220.0), 1)


def simulate_daily_grid_events(
    start_date: date,
    end_date: date,
    settings: GridSimulationSettings | None = None,
) -> list[DailyGridSimulation]:
    if end_date < start_date:
        return []
    settings = settings or GridSimulationSettings()
    previous_state = attack_state_before_date(
        start_date,
        seed=settings.outage_schedule_seed,
    )
    days: list[DailyGridSimulation] = []
    current_date = start_date
    while current_date <= end_date:
        day = simulate_grid_day(
            current_date,
            previous_state,
            settings=settings,
        )
        days.append(day)
        previous_state = day.attack_state
        current_date += timedelta(days=1)
    return days


def simulate_grid_day(
    local_date: date,
    previous_state: AttackState = "quiet",
    settings: GridSimulationSettings | None = None,
) -> DailyGridSimulation:
    settings = settings or GridSimulationSettings()
    seed = settings.outage_schedule_seed
    attack_state = choose_attack_state(local_date, previous_state, seed)
    national_counts = sample_national_attack_counts(local_date, attack_state, seed)
    kyiv_focus_mode, kyiv_share = sample_kyiv_focus_mode(local_date, attack_state, seed)
    local_counts = {
        element_type: binomial(
            count,
            kyiv_share,
            _rng(seed, "local-count", local_date.isoformat(), element_type),
        )
        for element_type, count in national_counts.items()
    }
    damage = simulate_energy_damage_from_local_elements(
        local_counts=local_counts,
        event_date=local_date,
        attack_state=attack_state,
        kyiv_focus_mode=kyiv_focus_mode,
        seed=seed,
        timezone_name=settings.local_timezone,
    )
    return DailyGridSimulation(
        local_date=local_date,
        attack_state=attack_state,
        kyiv_focus_mode=kyiv_focus_mode,
        national_counts=national_counts,
        local_counts=local_counts,
        damage=damage,
    )


def choose_attack_state(
    local_date: date,
    previous_state: AttackState,
    seed: int = DEFAULT_GRID_SIMULATION_SEED,
) -> AttackState:
    probabilities = _attack_state_probabilities(local_date, previous_state)
    rng = _rng(seed, "attack-state", local_date.isoformat(), previous_state)
    return _weighted_choice(probabilities, rng)


def attack_state_before_date(
    local_date: date,
    seed: int = DEFAULT_GRID_SIMULATION_SEED,
) -> AttackState:
    if local_date <= GRID_ATTACK_ANCHOR_DATE:
        return "quiet"
    state: AttackState = "quiet"
    current = GRID_ATTACK_ANCHOR_DATE
    while current < local_date:
        state = choose_attack_state(current, state, seed)
        current += timedelta(days=1)
    return state


def sample_national_attack_counts(
    local_date: date,
    attack_state: AttackState,
    seed: int = DEFAULT_GRID_SIMULATION_SEED,
) -> dict[ElementType, int]:
    winter = season_intensity(local_date)
    rng = _rng(seed, "national-counts", local_date.isoformat(), attack_state)

    if attack_state == "quiet":
        return {
            "uav": _count(truncated_normal(rng, 10.0, 15.0, 0.0, 50.0)),
            "cruise": 0,
            "ballistic": 0,
        }
    if attack_state == "routine_uav":
        return {
            "uav": _count(truncated_normal(rng, 120.0 + 80.0 * winter, 55.0, 40.0, 300.0)),
            "cruise": (
                _count(truncated_normal(rng, 3.0, 2.0, 1.0, 8.0))
                if bernoulli(0.10, rng)
                else 0
            ),
            "ballistic": (
                _count(truncated_normal(rng, 2.0, 1.0, 1.0, 5.0))
                if bernoulli(0.05, rng)
                else 0
            ),
        }
    if attack_state == "dense_uav":
        return {
            "uav": _count(truncated_normal(rng, 300.0 + 120.0 * winter, 85.0, 180.0, 700.0)),
            "cruise": (
                _count(truncated_normal(rng, 5.0, 3.0, 1.0, 15.0))
                if bernoulli(0.20, rng)
                else 0
            ),
            "ballistic": (
                _count(truncated_normal(rng, 3.0, 2.0, 1.0, 8.0))
                if bernoulli(0.10, rng)
                else 0
            ),
        }
    if attack_state == "combined":
        return {
            "uav": _count(truncated_normal(rng, 430.0 + 150.0 * winter, 130.0, 250.0, 950.0)),
            "cruise": _count(truncated_normal(rng, 20.0 + 15.0 * winter, 14.0, 5.0, 90.0)),
            "ballistic": _count(truncated_normal(rng, 8.0 + 8.0 * winter, 7.0, 2.0, 40.0)),
        }
    return {
        "uav": _count(truncated_normal(rng, 70.0 + 60.0 * winter, 50.0, 0.0, 220.0)),
        "cruise": (
            _count(truncated_normal(rng, 3.0, 2.0, 1.0, 8.0))
            if bernoulli(0.05, rng)
            else 0
        ),
        "ballistic": (
            _count(truncated_normal(rng, 2.0, 1.0, 1.0, 5.0))
            if bernoulli(0.03, rng)
            else 0
        ),
    }


def sample_kyiv_focus_mode(
    local_date: date,
    attack_state: AttackState,
    seed: int = DEFAULT_GRID_SIMULATION_SEED,
) -> tuple[KyivFocusMode, float]:
    rng = _rng(seed, "kyiv-focus", local_date.isoformat(), attack_state)
    mode = _weighted_choice(_kyiv_focus_probabilities(attack_state), rng)
    if mode == "no_kyiv":
        return mode, 0.0
    if mode == "spillover":
        share = truncated_normal(rng, 0.06, 0.03, 0.02, 0.12)
    elif mode == "secondary":
        share = truncated_normal(rng, 0.25, 0.08, 0.12, 0.45)
    else:
        share = truncated_normal(rng, 0.80, 0.15, 0.55, 1.00)
    return mode, share


def simulate_energy_damage_from_local_elements(
    local_counts: dict[ElementType, int],
    event_date: date,
    attack_state: AttackState = "routine_uav",
    kyiv_focus_mode: KyivFocusMode = "secondary",
    seed: int = DEFAULT_GRID_SIMULATION_SEED,
    timezone_name: str = DEFAULT_LOCAL_TIMEZONE,
    energy_target_probability_override: float | None = None,
    defence_efficiency_override: dict[ElementType, float] | None = None,
) -> GridCycleDamage:
    rng = _rng(
        seed,
        "energy-damage",
        event_date.isoformat(),
        attack_state,
        kyiv_focus_mode,
        tuple(sorted(local_counts.items())),
    )
    timezone_info = ZoneInfo(timezone_name)
    event_timestamp_utc = datetime.combine(
        event_date,
        time(3, 0),
        tzinfo=timezone_info,
    ).astimezone(timezone.utc)
    energy_hit_counts: dict[ElementType, int] = {
        "uav": 0,
        "cruise": 0,
        "ballistic": 0,
    }
    raw_events: list[GridDamageEvent] = []
    event_index = 0
    for element_type in ("uav", "cruise", "ballistic"):
        count = max(0, int(local_counts.get(element_type, 0)))
        for _ in range(count):
            target_probability = (
                energy_target_probability_override
                if energy_target_probability_override is not None
                else energy_target_probability(event_date, rng)
            )
            energy_targeted = bernoulli(target_probability, rng)
            defence_efficiency = (
                defence_efficiency_override[element_type]
                if defence_efficiency_override is not None
                and element_type in defence_efficiency_override
                else sample_defence_efficiency(element_type, rng)
            )
            neutralized = bernoulli(defence_efficiency, rng)
            if not energy_targeted or neutralized:
                continue

            energy_hit_counts[element_type] += 1
            damage_class, damage_amount = sample_damage_amount(element_type, rng)
            generation_damage, delivery_damage = split_damage_by_element(
                element_type,
                damage_amount,
                rng,
            )
            recovery_days = sample_recovery_days(damage_class, rng)
            raw_events.append(
                GridDamageEvent(
                    event_key=_event_key(seed, event_date, event_index),
                    event_date=event_date,
                    event_timestamp_utc=event_timestamp_utc,
                    attack_state=attack_state,
                    kyiv_focus_mode=kyiv_focus_mode,
                    element_type=element_type,
                    damage_class=damage_class,
                    raw_damage_percent=damage_amount,
                    applied_generation_damage_percent=generation_damage,
                    applied_delivery_damage_percent=delivery_damage,
                    recovery_days=recovery_days,
                    seed=seed,
                    metadata={
                        "local_counts": dict(local_counts),
                        "energy_hit_counts": dict(energy_hit_counts),
                    },
                )
            )
            event_index += 1

    saturated_events = apply_cycle_damage_saturation(raw_events, event_date, attack_state)
    return GridCycleDamage(
        events=saturated_events,
        local_counts={
            "uav": max(0, int(local_counts.get("uav", 0))),
            "cruise": max(0, int(local_counts.get("cruise", 0))),
            "ballistic": max(0, int(local_counts.get("ballistic", 0))),
        },
        energy_hit_counts=energy_hit_counts,
    )


def apply_cycle_damage_saturation(
    events: list[GridDamageEvent],
    event_date: date,
    attack_state: AttackState,
) -> list[GridDamageEvent]:
    total_raw_damage = sum(event.raw_damage_percent for event in events)
    if total_raw_damage <= 0.0:
        return []
    cap = attack_damage_cap(event_date, attack_state)
    applied_total = cap * (1.0 - math.exp(-total_raw_damage / cap))
    scale = applied_total / total_raw_damage
    return [
        GridDamageEvent(
            event_key=event.event_key,
            event_date=event.event_date,
            event_timestamp_utc=event.event_timestamp_utc,
            attack_state=event.attack_state,
            kyiv_focus_mode=event.kyiv_focus_mode,
            element_type=event.element_type,
            damage_class=event.damage_class,
            raw_damage_percent=round(event.raw_damage_percent, 6),
            applied_generation_damage_percent=round(
                event.applied_generation_damage_percent * scale,
                6,
            ),
            applied_delivery_damage_percent=round(
                event.applied_delivery_damage_percent * scale,
                6,
            ),
            recovery_days=round(event.recovery_days, 4),
            seed=event.seed,
            metadata=event.metadata,
        )
        for event in events
    ]


def attack_damage_cap(event_date: date, attack_state: AttackState) -> float:
    winter = season_intensity(event_date)
    if attack_state == "combined" and winter >= 0.75:
        return 45.0
    return 25.0 + 10.0 * winter


def remaining_damage(
    event: GridDamageEvent,
    at_timestamp: datetime,
) -> tuple[float, float]:
    at_utc = _as_utc(at_timestamp)
    event_utc = _as_utc(event.event_timestamp_utc)
    if at_utc < event_utc:
        return 0.0, 0.0

    elapsed_days = (at_utc - event_utc).total_seconds() / 86400.0
    if elapsed_days >= event.recovery_days:
        return 0.0, 0.0

    remaining_fraction = _remaining_damage_fraction(
        event.damage_class,
        elapsed_days,
        event.recovery_days,
    )
    return (
        event.applied_generation_damage_percent * remaining_fraction,
        event.applied_delivery_damage_percent * remaining_fraction,
    )


def calculate_health_from_events(
    events: list[GridDamageEvent],
    at_timestamp: datetime,
    settings: GridSimulationSettings | None = None,
) -> GridHealthStatus:
    generation_damage = 0.0
    delivery_damage = 0.0
    for event in events:
        event_generation, event_delivery = remaining_damage(event, at_timestamp)
        generation_damage += event_generation
        delivery_damage += event_delivery
    return calculate_grid_health(
        generation_damage,
        delivery_damage,
        settings=settings,
    )


def generate_grid_availability_points(
    start_date: date,
    end_date: date,
    settings: GridSimulationSettings | None = None,
) -> tuple[list[GridAvailabilityPoint], list[GridDamageEvent]]:
    settings = settings or GridSimulationSettings()
    if end_date < start_date:
        return [], []

    schedule_start_date = start_date - timedelta(days=1)
    schedule_end_date = end_date + timedelta(days=1)
    daily_simulations = simulate_daily_grid_events(
        schedule_start_date,
        schedule_end_date,
        settings,
    )
    events = [
        event
        for day in daily_simulations
        for event in day.damage.events
    ]

    timezone_info = ZoneInfo(settings.local_timezone)
    horizon_windows: list[OutageWindow] = []
    daily_outage_hours_by_date: dict[date, float] = {}
    current_schedule_date = schedule_start_date
    while current_schedule_date <= schedule_end_date:
        daily_status_time = datetime.combine(
            current_schedule_date,
            time(12, 0),
            tzinfo=timezone_info,
        ).astimezone(timezone.utc)
        daily_status = calculate_health_from_events(
            events,
            daily_status_time,
            settings=settings,
        )
        daily_outage_hours_by_date[current_schedule_date] = (
            daily_outage_hours_from_deficit(daily_status.deficit_percent)
        )
        horizon_windows.extend(
            generate_rolling_outage_schedule(
                current_schedule_date,
                daily_status.deficit_percent,
                queue=settings.outage_queue,
                seed=settings.outage_schedule_seed,
                timezone_name=settings.local_timezone,
            )
        )
        current_schedule_date += timedelta(days=1)
    horizon_windows = _deduplicate_outage_windows(horizon_windows)

    points: list[GridAvailabilityPoint] = []
    current_date = start_date
    while current_date <= end_date:
        daily_outage_hours = daily_outage_hours_by_date[current_date]
        day_start_utc = datetime.combine(
            current_date,
            time.min,
            tzinfo=timezone_info,
        ).astimezone(timezone.utc)
        day_end_utc = datetime.combine(
            current_date + timedelta(days=1),
            time.min,
            tzinfo=timezone_info,
        ).astimezone(timezone.utc)
        timestamp_utc = day_start_utc
        while timestamp_utc < day_end_utc:
            points.append(
                build_grid_availability_point(
                    timestamp_utc=timestamp_utc,
                    events=events,
                    outage_windows=horizon_windows,
                    daily_outage_hours=daily_outage_hours,
                    settings=settings,
                )
            )
            timestamp_utc += timedelta(minutes=GRID_AVAILABILITY_CADENCE_MINUTES)
        current_date += timedelta(days=1)
    return points, events


def build_grid_availability_point(
    timestamp_utc: datetime,
    events: list[GridDamageEvent],
    outage_windows: list[OutageWindow],
    daily_outage_hours: float,
    settings: GridSimulationSettings,
) -> GridAvailabilityPoint:
    timestamp_utc = _as_utc(timestamp_utc)
    timestamp_local = timestamp_utc.astimezone(ZoneInfo(settings.local_timezone))
    status = calculate_health_from_events(events, timestamp_utc, settings=settings)
    current_window, next_window = outage_window_context(timestamp_utc, outage_windows)
    is_outage_now = current_window is not None
    return GridAvailabilityPoint(
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_local,
        generation_health_percent=status.generation_health_percent,
        delivery_health_percent=status.delivery_health_percent,
        effective_health_percent=status.effective_health_percent,
        deficit_percent=status.deficit_percent,
        daily_outage_hours=daily_outage_hours,
        outage_level=status.outage_level,
        outage_queue=settings.outage_queue,
        local_grid_available=not is_outage_now,
        is_outage_now=is_outage_now,
        grid_voltage_v=grid_voltage_visual(
            timestamp_utc,
            status.deficit_percent,
            is_outage_now,
            seed=settings.outage_schedule_seed,
        ),
        reason=status.reason,
        current_outage_window_start_utc=(
            None if current_window is None else current_window.start_utc
        ),
        current_outage_window_end_utc=(
            None if current_window is None else current_window.end_utc
        ),
        next_outage_window_start_utc=(
            None if next_window is None else next_window.start_utc
        ),
        next_outage_window_end_utc=(
            None if next_window is None else next_window.end_utc
        ),
    )


def outage_window_context(
    timestamp_utc: datetime,
    windows: list[OutageWindow],
) -> tuple[OutageWindow | None, OutageWindow | None]:
    timestamp = _as_utc(timestamp_utc)
    sorted_windows = sorted(windows, key=lambda value: value.start_utc)
    current_window = _continuous_current_outage_window(timestamp, sorted_windows)
    if current_window is not None:
        next_window = _next_outage_window_after(
            current_window.end_utc,
            sorted_windows,
        )
        return current_window, next_window

    for window in sorted_windows:
        if window.start_utc > timestamp:
            return None, window
    return None, None


def _continuous_current_outage_window(
    timestamp_utc: datetime,
    sorted_windows: list[OutageWindow],
) -> OutageWindow | None:
    group_start_utc: datetime | None = None
    group_end_utc: datetime | None = None
    timezone_info = None
    for window in sorted_windows:
        if window.end_utc <= timestamp_utc:
            continue
        if window.start_utc <= timestamp_utc < window.end_utc:
            group_start_utc = window.start_utc
            group_end_utc = window.end_utc
            timezone_info = window.start_local.tzinfo
            break
        if window.start_utc > timestamp_utc:
            break

    if group_start_utc is None or group_end_utc is None or timezone_info is None:
        return None

    expanded = True
    while expanded:
        expanded = False
        for window in sorted_windows:
            if window.end_utc < group_start_utc:
                continue
            if window.start_utc > group_end_utc:
                break
            if window.start_utc <= group_end_utc and window.end_utc >= group_start_utc:
                next_start = min(group_start_utc, window.start_utc)
                next_end = max(group_end_utc, window.end_utc)
                if next_start != group_start_utc or next_end != group_end_utc:
                    group_start_utc = next_start
                    group_end_utc = next_end
                    expanded = True

    return OutageWindow(
        start_local=group_start_utc.astimezone(timezone_info),
        end_local=group_end_utc.astimezone(timezone_info),
    )


def _next_outage_window_after(
    timestamp_utc: datetime,
    sorted_windows: list[OutageWindow],
) -> OutageWindow | None:
    for window in sorted_windows:
        if window.start_utc > timestamp_utc:
            return window
    return None


def _deduplicate_outage_windows(
    windows: list[OutageWindow],
) -> list[OutageWindow]:
    seen: set[tuple[datetime, datetime]] = set()
    unique_windows: list[OutageWindow] = []
    for window in sorted(windows, key=lambda value: value.start_utc):
        key = (window.start_utc, window.end_utc)
        if key in seen:
            continue
        seen.add(key)
        unique_windows.append(window)
    return unique_windows


def season_intensity(current_date: date) -> float:
    if current_date.month in (11, 12, 1, 2):
        return 1.0
    if current_date.month in (4, 5, 6, 7, 8):
        return 0.0
    if current_date.month == 3:
        return _clamp(1.0 - (current_date.day - 1) / 30.0, 0.0, 1.0)
    if current_date.month == 9:
        return _clamp((current_date.day - 1) / 60.0, 0.0, 1.0)
    if current_date.month == 10:
        return _clamp((30.0 + current_date.day) / 60.0, 0.0, 1.0)
    return 0.0


def energy_target_probability(current_date: date, rng: random.Random) -> float:
    winter = season_intensity(current_date)
    summer_value = truncated_normal(rng, 0.20, 0.10, 0.00, 0.40)
    winter_value = truncated_normal(rng, 0.70, 0.15, 0.40, 1.00)
    return summer_value + (winter_value - summer_value) * winter


def sample_defence_efficiency(element_type: ElementType, rng: random.Random) -> float:
    if element_type == "uav":
        return truncated_normal(rng, 0.90, 0.035, 0.78, 0.95)
    if element_type == "cruise":
        return truncated_normal(rng, 0.78, 0.08, 0.60, 0.92)
    return truncated_normal(rng, 0.35, 0.06, 0.20, 0.45)


def sample_damage_amount(
    element_type: ElementType,
    rng: random.Random,
) -> tuple[str, float]:
    draw = rng.random()
    if element_type == "uav":
        if draw < 0.80:
            return "fast", truncated_normal(rng, 0.30, 0.18, 0.05, 0.80)
        return "medium_low", truncated_normal(rng, 1.20, 0.50, 0.50, 2.50)
    if element_type == "cruise":
        if draw < 0.20:
            return "fast", truncated_normal(rng, 0.90, 0.35, 0.30, 1.80)
        if draw < 0.90:
            return "medium", truncated_normal(rng, 3.20, 1.20, 1.20, 6.50)
        return "heavy", truncated_normal(rng, 7.00, 2.00, 4.00, 12.00)
    if draw < 0.55:
        return "medium", truncated_normal(rng, 5.00, 1.70, 2.00, 9.00)
    if draw < 0.90:
        return "heavy", truncated_normal(rng, 10.00, 3.00, 5.00, 17.00)
    return "structural", truncated_normal(rng, 16.00, 5.00, 8.00, 28.00)


def split_damage_by_element(
    element_type: ElementType,
    damage_amount: float,
    rng: random.Random,
) -> tuple[float, float]:
    if element_type == "uav":
        delivery_probability = 0.70
    elif element_type == "cruise":
        delivery_probability = 0.50
    else:
        delivery_probability = 0.35

    if bernoulli(delivery_probability, rng):
        return 0.0, damage_amount
    return damage_amount, 0.0


def sample_recovery_days(damage_class: str, rng: random.Random) -> float:
    recovery_class = _recovery_class(damage_class)
    if recovery_class == "fast":
        return rng.uniform(2.0, 7.0)
    if recovery_class == "medium":
        return rng.uniform(7.0, 45.0)
    if recovery_class == "heavy":
        return rng.uniform(30.0, 180.0)
    return rng.uniform(180.0, 540.0)


def truncated_normal(
    rng: random.Random,
    mean: float,
    sd: float,
    min_value: float,
    max_value: float,
) -> float:
    value = mean
    for _ in range(40):
        value = rng.gauss(mean, sd)
        if min_value <= value <= max_value:
            return value
    return _clamp(value, min_value, max_value)


def bernoulli(probability: float, rng: random.Random) -> bool:
    return rng.random() < _clamp(probability, 0.0, 1.0)


def binomial(n: int, probability: float, rng: random.Random) -> int:
    return sum(1 for _ in range(max(0, n)) if bernoulli(probability, rng))


def _attack_state_probabilities(
    local_date: date,
    previous_state: AttackState,
) -> dict[AttackState, float]:
    winter = season_intensity(local_date)
    probabilities: dict[AttackState, float] = {
        "quiet": 0.50 - 0.28 * winter,
        "routine_uav": 0.35 - 0.01 * winter,
        "dense_uav": 0.08 + 0.18 * winter,
        "combined": 0.02 + 0.16 * winter,
        "cooldown": 0.05 - 0.05 * winter,
    }
    if previous_state == "dense_uav":
        probabilities["combined"] += 0.20
        probabilities["quiet"] *= 0.75
        probabilities["routine_uav"] *= 0.80
    elif previous_state == "combined":
        probabilities = {
            "quiet": 0.20,
            "routine_uav": 0.10,
            "dense_uav": 0.03,
            "combined": 0.02,
            "cooldown": 0.65,
        }
    elif previous_state == "cooldown":
        probabilities["quiet"] += 0.20
        probabilities["combined"] *= 0.40
        probabilities["dense_uav"] *= 0.60
    return _normalize_weights(probabilities)


def _kyiv_focus_probabilities(attack_state: AttackState) -> dict[KyivFocusMode, float]:
    if attack_state == "dense_uav":
        return {
            "no_kyiv": 0.30,
            "spillover": 0.25,
            "secondary": 0.30,
            "primary": 0.15,
        }
    if attack_state == "combined":
        return {
            "no_kyiv": 0.15,
            "spillover": 0.15,
            "secondary": 0.30,
            "primary": 0.40,
        }
    if attack_state in ("quiet", "cooldown"):
        return _normalize_weights(
            {
                "no_kyiv": 0.65,
                "spillover": 0.22,
                "secondary": 0.11,
                "primary": 0.02,
            }
        )
    return {
        "no_kyiv": 0.45,
        "spillover": 0.30,
        "secondary": 0.20,
        "primary": 0.05,
    }


def _remaining_damage_fraction(
    damage_class: str,
    elapsed_days: float,
    recovery_days: float,
) -> float:
    if recovery_days <= 0.0:
        return 0.0
    recovery_class = _recovery_class(damage_class)
    quick_recovery = {
        "fast": 0.40,
        "medium": 0.10,
        "heavy": 0.02,
        "structural": 0.0,
    }[recovery_class]
    if elapsed_days <= 1.0:
        return _clamp(1.0 - quick_recovery * elapsed_days, 0.0, 1.0)
    if recovery_days <= 1.0:
        return 0.0
    tail_progress = _clamp((elapsed_days - 1.0) / (recovery_days - 1.0), 0.0, 1.0)
    return _clamp((1.0 - quick_recovery) * (1.0 - tail_progress), 0.0, 1.0)


def _recovery_class(damage_class: str) -> str:
    if damage_class in ("fast", "medium", "heavy", "structural"):
        return damage_class
    if damage_class == "medium_low":
        return "medium"
    return "medium"


def _split_outage_units(total_units: int) -> list[int]:
    if total_units <= 0:
        return []
    if total_units <= 8:
        preferred_block_units = 4
    elif total_units <= 18:
        preferred_block_units = 5
    else:
        preferred_block_units = 8
    block_count = max(1, math.ceil(total_units / preferred_block_units))
    base_units = total_units // block_count
    remainder = total_units % block_count
    return [
        base_units + (1 if index < remainder else 0)
        for index in range(block_count)
    ]


def _split_powered_gap_units(powered_units: int, block_count: int) -> list[int]:
    if block_count <= 0:
        return []
    powered_units = max(0, powered_units)
    base_units = powered_units // block_count
    remainder = powered_units % block_count
    return [
        base_units + (1 if index < remainder else 0)
        for index in range(block_count)
    ]


def _queue_index(queue: str) -> int:
    try:
        major_text, minor_text = queue.split(".", maxsplit=1)
        major = int(major_text)
        minor = int(minor_text)
    except ValueError:
        return _stable_seed("queue", queue) % 12
    major_index = _clamp(major, 1, 6) - 1
    minor_index = _clamp(minor, 1, 2) - 1
    return int(major_index * 2 + minor_index)


def _weighted_choice(weights: dict[Any, float], rng: random.Random) -> Any:
    normalized = _normalize_weights(weights)
    draw = rng.random()
    cumulative = 0.0
    last_key: Any = None
    for key, weight in normalized.items():
        cumulative += weight
        last_key = key
        if draw <= cumulative:
            return key
    return last_key


def _normalize_weights(weights: dict[Any, float]) -> dict[Any, float]:
    total = sum(max(0.0, value) for value in weights.values())
    if total <= 0.0:
        equal_weight = 1.0 / len(weights)
        return {key: equal_weight for key in weights}
    return {key: max(0.0, value) / total for key, value in weights.items()}


def _event_key(seed: int, event_date: date, event_index: int) -> str:
    digest = hashlib.sha256(
        f"{seed}|grid-event|{event_date.isoformat()}|{event_index}".encode("utf-8")
    ).hexdigest()
    return digest[:32]


def _rng(seed: int, *parts: object) -> random.Random:
    return random.Random(_stable_seed(seed, *parts))


def _stable_seed(*parts: object) -> int:
    payload = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _count(value: float) -> int:
    return max(0, int(round(value)))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime is required")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
