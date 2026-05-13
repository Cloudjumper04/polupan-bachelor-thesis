from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Column, UniqueConstraint
from sqlmodel import Field, Session, SQLModel, select

from app.storage.solar_repository import TimezoneAwareDateTime


class GridDamageEventRecord(SQLModel, table=True):
    __tablename__ = "grid_damage_event"
    __table_args__ = (UniqueConstraint("event_key", name="uq_grid_damage_event_key"),)

    id: int | None = Field(default=None, primary_key=True)
    event_key: str = Field(index=True)
    event_date: str = Field(index=True)
    event_timestamp_utc: datetime = Field(
        sa_column=Column(TimezoneAwareDateTime(), index=True)
    )
    attack_state: str = Field(index=True)
    kyiv_focus_mode: str = Field(index=True)
    element_type: str = Field(index=True)
    damage_class: str = Field(index=True)
    raw_damage_percent: float
    applied_generation_damage_percent: float
    applied_delivery_damage_percent: float
    recovery_days: float
    seed: int = Field(index=True)
    metadata_json: str | None = None


class GridAvailabilityPointRecord(SQLModel, table=True):
    __tablename__ = "grid_availability_point"
    __table_args__ = (
        UniqueConstraint("timestamp_utc", name="uq_grid_availability_timestamp_utc"),
    )

    id: int | None = Field(default=None, primary_key=True)
    timestamp_utc: datetime = Field(
        sa_column=Column(TimezoneAwareDateTime(), index=True)
    )
    timestamp_local: datetime = Field(sa_column=Column(TimezoneAwareDateTime()))
    generation_health_percent: float
    delivery_health_percent: float
    effective_health_percent: float
    deficit_percent: float
    daily_outage_hours: float
    outage_level: str = Field(index=True)
    outage_queue: str = Field(index=True)
    local_grid_available: bool
    is_outage_now: bool
    grid_voltage_v: float
    reason: str
    current_outage_window_start_utc: datetime | None = Field(
        default=None,
        sa_column=Column(TimezoneAwareDateTime(), nullable=True),
    )
    current_outage_window_end_utc: datetime | None = Field(
        default=None,
        sa_column=Column(TimezoneAwareDateTime(), nullable=True),
    )
    next_outage_window_start_utc: datetime | None = Field(
        default=None,
        sa_column=Column(TimezoneAwareDateTime(), nullable=True),
    )
    next_outage_window_end_utc: datetime | None = Field(
        default=None,
        sa_column=Column(TimezoneAwareDateTime(), nullable=True),
    )


def save_grid_damage_events(
    session: Session,
    events: list[GridDamageEventRecord],
) -> int:
    unique_events = _unique_by_key(events)
    if not unique_events:
        return 0

    existing_keys = _existing_event_keys(
        session,
        [event.event_key for event in unique_events],
    )
    rows_to_insert = [
        event for event in unique_events if event.event_key not in existing_keys
    ]
    if not rows_to_insert:
        return 0
    session.add_all(rows_to_insert)
    session.commit()
    return len(rows_to_insert)


def list_grid_damage_events(
    session: Session,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
) -> list[GridDamageEventRecord]:
    statement = select(GridDamageEventRecord).order_by(
        GridDamageEventRecord.event_timestamp_utc,
        GridDamageEventRecord.id,
    )
    if start_utc is not None:
        statement = statement.where(GridDamageEventRecord.event_timestamp_utc >= start_utc)
    if end_utc is not None:
        statement = statement.where(GridDamageEventRecord.event_timestamp_utc < end_utc)
    return list(session.exec(statement).all())


def save_grid_availability_points(
    session: Session,
    points: list[GridAvailabilityPointRecord],
) -> int:
    unique_points = _unique_by_timestamp(points)
    if not unique_points:
        return 0

    timestamps = [point.timestamp_utc for point in unique_points]
    start_utc = min(timestamps)
    end_utc = max(timestamps) + timedelta(microseconds=1)
    existing_timestamps = {
        point.timestamp_utc
        for point in list_grid_availability_points(session, start_utc, end_utc)
    }
    rows_to_insert = [
        point
        for point in unique_points
        if point.timestamp_utc not in existing_timestamps
    ]
    if not rows_to_insert:
        return 0
    session.add_all(rows_to_insert)
    session.commit()
    return len(rows_to_insert)


def list_grid_availability_points(
    session: Session,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
    limit: int | None = None,
) -> list[GridAvailabilityPointRecord]:
    statement = select(GridAvailabilityPointRecord).order_by(
        GridAvailabilityPointRecord.timestamp_utc,
    )
    if start_utc is not None:
        statement = statement.where(GridAvailabilityPointRecord.timestamp_utc >= start_utc)
    if end_utc is not None:
        statement = statement.where(GridAvailabilityPointRecord.timestamp_utc < end_utc)
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.exec(statement).all())


def get_nearest_grid_availability_point(
    session: Session,
    target_utc: datetime,
) -> GridAvailabilityPointRecord | None:
    before_statement = (
        select(GridAvailabilityPointRecord)
        .where(GridAvailabilityPointRecord.timestamp_utc <= target_utc)
        .order_by(GridAvailabilityPointRecord.timestamp_utc.desc())
        .limit(1)
    )
    after_statement = (
        select(GridAvailabilityPointRecord)
        .where(GridAvailabilityPointRecord.timestamp_utc >= target_utc)
        .order_by(GridAvailabilityPointRecord.timestamp_utc)
        .limit(1)
    )
    before = session.exec(before_statement).first()
    after = session.exec(after_statement).first()
    if before is None:
        return after
    if after is None:
        return before
    before_delta = abs((target_utc - before.timestamp_utc).total_seconds())
    after_delta = abs((after.timestamp_utc - target_utc).total_seconds())
    return before if before_delta <= after_delta else after


def get_grid_availability_range(
    session: Session,
) -> tuple[datetime | None, datetime | None]:
    first_statement = (
        select(GridAvailabilityPointRecord)
        .order_by(GridAvailabilityPointRecord.timestamp_utc)
        .limit(1)
    )
    last_statement = (
        select(GridAvailabilityPointRecord)
        .order_by(GridAvailabilityPointRecord.timestamp_utc.desc())
        .limit(1)
    )
    first_row = session.exec(first_statement).first()
    last_row = session.exec(last_statement).first()
    return (
        first_row.timestamp_utc if first_row is not None else None,
        last_row.timestamp_utc if last_row is not None else None,
    )


def find_missing_grid_availability_timestamps(
    session: Session,
    start_utc: datetime,
    end_utc: datetime,
    cadence_minutes: int = 30,
) -> list[datetime]:
    if cadence_minutes <= 0:
        raise ValueError("cadence_minutes must be greater than 0")
    if end_utc <= start_utc:
        return []

    start = _as_utc(start_utc)
    end = _as_utc(end_utc)
    existing = {
        point.timestamp_utc
        for point in list_grid_availability_points(session, start, end)
    }
    missing: list[datetime] = []
    current = start
    step = timedelta(minutes=cadence_minutes)
    while current < end:
        if current not in existing:
            missing.append(current)
        current += step
    return missing


def encode_metadata(value: dict[str, Any] | None) -> str | None:
    if not value:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def decode_metadata(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    return json.loads(value)


def _existing_event_keys(session: Session, event_keys: list[str]) -> set[str]:
    if not event_keys:
        return set()
    existing: set[str] = set()
    for index in range(0, len(event_keys), 500):
        chunk = event_keys[index : index + 500]
        statement = select(GridDamageEventRecord.event_key).where(
            GridDamageEventRecord.event_key.in_(chunk),
        )
        existing.update(session.exec(statement).all())
    return existing


def _unique_by_key(
    events: list[GridDamageEventRecord],
) -> list[GridDamageEventRecord]:
    seen: set[str] = set()
    unique_events: list[GridDamageEventRecord] = []
    for event in events:
        if event.event_key in seen:
            continue
        seen.add(event.event_key)
        unique_events.append(event)
    return unique_events


def _unique_by_timestamp(
    points: list[GridAvailabilityPointRecord],
) -> list[GridAvailabilityPointRecord]:
    seen: set[datetime] = set()
    unique_points: list[GridAvailabilityPointRecord] = []
    for point in points:
        timestamp_utc = _as_utc(point.timestamp_utc)
        if timestamp_utc in seen:
            continue
        seen.add(timestamp_utc)
        point.timestamp_utc = timestamp_utc
        unique_points.append(point)
    return unique_points


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime is required")
    return value.astimezone(timezone.utc).replace(microsecond=0)
