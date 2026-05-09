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
