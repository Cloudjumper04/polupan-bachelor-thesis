from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from app.config_loader import calculate_config_hash, load_config
from app.main import (
    app,
    build_solar_daily_energy_history_payload,
    get_solar_current_buffer,
    get_solar_current,
    get_solar_dashboard,
    get_solar_history_bounds,
    get_solar_power_history,
    get_solar_weather_current,
)
from app.storage.database import create_db_and_tables, get_engine
from app.storage.forecast_repository import WeatherForecast, save_forecast_rows
from app.storage.forecast_solar_repository import (
    ForecastSolarProduction,
    save_forecast_solar_points,
)
from app.storage.interpolated_solar_repository import (
    InterpolatedSolarProduction,
    save_interpolated_solar_points,
)
from app.storage.simulated_solar_repository import (
    SimulatedSolarProduction,
    save_simulated_solar_points,
)
from app.storage.solar_repository import IdealSolarProduction, save_ideal_solar_points


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "station.default.yaml"
STATION_TIMEZONE = ZoneInfo("Europe/Kyiv")


def test_solar_dashboard_route_is_registered() -> None:
    assert any(route.path == "/api/solar/dashboard" for route in app.routes)
    assert any(route.path == "/api/solar/current" for route in app.routes)
    assert any(route.path == "/api/solar/current-buffer" for route in app.routes)
    assert any(route.path == "/api/solar/weather-current" for route in app.routes)
    assert any(route.path == "/api/solar/history/power" for route in app.routes)
    assert any(route.path == "/api/solar/history/daily-energy" for route in app.routes)
    assert any(route.path == "/api/solar/history/bounds" for route in app.routes)


def test_solar_current_endpoint_returns_current_summary_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'current.db'}"
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    engine = get_engine(database_url)
    create_db_and_tables(engine)

    with Session(engine) as session:
        save_interpolated_solar_points(
            session,
            [
                _interpolated_row(
                    now,
                    config_hash,
                    power_w=321.0,
                    resolution_seconds=1,
                ),
            ],
        )

    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_solar_current(now=now)

    assert payload == {
        "timestamp_local": now.astimezone(STATION_TIMEZONE).isoformat(),
        "solar_power_w": 321.0,
    }


def test_solar_current_buffer_endpoint_returns_bounded_points_without_charts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'current-buffer.db'}"
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    engine = get_engine(database_url)
    create_db_and_tables(engine)

    with Session(engine) as session:
        save_interpolated_solar_points(
            session,
            [
                _interpolated_row(
                    now + timedelta(seconds=index),
                    config_hash,
                    power_w=100.0 + index,
                    resolution_seconds=1,
                )
                for index in range(90)
            ],
        )

    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_solar_current_buffer(now=now, seconds=60)

    assert "charts" not in payload
    assert payload["current"]["timestamp_local"] == now.astimezone(
        STATION_TIMEZONE
    ).isoformat()
    assert payload["current"]["solar_power_w"] == 100.0
    assert payload["current"]["pv_voltage_v"] == pytest.approx(66.0, abs=0.8)
    assert payload["current"]["pv_current_a"] == pytest.approx(
        100.0 / payload["current"]["pv_voltage_v"]
    )
    assert len(payload["points"]) <= 61
    assert payload["points"][0]["solar_power_w"] == 100.0
    assert payload["points"][0]["pv_voltage_v"] == pytest.approx(66.0, abs=0.8)
    assert payload["points"][0]["pv_current_a"] == pytest.approx(
        100.0 / payload["points"][0]["pv_voltage_v"]
    )
    assert payload["points"][-1]["solar_power_w"] == 160.0
    assert {
        "timestamp_utc",
        "timestamp_local",
        "solar_power_w",
        "pv_voltage_v",
        "pv_current_a",
    } <= set(payload["points"][0])


def test_solar_weather_current_endpoint_returns_compact_weather_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    database_url = f"sqlite:///{tmp_path / 'weather-current.db'}"
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    engine = get_engine(database_url)
    create_db_and_tables(engine)

    with Session(engine) as session:
        save_forecast_rows(
            session,
            [
                WeatherForecast(
                    station_id=config.station.id,
                    fetched_at_utc=now - timedelta(hours=1),
                    forecast_timestamp_utc=now,
                    forecast_timestamp_local=now.astimezone(STATION_TIMEZONE),
                    weather_code=61,
                    temperature_c=14.6,
                    cloud_cover_percent=82.0,
                    precipitation_mm=1.0,
                    rain_mm=1.0,
                    snowfall_cm=0.0,
                    shortwave_radiation_w_m2=40.0,
                    direct_radiation_w_m2=10.0,
                    diffuse_radiation_w_m2=30.0,
                    source="open_meteo_forecast",
                    resolution_minutes=60,
                )
            ],
        )

    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_solar_weather_current(now=now)

    assert "charts" not in payload
    assert payload["timestamp_local"] == now.astimezone(STATION_TIMEZONE).isoformat()
    assert payload["weather_code"] == 61
    assert payload["weather_state"] == "rain"
    assert payload["weather_label"] == "дощ"
    assert payload["temperature_c"] == 14.6
    assert payload["cloud_cover_percent"] == 82.0
    assert payload["sunrise_local"].startswith("2026-05-10T")
    assert payload["sunset_local"].startswith("2026-05-10T")


def test_solar_weather_current_endpoint_handles_missing_temperature(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    database_url = f"sqlite:///{tmp_path / 'weather-current-missing-temp.db'}"
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    engine = get_engine(database_url)
    create_db_and_tables(engine)

    with Session(engine) as session:
        save_forecast_rows(
            session,
            [
                WeatherForecast(
                    station_id=config.station.id,
                    fetched_at_utc=now - timedelta(hours=1),
                    forecast_timestamp_utc=now,
                    forecast_timestamp_local=now.astimezone(STATION_TIMEZONE),
                    weather_code=3,
                    cloud_cover_percent=90.0,
                    precipitation_mm=0.0,
                    rain_mm=0.0,
                    snowfall_cm=0.0,
                    shortwave_radiation_w_m2=40.0,
                    direct_radiation_w_m2=10.0,
                    diffuse_radiation_w_m2=30.0,
                    source="open_meteo_forecast",
                    resolution_minutes=60,
                )
            ],
        )

    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_solar_weather_current(now=now)

    assert payload["weather_state"] == "cloudy"
    assert payload["temperature_c"] is None
    assert payload["sunrise_local"] is not None
    assert payload["sunset_local"] is not None


def test_solar_weather_current_sun_times_use_station_dst_offset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'weather-current-sun-offsets.db'}"
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    summer_payload = get_solar_weather_current(
        now=datetime(2026, 5, 11, 12, 0, tzinfo=STATION_TIMEZONE),
    )
    winter_payload = get_solar_weather_current(
        now=datetime(2026, 1, 11, 12, 0, tzinfo=STATION_TIMEZONE),
    )

    assert summer_payload["sunrise_local"].startswith("2026-05-11T")
    assert summer_payload["sunrise_local"].endswith("+03:00")
    assert summer_payload["sunset_local"].startswith("2026-05-11T")
    assert summer_payload["sunset_local"].endswith("+03:00")
    assert winter_payload["sunrise_local"].startswith("2026-01-11T")
    assert winter_payload["sunrise_local"].endswith("+02:00")
    assert winter_payload["sunset_local"].startswith("2026-01-11T")
    assert winter_payload["sunset_local"].endswith("+02:00")


def test_solar_dashboard_endpoint_returns_chart_ready_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'dashboard.db'}"
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    engine = get_engine(database_url)
    create_db_and_tables(engine)

    with Session(engine) as session:
        save_interpolated_solar_points(
            session,
            [
                _interpolated_row(
                    now - timedelta(minutes=20),
                    config_hash,
                    power_w=90.0,
                    resolution_seconds=1,
                ),
                _interpolated_row(
                    now,
                    config_hash,
                    power_w=123.4,
                    resolution_seconds=1,
                ),
                _interpolated_row(
                    now - timedelta(hours=2),
                    config_hash,
                    power_w=70.0,
                    resolution_seconds=5,
                ),
            ],
        )
        save_forecast_rows(
            session,
            [
                WeatherForecast(
                    station_id=config.station.id,
                    fetched_at_utc=now - timedelta(hours=1),
                    forecast_timestamp_utc=now,
                    forecast_timestamp_local=now.astimezone(STATION_TIMEZONE),
                    weather_code=2,
                    temperature_c=18.4,
                    cloud_cover_percent=45.0,
                    precipitation_mm=0.0,
                    rain_mm=0.0,
                    snowfall_cm=0.0,
                    shortwave_radiation_w_m2=100.0,
                    direct_radiation_w_m2=80.0,
                    diffuse_radiation_w_m2=20.0,
                    source="open_meteo_forecast",
                    resolution_minutes=60,
                )
            ],
        )

    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_solar_dashboard(now=now)

    assert payload["station"]["id"] == config.station.id
    assert payload["station"]["name"] == "SmartEnergy Lab"
    assert payload["station"]["timezone"] == "Europe/Kyiv"
    assert payload["current"]["solar_power_w"] == 123.4
    assert payload["current"]["pv_voltage_v"] == pytest.approx(66.0, abs=0.8)
    assert payload["current"]["pv_current_a"] == pytest.approx(
        123.4 / payload["current"]["pv_voltage_v"]
    )
    assert payload["weather"]["cloud_cover_percent"] == 45.0
    assert payload["weather"]["weather_code"] == 2
    assert payload["weather"]["weather_state"] == "partly_cloudy"
    assert payload["weather"]["weather_label"] == "мінлива хмарність"
    assert payload["weather"]["temperature_c"] == 18.4
    assert payload["weather"]["sunrise_local"].startswith("2026-05-10T")
    assert payload["weather"]["sunset_local"].startswith("2026-05-10T")
    assert payload["weather"]["sunrise"] == payload["weather"]["sunrise_local"]
    assert payload["weather"]["sunset"] == payload["weather"]["sunset_local"]
    assert payload["available_start_local"] == (
        now - timedelta(hours=2)
    ).astimezone(STATION_TIMEZONE).isoformat()
    assert payload["available_end_local"] == now.astimezone(STATION_TIMEZONE).isoformat()
    assert len(payload["charts"]["last30m"]["points"]) == 2
    assert payload["charts"]["last30m"]["points"][-1]["power_w"] == 123.4
    assert payload["charts"]["last30m"]["metadata"]["returned_points"] == 2
    assert payload["charts"]["last3h"]["points"][0] == {
        "timestamp_utc": (now - timedelta(hours=2)).isoformat(),
        "timestamp_local": (now - timedelta(hours=2))
        .astimezone(STATION_TIMEZONE)
        .isoformat(),
        "power_w": 70.0,
        "source": "forecast",
        "resolution_seconds": 60,
        "source_resolution_seconds": 5,
    }
    assert payload["charts"]["last3h"]["points"][-1]["timestamp_utc"] == now.isoformat()
    assert payload["charts"]["last3h"]["points"][-1]["source_resolution_seconds"] == 1
    assert payload["charts"]["last3h"]["metadata"]["visual_resolution_seconds"] == 60
    assert payload["charts"]["last3h"]["metadata"]["source_resolutions_used"] == [5, 1]
    assert payload["charts"]["last3h"]["metadata"]["cache_status"] == "ok"


def test_solar_dashboard_at_uses_weather_adjusted_source_tables(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'dashboard-at.db'}"
    target = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    engine = get_engine(database_url)
    create_db_and_tables(engine)

    with Session(engine) as session:
        save_simulated_solar_points(
            session,
            [
                _simulated_solar_row(
                    target - timedelta(minutes=30),
                    config_hash,
                    180.0,
                ),
                _simulated_solar_row(
                    target - timedelta(minutes=15),
                    config_hash,
                    210.0,
                ),
                _simulated_solar_row(target, config_hash, 240.0),
            ],
        )
        save_forecast_rows(
            session,
            [
                WeatherForecast(
                    station_id=config.station.id,
                    fetched_at_utc=target - timedelta(hours=1),
                    forecast_timestamp_utc=target,
                    forecast_timestamp_local=target.astimezone(STATION_TIMEZONE),
                    weather_code=0,
                    temperature_c=20.0,
                    cloud_cover_percent=10.0,
                    precipitation_mm=0.0,
                    rain_mm=0.0,
                    snowfall_cm=0.0,
                    shortwave_radiation_w_m2=500.0,
                    direct_radiation_w_m2=400.0,
                    diffuse_radiation_w_m2=100.0,
                    source="open_meteo_forecast",
                    resolution_minutes=60,
                )
            ],
        )

    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_solar_dashboard(at=target)

    assert payload["requested_at_utc"] == target.isoformat()
    assert payload["resolved_at_utc"] == target.isoformat()
    assert payload["current"]["solar_power_w"] == 240.0
    assert payload["charts"]["last30m"]["metadata"]["returned_points"] > 0
    assert payload["charts"]["last30m"]["points"][-1]["source"] == "historical"


def test_solar_dashboard_at_outside_source_range_returns_404(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'dashboard-at-out-of-range.db'}"
    create_db_and_tables(get_engine(database_url))
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    with pytest.raises(HTTPException) as exc_info:
        get_solar_dashboard(at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc))

    assert exc_info.value.status_code == 404
    assert "Solar dashboard data is not available" in exc_info.value.detail["message"]


def test_solar_dashboard_charts_return_reduced_visual_cadence_series(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'full-series.db'}"
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    engine = get_engine(database_url)
    create_db_and_tables(engine)

    with Session(engine) as session:
        rows: list[InterpolatedSolarProduction] = []
        chart_specs = {
            "last30m": {
                "minutes": 30,
                "visual_resolution": 10,
                "source_resolution": 1,
                "expected_points": 181,
            },
            "last3h": {
                "minutes": 180,
                "visual_resolution": 60,
                "source_resolution": 5,
                "expected_points": 181,
            },
            "last12h": {
                "minutes": 720,
                "visual_resolution": 180,
                "source_resolution": 30,
                "expected_points": 241,
            },
            "last24h": {
                "minutes": 1440,
                "visual_resolution": 300,
                "source_resolution": 60,
                "expected_points": 289,
            },
            "last7d": {
                "minutes": 10080,
                "visual_resolution": 900,
                "source_resolution": 300,
                "expected_points": 673,
            },
        }
        for index, spec in enumerate(
            chart_specs.values(),
            start=1,
        ):
            current = now - timedelta(minutes=spec["minutes"])
            step = timedelta(seconds=spec["visual_resolution"])
            point_index = 1
            while current <= now:
                rows.append(
                    _interpolated_row(
                        current,
                        config_hash,
                        power_w=(10.0 * index) + point_index,
                        resolution_seconds=spec["source_resolution"],
                    )
                )
                current += step
                point_index += 1
        save_interpolated_solar_points(session, rows)

    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_solar_dashboard(now=now)

    for chart_id, spec in chart_specs.items():
        chart = payload["charts"][chart_id]
        assert len(chart["points"]) == spec["expected_points"]
        assert chart["metadata"]["visual_resolution_seconds"] == spec["visual_resolution"]
        assert chart["metadata"]["cache_status"] == "ok"
        assert chart["metadata"]["returned_points"] == len(chart["points"])
        assert chart["metadata"]["actual_start_local"] is not None
        assert chart["metadata"]["actual_end_local"] == now.astimezone(
            STATION_TIMEZONE
        ).isoformat()
        assert {point["resolution_seconds"] for point in chart["points"]} == {
            spec["visual_resolution"]
        }


def test_solar_dashboard_preserves_expected_source_tiers_for_live_charts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'source-tiers.db'}"
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    engine = get_engine(database_url)
    create_db_and_tables(engine)

    with Session(engine) as session:
        save_interpolated_solar_points(
            session,
            [
                _interpolated_row(
                    now - timedelta(days=6),
                    config_hash,
                    power_w=300.0,
                    resolution_seconds=300,
                ),
                _interpolated_row(
                    now - timedelta(hours=20),
                    config_hash,
                    power_w=60.0,
                    resolution_seconds=60,
                ),
                _interpolated_row(
                    now - timedelta(hours=10),
                    config_hash,
                    power_w=30.0,
                    resolution_seconds=30,
                ),
                _interpolated_row(
                    now - timedelta(hours=2),
                    config_hash,
                    power_w=5.0,
                    resolution_seconds=5,
                ),
                _interpolated_row(
                    now - timedelta(minutes=20),
                    config_hash,
                    power_w=1.0,
                    resolution_seconds=1,
                ),
                _interpolated_row(
                    now,
                    config_hash,
                    power_w=2.0,
                    resolution_seconds=1,
                ),
            ],
        )

    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_solar_dashboard(now=now)

    assert {
        chart_id: chart["metadata"]["source_resolutions_used"]
        for chart_id, chart in payload["charts"].items()
    } == {
        "last30m": [1],
        "last3h": [5, 1],
        "last12h": [30, 5, 1],
        "last24h": [60, 30, 5, 1],
        "last7d": [300, 60, 30, 5, 1],
    }


def test_solar_dashboard_last24h_composes_finer_tiers_at_five_minute_cadence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'last24h-composed.db'}"
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    engine = get_engine(database_url)
    create_db_and_tables(engine)

    with Session(engine) as session:
        rows = [
            _interpolated_row(
                now - timedelta(hours=20),
                config_hash,
                power_w=60.0,
                resolution_seconds=60,
            ),
            _interpolated_row(
                now - timedelta(hours=10),
                config_hash,
                power_w=30.0,
                resolution_seconds=30,
            ),
            _interpolated_row(
                now - timedelta(hours=2),
                config_hash,
                power_w=5.0,
                resolution_seconds=5,
            ),
        ]
        current = now - timedelta(minutes=5)
        while current <= now:
            rows.append(
                _interpolated_row(
                    current,
                    config_hash,
                    power_w=1.0,
                    resolution_seconds=1,
                )
            )
            current += timedelta(seconds=1)
        save_interpolated_solar_points(session, rows)

    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_solar_dashboard(now=now)
    chart = payload["charts"]["last24h"]
    near_now_points = [
        point
        for point in chart["points"]
        if datetime.fromisoformat(point["timestamp_utc"]) >= now - timedelta(minutes=5)
    ]

    assert chart["metadata"]["visual_resolution_seconds"] == 300
    assert chart["metadata"]["source_resolutions_used"] == [60, 30, 5, 1]
    assert chart["metadata"]["cache_status"] == "ok"
    assert {point["resolution_seconds"] for point in chart["points"]} == {300}
    assert {point["source_resolution_seconds"] for point in chart["points"]} == {
        1,
        5,
        30,
        60,
    }
    assert len(near_now_points) == 2
    assert all(point["source_resolution_seconds"] == 1 for point in near_now_points)


def test_solar_dashboard_last7d_composes_all_tiers_at_fifteen_minute_cadence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'last7d-composed.db'}"
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    engine = get_engine(database_url)
    create_db_and_tables(engine)

    with Session(engine) as session:
        rows = [
            _interpolated_row(
                now - timedelta(days=6),
                config_hash,
                power_w=300.0,
                resolution_seconds=300,
            ),
            _interpolated_row(
                now - timedelta(hours=20),
                config_hash,
                power_w=60.0,
                resolution_seconds=60,
            ),
            _interpolated_row(
                now - timedelta(hours=10),
                config_hash,
                power_w=30.0,
                resolution_seconds=30,
            ),
            _interpolated_row(
                now - timedelta(hours=2),
                config_hash,
                power_w=5.0,
                resolution_seconds=5,
            ),
        ]
        current = now - timedelta(minutes=10)
        while current <= now:
            rows.append(
                _interpolated_row(
                    current,
                    config_hash,
                    power_w=1.0,
                    resolution_seconds=1,
                )
            )
            current += timedelta(seconds=1)
        save_interpolated_solar_points(session, rows)

    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_solar_dashboard(now=now)
    chart = payload["charts"]["last7d"]
    near_now_points = [
        point
        for point in chart["points"]
        if datetime.fromisoformat(point["timestamp_utc"]) >= now - timedelta(minutes=10)
    ]

    assert chart["metadata"]["visual_resolution_seconds"] == 900
    assert chart["metadata"]["source_resolutions_used"] == [300, 60, 30, 5, 1]
    assert chart["metadata"]["cache_status"] == "ok"
    assert {point["resolution_seconds"] for point in chart["points"]} == {900}
    assert {point["source_resolution_seconds"] for point in chart["points"]} == {
        1,
        5,
        30,
        60,
        300,
    }
    assert len(near_now_points) == 1
    assert all(point["source_resolution_seconds"] == 1 for point in near_now_points)


def test_solar_dashboard_empty_range_reports_degraded_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'empty-range.db'}"
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    engine = get_engine(database_url)
    create_db_and_tables(engine)

    with Session(engine) as session:
        save_interpolated_solar_points(
            session,
            [
                _interpolated_row(
                    now - timedelta(hours=4),
                    config_hash,
                    power_w=10.0,
                    resolution_seconds=5,
                )
            ],
        )

    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_solar_dashboard(now=now)
    chart = payload["charts"]["last3h"]

    assert chart["metadata"]["visual_resolution_seconds"] == 60
    assert chart["metadata"]["source_resolutions_used"] == []
    assert chart["metadata"]["cache_status"] == "empty_range"
    assert chart["metadata"]["returned_points"] == 0
    assert chart["points"] == []


def test_solar_dashboard_endpoint_handles_empty_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'empty-dashboard.db'}"
    create_db_and_tables(get_engine(database_url))
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_solar_dashboard(now=now)
    assert payload["current"]["solar_power_w"] is None
    assert payload["weather"]["weather_label"] == "невідомо"
    assert payload["weather"]["weather_state"] == "unknown"
    assert payload["weather"]["temperature_c"] is None
    assert payload["weather"]["sunrise_local"].startswith("2026-05-10T")
    assert payload["weather"]["sunset_local"].startswith("2026-05-10T")
    assert payload["available_start_local"] is None
    assert payload["available_end_local"] is None
    assert payload["charts"]["last30m"]["points"] == []
    assert payload["charts"]["last30m"]["metadata"]["returned_points"] == 0
    assert payload["charts"]["last30m"]["metadata"]["cache_status"] == "empty_range"


def test_solar_dashboard_chart_end_times_align_to_visual_cadence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'aligned-dashboard.db'}"
    create_db_and_tables(get_engine(database_url))
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    payload = get_solar_dashboard(
        now=datetime(2026, 5, 10, 0, 7, 41, 123456, tzinfo=timezone.utc)
    )

    assert payload["charts"]["last30m"]["metadata"]["requested_end_local"].endswith(
        "03:07:40+03:00"
    )
    assert payload["charts"]["last3h"]["metadata"]["requested_end_local"].endswith(
        "03:07:00+03:00"
    )
    assert payload["charts"]["last12h"]["metadata"]["requested_end_local"].endswith(
        "03:06:00+03:00"
    )
    assert payload["charts"]["last24h"]["metadata"]["requested_end_local"].endswith(
        "03:05:00+03:00"
    )
    assert payload["charts"]["last7d"]["metadata"]["requested_end_local"].endswith(
        "03:00:00+03:00"
    )


def test_solar_history_bounds_endpoint_caps_end_to_current_local_hour(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'history-bounds.db'}"
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))
    now_local = datetime(2026, 5, 11, 16, 37, tzinfo=STATION_TIMEZONE)
    expected_history_end = now_local.replace(minute=0, second=0, microsecond=0)
    power_start = datetime(2025, 10, 6, 0, 0, tzinfo=STATION_TIMEZONE).astimezone(
        timezone.utc
    )
    power_mid = datetime(2026, 4, 4, 12, 0, tzinfo=STATION_TIMEZONE).astimezone(
        timezone.utc
    )
    power_end = datetime(2026, 5, 20, 0, 0, tzinfo=STATION_TIMEZONE).astimezone(
        timezone.utc
    )
    ideal_start = datetime(2025, 10, 1, 0, 0, tzinfo=STATION_TIMEZONE)
    ideal_future = datetime(2026, 12, 31, 0, 0, tzinfo=STATION_TIMEZONE)

    with Session(engine) as session:
        save_simulated_solar_points(
            session,
            [
                _simulated_solar_row(power_start, config_hash, 100.0),
                _simulated_solar_row(power_mid, config_hash, 120.0),
            ],
        )
        save_forecast_solar_points(
            session,
            [_forecast_solar_row(power_end, config_hash, 80.0)],
        )
        save_ideal_solar_points(
            session,
            [
                _ideal_solar_row(ideal_start, config_hash, 90.0),
                _ideal_solar_row(ideal_future, config_hash, 90.0),
            ],
        )

    payload = get_solar_history_bounds(now=now_local)

    assert payload == {
        "power_start_local": power_start.astimezone(STATION_TIMEZONE).isoformat(),
        "power_end_local": expected_history_end.isoformat(),
        "daily_start_local": power_start.astimezone(STATION_TIMEZONE).isoformat(),
        "daily_end_local": expected_history_end.isoformat(),
    }


def test_solar_power_history_accepts_april_range_before_may_without_recent_clamp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'power-history-april.db'}"
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))
    start_local = datetime(2026, 4, 1, 0, 0, tzinfo=STATION_TIMEZONE)
    end_local = datetime(2026, 4, 8, 0, 0, tzinfo=STATION_TIMEZONE)

    with Session(engine) as session:
        rows: list[SimulatedSolarProduction] = []
        current = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)
        while current <= end_utc:
            rows.append(_simulated_solar_row(current, config_hash, 100.0))
            current += timedelta(minutes=15)
        save_simulated_solar_points(session, rows)

    payload = get_solar_power_history(
        start=start_local,
        end=end_local,
        now=datetime(2026, 5, 11, 12, 0, tzinfo=STATION_TIMEZONE),
    )

    assert payload["metadata"]["requested_start_local"] == start_local.isoformat()
    assert payload["metadata"]["requested_end_local"] == end_local.isoformat()
    assert payload["metadata"]["actual_start_local"] == start_local.isoformat()
    assert payload["metadata"]["actual_end_local"] == end_local.isoformat()
    assert payload["metadata"]["visual_resolution_seconds"] == 15 * 60
    assert payload["metadata"]["clamped_start"] is False
    assert payload["metadata"]["clamped_end"] is False
    assert payload["metadata"]["returned_points"] == len(payload["points"])
    assert payload["points"][0]["timestamp_local"].startswith("2026-04-01T00:00:00")
    assert payload["points"][-1]["timestamp_local"].startswith("2026-04-08T00:00:00")


def test_solar_power_history_preserves_valid_start_when_clamping_to_31_days(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'power-history-max-range.db'}"
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))
    start_local = datetime(2026, 1, 1, 0, 0, tzinfo=STATION_TIMEZONE)
    requested_end_local = datetime(2026, 2, 20, 0, 0, tzinfo=STATION_TIMEZONE)
    expected_end_local = datetime(2026, 2, 1, 0, 0, tzinfo=STATION_TIMEZONE)

    with Session(engine) as session:
        rows: list[SimulatedSolarProduction] = []
        current = start_local.astimezone(timezone.utc)
        end_utc = requested_end_local.astimezone(timezone.utc)
        while current <= end_utc:
            rows.append(_simulated_solar_row(current, config_hash, 100.0))
            current += timedelta(minutes=90)
        save_simulated_solar_points(session, rows)

    payload = get_solar_power_history(
        start=start_local,
        end=requested_end_local,
        now=datetime(2026, 3, 1, 12, 0, tzinfo=STATION_TIMEZONE),
    )

    assert payload["metadata"]["requested_start_local"] == start_local.isoformat()
    assert payload["metadata"]["requested_end_local"] == requested_end_local.isoformat()
    assert payload["metadata"]["actual_start_local"] == start_local.isoformat()
    assert payload["metadata"]["actual_end_local"] == expected_end_local.isoformat()
    assert payload["metadata"]["visual_resolution_seconds"] == 90 * 60
    assert payload["metadata"]["clamped_start"] is False
    assert payload["metadata"]["clamped_end"] is True
    assert payload["metadata"]["max_range_days"] == 31


def test_solar_power_history_clamps_future_request_to_latest_elapsed_hour(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'power-history-current-hour.db'}"
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))
    now_local = datetime(2026, 5, 11, 16, 37, tzinfo=STATION_TIMEZONE)
    expected_end_local = now_local.replace(minute=0, second=0, microsecond=0)
    start_local = datetime(2026, 5, 11, 12, 0, tzinfo=STATION_TIMEZONE)
    requested_end_local = datetime(2026, 5, 12, 23, 59, 59, tzinfo=STATION_TIMEZONE)

    with Session(engine) as session:
        rows: list[SimulatedSolarProduction] = []
        current = start_local.astimezone(timezone.utc)
        source_end = datetime(2026, 5, 12, 20, 0, tzinfo=STATION_TIMEZONE).astimezone(
            timezone.utc
        )
        while current <= source_end:
            rows.append(_simulated_solar_row(current, config_hash, 100.0))
            current += timedelta(minutes=15)
        save_simulated_solar_points(session, rows)

    payload = get_solar_power_history(
        start=start_local,
        end=requested_end_local,
        now=now_local,
    )

    assert payload["metadata"]["available_end_local"] == expected_end_local.isoformat()
    assert payload["metadata"]["actual_end_local"] == expected_end_local.isoformat()
    assert payload["metadata"]["clamped_end"] is True
    assert all(
        datetime.fromisoformat(point["timestamp_local"]) <= expected_end_local
        for point in payload["points"]
    )


def test_solar_power_history_selects_dynamic_cadence_and_clamps_long_range(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'power-history-cadence.db'}"
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))

    start = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
    now = start + timedelta(days=60)

    with Session(engine) as session:
        save_simulated_solar_points(
            session,
            [
                _simulated_solar_row(start, config_hash, 100.0),
                _simulated_solar_row(start + timedelta(days=40), config_hash, 100.0),
            ],
        )

    assert get_solar_power_history(
        start=start,
        end=start + timedelta(days=1),
        now=now,
    )["metadata"]["visual_resolution_seconds"] == 5 * 60
    assert get_solar_power_history(
        start=start,
        end=start + timedelta(days=7),
        now=now,
    )["metadata"]["visual_resolution_seconds"] == 15 * 60
    assert get_solar_power_history(
        start=start,
        end=start + timedelta(days=12),
        now=now,
    )["metadata"]["visual_resolution_seconds"] == 30 * 60
    assert get_solar_power_history(
        start=start,
        end=start + timedelta(days=15),
        now=now,
    )["metadata"]["visual_resolution_seconds"] == 45 * 60
    assert get_solar_power_history(
        start=start,
        end=start + timedelta(days=21),
        now=now,
    )["metadata"]["visual_resolution_seconds"] == 60 * 60
    assert get_solar_power_history(
        start=start,
        end=start + timedelta(days=25),
        now=now,
    )["metadata"]["visual_resolution_seconds"] == 75 * 60
    assert get_solar_power_history(
        start=start,
        end=start + timedelta(days=31),
        now=now,
    )["metadata"]["visual_resolution_seconds"] == 90 * 60

    clamped = get_solar_power_history(
        start=start,
        end=start + timedelta(days=40),
        now=now,
    )
    assert clamped["metadata"]["visual_resolution_seconds"] == 90 * 60
    assert clamped["metadata"]["clamped_start"] is False
    assert clamped["metadata"]["clamped_end"] is True
    assert clamped["metadata"]["max_range_days"] == 31


def test_solar_power_history_composes_historical_and_forecast_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'power-history-compose.db'}"
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))
    start = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 10, 8, 35, tzinfo=timezone.utc)

    with Session(engine) as session:
        save_simulated_solar_points(
            session,
            [
                _simulated_solar_row(start, config_hash, 100.0),
                _simulated_solar_row(start + timedelta(minutes=5), config_hash, 105.0),
                _simulated_solar_row(start + timedelta(minutes=10), config_hash, 110.0),
                _simulated_solar_row(start + timedelta(minutes=15), config_hash, 115.0),
            ],
        )
        save_forecast_solar_points(
            session,
            [
                _forecast_solar_row(start + timedelta(minutes=20), config_hash, 200.0),
                _forecast_solar_row(start + timedelta(minutes=25), config_hash, 205.0),
                _forecast_solar_row(start + timedelta(minutes=30), config_hash, 210.0),
                _forecast_solar_row(
                    start + timedelta(minutes=35),
                    config_hash,
                    215.0,
                ),
            ],
        )

    payload = get_solar_power_history(start=start, end=end)

    assert payload["metadata"]["mode"] == "power"
    assert payload["metadata"]["visual_resolution_seconds"] == 5 * 60
    assert payload["metadata"]["returned_points"] == 8
    assert [point["source"] for point in payload["points"]] == [
        "historical",
        "historical",
        "historical",
        "historical",
        "forecast",
        "forecast",
        "forecast",
        "forecast",
    ]


def test_solar_power_history_30_day_point_count_stays_bounded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'power-history-bounded.db'}"
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    monkeypatch.setenv("SMARTENERGY_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTENERGY_CONFIG_PATH", str(CONFIG_PATH))
    start = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=30)

    with Session(engine) as session:
        current = start
        rows: list[SimulatedSolarProduction] = []
        while current <= end:
            rows.append(_simulated_solar_row(current, config_hash, 100.0))
            current += timedelta(hours=1)
        save_simulated_solar_points(session, rows)

    payload = get_solar_power_history(start=start, end=end)

    assert payload["metadata"]["visual_resolution_seconds"] == 90 * 60
    assert payload["metadata"]["returned_points"] <= 600
    assert payload["metadata"]["returned_points"] == len(payload["points"])


def test_solar_daily_energy_returns_one_weather_adjusted_point_per_day(
    tmp_path: Path,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'daily-energy.db'}"
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    day_start = datetime(2026, 5, 8, 0, 0, tzinfo=STATION_TIMEZONE)

    with Session(engine) as session:
        save_ideal_solar_points(
            session,
            [
                _ideal_solar_row(day_start + timedelta(minutes=15 * index), config_hash, 100.0)
                for index in range(96)
            ],
        )
        save_simulated_solar_points(
            session,
            [
                _simulated_solar_row(
                    (day_start + timedelta(minutes=15 * index)).astimezone(timezone.utc),
                    config_hash,
                    50.0,
                )
                for index in range(96)
            ],
        )

        payload = build_solar_daily_energy_history_payload(
            session,
            config,
            start=day_start,
            end=day_start + timedelta(hours=23, minutes=59),
            now=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        )

    assert payload["metadata"]["mode"] == "daily_energy"
    assert payload["metadata"]["returned_days"] == 1
    assert payload["points"] == [
        {
            "date_local": "2026-05-08",
            "weather_adjusted_daily_energy_kwh": 1.2,
        }
    ]


def test_solar_daily_energy_handles_long_ranges_without_dense_output(
    tmp_path: Path,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'daily-energy-long.db'}"
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    start = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)

    with Session(engine) as session:
        save_ideal_solar_points(
            session,
            [
                _ideal_solar_row(start + timedelta(days=index), config_hash, 100.0)
                for index in range(45)
            ],
        )
        save_simulated_solar_points(
            session,
            [
                _simulated_solar_row(start + timedelta(days=index), config_hash, 50.0)
                for index in range(45)
            ],
        )
        payload = build_solar_daily_energy_history_payload(
            session,
            config,
            start=start,
            end=start + timedelta(days=44, hours=23),
            now=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        )

    assert payload["metadata"]["returned_days"] == 45
    assert len(payload["points"]) == 45
    assert all("date_local" in point for point in payload["points"])
    assert all(
        point["weather_adjusted_daily_energy_kwh"] is not None
        for point in payload["points"]
    )


def test_solar_daily_energy_future_range_does_not_use_future_ideal_data(
    tmp_path: Path,
) -> None:
    config = load_config(CONFIG_PATH)
    config_hash = calculate_config_hash(config)
    database_url = f"sqlite:///{tmp_path / 'daily-energy-future-ideal.db'}"
    engine = get_engine(database_url)
    create_db_and_tables(engine)
    now_local = datetime(2026, 5, 11, 16, 37, tzinfo=STATION_TIMEZONE)
    weather_day = datetime(2026, 5, 10, 0, 0, tzinfo=STATION_TIMEZONE)
    future_ideal_day = datetime(2026, 6, 1, 0, 0, tzinfo=STATION_TIMEZONE)

    with Session(engine) as session:
        save_simulated_solar_points(
            session,
            [
                _simulated_solar_row(
                    (weather_day + timedelta(minutes=15 * index)).astimezone(
                        timezone.utc
                    ),
                    config_hash,
                    50.0,
                )
                for index in range(96)
            ],
        )
        save_ideal_solar_points(
            session,
            [
                _ideal_solar_row(
                    future_ideal_day + timedelta(minutes=15 * index),
                    config_hash,
                    100.0,
                )
                for index in range(96)
            ],
        )
        payload = build_solar_daily_energy_history_payload(
            session,
            config,
            start=weather_day,
            end=future_ideal_day + timedelta(hours=23),
            now=now_local,
        )

    assert payload["metadata"]["available_end_local"] == weather_day.replace(
        hour=23,
        minute=45,
    ).isoformat()
    assert payload["metadata"]["actual_end_local"] == "2026-05-10"
    assert payload["metadata"]["returned_days"] == 1
    assert payload["points"] == [
        {
            "date_local": "2026-05-10",
            "weather_adjusted_daily_energy_kwh": 1.2,
        }
    ]


def _interpolated_row(
    timestamp_utc: datetime,
    config_hash: str,
    power_w: float,
    resolution_seconds: int,
) -> InterpolatedSolarProduction:
    return InterpolatedSolarProduction(
        station_id="smart_energy_lab",
        config_hash=config_hash,
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc.astimezone(STATION_TIMEZONE),
        source_type="forecast",
        resolution_seconds=resolution_seconds,
        lower_source_timestamp_utc=timestamp_utc,
        upper_source_timestamp_utc=timestamp_utc,
        lower_power_w=power_w,
        upper_power_w=power_w,
        interpolation_ratio=0.0,
        baseline_power_w=power_w,
        variation_factor=1.0,
        power_w=power_w,
        generated_at_utc=timestamp_utc,
    )


def _simulated_solar_row(
    timestamp_utc: datetime,
    config_hash: str,
    power_w: float,
) -> SimulatedSolarProduction:
    return SimulatedSolarProduction(
        station_id="smart_energy_lab",
        config_hash=config_hash,
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc.astimezone(STATION_TIMEZONE),
        ideal_power_w=power_w,
        weather_code=0,
        weather_state="clear",
        cloud_cover_percent=0.0,
        weather_factor=1.0,
        simulated_power_w=power_w,
    )


def _forecast_solar_row(
    timestamp_utc: datetime,
    config_hash: str,
    power_w: float,
) -> ForecastSolarProduction:
    return ForecastSolarProduction(
        station_id="smart_energy_lab",
        config_hash=config_hash,
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc.astimezone(STATION_TIMEZONE),
        ideal_power_w=power_w,
        weather_code=0,
        weather_state="clear",
        cloud_cover_percent=0.0,
        weather_factor=1.0,
        forecast_power_w=power_w,
    )


def _ideal_solar_row(
    timestamp: datetime,
    config_hash: str,
    power_w: float,
) -> IdealSolarProduction:
    timestamp_utc = timestamp.astimezone(timezone.utc)
    return IdealSolarProduction(
        station_id="smart_energy_lab",
        config_hash=config_hash,
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc.astimezone(STATION_TIMEZONE),
        sun_elevation_deg=30.0,
        sun_azimuth_deg=180.0,
        incidence_factor=1.0,
        ambient_factor=0.0,
        direct_power_w=power_w,
        ambient_power_w=0.0,
        ideal_power_w=power_w,
    )
