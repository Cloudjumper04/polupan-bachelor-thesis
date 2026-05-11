from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column
from sqlmodel import Field, Session, SQLModel, select

from app.storage.solar_repository import TimezoneAwareDateTime


class WeatherForecast(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    station_id: str = Field(index=True)
    fetched_at_utc: datetime = Field(
        sa_column=Column(TimezoneAwareDateTime(), index=True)
    )
    forecast_timestamp_utc: datetime = Field(
        sa_column=Column(TimezoneAwareDateTime(), index=True)
    )
    forecast_timestamp_local: datetime = Field(sa_column=Column(TimezoneAwareDateTime()))
    weather_code: int | None = None
    temperature_c: float | None = None
    cloud_cover_percent: float | None = None
    precipitation_mm: float | None = None
    rain_mm: float | None = None
    snowfall_cm: float | None = None
    shortwave_radiation_w_m2: float | None = None
    direct_radiation_w_m2: float | None = None
    diffuse_radiation_w_m2: float | None = None
    source: str
    resolution_minutes: int


def delete_forecast_for_station(session: Session, station_id: str) -> None:
    existing_rows = list_forecast_for_station(session, station_id)
    for row in existing_rows:
        session.delete(row)
    session.commit()


def save_forecast_rows(session: Session, rows: list[WeatherForecast]) -> None:
    session.add_all(rows)
    session.commit()


def list_forecast_for_station(
    session: Session,
    station_id: str,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
) -> list[WeatherForecast]:
    statement = (
        select(WeatherForecast)
        .where(WeatherForecast.station_id == station_id)
        .order_by(WeatherForecast.forecast_timestamp_utc)
    )
    if start_utc is not None:
        statement = statement.where(WeatherForecast.forecast_timestamp_utc >= start_utc)
    if end_utc is not None:
        statement = statement.where(WeatherForecast.forecast_timestamp_utc < end_utc)
    return list(session.exec(statement).all())


def get_nearest_forecast_for_station(
    session: Session,
    station_id: str,
    target_utc: datetime,
) -> WeatherForecast | None:
    before_statement = (
        select(WeatherForecast)
        .where(WeatherForecast.station_id == station_id)
        .where(WeatherForecast.forecast_timestamp_utc <= target_utc)
        .order_by(WeatherForecast.forecast_timestamp_utc.desc())
        .limit(1)
    )
    after_statement = (
        select(WeatherForecast)
        .where(WeatherForecast.station_id == station_id)
        .where(WeatherForecast.forecast_timestamp_utc >= target_utc)
        .order_by(WeatherForecast.forecast_timestamp_utc)
        .limit(1)
    )
    before = session.exec(before_statement).first()
    after = session.exec(after_statement).first()
    if before is None:
        return after
    if after is None:
        return before

    before_delta = abs((target_utc - before.forecast_timestamp_utc).total_seconds())
    after_delta = abs((after.forecast_timestamp_utc - target_utc).total_seconds())
    return before if before_delta <= after_delta else after


def get_forecast_range(
    session: Session,
    station_id: str,
) -> tuple[datetime | None, datetime | None]:
    first_statement = (
        select(WeatherForecast)
        .where(WeatherForecast.station_id == station_id)
        .order_by(WeatherForecast.forecast_timestamp_utc)
        .limit(1)
    )
    last_statement = (
        select(WeatherForecast)
        .where(WeatherForecast.station_id == station_id)
        .order_by(WeatherForecast.forecast_timestamp_utc.desc())
        .limit(1)
    )
    first_row = session.exec(first_statement).first()
    last_row = session.exec(last_statement).first()
    return (
        first_row.forecast_timestamp_utc if first_row else None,
        last_row.forecast_timestamp_utc if last_row else None,
    )


def get_latest_forecast_fetch_time(
    session: Session,
    station_id: str,
) -> datetime | None:
    statement = (
        select(WeatherForecast)
        .where(WeatherForecast.station_id == station_id)
        .order_by(WeatherForecast.fetched_at_utc.desc())
        .limit(1)
    )
    row = session.exec(statement).first()
    if row is None:
        return None
    return row.fetched_at_utc
