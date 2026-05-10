from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column
from sqlmodel import Field, Session, SQLModel, select

from app.storage.solar_repository import TimezoneAwareDateTime


class ForecastSolarProduction(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    station_id: str = Field(index=True)
    config_hash: str = Field(index=True)
    timestamp_utc: datetime = Field(
        sa_column=Column(TimezoneAwareDateTime(), index=True)
    )
    timestamp_local: datetime = Field(sa_column=Column(TimezoneAwareDateTime()))
    ideal_power_w: float
    weather_code: int | None = None
    weather_state: str
    cloud_cover_percent: float | None = None
    weather_factor: float
    forecast_power_w: float


def delete_forecast_solar_for_config(
    session: Session,
    station_id: str,
    config_hash: str,
    start_utc: datetime,
    end_utc: datetime,
) -> None:
    existing_points = list_forecast_solar_for_config(
        session,
        station_id,
        config_hash,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    for point in existing_points:
        session.delete(point)
    session.commit()


def save_forecast_solar_points(
    session: Session,
    points: list[ForecastSolarProduction],
) -> None:
    session.add_all(points)
    session.commit()


def list_forecast_solar_for_config(
    session: Session,
    station_id: str,
    config_hash: str,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
    limit: int | None = None,
) -> list[ForecastSolarProduction]:
    statement = (
        select(ForecastSolarProduction)
        .where(ForecastSolarProduction.station_id == station_id)
        .where(ForecastSolarProduction.config_hash == config_hash)
        .order_by(ForecastSolarProduction.timestamp_utc)
    )
    if start_utc is not None:
        statement = statement.where(ForecastSolarProduction.timestamp_utc >= start_utc)
    if end_utc is not None:
        statement = statement.where(ForecastSolarProduction.timestamp_utc < end_utc)
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.exec(statement).all())


def get_forecast_solar_range(
    session: Session,
    station_id: str,
    config_hash: str,
) -> tuple[datetime | None, datetime | None]:
    first_statement = (
        select(ForecastSolarProduction)
        .where(ForecastSolarProduction.station_id == station_id)
        .where(ForecastSolarProduction.config_hash == config_hash)
        .order_by(ForecastSolarProduction.timestamp_utc)
        .limit(1)
    )
    last_statement = (
        select(ForecastSolarProduction)
        .where(ForecastSolarProduction.station_id == station_id)
        .where(ForecastSolarProduction.config_hash == config_hash)
        .order_by(ForecastSolarProduction.timestamp_utc.desc())
        .limit(1)
    )
    first_row = session.exec(first_statement).first()
    last_row = session.exec(last_statement).first()
    return (
        first_row.timestamp_utc if first_row else None,
        last_row.timestamp_utc if last_row else None,
    )
