from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from math import isfinite
from typing import Callable, Literal, Mapping, Any
from zoneinfo import ZoneInfo

import pandas as pd
import pvlib


GridBehaviorState = Literal[
    "grid_normal",
    "planned_outage_soon",
    "outage_active",
    "post_outage_recovery",
]
SocBand = Literal["comfortable", "cautious", "restricted", "critical", "emergency"]
ProfessorDailyState = Literal[
    "present",
    "absent_ill",
    "absent_holiday",
    "absent_field_work",
    "absent_regular",
]
ProfessorActivity = Literal[
    "workstation_work",
    "soldering_work",
    "short_absence",
    "coffee_break",
    "consultation",
    "idle_present",
]
LessonSubtype = Literal[
    "debugging_session",
    "electronics_practice",
    "soldering_practice",
]
StudentVisitActivity = Literal[
    "laptop_debugging",
    "soldering_project",
    "consultation",
    "quick_visit",
]
LaptopProfile = Literal["macbook", "business", "gaming"]


@dataclass(frozen=True)
class DeviceLoadDefinition:
    id: str
    group: str
    count: int
    standby_w: float
    typical_w: float
    active_w: float
    peak_w: float
    behavior: str


@dataclass(frozen=True)
class LoadSimulationSettings:
    seed: int = 20260529
    timezone_name: str = "Europe/Kyiv"
    station_latitude: float | None = None
    station_longitude: float | None = None
    professor_count: int = 5
    workstation_count: int = 5
    soldering_workplace_count: int = 6
    default_soc_percent: float = 80.0
    class_weekdays: tuple[int, int] = (1, 3)
    class_start: time = time(14, 0)
    class_duration_minutes: int = 90
    enable_professors: bool = True
    enable_student_classes: bool = True
    enable_random_student_visits: bool = True
    enable_kettle_events: bool = True
    force_professor_daily_states: Mapping[int, ProfessorDailyState] = field(
        default_factory=dict,
    )

    @property
    def baseline_power_w(self) -> float:
        return critical_internet_baseline_w()


@dataclass(frozen=True)
class LoadContext:
    grid_behavior: GridBehaviorState = "grid_normal"
    grid_available: bool = True
    soc_percent: float | None = None
    weather_state: str = "clear"
    is_dark: bool = False
    sunrise_local: datetime | None = None
    sunset_local: datetime | None = None
    force_present_professors: int | None = None
    force_student_count: int | None = None
    force_kettle_active: bool = False
    force_high_power_events: bool = False
    force_workstation_only: bool = False


@dataclass(frozen=True)
class OneMinuteLoadPoint:
    timestamp_utc: datetime
    timestamp_local: datetime
    total_power_draw_w: float
    active_professor_count: int = 0
    active_student_count: int = 0
    active_event_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class FifteenMinuteLoadAggregate:
    timestamp_utc: datetime
    timestamp_local: datetime
    momentary_power_w: float
    energy_wh_last_15m: float
    active_professor_count: int = 0
    active_student_count: int = 0
    active_event_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfessorDayPlan:
    professor_index: int
    local_date: date
    state: ProfessorDailyState
    arrival_local: datetime | None
    departure_local: datetime | None
    profile: str


@dataclass(frozen=True)
class StudentClassEvent:
    local_date: date
    start_local: datetime
    end_local: datetime
    student_count: int
    subtype: LessonSubtype


@dataclass(frozen=True)
class StudentVisitEvent:
    local_date: date
    start_local: datetime
    end_local: datetime
    student_count: int
    activity: StudentVisitActivity


@dataclass(frozen=True)
class KettleWindow:
    start_local: datetime
    end_local: datetime


ContextProvider = Callable[[datetime], LoadContext]


class LoadDaylightConfigurationError(ValueError):
    pass


DEVICE_CATALOG: tuple[DeviceLoadDefinition, ...] = (
    DeviceLoadDefinition("onu", "critical_internet", 1, 5.0, 6.0, 7.0, 8.0, "always_on"),
    DeviceLoadDefinition("router", "critical_internet", 1, 8.0, 10.0, 12.0, 12.0, "always_on"),
    DeviceLoadDefinition("ethernet_switch", "critical_internet", 1, 3.0, 4.0, 5.0, 6.0, "always_on"),
    DeviceLoadDefinition("monoblock", "workstations", 5, 5.0, 45.0, 65.0, 90.0, "person_workstation"),
    DeviceLoadDefinition("workstation_desk_lamp", "workstations", 5, 0.0, 6.0, 6.0, 7.0, "person_lighting"),
    DeviceLoadDefinition("ceiling_led_lamp", "room_lighting", 2, 0.0, 40.0, 40.0, 45.0, "shared_lighting"),
    DeviceLoadDefinition("soldering_station", "soldering_table", 6, 3.0, 25.0, 60.0, 75.0, "soldering"),
    DeviceLoadDefinition("soldering_desk_lamp", "soldering_table", 6, 0.0, 6.0, 6.0, 7.0, "soldering_lighting"),
    DeviceLoadDefinition("lab_psu", "soldering_table", 6, 2.0, 10.0, 30.0, 200.0, "lab_equipment"),
    DeviceLoadDefinition("hot_glue_gun", "soldering_table", 6, 0.0, 20.0, 40.0, 60.0, "short_thermostat"),
    DeviceLoadDefinition("phone_charger", "personal", 11, 0.0, 5.0, 12.0, 20.0, "person_event"),
    DeviceLoadDefinition("student_macbook", "student_laptops", 6, 0.0, 20.0, 30.0, 30.0, "student_laptop"),
    DeviceLoadDefinition("student_business_laptop", "student_laptops", 6, 0.0, 45.0, 65.0, 65.0, "student_laptop"),
    DeviceLoadDefinition("student_gaming_laptop", "student_laptops", 6, 0.0, 140.0, 230.0, 230.0, "student_laptop"),
    DeviceLoadDefinition("hand_drill", "high_power", 1, 0.0, 0.0, 400.0, 450.0, "short_high_power"),
    DeviceLoadDefinition("heat_gun", "high_power", 1, 0.0, 0.0, 1500.0, 1500.0, "high_power"),
    DeviceLoadDefinition("kettle", "high_power", 1, 0.0, 0.0, 1200.0, 1200.0, "shared_event"),
)


DEVICE_BY_ID = {device.id: device for device in DEVICE_CATALOG}
HIGH_POWER_TAGS = {"kettle", "heat_gun", "hand_drill"}
LIGHT_OBSTRUCTING_WEATHER_STATES = {
    "cloudy",
    "drizzle",
    "fog",
    "foggy",
    "fully_cloudy",
    "heavy_cloud",
    "mist",
    "overcast",
    "rain",
    "sleet",
    "snow",
    "thunderstorm",
}
DARK_WEATHER_STATES = LIGHT_OBSTRUCTING_WEATHER_STATES


class LoadSimulator:
    def __init__(self, settings: LoadSimulationSettings | None = None) -> None:
        self.settings = settings or LoadSimulationSettings()
        self.timezone = _zone_info_or_raise(self.settings.timezone_name)
        self._calendar_cache: dict[int, dict[int, dict[date, ProfessorDailyState]]] = {}

    def professor_daily_states(
        self,
        year: int,
    ) -> dict[int, dict[date, ProfessorDailyState]]:
        if year not in self._calendar_cache:
            self._calendar_cache[year] = build_professor_year_calendar(
                year,
                self.settings,
            )
        return self._calendar_cache[year]

    def generate_one_minute_points(
        self,
        start: datetime,
        end: datetime,
        context_provider: ContextProvider | LoadContext | None = None,
    ) -> list[OneMinuteLoadPoint]:
        start_utc = _as_utc(start)
        end_utc = _as_utc(end)
        if end_utc <= start_utc:
            raise ValueError("end must be later than start")

        points: list[OneMinuteLoadPoint] = []
        current = start_utc.replace(second=0, microsecond=0)
        while current < end_utc:
            context = _resolve_context(context_provider, current)
            points.append(self.build_point(current, context))
            current += timedelta(minutes=1)
        return points

    def build_point(
        self,
        timestamp_utc: datetime,
        context: LoadContext | None = None,
    ) -> OneMinuteLoadPoint:
        timestamp_utc = _as_utc(timestamp_utc).replace(second=0, microsecond=0)
        timestamp_local = timestamp_utc.astimezone(self.timezone)
        resolved_context = context or LoadContext()
        grid_behavior = effective_grid_behavior(resolved_context)
        soc_percent = _resolved_soc(resolved_context, self.settings)
        soc_band = soc_band_for_percent(soc_percent)
        tags: set[str] = {"critical_internet"}
        total_power = self.settings.baseline_power_w

        professor_activities = self._active_professor_activities(
            timestamp_local,
            resolved_context,
        )
        active_professors = len(professor_activities)
        active_workstation_professors = sum(
            1
            for activity in professor_activities.values()
            if activity in {"workstation_work", "consultation", "idle_present"}
        )
        active_soldering_professors = sum(
            1
            for activity in professor_activities.values()
            if activity == "soldering_work"
        )

        class_event = None
        visit_event = None
        forced_students = _bounded_count(
            resolved_context.force_student_count,
            self.settings.soldering_workplace_count,
        )
        if forced_students is None and not resolved_context.force_workstation_only:
            class_event = self._student_class_at(timestamp_local)
            visit_event = self._student_visit_at(timestamp_local, active_professors)
        active_students = (
            forced_students
            if forced_students is not None
            else (
                (class_event.student_count if class_event is not None else 0)
                + (visit_event.student_count if visit_event is not None else 0)
            )
        )

        if active_workstation_professors:
            tags.add("professor_workstation")
            total_power += active_workstation_professors * DEVICE_BY_ID["monoblock"].active_w

        phone_chargers = self._phone_charger_count(
            timestamp_local,
            active_professors,
            active_students,
            grid_behavior,
            soc_band,
        )
        if phone_chargers:
            tags.add("phone_charging")
            total_power += phone_chargers * DEVICE_BY_ID["phone_charger"].active_w

        soldering_workplaces = 0
        lab_psus = 0
        soldering_laptops_w = 0.0
        hot_glue_active = 0
        heat_gun_active = False
        drill_active = False

        if active_soldering_professors:
            allowed_professor_soldering = _scaled_count(
                active_soldering_professors,
                soldering_multiplier(grid_behavior, soc_band),
            )
            soldering_workplaces += allowed_professor_soldering
            lab_psus += allowed_professor_soldering
            if allowed_professor_soldering:
                tags.add("professor_soldering")

        if class_event is not None:
            class_load = self._class_equipment_load(
                timestamp_local,
                class_event,
                grid_behavior,
                soc_band,
            )
            soldering_workplaces += class_load["soldering_workplaces"]
            lab_psus += class_load["lab_psus"]
            soldering_laptops_w += class_load["laptop_power_w"]
            hot_glue_active += class_load["hot_glue"]
            heat_gun_active = heat_gun_active or bool(class_load["heat_gun"])
            tags.update(class_load["tags"])

        if visit_event is not None:
            visit_load = self._visit_equipment_load(
                timestamp_local,
                visit_event,
                grid_behavior,
                soc_band,
                supervised=active_professors > 0,
            )
            soldering_workplaces += visit_load["soldering_workplaces"]
            lab_psus += visit_load["lab_psus"]
            soldering_laptops_w += visit_load["laptop_power_w"]
            hot_glue_active += visit_load["hot_glue"]
            heat_gun_active = heat_gun_active or bool(visit_load["heat_gun"])
            drill_active = drill_active or bool(visit_load["drill"])
            tags.update(visit_load["tags"])

        if resolved_context.force_high_power_events and active_professors + active_students > 0:
            heat_gun_active = heat_gun_active or high_power_allowed(
                "heat_gun",
                grid_behavior,
                soc_band,
            )
            drill_active = drill_active or high_power_allowed(
                "hand_drill",
                grid_behavior,
                soc_band,
            )

        soldering_workplaces = min(
            self.settings.soldering_workplace_count,
            max(0, soldering_workplaces),
        )
        lab_psus = min(self.settings.soldering_workplace_count, max(0, lab_psus))
        hot_glue_active = min(self.settings.soldering_workplace_count, max(0, hot_glue_active))

        if soldering_workplaces:
            tags.add("soldering_work")
            total_power += soldering_workplaces * self._soldering_station_power(
                timestamp_local,
            )
        if lab_psus:
            tags.add("lab_psu")
            total_power += lab_psus * DEVICE_BY_ID["lab_psu"].active_w
        if soldering_laptops_w:
            tags.add("student_laptops")
            total_power += soldering_laptops_w
        if hot_glue_active and high_power_allowed("hot_glue_gun", grid_behavior, soc_band):
            tags.add("hot_glue")
            total_power += hot_glue_active * DEVICE_BY_ID["hot_glue_gun"].active_w
        if heat_gun_active and high_power_allowed("heat_gun", grid_behavior, soc_band):
            tags.add("heat_gun")
            total_power += DEVICE_BY_ID["heat_gun"].active_w
        if drill_active and high_power_allowed("hand_drill", grid_behavior, soc_band):
            tags.add("hand_drill")
            total_power += DEVICE_BY_ID["hand_drill"].active_w

        people_count = active_professors + active_students
        kettle_active = self._kettle_active(
            timestamp_local,
            people_count,
            class_event is not None,
            resolved_context,
            grid_behavior,
            soc_band,
        )
        if kettle_active:
            tags.add("kettle")
            total_power += DEVICE_BY_ID["kettle"].active_w

        if people_count > 0:
            lighting_factor = lighting_need_factor(
                timestamp_local,
                resolved_context,
                self.settings,
            )
        else:
            lighting_factor = 0.0
        if lighting_factor > 0.0:
            workstation_lamps, soldering_lamps, ceiling_lamps = lighting_counts(
                active_workstation_professors=active_workstation_professors,
                active_soldering_workplaces=soldering_workplaces,
                active_students=active_students,
                grid_behavior=grid_behavior,
                soc_band=soc_band,
                workstation_only=(
                    soldering_workplaces == 0
                    and class_event is None
                    and visit_event is None
                    and not resolved_context.force_high_power_events
                ),
                lighting_need_factor=lighting_factor,
            )
            if workstation_lamps or soldering_lamps or ceiling_lamps:
                tags.add("dark_condition")
            if workstation_lamps:
                tags.add("workstation_lamps")
                total_power += workstation_lamps * DEVICE_BY_ID["workstation_desk_lamp"].active_w
            if soldering_lamps:
                tags.add("soldering_lamps")
                total_power += soldering_lamps * DEVICE_BY_ID["soldering_desk_lamp"].active_w
            if ceiling_lamps:
                tags.add("ceiling_lamp")
                total_power += ceiling_lamps * DEVICE_BY_ID["ceiling_led_lamp"].active_w

        return OneMinuteLoadPoint(
            timestamp_utc=timestamp_utc,
            timestamp_local=timestamp_local,
            total_power_draw_w=round(total_power, 4),
            active_professor_count=active_professors,
            active_student_count=active_students,
            active_event_tags=tuple(sorted(tags)),
        )

    def _active_professor_activities(
        self,
        timestamp_local: datetime,
        context: LoadContext,
    ) -> dict[int, ProfessorActivity]:
        forced_count = _bounded_count(context.force_present_professors, self.settings.professor_count)
        if forced_count is not None:
            return {
                professor_index: "workstation_work"
                for professor_index in range(forced_count)
            }
        if not self.settings.enable_professors:
            return {}

        states_by_professor = self.professor_daily_states(timestamp_local.year)
        active: dict[int, ProfessorActivity] = {}
        for professor_index in range(self.settings.professor_count):
            day_plan = self._professor_day_plan(
                professor_index,
                timestamp_local.date(),
                states_by_professor[professor_index],
            )
            if not _is_inside_plan(timestamp_local, day_plan):
                continue
            activity = self._professor_activity(professor_index, timestamp_local)
            if activity == "short_absence":
                continue
            active[professor_index] = activity
        return active

    def _professor_day_plan(
        self,
        professor_index: int,
        local_date: date,
        states: Mapping[date, ProfessorDailyState],
    ) -> ProfessorDayPlan:
        forced_state = self.settings.force_professor_daily_states.get(professor_index)
        state = forced_state or states.get(local_date, "absent_regular")
        profile = "young_enthusiast" if professor_index == 0 else "regular"
        if state != "present":
            return ProfessorDayPlan(professor_index, local_date, state, None, None, profile)

        rng = _rng(self.settings.seed, "professor-plan", professor_index, local_date)
        if profile == "young_enthusiast":
            arrival_minutes = rng.randint(8 * 60 + 15, 11 * 60 + 15)
            duration_minutes = rng.randint(6 * 60, 10 * 60)
            if rng.random() < 0.08:
                duration_minutes += rng.randint(2 * 60, 5 * 60)
            if rng.random() < 0.02:
                duration_minutes += rng.randint(8 * 60, 12 * 60)
        else:
            arrival_minutes = rng.randint(8 * 60 + 30, 11 * 60)
            duration_minutes = rng.randint(5 * 60, 9 * 60)
        arrival = datetime.combine(local_date, time.min, tzinfo=self.timezone) + timedelta(
            minutes=arrival_minutes,
        )
        departure = arrival + timedelta(minutes=duration_minutes)
        return ProfessorDayPlan(professor_index, local_date, state, arrival, departure, profile)

    def _professor_activity(
        self,
        professor_index: int,
        timestamp_local: datetime,
    ) -> ProfessorActivity:
        bucket = _minute_bucket(timestamp_local, 15)
        rng = _rng(
            self.settings.seed,
            "professor-activity",
            professor_index,
            timestamp_local.date(),
            bucket,
        )
        young = professor_index == 0
        draw = rng.random()
        if young:
            if draw < 0.62:
                return "workstation_work"
            if draw < 0.80:
                return "soldering_work"
            if draw < 0.88:
                return "consultation"
            if draw < 0.94:
                return "short_absence"
            if draw < 0.98:
                return "coffee_break"
            return "idle_present"
        if draw < 0.74:
            return "workstation_work"
        if draw < 0.84:
            return "soldering_work"
        if draw < 0.90:
            return "consultation"
        if draw < 0.96:
            return "short_absence"
        if draw < 0.985:
            return "coffee_break"
        return "idle_present"

    def _student_class_at(self, timestamp_local: datetime) -> StudentClassEvent | None:
        if not self.settings.enable_student_classes:
            return None
        if timestamp_local.weekday() not in self.settings.class_weekdays:
            return None
        start = datetime.combine(
            timestamp_local.date(),
            self.settings.class_start,
            tzinfo=self.timezone,
        )
        end = start + timedelta(minutes=self.settings.class_duration_minutes)
        if not (start <= timestamp_local < end):
            return None
        rng = _rng(self.settings.seed, "class-subtype", timestamp_local.date())
        draw = rng.random()
        if draw < 0.15:
            subtype: LessonSubtype = "debugging_session"
        elif draw < 0.65:
            subtype = "electronics_practice"
        else:
            subtype = "soldering_practice"
        return StudentClassEvent(
            local_date=timestamp_local.date(),
            start_local=start,
            end_local=end,
            student_count=self.settings.soldering_workplace_count,
            subtype=subtype,
        )

    def _student_visit_at(
        self,
        timestamp_local: datetime,
        active_professors: int,
    ) -> StudentVisitEvent | None:
        if not self.settings.enable_random_student_visits:
            return None
        rng = _rng(self.settings.seed, "student-visit-day", timestamp_local.date())
        day_probability = 0.48 if timestamp_local.weekday() < 5 else 0.10
        if rng.random() >= day_probability:
            return None
        count = rng.randint(1, 3)
        duration_minutes = rng.randint(60, 8 * 60)
        start_minutes = rng.randint(9 * 60, 17 * 60)
        start = datetime.combine(
            timestamp_local.date(),
            time.min,
            tzinfo=self.timezone,
        ) + timedelta(minutes=start_minutes)
        end = start + timedelta(minutes=duration_minutes)
        if not (start <= timestamp_local < end):
            return None

        activity_draw = rng.random()
        if activity_draw < 0.35:
            activity: StudentVisitActivity = "laptop_debugging"
        elif activity_draw < 0.65:
            activity = "soldering_project"
        elif activity_draw < 0.85:
            activity = "consultation"
        else:
            activity = "quick_visit"
        if activity == "consultation" and active_professors <= 0:
            return None
        if activity == "soldering_project" and active_professors <= 0:
            return None
        return StudentVisitEvent(
            local_date=timestamp_local.date(),
            start_local=start,
            end_local=end,
            student_count=count,
            activity=activity,
        )

    def _class_equipment_load(
        self,
        timestamp_local: datetime,
        event: StudentClassEvent,
        grid_behavior: GridBehaviorState,
        soc_band: SocBand,
    ) -> dict[str, object]:
        elapsed_minutes = int((timestamp_local - event.start_local).total_seconds() // 60)
        rng = _rng(
            self.settings.seed,
            "class-minute",
            event.local_date,
            event.subtype,
            elapsed_minutes,
        )
        if event.subtype == "debugging_session":
            laptop_ratio, psu_ratio, solder_ratio = 0.90, 0.65, 0.20
            hot_glue_probability, heat_gun_probability = 0.02, 0.0
        elif event.subtype == "electronics_practice":
            laptop_ratio, psu_ratio, solder_ratio = 0.65, 0.75, 0.55
            hot_glue_probability, heat_gun_probability = 0.10, 0.03
        else:
            laptop_ratio, psu_ratio, solder_ratio = 0.35, 0.45, 0.90
            hot_glue_probability, heat_gun_probability = 0.18, 0.07

        modifier = soldering_multiplier(grid_behavior, soc_band)
        soldering_count = _scaled_count(event.student_count, solder_ratio * modifier)
        psu_count = _scaled_count(event.student_count, psu_ratio * _equipment_multiplier(grid_behavior, soc_band))
        laptop_count = _scaled_count(event.student_count, laptop_ratio * laptop_multiplier(grid_behavior, soc_band))
        laptop_power = sum(
            self._student_laptop_power(event.local_date, index, grid_behavior, soc_band)
            for index in range(laptop_count)
        )
        hot_glue = 1 if rng.random() < hot_glue_probability * _equipment_multiplier(grid_behavior, soc_band) else 0
        heat_gun = (
            rng.random() < heat_gun_probability * high_power_probability_multiplier(
                "heat_gun",
                grid_behavior,
                soc_band,
            )
            and 20 <= elapsed_minutes <= 75
            and high_power_allowed("heat_gun", grid_behavior, soc_band)
        )
        return {
            "soldering_workplaces": soldering_count,
            "lab_psus": psu_count,
            "laptop_power_w": laptop_power,
            "hot_glue": hot_glue,
            "heat_gun": heat_gun,
            "drill": False,
            "tags": {"student_class", event.subtype},
        }

    def _visit_equipment_load(
        self,
        timestamp_local: datetime,
        event: StudentVisitEvent,
        grid_behavior: GridBehaviorState,
        soc_band: SocBand,
        supervised: bool,
    ) -> dict[str, object]:
        elapsed_minutes = int((timestamp_local - event.start_local).total_seconds() // 60)
        rng = _rng(
            self.settings.seed,
            "visit-minute",
            event.local_date,
            event.activity,
            elapsed_minutes,
        )
        soldering_count = 0
        psu_count = 0
        laptop_count = 0
        hot_glue = 0
        heat_gun = False
        drill = False
        if event.activity == "laptop_debugging":
            laptop_count = event.student_count
            psu_count = _scaled_count(event.student_count, 0.35)
        elif event.activity == "soldering_project" and supervised:
            modifier = soldering_multiplier(grid_behavior, soc_band)
            soldering_count = _scaled_count(event.student_count, 0.80 * modifier)
            psu_count = _scaled_count(event.student_count, 0.70 * _equipment_multiplier(grid_behavior, soc_band))
            laptop_count = _scaled_count(event.student_count, 0.55 * laptop_multiplier(grid_behavior, soc_band))
            hot_glue = 1 if rng.random() < 0.10 * _equipment_multiplier(grid_behavior, soc_band) else 0
            heat_gun = (
                rng.random() < 0.04 * high_power_probability_multiplier(
                    "heat_gun",
                    grid_behavior,
                    soc_band,
                )
                and high_power_allowed("heat_gun", grid_behavior, soc_band)
            )
            drill = (
                rng.random() < 0.03 * high_power_probability_multiplier(
                    "hand_drill",
                    grid_behavior,
                    soc_band,
                )
                and high_power_allowed("hand_drill", grid_behavior, soc_band)
            )
        elif event.activity == "consultation":
            laptop_count = _scaled_count(event.student_count, 0.50)
        elif event.activity == "quick_visit":
            laptop_count = _scaled_count(event.student_count, 0.15)
        laptop_power = sum(
            self._student_laptop_power(event.local_date, index + 20, grid_behavior, soc_band)
            for index in range(laptop_count)
        )
        return {
            "soldering_workplaces": soldering_count,
            "lab_psus": psu_count,
            "laptop_power_w": laptop_power,
            "hot_glue": hot_glue,
            "heat_gun": heat_gun,
            "drill": drill,
            "tags": {"student_visit", event.activity},
        }

    def _student_laptop_power(
        self,
        local_date: date,
        student_index: int,
        grid_behavior: GridBehaviorState,
        soc_band: SocBand,
    ) -> float:
        profile = student_laptop_profile(self.settings.seed, local_date, student_index)
        plug_multiplier = laptop_multiplier(grid_behavior, soc_band)
        if profile == "macbook":
            return 22.0 * plug_multiplier
        if profile == "business":
            return 55.0 * plug_multiplier
        return 170.0 * plug_multiplier

    def _phone_charger_count(
        self,
        timestamp_local: datetime,
        active_professors: int,
        active_students: int,
        grid_behavior: GridBehaviorState,
        soc_band: SocBand,
    ) -> int:
        people = active_professors + active_students
        if people <= 0:
            return 0
        rng = _rng(
            self.settings.seed,
            "phone-charging",
            timestamp_local.date(),
            _minute_bucket(timestamp_local, 30),
        )
        base_probability = min(0.80, 0.18 + people * 0.08)
        probability = base_probability * phone_charging_multiplier(grid_behavior, soc_band)
        return sum(1 for _ in range(people) if rng.random() < probability)

    def _soldering_station_power(self, timestamp_local: datetime) -> float:
        minute = timestamp_local.minute % 20
        if minute < 6:
            return DEVICE_BY_ID["soldering_station"].active_w
        return 28.0

    def _kettle_active(
        self,
        timestamp_local: datetime,
        people_count: int,
        class_active: bool,
        context: LoadContext,
        grid_behavior: GridBehaviorState,
        soc_band: SocBand,
    ) -> bool:
        if people_count <= 0:
            return False
        if not high_power_allowed("kettle", grid_behavior, soc_band):
            return False
        if context.force_kettle_active:
            return True
        if not self.settings.enable_kettle_events:
            return False
        windows = self._kettle_windows(timestamp_local.date(), people_count, class_active)
        if not any(window.start_local <= timestamp_local < window.end_local for window in windows):
            return False
        probability = high_power_probability_multiplier("kettle", grid_behavior, soc_band)
        if probability >= 1.0:
            return True
        rng = _rng(
            self.settings.seed,
            "kettle-grid-suppression",
            timestamp_local.date(),
            _minute_bucket(timestamp_local, 15),
            grid_behavior,
            soc_band,
        )
        return rng.random() < probability

    def _kettle_windows(
        self,
        local_date: date,
        people_count: int,
        class_active: bool,
    ) -> list[KettleWindow]:
        if people_count <= 0:
            return []
        rng = _rng(self.settings.seed, "kettle-windows", local_date)
        if people_count == 1:
            count = 1 if rng.random() < 0.35 else 0
        else:
            draw = rng.random()
            if draw < 0.15:
                count = 1
            elif draw < 0.65:
                count = 2
            elif draw < 0.95:
                count = 3
            else:
                count = 4
        if class_active and rng.random() < 0.30:
            count += 1
        base_times = [time(10, 20), time(12, 45), time(15, 35), time(17, 20)]
        windows: list[KettleWindow] = []
        for index in range(min(count, len(base_times))):
            jitter = rng.randint(-12, 12)
            duration = rng.randint(3, 6)
            start = datetime.combine(local_date, base_times[index], tzinfo=self.timezone)
            start = start + timedelta(minutes=jitter)
            windows.append(KettleWindow(start, start + timedelta(minutes=duration)))
        return windows


def generate_one_minute_load_points(
    start: datetime,
    end: datetime,
    settings: LoadSimulationSettings | None = None,
    context_provider: ContextProvider | LoadContext | None = None,
) -> list[OneMinuteLoadPoint]:
    return LoadSimulator(settings).generate_one_minute_points(
        start,
        end,
        context_provider=context_provider,
    )


def load_settings_from_station_config(
    config: Any,
    base_settings: LoadSimulationSettings | None = None,
) -> LoadSimulationSettings:
    settings = base_settings or LoadSimulationSettings()
    try:
        installation = config.station.solar.installation
        latitude = installation.latitude
        longitude = installation.longitude
        timezone_name = installation.timezone
    except AttributeError as exc:
        raise LoadDaylightConfigurationError(
            "station config must provide station.solar.installation latitude, longitude, and timezone"
        ) from exc
    _validate_daylight_settings(timezone_name, latitude, longitude)
    return replace(
        settings,
        timezone_name=str(timezone_name),
        station_latitude=float(latitude),
        station_longitude=float(longitude),
    )


def aggregate_to_15_minute_candidates(
    points: list[OneMinuteLoadPoint],
) -> list[FifteenMinuteLoadAggregate]:
    if not points:
        return []
    ordered_points = sorted(points, key=lambda point: point.timestamp_utc)
    point_by_timestamp = {point.timestamp_utc: point for point in ordered_points}
    aggregates: list[FifteenMinuteLoadAggregate] = []
    for point in ordered_points:
        if point.timestamp_local.minute % 15 != 0:
            continue
        window_start = point.timestamp_utc - timedelta(minutes=15)
        required_timestamps = [
            window_start + timedelta(minutes=offset)
            for offset in range(15)
        ]
        if any(timestamp not in point_by_timestamp for timestamp in required_timestamps):
            continue
        window = [point_by_timestamp[timestamp] for timestamp in required_timestamps]
        energy_wh = sum(sample.total_power_draw_w / 60.0 for sample in window)
        aggregate_tags = tuple(sorted({tag for sample in window for tag in sample.active_event_tags}))
        aggregates.append(
            FifteenMinuteLoadAggregate(
                timestamp_utc=point.timestamp_utc,
                timestamp_local=point.timestamp_local,
                momentary_power_w=point.total_power_draw_w,
                energy_wh_last_15m=round(energy_wh, 4),
                active_professor_count=point.active_professor_count,
                active_student_count=point.active_student_count,
                active_event_tags=aggregate_tags,
            )
        )
    return aggregates


def build_professor_year_calendar(
    year: int,
    settings: LoadSimulationSettings | None = None,
) -> dict[int, dict[date, ProfessorDailyState]]:
    resolved_settings = settings or LoadSimulationSettings()
    calendars: dict[int, dict[date, ProfessorDailyState]] = {}
    days = _year_days(year)
    for professor_index in range(resolved_settings.professor_count):
        calendar: dict[date, ProfessorDailyState] = {}
        holiday_days = _professor_holiday_days(year, professor_index, resolved_settings)
        for holiday in holiday_days:
            calendar[holiday] = "absent_holiday"

        for illness_day in _professor_illness_days(year, professor_index, resolved_settings, holiday_days):
            calendar.setdefault(illness_day, "absent_ill")

        field_days = _professor_field_work_days(year, professor_index, resolved_settings, set(calendar))
        for field_day in field_days:
            calendar.setdefault(field_day, "absent_field_work")

        for day in days:
            if day in calendar:
                continue
            calendar[day] = (
                "present"
                if _professor_present_on_day(day, professor_index, resolved_settings)
                else "absent_regular"
            )
        calendars[professor_index] = calendar
    return calendars


def critical_internet_baseline_w() -> float:
    return sum(
        device.typical_w * device.count
        for device in DEVICE_CATALOG
        if device.group == "critical_internet"
    )


def holiday_day_count(
    calendar: Mapping[date, ProfessorDailyState],
) -> int:
    return sum(1 for state in calendar.values() if state == "absent_holiday")


def effective_grid_behavior(context: LoadContext) -> GridBehaviorState:
    if context.grid_behavior != "grid_normal":
        return context.grid_behavior
    if not context.grid_available:
        return "outage_active"
    return "grid_normal"


def soc_band_for_percent(soc_percent: float) -> SocBand:
    value = _clamp(soc_percent, 0.0, 100.0)
    if value >= 70.0:
        return "comfortable"
    if value >= 50.0:
        return "cautious"
    if value >= 30.0:
        return "restricted"
    if value >= 15.0:
        return "critical"
    return "emergency"


def soldering_multiplier(
    grid_behavior: GridBehaviorState,
    soc_band: SocBand,
) -> float:
    if grid_behavior == "grid_normal":
        return 1.0
    if grid_behavior == "planned_outage_soon":
        return 0.60
    if grid_behavior == "post_outage_recovery":
        return 0.75
    return {
        "comfortable": 0.60,
        "cautious": 0.30,
        "restricted": 0.10,
        "critical": 0.0,
        "emergency": 0.0,
    }[soc_band]


def phone_charging_multiplier(
    grid_behavior: GridBehaviorState,
    soc_band: SocBand,
) -> float:
    if grid_behavior == "planned_outage_soon":
        return 1.30
    if grid_behavior == "post_outage_recovery":
        return 1.40
    if grid_behavior == "outage_active":
        return {
            "comfortable": 0.70,
            "cautious": 0.50,
            "restricted": 0.25,
            "critical": 0.10,
            "emergency": 0.0,
        }[soc_band]
    return 1.0


def laptop_multiplier(
    grid_behavior: GridBehaviorState,
    soc_band: SocBand,
) -> float:
    if grid_behavior == "planned_outage_soon":
        return 1.20
    if grid_behavior == "post_outage_recovery":
        return 1.30
    if grid_behavior == "outage_active":
        return {
            "comfortable": 0.70,
            "cautious": 0.50,
            "restricted": 0.25,
            "critical": 0.10,
            "emergency": 0.0,
        }[soc_band]
    return 1.0


def high_power_allowed(
    device_id: str,
    grid_behavior: GridBehaviorState,
    soc_band: SocBand,
) -> bool:
    if grid_behavior in {"grid_normal", "post_outage_recovery"}:
        return True
    if grid_behavior == "planned_outage_soon":
        return device_id != "heat_gun" or soc_band == "comfortable"
    if device_id == "kettle":
        return soc_band == "comfortable"
    if device_id == "heat_gun":
        return soc_band == "comfortable"
    if device_id == "hand_drill":
        return soc_band in {"comfortable", "cautious"}
    if device_id == "hot_glue_gun":
        return soc_band in {"comfortable", "cautious", "restricted"}
    return False


def high_power_probability_multiplier(
    device_id: str,
    grid_behavior: GridBehaviorState,
    soc_band: SocBand,
) -> float:
    if grid_behavior in {"grid_normal", "post_outage_recovery"}:
        return 1.0
    if grid_behavior == "planned_outage_soon":
        return {
            "kettle": 0.35,
            "heat_gun": 0.30 if soc_band == "comfortable" else 0.0,
            "hand_drill": 0.60,
            "hot_glue_gun": 0.75,
        }.get(device_id, 0.75)
    if device_id == "kettle":
        return 0.20 if soc_band == "comfortable" else 0.0
    if device_id == "heat_gun":
        return 0.05 if soc_band == "comfortable" else 0.0
    if device_id == "hand_drill":
        return {
            "comfortable": 0.25,
            "cautious": 0.10,
            "restricted": 0.0,
            "critical": 0.0,
            "emergency": 0.0,
        }[soc_band]
    if device_id == "hot_glue_gun":
        return {
            "comfortable": 0.60,
            "cautious": 0.35,
            "restricted": 0.10,
            "critical": 0.0,
            "emergency": 0.0,
        }[soc_band]
    return 0.0


def is_dark_condition(context: LoadContext) -> bool:
    return context.is_dark


def is_light_obstructing_weather(weather_state: str) -> bool:
    normalized = weather_state.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in LIGHT_OBSTRUCTING_WEATHER_STATES:
        return True
    if normalized in {"clear", "sunny", "partly_cloudy", "partly_sunny"}:
        return False
    return any(
        token in normalized
        for token in ("drizzle", "fog", "rain", "sleet", "snow", "storm", "thunder")
    ) or normalized in {"100_cloudy", "100%_cloudy"}


def lighting_need_factor(
    timestamp_local: datetime,
    context: LoadContext,
    settings: LoadSimulationSettings | None = None,
) -> float:
    if context.is_dark:
        return 1.0

    resolved_settings = settings or LoadSimulationSettings()
    light_obstructing = is_light_obstructing_weather(context.weather_state)
    transition_minutes = 120.0 if light_obstructing else 60.0
    sunrise_local, sunset_local = _lighting_sun_times(
        timestamp_local,
        context,
        resolved_settings,
    )

    if timestamp_local < sunrise_local or timestamp_local >= sunset_local:
        return 1.0

    factor = 0.0
    minutes_after_sunrise = (timestamp_local - sunrise_local).total_seconds() / 60.0
    if 0.0 <= minutes_after_sunrise <= transition_minutes:
        factor = max(factor, 1.0 - minutes_after_sunrise / transition_minutes)

    minutes_before_sunset = (sunset_local - timestamp_local).total_seconds() / 60.0
    if 0.0 <= minutes_before_sunset <= transition_minutes:
        factor = max(factor, 1.0 - minutes_before_sunset / transition_minutes)

    if light_obstructing:
        factor = max(factor, 0.35)
    return _clamp(factor, 0.0, 1.0)


def _lighting_sun_times(
    timestamp_local: datetime,
    context: LoadContext,
    settings: LoadSimulationSettings,
) -> tuple[datetime, datetime]:
    if context.sunrise_local is not None and context.sunset_local is not None:
        return (
            _as_reference_local(context.sunrise_local, timestamp_local),
            _as_reference_local(context.sunset_local, timestamp_local),
        )
    sunrise_local, sunset_local = _calculate_sun_times_or_raise(
        timestamp_local.date(),
        settings.timezone_name,
        settings.station_latitude,
        settings.station_longitude,
    )
    return (
        sunrise_local.astimezone(timestamp_local.tzinfo),
        sunset_local.astimezone(timestamp_local.tzinfo),
    )


@lru_cache(maxsize=512)
def _calculate_sun_times_or_raise(
    local_date: date,
    timezone_name: str,
    latitude: float | None,
    longitude: float | None,
) -> tuple[datetime, datetime]:
    _validate_daylight_settings(timezone_name, latitude, longitude)
    try:
        station_timezone = _zone_info_or_raise(timezone_name)
        noon_local = datetime.combine(local_date, time(12, 0), tzinfo=station_timezone)
        times = pd.DatetimeIndex([noon_local])
        sun_times = pvlib.solarposition.sun_rise_set_transit_spa(
            times,
            float(latitude),
            float(longitude),
        )
    except Exception as exc:
        raise LoadDaylightConfigurationError(
            f"failed to calculate sunrise/sunset for {local_date.isoformat()}"
        ) from exc
    row = sun_times.iloc[0]
    sunrise = _pandas_datetime_to_python(row.get("sunrise"))
    sunset = _pandas_datetime_to_python(row.get("sunset"))
    if sunrise is None or sunset is None:
        raise LoadDaylightConfigurationError(
            f"pvlib returned no sunrise/sunset for {local_date.isoformat()}"
        )
    return sunrise, sunset


def _validate_daylight_settings(
    timezone_name: str,
    latitude: float | None,
    longitude: float | None,
) -> None:
    if not timezone_name:
        raise LoadDaylightConfigurationError(
            "load daylight calculation requires station timezone"
        )
    _zone_info_or_raise(timezone_name)
    if latitude is None or longitude is None:
        raise LoadDaylightConfigurationError(
            "load daylight calculation requires station latitude and longitude; "
            "use load_settings_from_station_config(config) or provide LoadContext sunrise/sunset overrides"
        )
    try:
        numeric_latitude = float(latitude)
        numeric_longitude = float(longitude)
    except (TypeError, ValueError) as exc:
        raise LoadDaylightConfigurationError(
            "station latitude and longitude must be numeric for load daylight calculation"
        ) from exc
    if not (
        isfinite(numeric_latitude)
        and isfinite(numeric_longitude)
        and -90.0 <= numeric_latitude <= 90.0
        and -180.0 <= numeric_longitude <= 180.0
    ):
        raise LoadDaylightConfigurationError(
            "station latitude/longitude are outside valid ranges for load daylight calculation"
        )


def _pandas_datetime_to_python(value: object) -> datetime | None:
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "to_pydatetime"):
        return value.round("us").to_pydatetime()
    if isinstance(value, datetime):
        return value
    return None


def _as_reference_local(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=reference.tzinfo)
    return value.astimezone(reference.tzinfo)


def _zone_info_or_raise(timezone_name: str) -> ZoneInfo:
    if not timezone_name:
        raise LoadDaylightConfigurationError("load simulation requires station timezone")
    try:
        return ZoneInfo(str(timezone_name))
    except Exception as exc:
        raise LoadDaylightConfigurationError(
            f"invalid station timezone for load simulation: {timezone_name!r}"
        ) from exc


def lighting_counts(
    active_workstation_professors: int,
    active_soldering_workplaces: int,
    active_students: int,
    grid_behavior: GridBehaviorState,
    soc_band: SocBand,
    workstation_only: bool,
    lighting_need_factor: float,
) -> tuple[int, int, int]:
    need = _clamp(lighting_need_factor, 0.0, 1.0)
    workstation_lamps = _lamp_count(
        min(active_workstation_professors, 5),
        need,
        threshold=0.30,
    )
    soldering_lamps = _lamp_count(
        min(active_soldering_workplaces, 6),
        min(1.0, need + 0.25),
        threshold=0.30,
    )
    ceiling_lamps = 0
    people = active_workstation_professors + active_soldering_workplaces + active_students
    if people <= 0:
        return 0, 0, 0
    if grid_behavior == "outage_active":
        if workstation_only:
            return workstation_lamps, soldering_lamps, 0
        if active_soldering_workplaces > 0 and soc_band == "comfortable" and need >= 0.75:
            ceiling_lamps = 1
        return workstation_lamps, soldering_lamps, ceiling_lamps
    if need >= 0.85 and (active_soldering_workplaces > 0 or active_students >= 4):
        ceiling_lamps = 2
    elif active_soldering_workplaces > 0 and need >= 0.45:
        ceiling_lamps = 1
    elif active_students >= 4 and need >= 0.65:
        ceiling_lamps = 1
    elif active_workstation_professors > 0 and need >= 0.75:
        ceiling_lamps = 1
    return workstation_lamps, soldering_lamps, ceiling_lamps


def _lamp_count(
    available_count: int,
    factor: float,
    threshold: float,
) -> int:
    if available_count <= 0 or factor < threshold:
        return 0
    return max(1, _scaled_count(available_count, factor))


def student_laptop_profile(
    seed: int,
    local_date: date,
    student_index: int,
) -> LaptopProfile:
    draw = _rng(seed, "laptop-profile", local_date, student_index).random()
    if draw < 0.23:
        return "macbook"
    if draw < 0.78:
        return "business"
    return "gaming"


def _professor_present_on_day(
    local_date: date,
    professor_index: int,
    settings: LoadSimulationSettings,
) -> bool:
    rng = _rng(settings.seed, "professor-present", professor_index, local_date)
    young = professor_index == 0
    if local_date.weekday() < 5:
        probability = 0.95 if young else 0.90
    else:
        probability = 0.35 if young else 0.05
    return rng.random() < probability


def _professor_holiday_days(
    year: int,
    professor_index: int,
    settings: LoadSimulationSettings,
) -> set[date]:
    rng = _rng(settings.seed, "professor-holidays", year, professor_index)
    draw = rng.random()
    if draw < 0.40:
        block_count = 1
    elif draw < 0.75:
        block_count = 3
    else:
        block_count = 4
    lengths = _split_total_days(14, block_count, rng)
    holidays: set[date] = set()
    days = _year_days(year)
    for length in lengths:
        for _ in range(120):
            start_index = rng.randint(0, max(0, len(days) - length))
            candidate = {days[start_index + offset] for offset in range(length)}
            if holidays.isdisjoint(candidate):
                holidays.update(candidate)
                break
        else:
            for day in days:
                if len(holidays) >= 14:
                    break
                holidays.add(day)
    return set(sorted(holidays)[:14])


def _professor_illness_days(
    year: int,
    professor_index: int,
    settings: LoadSimulationSettings,
    blocked_days: set[date],
) -> set[date]:
    rng = _rng(settings.seed, "professor-illness", year, professor_index)
    episode_count = rng.randint(0, 2)
    days = _year_days(year)
    illness_days: set[date] = set()
    for episode_index in range(episode_count):
        duration = _bounded_int(round(rng.gauss(5.0, 3.0)), 2, 14)
        if rng.random() < 0.65:
            candidate_days = [day for day in days if day.month in {11, 12, 1, 2, 3}]
        else:
            candidate_days = days
        if not candidate_days:
            continue
        start_day = candidate_days[rng.randrange(len(candidate_days))]
        for offset in range(duration):
            current = start_day + timedelta(days=offset)
            if current.year == year and current not in blocked_days:
                illness_days.add(current)
    return illness_days


def _professor_field_work_days(
    year: int,
    professor_index: int,
    settings: LoadSimulationSettings,
    blocked_days: set[date],
) -> set[date]:
    days: set[date] = set()
    current = date(year, 1, 1)
    while current.year == year:
        if current not in blocked_days:
            rng = _rng(settings.seed, "professor-field", professor_index, current)
            probability = 0.008 if current.month in {11, 12, 1, 2} else 0.025
            if rng.random() < probability:
                duration = rng.randint(1, 3)
                for offset in range(duration):
                    field_day = current + timedelta(days=offset)
                    if field_day.year == year and field_day not in blocked_days:
                        days.add(field_day)
                current += timedelta(days=duration)
                continue
        current += timedelta(days=1)
    return days


def _equipment_multiplier(
    grid_behavior: GridBehaviorState,
    soc_band: SocBand,
) -> float:
    if grid_behavior == "grid_normal":
        return 1.0
    if grid_behavior == "planned_outage_soon":
        return 0.75
    if grid_behavior == "post_outage_recovery":
        return 0.90
    return {
        "comfortable": 0.70,
        "cautious": 0.45,
        "restricted": 0.20,
        "critical": 0.05,
        "emergency": 0.0,
    }[soc_band]


def _resolve_context(
    provider: ContextProvider | LoadContext | None,
    timestamp_utc: datetime,
) -> LoadContext:
    if provider is None:
        return LoadContext()
    if isinstance(provider, LoadContext):
        return provider
    return provider(timestamp_utc)


def _resolved_soc(
    context: LoadContext,
    settings: LoadSimulationSettings,
) -> float:
    value = settings.default_soc_percent if context.soc_percent is None else context.soc_percent
    if not isfinite(float(value)):
        return settings.default_soc_percent
    return _clamp(float(value), 0.0, 100.0)


def _bounded_count(value: int | None, maximum: int) -> int | None:
    if value is None:
        return None
    return max(0, min(maximum, int(value)))


def _scaled_count(count: int, ratio: float) -> int:
    return max(0, min(count, int(round(count * _clamp(ratio, 0.0, 1.0)))))


def _is_inside_plan(timestamp_local: datetime, plan: ProfessorDayPlan) -> bool:
    if plan.state != "present" or plan.arrival_local is None or plan.departure_local is None:
        return False
    return plan.arrival_local <= timestamp_local < plan.departure_local


def _split_total_days(total: int, block_count: int, rng: random.Random) -> list[int]:
    if block_count <= 1:
        return [total]
    minimum = 2
    remaining = total - minimum * block_count
    lengths = [minimum for _ in range(block_count)]
    for _ in range(remaining):
        lengths[rng.randrange(block_count)] += 1
    rng.shuffle(lengths)
    return lengths


def _year_days(year: int) -> list[date]:
    days: list[date] = []
    current = date(year, 1, 1)
    while current.year == year:
        days.append(current)
        current += timedelta(days=1)
    return days


def _minute_bucket(value: datetime, minutes: int) -> int:
    return (value.hour * 60 + value.minute) // minutes


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime is required")
    return value.astimezone(timezone.utc)


def _bounded_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _rng(seed: int, *parts: object) -> random.Random:
    return random.Random(_stable_seed(seed, *parts))


def _stable_seed(seed: int, *parts: object) -> int:
    payload = "|".join(str(part) for part in (seed, *parts))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)
