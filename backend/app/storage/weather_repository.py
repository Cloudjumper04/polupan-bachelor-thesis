from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column
from sqlmodel import Field, Session, SQLModel, select

from app.storage.solar_repository import TimezoneAwareDateTime


class WeatherObservation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    station_id: str = Field(index=True)
    timestamp_utc: datetime = Field(
        sa_column=Column(TimezoneAwareDateTime(), index=True)
    )
    timestamp_local: datetime = Field(sa_column=Column(TimezoneAwareDateTime()))
    weather_code: int | None = None
    cloud_cover_percent: float | None = None
    precipitation_mm: float | None = None
    rain_mm: float | None = None
    snowfall_cm: float | None = None
    shortwave_radiation_w_m2: float | None = None
    direct_radiation_w_m2: float | None = None
    diffuse_radiation_w_m2: float | None = None
    source: str


def delete_weather_observations(
    session: Session,
    station_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> None:
    delete_weather_observations_for_range(session, station_id, start_utc, end_utc)


def delete_weather_observations_for_range(
    session: Session,
    station_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> None:
    existing_observations = list_weather_observations(
        session,
        station_id,
        start_utc,
        end_utc,
    )
    for observation in existing_observations:
        session.delete(observation)
    session.commit()


def save_weather_observations(
    session: Session,
    observations: list[WeatherObservation],
) -> None:
    session.add_all(observations)
    session.commit()


def list_weather_observations(
    session: Session,
    station_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[WeatherObservation]:
    statement = (
        select(WeatherObservation)
        .where(WeatherObservation.station_id == station_id)
        .where(WeatherObservation.timestamp_utc >= start_utc)
        .where(WeatherObservation.timestamp_utc < end_utc)
        .order_by(WeatherObservation.timestamp_utc)
    )
    return list(session.exec(statement).all())


def get_weather_observation_range(
    session: Session,
    station_id: str,
) -> tuple[datetime | None, datetime | None]:
    first_statement = (
        select(WeatherObservation)
        .where(WeatherObservation.station_id == station_id)
        .order_by(WeatherObservation.timestamp_utc)
        .limit(1)
    )
    last_statement = (
        select(WeatherObservation)
        .where(WeatherObservation.station_id == station_id)
        .order_by(WeatherObservation.timestamp_utc.desc())
        .limit(1)
    )
    first_row = session.exec(first_statement).first()
    last_row = session.exec(last_statement).first()
    return (
        first_row.timestamp_utc if first_row else None,
        last_row.timestamp_utc if last_row else None,
    )


def get_latest_weather_observation_time(
    session: Session,
    station_id: str,
) -> datetime | None:
    _, latest_timestamp = get_weather_observation_range(session, station_id)
    return latest_timestamp
