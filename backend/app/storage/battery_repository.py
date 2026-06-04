from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Column, UniqueConstraint
from sqlmodel import Field, Session, SQLModel, select

from app.storage.solar_repository import TimezoneAwareDateTime


HISTORY_MINUTES = {0, 15, 30, 45}


def _utc_timestamp_field(index: bool = False) -> Any:
    return Field(sa_column=Column(TimezoneAwareDateTime(), index=index))


class BatteryHistoryPoint(SQLModel, table=True):
    __tablename__ = "battery_history_point"
    __table_args__ = (
        UniqueConstraint(
            "station_id",
            "config_hash",
            "timestamp_utc",
            name="uq_battery_history_station_config_timestamp",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    station_id: str = Field(index=True)
    config_hash: str = Field(index=True)
    timestamp_utc: datetime = _utc_timestamp_field(index=True)
    timestamp_local: datetime = _utc_timestamp_field()
    soc_percent: float
    soh_percent: float
    voltage_v: float
    energy_wh: float
    usable_capacity_wh: float
    current_usable_capacity_wh: float
    applied_charge_power_w: float
    applied_discharge_power_w: float
    net_battery_power_w: float
    cycle_fraction_increment: float = 0.0
    soh_loss_percent: float = 0.0
    status: str = "idle"


class BatteryCachePoint(SQLModel, table=True):
    __tablename__ = "battery_cache_point"
    __table_args__ = (
        UniqueConstraint(
            "station_id",
            "config_hash",
            "timestamp_utc",
            name="uq_battery_cache_station_config_timestamp",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    station_id: str = Field(index=True)
    config_hash: str = Field(index=True)
    timestamp_utc: datetime = _utc_timestamp_field(index=True)
    timestamp_local: datetime = _utc_timestamp_field()
    soc_percent: float
    soh_percent: float
    voltage_v: float
    energy_wh: float
    usable_capacity_wh: float
    current_usable_capacity_wh: float
    applied_charge_power_w: float
    applied_discharge_power_w: float
    net_battery_power_w: float
    cycle_fraction_increment: float = 0.0
    soh_loss_percent: float = 0.0
    status: str = "idle"


def save_battery_history_points(
    session: Session,
    points: list[BatteryHistoryPoint],
) -> int:
    return _save_points(
        session,
        points,
        list_battery_history_points,
        validate_history_timestamp=True,
    )


def save_battery_cache_points(
    session: Session,
    points: list[BatteryCachePoint],
) -> int:
    return _save_points(
        session,
        points,
        list_battery_cache_points,
        validate_history_timestamp=False,
    )


def list_battery_history_points(
    session: Session,
    station_id: str | None = None,
    config_hash: str | None = None,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
    limit: int | None = None,
) -> list[BatteryHistoryPoint]:
    return _list_points(
        session,
        BatteryHistoryPoint,
        station_id,
        config_hash,
        start_utc,
        end_utc,
        limit,
    )


def list_battery_cache_points(
    session: Session,
    station_id: str | None = None,
    config_hash: str | None = None,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
    limit: int | None = None,
) -> list[BatteryCachePoint]:
    return _list_points(
        session,
        BatteryCachePoint,
        station_id,
        config_hash,
        start_utc,
        end_utc,
        limit,
    )


def delete_battery_cache_points(
    session: Session,
    station_id: str,
    config_hash: str,
    start_utc: datetime,
    end_utc: datetime,
) -> int:
    rows = list_battery_cache_points(
        session,
        station_id,
        config_hash,
        start_utc,
        end_utc,
    )
    for row in rows:
        session.delete(row)
    session.commit()
    return len(rows)


def delete_battery_history_points(
    session: Session,
    station_id: str,
    config_hash: str,
    start_utc: datetime,
    end_utc: datetime,
) -> int:
    rows = list_battery_history_points(
        session,
        station_id,
        config_hash,
        start_utc,
        end_utc,
    )
    for row in rows:
        session.delete(row)
    session.commit()
    return len(rows)


def get_nearest_battery_cache_point(
    session: Session,
    station_id: str,
    config_hash: str,
    target_utc: datetime,
) -> BatteryCachePoint | None:
    return _nearest_point(session, BatteryCachePoint, station_id, config_hash, target_utc)


def get_latest_battery_cache_point(
    session: Session,
    station_id: str,
    config_hash: str,
    at_or_before_utc: datetime | None = None,
) -> BatteryCachePoint | None:
    return _latest_point(
        session,
        BatteryCachePoint,
        station_id,
        config_hash,
        at_or_before_utc,
    )


def get_latest_battery_history_point(
    session: Session,
    station_id: str,
    config_hash: str,
    at_or_before_utc: datetime | None = None,
) -> BatteryHistoryPoint | None:
    return _latest_point(
        session,
        BatteryHistoryPoint,
        station_id,
        config_hash,
        at_or_before_utc,
    )


def _save_points(
    session: Session,
    points: list[Any],
    list_function: Any,
    *,
    validate_history_timestamp: bool,
) -> int:
    unique_points = _unique_points(points, validate_history_timestamp)
    if not unique_points:
        return 0

    timestamps = [point.timestamp_utc for point in unique_points]
    station_id = unique_points[0].station_id
    config_hash = unique_points[0].config_hash
    existing_timestamps = {
        point.timestamp_utc
        for point in list_function(
            session,
            station_id,
            config_hash,
            min(timestamps),
            max(timestamps) + timedelta(microseconds=1),
        )
    }
    rows_to_insert = [
        point for point in unique_points if point.timestamp_utc not in existing_timestamps
    ]
    if not rows_to_insert:
        return 0
    session.add_all(rows_to_insert)
    session.commit()
    return len(rows_to_insert)


def _list_points(
    session: Session,
    model: Any,
    station_id: str | None,
    config_hash: str | None,
    start_utc: datetime | None,
    end_utc: datetime | None,
    limit: int | None,
) -> list[Any]:
    statement = select(model).order_by(model.timestamp_utc)
    if station_id is not None:
        statement = statement.where(model.station_id == station_id)
    if config_hash is not None:
        statement = statement.where(model.config_hash == config_hash)
    if start_utc is not None:
        statement = statement.where(model.timestamp_utc >= _as_utc(start_utc))
    if end_utc is not None:
        statement = statement.where(model.timestamp_utc < _as_utc(end_utc))
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.exec(statement).all())


def _nearest_point(
    session: Session,
    model: Any,
    station_id: str,
    config_hash: str,
    target_utc: datetime,
) -> Any | None:
    target = _as_utc(target_utc)
    before = session.exec(
        select(model)
        .where(model.station_id == station_id)
        .where(model.config_hash == config_hash)
        .where(model.timestamp_utc <= target)
        .order_by(model.timestamp_utc.desc())
        .limit(1)
    ).first()
    after = session.exec(
        select(model)
        .where(model.station_id == station_id)
        .where(model.config_hash == config_hash)
        .where(model.timestamp_utc >= target)
        .order_by(model.timestamp_utc)
        .limit(1)
    ).first()
    if before is None:
        return after
    if after is None:
        return before
    before_delta = abs((target - before.timestamp_utc).total_seconds())
    after_delta = abs((after.timestamp_utc - target).total_seconds())
    return before if before_delta <= after_delta else after


def _latest_point(
    session: Session,
    model: Any,
    station_id: str,
    config_hash: str,
    at_or_before_utc: datetime | None,
) -> Any | None:
    statement = (
        select(model)
        .where(model.station_id == station_id)
        .where(model.config_hash == config_hash)
        .order_by(model.timestamp_utc.desc())
        .limit(1)
    )
    if at_or_before_utc is not None:
        statement = statement.where(model.timestamp_utc <= _as_utc(at_or_before_utc))
    return session.exec(statement).first()


def _unique_points(points: list[Any], validate_history_timestamp: bool) -> list[Any]:
    seen: set[tuple[str, str, datetime]] = set()
    unique_points: list[Any] = []
    for point in points:
        point.timestamp_utc = _as_utc(point.timestamp_utc)
        _validate_minute_timestamp(point.timestamp_utc)
        if validate_history_timestamp:
            _validate_history_timestamp(point.timestamp_utc)
        key = (point.station_id, point.config_hash, point.timestamp_utc)
        if key in seen:
            continue
        seen.add(key)
        unique_points.append(point)
    return unique_points


def _validate_minute_timestamp(value: datetime) -> None:
    if value.second != 0 or value.microsecond != 0:
        raise ValueError("cache/history timestamps must be minute-aligned")


def _validate_history_timestamp(value: datetime) -> None:
    if value.minute not in HISTORY_MINUTES:
        raise ValueError("history timestamps must use 15-minute cadence (00/15/30/45)")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime is required")
    return value.astimezone(timezone.utc).replace(microsecond=0)
