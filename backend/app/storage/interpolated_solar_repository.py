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
