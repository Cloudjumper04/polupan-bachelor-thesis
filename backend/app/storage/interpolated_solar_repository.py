from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column
from sqlmodel import Field, Session, SQLModel, select

from app.storage.solar_repository import TimezoneAwareDateTime


class InterpolatedSolarProduction(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    station_id: str = Field(index=True)
    config_hash: str = Field(index=True)
    timestamp_utc: datetime = Field(
        sa_column=Column(TimezoneAwareDateTime(), index=True)
    )
    timestamp_local: datetime = Field(sa_column=Column(TimezoneAwareDateTime()))
    source_type: str = Field(index=True)
    resolution_seconds: int = Field(index=True)
    lower_source_timestamp_utc: datetime = Field(
        sa_column=Column(TimezoneAwareDateTime())
    )
    upper_source_timestamp_utc: datetime = Field(
        sa_column=Column(TimezoneAwareDateTime())
    )
    lower_power_w: float
    upper_power_w: float
    interpolation_ratio: float
    baseline_power_w: float
    variation_factor: float
    power_w: float
    generated_at_utc: datetime = Field(
        sa_column=Column(TimezoneAwareDateTime(), index=True)
    )


def delete_interpolated_solar_for_config(
    session: Session,
    station_id: str,
    config_hash: str,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
    resolution_seconds: int | None = None,
) -> None:
    existing_points = list_interpolated_solar_for_config(
        session,
        station_id,
        config_hash,
        start_utc=start_utc,
        end_utc=end_utc,
        resolution_seconds=resolution_seconds,
    )
    for point in existing_points:
        session.delete(point)
    session.commit()


def save_interpolated_solar_points(
    session: Session,
    points: list[InterpolatedSolarProduction],
) -> None:
    session.add_all(points)
    session.commit()


def list_interpolated_solar_for_config(
    session: Session,
    station_id: str,
    config_hash: str,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
    resolution_seconds: int | None = None,
    limit: int | None = None,
) -> list[InterpolatedSolarProduction]:
    statement = (
        select(InterpolatedSolarProduction)
        .where(InterpolatedSolarProduction.station_id == station_id)
        .where(InterpolatedSolarProduction.config_hash == config_hash)
        .order_by(InterpolatedSolarProduction.timestamp_utc)
    )
    if start_utc is not None:
        statement = statement.where(InterpolatedSolarProduction.timestamp_utc >= start_utc)
    if end_utc is not None:
        statement = statement.where(InterpolatedSolarProduction.timestamp_utc < end_utc)
    if resolution_seconds is not None:
        statement = statement.where(
            InterpolatedSolarProduction.resolution_seconds == resolution_seconds
        )
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.exec(statement).all())


def list_interpolated_solar_resolutions_for_config(
    session: Session,
    station_id: str,
    config_hash: str,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
) -> list[int]:
    statement = (
        select(InterpolatedSolarProduction.resolution_seconds)
        .where(InterpolatedSolarProduction.station_id == station_id)
        .where(InterpolatedSolarProduction.config_hash == config_hash)
        .distinct()
    )
    if start_utc is not None:
        statement = statement.where(InterpolatedSolarProduction.timestamp_utc >= start_utc)
    if end_utc is not None:
        statement = statement.where(InterpolatedSolarProduction.timestamp_utc < end_utc)
    return sorted(session.exec(statement).all())


def has_interpolated_solar_resolution_for_config(
    session: Session,
    station_id: str,
    config_hash: str,
    resolution_seconds: int,
) -> bool:
    statement = (
        select(InterpolatedSolarProduction.id)
        .where(InterpolatedSolarProduction.station_id == station_id)
        .where(InterpolatedSolarProduction.config_hash == config_hash)
        .where(InterpolatedSolarProduction.resolution_seconds == resolution_seconds)
        .limit(1)
    )
    return session.exec(statement).first() is not None


def get_latest_interpolated_solar_for_config(
    session: Session,
    station_id: str,
    config_hash: str,
    at_or_before_utc: datetime | None = None,
    resolution_seconds: int | None = None,
) -> InterpolatedSolarProduction | None:
    statement = (
        select(InterpolatedSolarProduction)
        .where(InterpolatedSolarProduction.station_id == station_id)
        .where(InterpolatedSolarProduction.config_hash == config_hash)
        .order_by(InterpolatedSolarProduction.timestamp_utc.desc())
        .limit(1)
    )
    if at_or_before_utc is not None:
        statement = statement.where(
            InterpolatedSolarProduction.timestamp_utc <= at_or_before_utc
        )
    if resolution_seconds is not None:
        statement = statement.where(
            InterpolatedSolarProduction.resolution_seconds == resolution_seconds
        )
    return session.exec(statement).first()


def get_nearest_interpolated_solar_for_config(
    session: Session,
    station_id: str,
    config_hash: str,
    target_utc: datetime,
    resolution_seconds: int | None = None,
) -> InterpolatedSolarProduction | None:
    before_statement = (
        select(InterpolatedSolarProduction)
        .where(InterpolatedSolarProduction.station_id == station_id)
        .where(InterpolatedSolarProduction.config_hash == config_hash)
        .where(InterpolatedSolarProduction.timestamp_utc <= target_utc)
        .order_by(InterpolatedSolarProduction.timestamp_utc.desc())
        .limit(1)
    )
    after_statement = (
        select(InterpolatedSolarProduction)
        .where(InterpolatedSolarProduction.station_id == station_id)
        .where(InterpolatedSolarProduction.config_hash == config_hash)
        .where(InterpolatedSolarProduction.timestamp_utc >= target_utc)
        .order_by(InterpolatedSolarProduction.timestamp_utc)
        .limit(1)
    )
    if resolution_seconds is not None:
        before_statement = before_statement.where(
            InterpolatedSolarProduction.resolution_seconds == resolution_seconds
        )
        after_statement = after_statement.where(
            InterpolatedSolarProduction.resolution_seconds == resolution_seconds
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


def get_interpolated_solar_range(
    session: Session,
    station_id: str,
    config_hash: str,
) -> tuple[datetime | None, datetime | None]:
    first_statement = (
        select(InterpolatedSolarProduction)
        .where(InterpolatedSolarProduction.station_id == station_id)
        .where(InterpolatedSolarProduction.config_hash == config_hash)
        .order_by(InterpolatedSolarProduction.timestamp_utc)
        .limit(1)
    )
    last_statement = (
        select(InterpolatedSolarProduction)
        .where(InterpolatedSolarProduction.station_id == station_id)
        .where(InterpolatedSolarProduction.config_hash == config_hash)
        .order_by(InterpolatedSolarProduction.timestamp_utc.desc())
        .limit(1)
    )
    first_row = session.exec(first_statement).first()
    last_row = session.exec(last_statement).first()
    return (
        first_row.timestamp_utc if first_row else None,
        last_row.timestamp_utc if last_row else None,
    )
