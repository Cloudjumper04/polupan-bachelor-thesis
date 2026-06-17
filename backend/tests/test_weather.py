from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.simulation import weather
from app.simulation.weather import (
    OPEN_METEO_FORECAST_SOURCE,
    calculate_weather_factor,
    fetch_open_meteo_forecast,
    fetch_open_meteo_historical_weather,
    generate_weather_adjusted_solar,
    map_weather_code_to_state,
)
from app.storage.solar_repository import IdealSolarProduction
from app.storage.weather_repository import WeatherObservation


@pytest.mark.parametrize(
    ("weather_code", "expected_state"),
    [
        (0, "clear"),
        (1, "partly_cloudy"),
        (2, "partly_cloudy"),
        (3, "cloudy"),
        (45, "fog"),
        (48, "fog"),
        (51, "drizzle"),
        (61, "rain"),
        (80, "rain"),
        (71, "snow"),
        (85, "snow"),
        (95, "thunderstorm"),
        (99, "thunderstorm"),
        (1234, "unknown"),
        (None, "unknown"),
    ],
)
def test_map_weather_code_to_state_maps_known_codes(
    weather_code: int | None,
    expected_state: str,
) -> None:
    assert map_weather_code_to_state(weather_code) == expected_state


@pytest.mark.parametrize(
    ("weather_code", "cloud_cover_percent"),
    [
        (0, 0.0),
        (1, 25.0),
        (3, 100.0),
        (45, None),
        (61, 80.0),
        (71, None),
        (95, 40.0),
        (None, None),
    ],
)
def test_calculate_weather_factor_is_clamped(
    weather_code: int | None,
    cloud_cover_percent: float | None,
) -> None:
    factor = calculate_weather_factor(
        weather_code,
        cloud_cover_percent,
        "2026-01-01T00:00:00+00:00",
    )

    assert 0.05 <= factor <= 1.0


def test_calculate_weather_factor_is_deterministic_for_same_inputs() -> None:
    first = calculate_weather_factor(0, 35.0, "2026-01-01T00:15:00+00:00")
    second = calculate_weather_factor(0, 35.0, "2026-01-01T00:15:00+00:00")

    assert first == second


def test_high_cloud_cover_produces_lower_average_factor_than_low_cloud_cover() -> None:
    low_cloud_factors = [
        calculate_weather_factor(0, 5.0, f"timestamp-{index}")
        for index in range(300)
    ]
    high_cloud_factors = [
        calculate_weather_factor(0, 95.0, f"timestamp-{index}")
        for index in range(300)
    ]

    assert sum(high_cloud_factors) / len(high_cloud_factors) < sum(
        low_cloud_factors
    ) / len(low_cloud_factors)


def test_precipitation_states_produce_lower_factors_than_clear_state() -> None:
    timestamp_key = "2026-01-01T12:00:00+00:00"
    clear_factor = calculate_weather_factor(0, 0.0, timestamp_key)

    assert calculate_weather_factor(61, None, timestamp_key) < clear_factor
    assert calculate_weather_factor(71, None, timestamp_key) < clear_factor
    assert calculate_weather_factor(95, None, timestamp_key) < clear_factor


def test_fetch_open_meteo_historical_weather_parses_mocked_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _utc_payload_for_local_dates(
        date(2026, 1, 1),
        date(2026, 1, 1),
        "Europe/Kyiv",
    )
    captured_params: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return payload

    def fake_get(url: str, params: dict[str, object], timeout: int) -> FakeResponse:
        captured_params.update(params)
        assert url == weather.OPEN_METEO_ARCHIVE_URL
        assert timeout == 30
        return FakeResponse()

    monkeypatch.setattr(weather.requests, "get", fake_get)

    observations = fetch_open_meteo_historical_weather(
        latitude=50.0,
        longitude=30.0,
        timezone="Europe/Kyiv",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 1),
    )

    assert captured_params["start_date"] == "2025-12-31"
    assert captured_params["end_date"] == "2026-01-01"
    assert captured_params["timezone"] == "UTC"
    assert len(observations) == 24
    assert observations[0].timestamp_local.isoformat() == "2026-01-01T00:00:00+02:00"
    assert observations[0].timestamp_utc.isoformat() == "2025-12-31T22:00:00+00:00"
    assert observations[-1].timestamp_local.isoformat() == "2026-01-01T23:00:00+02:00"
    assert observations[-1].timestamp_utc.isoformat() == "2026-01-01T21:00:00+00:00"
    assert observations[1].weather_code == 0
    assert observations[1].temperature_c == 4.5
    assert observations[1].cloud_cover_percent == 10.0
    assert observations[1].rain_mm == 0.0
    assert observations[1].source == weather.OPEN_METEO_SOURCE


def test_fetch_open_meteo_forecast_parses_mocked_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _utc_payload_for_local_dates(
        date(2026, 1, 1),
        date(2026, 1, 3),
        "Europe/Kyiv",
    )
    captured_params: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return payload

    def fake_get(url: str, params: dict[str, object], timeout: int) -> FakeResponse:
        captured_params.update(params)
        assert url == weather.OPEN_METEO_FORECAST_URL
        assert timeout == 30
        return FakeResponse()

    monkeypatch.setattr(weather.requests, "get", fake_get)

    forecasts = fetch_open_meteo_forecast(
        latitude=50.0,
        longitude=30.0,
        timezone="Europe/Kyiv",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 3),
    )

    assert captured_params["start_date"] == "2025-12-31"
    assert captured_params["end_date"] == "2026-01-03"
    assert "forecast_hours" not in captured_params
    assert captured_params["timezone"] == "UTC"
    assert len(forecasts) == 72
    assert forecasts[0].forecast_timestamp_local.isoformat() == (
        "2026-01-01T00:00:00+02:00"
    )
    assert forecasts[0].forecast_timestamp_utc.isoformat() == (
        "2025-12-31T22:00:00+00:00"
    )
    assert forecasts[-1].forecast_timestamp_local.isoformat() == (
        "2026-01-03T23:00:00+02:00"
    )
    assert forecasts[-1].forecast_timestamp_utc.isoformat() == (
        "2026-01-03T21:00:00+00:00"
    )
    assert forecasts[2].weather_code == 0
    assert forecasts[2].temperature_c == 4.5
    assert forecasts[2].cloud_cover_percent == 10.0
    assert forecasts[2].source == OPEN_METEO_FORECAST_SOURCE
    assert forecasts[2].resolution_minutes == 60


def test_historical_fetch_uses_utc_across_dst_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _utc_payload_for_local_dates(
        date(2025, 10, 26),
        date(2025, 10, 26),
        "Europe/Kyiv",
    )
    _mock_open_meteo_response(monkeypatch, weather.OPEN_METEO_ARCHIVE_URL, payload)

    observations = fetch_open_meteo_historical_weather(
        latitude=50.0,
        longitude=30.0,
        timezone="Europe/Kyiv",
        start_date=date(2025, 10, 26),
        end_date=date(2025, 10, 26),
    )

    assert len(observations) == 25
    assert len({row.timestamp_utc for row in observations}) == 25
    repeated_hour_rows = [
        row for row in observations if row.timestamp_local.hour == 3
    ]
    assert [row.timestamp_local.fold for row in repeated_hour_rows] == [0, 1]
    assert [row.timestamp_utc.isoformat() for row in repeated_hour_rows] == [
        "2025-10-26T00:00:00+00:00",
        "2025-10-26T01:00:00+00:00",
    ]


def test_historical_fetch_uses_utc_across_dst_spring_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _utc_payload_for_local_dates(
        date(2026, 3, 29),
        date(2026, 3, 29),
        "Europe/Kyiv",
    )
    captured_params: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return payload

    def fake_get(url: str, params: dict[str, object], timeout: int) -> FakeResponse:
        captured_params.update(params)
        assert url == weather.OPEN_METEO_ARCHIVE_URL
        assert timeout == 30
        return FakeResponse()

    monkeypatch.setattr(weather.requests, "get", fake_get)

    observations = fetch_open_meteo_historical_weather(
        latitude=50.0,
        longitude=30.0,
        timezone="Europe/Kyiv",
        start_date=date(2026, 3, 29),
        end_date=date(2026, 3, 29),
    )

    assert captured_params["start_date"] == "2026-03-28"
    assert captured_params["end_date"] == "2026-03-29"
    assert captured_params["timezone"] == "UTC"
    assert len(observations) == 23
    assert len({row.timestamp_utc for row in observations}) == 23
    assert {row.timestamp_local.date() for row in observations} == {date(2026, 3, 29)}
    assert 3 not in {row.timestamp_local.hour for row in observations}
    assert observations[0].timestamp_local.isoformat() == "2026-03-29T00:00:00+02:00"
    assert observations[-1].timestamp_local.isoformat() == "2026-03-29T23:00:00+03:00"


def test_forecast_fetch_uses_utc_across_dst_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _utc_payload_for_local_dates(
        date(2025, 10, 26),
        date(2025, 10, 26),
        "Europe/Kyiv",
    )
    _mock_open_meteo_response(monkeypatch, weather.OPEN_METEO_FORECAST_URL, payload)

    forecasts = fetch_open_meteo_forecast(
        latitude=50.0,
        longitude=30.0,
        timezone="Europe/Kyiv",
        start_date=date(2025, 10, 26),
        end_date=date(2025, 10, 26),
    )

    assert len(forecasts) == 25
    assert len({row.forecast_timestamp_utc for row in forecasts}) == 25
    repeated_hour_rows = [
        row for row in forecasts if row.forecast_timestamp_local.hour == 3
    ]
    assert [row.forecast_timestamp_local.fold for row in repeated_hour_rows] == [0, 1]
    assert [row.forecast_timestamp_utc.isoformat() for row in repeated_hour_rows] == [
        "2025-10-26T00:00:00+00:00",
        "2025-10-26T01:00:00+00:00",
    ]


def test_historical_fetch_rejects_duplicate_non_ambiguous_local_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _open_meteo_payload(["2026-05-10T00:00", "2026-05-10T00:00"])

    with pytest.raises(RuntimeError, match="duplicate non-ambiguous local timestamp"):
        weather._parse_open_meteo_hourly_response(payload, "Europe/Kyiv")


def test_forecast_fetch_rejects_duplicate_utc_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _open_meteo_payload(
        ["2026-05-10T00:00:00+00:00", "2026-05-10T00:00:00+00:00"]
    )
    _mock_open_meteo_response(monkeypatch, weather.OPEN_METEO_FORECAST_URL, payload)

    with pytest.raises(RuntimeError, match="duplicate UTC timestamp"):
        fetch_open_meteo_forecast(
            latitude=50.0,
            longitude=30.0,
            timezone="Europe/Kyiv",
            start_date=date(2026, 5, 10),
            end_date=date(2026, 5, 10),
        )


def test_fetch_open_meteo_forecast_raises_clear_error_on_http_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            raise weather.requests.HTTPError("503 Server Error")

    def fake_get(url: str, params: dict[str, object], timeout: int) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setattr(weather.requests, "get", fake_get)

    with pytest.raises(RuntimeError, match="Failed to fetch Open-Meteo forecast"):
        fetch_open_meteo_forecast(50.0, 30.0, "Europe/Kyiv")


def test_fetch_open_meteo_forecast_raises_clear_error_on_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"error": True, "reason": "forecast unavailable"}

    def fake_get(url: str, params: dict[str, object], timeout: int) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setattr(weather.requests, "get", fake_get)

    with pytest.raises(RuntimeError, match="forecast unavailable"):
        fetch_open_meteo_forecast(50.0, 30.0, "Europe/Kyiv")


def test_generate_weather_adjusted_solar_produces_one_point_per_ideal_point() -> None:
    station_timezone = ZoneInfo("Europe/Kyiv")
    start_utc = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    ideal_points = [
        _ideal_point(start_utc + timedelta(minutes=15 * index), station_timezone, 100.0)
        for index in range(4)
    ]
    ideal_points.append(
        _ideal_point(start_utc + timedelta(minutes=60), station_timezone, 0.0)
    )
    weather_observations = [
        _weather_observation(start_utc, station_timezone, weather_code=0),
        _weather_observation(
            start_utc + timedelta(hours=1),
            station_timezone,
            weather_code=61,
        ),
    ]

    simulated_points = generate_weather_adjusted_solar(
        ideal_points,
        weather_observations,
    )

    assert len(simulated_points) == len(ideal_points)
    assert all(
        point.simulated_power_w <= point.ideal_power_w
        for point in simulated_points
    )
    assert simulated_points[-1].ideal_power_w == 0.0
    assert simulated_points[-1].simulated_power_w == 0.0
    assert all(0.05 <= point.weather_factor <= 1.0 for point in simulated_points)


def _ideal_point(
    timestamp_utc: datetime,
    station_timezone: ZoneInfo,
    ideal_power_w: float,
) -> IdealSolarProduction:
    return IdealSolarProduction(
        station_id="smart_energy_lab",
        config_hash="test_hash",
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc.astimezone(station_timezone),
        sun_elevation_deg=20.0,
        sun_azimuth_deg=180.0,
        incidence_factor=0.5,
        ambient_factor=0.04,
        direct_power_w=80.0,
        ambient_power_w=20.0,
        ideal_power_w=ideal_power_w,
    )


def _weather_observation(
    timestamp_utc: datetime,
    station_timezone: ZoneInfo,
    weather_code: int,
) -> WeatherObservation:
    return WeatherObservation(
        station_id="smart_energy_lab",
        timestamp_utc=timestamp_utc,
        timestamp_local=timestamp_utc.astimezone(station_timezone),
        weather_code=weather_code,
        cloud_cover_percent=0.0,
        precipitation_mm=0.0,
        rain_mm=0.0,
        snowfall_cm=0.0,
        shortwave_radiation_w_m2=100.0,
        direct_radiation_w_m2=80.0,
        diffuse_radiation_w_m2=20.0,
        source="open-meteo-archive",
    )


def _open_meteo_payload(times: list[str]) -> dict[str, object]:
    count = len(times)
    return {
        "hourly": {
            "time": times,
            "temperature_2m": [4.5] * count,
            "cloud_cover": [10] * count,
            "precipitation": [0.0] * count,
            "rain": [0.0] * count,
            "snowfall": [0.0] * count,
            "weather_code": [0] * count,
            "shortwave_radiation": [0.0] * count,
            "direct_radiation": [0.0] * count,
            "diffuse_radiation": [0.0] * count,
        }
    }


def _utc_payload_for_local_dates(
    start_date: date,
    end_date: date,
    timezone_name: str,
) -> dict[str, object]:
    station_timezone = ZoneInfo(timezone_name)
    start_utc, end_utc = weather._local_date_range_to_utc_bounds(
        start_date,
        end_date,
        station_timezone,
    )
    request_start_date, request_end_date = weather._utc_request_dates_for_local_range(
        start_utc,
        end_utc,
    )
    return _open_meteo_payload(
        _utc_hour_strings_for_request_dates(request_start_date, request_end_date)
    )


def _utc_hour_strings_for_request_dates(
    start_date: date,
    end_date: date,
) -> list[str]:
    current = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.combine(
        end_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    values: list[str] = []
    while current < end:
        values.append(current.replace(tzinfo=None).isoformat(timespec="minutes"))
        current += timedelta(hours=1)
    return values


def _mock_open_meteo_response(
    monkeypatch: pytest.MonkeyPatch,
    expected_url: str,
    payload: dict[str, object],
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return payload

    def fake_get(url: str, params: dict[str, object], timeout: int) -> FakeResponse:
        assert url == expected_url
        assert timeout == 30
        return FakeResponse()

    monkeypatch.setattr(weather.requests, "get", fake_get)
