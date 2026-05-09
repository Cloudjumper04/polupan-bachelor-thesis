from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column
from sqlalchemy.types import String, TypeDecorator
from sqlmodel import Field, Session, SQLModel, select


class TimezoneAwareDateTime(TypeDecorator):
    impl = String
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone-aware datetime is required")
        return value.isoformat()

    def process_result_value(self, value: str | None, dialect) -> datetime | None:
        if value is None:
            return None
        return datetime.fromisoformat(value)


class IdealSolarProduction(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    station_id: str = Field(index=True)
    config_hash: str = Field(index=True)
    timestamp_utc: datetime = Field(
        sa_column=Column(TimezoneAwareDateTime(), index=True)
    )
    timestamp_local: datetime = Field(sa_column=Column(TimezoneAwareDateTime()))
    sun_elevation_deg: float
    sun_azimuth_deg: float
    incidence_factor: float
    ambient_factor: float
    direct_power_w: float
    ambient_power_w: float
    ideal_power_w: float


def delete_ideal_solar_for_config(
    session: Session,
    station_id: str,
    config_hash: str,
) -> None:
    existing_points = list_ideal_solar_for_config(session, station_id, config_hash)
    for point in existing_points:
        session.delete(point)
    session.commit()


def save_ideal_solar_points(
    session: Session,
    points: list[IdealSolarProduction],
) -> None:
    session.add_all(points)
    session.commit()


def list_ideal_solar_for_config(
    session: Session,
    station_id: str,
    config_hash: str,
    limit: int | None = None,
) -> list[IdealSolarProduction]:
    statement = (
        select(IdealSolarProduction)
        .where(IdealSolarProduction.station_id == station_id)
        .where(IdealSolarProduction.config_hash == config_hash)
        .order_by(IdealSolarProduction.timestamp_utc)
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.exec(statement).all())
