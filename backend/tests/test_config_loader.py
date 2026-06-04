from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config_loader import calculate_config_hash, calculate_system_config_hash, load_config


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "station.default.yaml"


def test_load_config_loads_default_station() -> None:
    config = load_config(CONFIG_PATH)

    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        raw_config = yaml.safe_load(file)

    assert config.station.id == raw_config["station"]["id"]
    assert config.station.name == "SmartEnergy Lab"
    assert config.station.installation_date == "2025-10-06"
    assert config.station.solar.installation.timezone == "Europe/Kyiv"
    assert config.station.economics.grid_tariff_uah_per_kwh == 4.32


def test_config_hash_is_stable() -> None:
    config = load_config(CONFIG_PATH)

    assert calculate_config_hash(config) == calculate_config_hash(config)
    assert calculate_system_config_hash(config) == calculate_system_config_hash(config)


def test_system_config_hash_includes_grid_settings(tmp_path: Path) -> None:
    base_config = load_config(CONFIG_PATH)
    changed_raw_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    changed_raw_config["station"]["grid"]["outage_queue"] = "4.2"
    changed_path = tmp_path / "station.changed-grid.yaml"
    changed_path.write_text(yaml.safe_dump(changed_raw_config), encoding="utf-8")
    changed_config = load_config(changed_path)

    assert calculate_config_hash(base_config) == calculate_config_hash(changed_config)
    assert calculate_system_config_hash(base_config) != calculate_system_config_hash(
        changed_config
    )


@pytest.mark.parametrize(
    ("section_path", "value"),
    [
        (("station", "economics", "grid_tariff_uah_per_kwh"), 7.5),
        (("station", "installation_date"), "2025-12-31"),
        (("station", "battery", "capacity_ah"), 180),
        (("station", "ems", "normal_target_soc_percent"), 75),
    ],
)
def test_non_solar_system_fields_do_not_change_solar_hash(
    tmp_path: Path,
    section_path: tuple[str, ...],
    value: object,
) -> None:
    base_config = load_config(CONFIG_PATH)
    changed_raw_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    target = changed_raw_config
    for key in section_path[:-1]:
        target = target[key]
    target[section_path[-1]] = value
    changed_path = tmp_path / "station.changed-non-solar.yaml"
    changed_path.write_text(yaml.safe_dump(changed_raw_config), encoding="utf-8")
    changed_config = load_config(changed_path)

    assert calculate_config_hash(base_config) == calculate_config_hash(changed_config)
    assert calculate_system_config_hash(base_config) != calculate_system_config_hash(
        changed_config
    )


def test_invalid_station_installation_date_raises_clear_error(tmp_path: Path) -> None:
    invalid_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    invalid_config["station"]["installation_date"] = "bad-date"
    config_path = tmp_path / "station.invalid-installation-date.yaml"
    config_path.write_text(yaml.safe_dump(invalid_config), encoding="utf-8")

    with pytest.raises(Exception, match="YYYY-MM-DD"):
        load_config(config_path)


def test_invalid_grid_tariff_raises_clear_error(tmp_path: Path) -> None:
    invalid_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    invalid_config["station"]["economics"]["grid_tariff_uah_per_kwh"] = 0
    config_path = tmp_path / "station.invalid-tariff.yaml"
    config_path.write_text(yaml.safe_dump(invalid_config), encoding="utf-8")

    with pytest.raises(Exception, match="greater than 0"):
        load_config(config_path)


def test_invalid_panel_type_reference_raises_clear_error(tmp_path: Path) -> None:
    invalid_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    invalid_config["station"]["solar"]["array"]["series_connections"][0][
        "panel_type_id"
    ] = "missing_panel"
    config_path = tmp_path / "station.invalid.yaml"
    config_path.write_text(yaml.safe_dump(invalid_config), encoding="utf-8")

    with pytest.raises(ValueError, match="missing panel_type_id 'missing_panel'"):
        load_config(config_path)


def test_invalid_timezone_raises_clear_error(tmp_path: Path) -> None:
    invalid_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    invalid_config["station"]["solar"]["installation"]["timezone"] = "Invalid/Zone"
    config_path = tmp_path / "station.invalid-timezone.yaml"
    config_path.write_text(yaml.safe_dump(invalid_config), encoding="utf-8")

    with pytest.raises(Exception, match="Invalid timezone 'Invalid/Zone'"):
        load_config(config_path)


def test_extra_battery_config_key_is_rejected(tmp_path: Path) -> None:
    invalid_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    invalid_config["station"]["battery"]["max_charge_c_rate"] = 0.5
    config_path = tmp_path / "station.extra-battery-key.yaml"
    config_path.write_text(yaml.safe_dump(invalid_config), encoding="utf-8")

    with pytest.raises(Exception, match="Extra|extra|not permitted"):
        load_config(config_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("chemistry", "nickel", "lead_acid|lifepo4|li_ion"),
        ("nominal_voltage_v", 48, "12 or 24"),
        ("capacity_ah", 0, "greater than 0"),
        ("installation_date", "bad-date", "YYYY-MM-DD"),
    ],
)
def test_invalid_battery_config_values_fail_clearly(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    invalid_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    invalid_config["station"]["battery"][field] = value
    config_path = tmp_path / f"station.invalid-battery-{field}.yaml"
    config_path.write_text(yaml.safe_dump(invalid_config), encoding="utf-8")

    with pytest.raises(Exception, match=message):
        load_config(config_path)
