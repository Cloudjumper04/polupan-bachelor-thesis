from __future__ import annotations

import ast
import inspect
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from app.config_loader import load_config
from app.simulation import ems as ems_module
from app.simulation.ems import (
    EmsConfig,
    EmsDecision,
    EmsDecisionEngine,
    EmsHistorySummary,
    EmsInput,
    EmsMode,
    is_cheap_tariff_active,
)


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "station.default.yaml"
STATION_TIMEZONE = ZoneInfo("Europe/Kyiv")


def test_config_validation_accepts_valid_ems_yaml_and_rejects_bad_values(
    tmp_path: Path,
) -> None:
    config = load_config(CONFIG_PATH)

    assert config.station.ems.mode == "auto"
    assert config.station.ems.inverter_output_limit_w == 2000

    ems_config = EmsConfig.from_station_config(config)
    assert ems_config.mode == EmsMode.AUTO
    assert ems_config.critical_soc_percent == 10
    assert ems_config.reserve_soc_percent == 30

    with pytest.raises(ValueError, match="mode"):
        EmsConfig(mode="invalid")
    with pytest.raises(ValueError, match="critical < reserve"):
        EmsConfig(critical_soc_percent=30, reserve_soc_percent=20)
    with pytest.raises(ValueError, match="positive"):
        EmsConfig(inverter_output_limit_w=0)

    invalid_mode = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    invalid_mode["station"]["ems"]["mode"] = "invalid"
    invalid_mode_path = tmp_path / "station.invalid-ems-mode.yaml"
    invalid_mode_path.write_text(yaml.safe_dump(invalid_mode), encoding="utf-8")
    with pytest.raises(Exception, match="auto|grid_priority|force_charge"):
        load_config(invalid_mode_path)

    invalid_thresholds = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    invalid_thresholds["station"]["ems"]["reserve_soc_percent"] = 5
    invalid_thresholds_path = tmp_path / "station.invalid-ems-thresholds.yaml"
    invalid_thresholds_path.write_text(
        yaml.safe_dump(invalid_thresholds),
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="critical < reserve"):
        load_config(invalid_thresholds_path)

    invalid_limit = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    invalid_limit["station"]["ems"]["inverter_output_limit_w"] = 0
    invalid_limit_path = tmp_path / "station.invalid-ems-limit.yaml"
    invalid_limit_path.write_text(yaml.safe_dump(invalid_limit), encoding="utf-8")
    with pytest.raises(Exception, match="greater than 0"):
        load_config(invalid_limit_path)


def test_grid_priority_with_grid_serves_load_from_grid_and_disables_discharge() -> None:
    decision = _decide(mode="grid_priority", load_w=600, solar_w=0, soc_percent=50)

    assert decision.selected_mode == EmsMode.GRID_PRIORITY
    assert decision.effective_served_load_w == 600
    assert decision.grid_to_load_w == 600
    assert decision.battery_to_load_w == 0
    assert decision.battery_provides_energy is False
    assert decision.requested_battery_discharge_energy_wh_last_minute == 0


def test_solar_priority_uses_solar_first_grid_remaining_and_surplus_charge() -> None:
    covering_remaining = _decide(
        mode="solar_priority",
        load_w=800,
        solar_w=300,
        soc_percent=50,
    )

    assert covering_remaining.solar_to_load_w == 300
    assert covering_remaining.grid_to_load_w == 500
    assert covering_remaining.battery_to_load_w == 0

    surplus = _decide(
        mode="solar_priority",
        load_w=500,
        solar_w=900,
        soc_percent=50,
    )

    assert surplus.solar_to_load_w == 500
    assert surplus.grid_to_load_w == 0
    assert surplus.solar_to_battery_w == 400
    assert surplus.requested_charge_power_w == 400


def test_self_consumption_uses_battery_above_reserve_and_grid_after_that() -> None:
    enough_reserve = _decide(
        mode="self_consumption",
        load_w=1000,
        solar_w=400,
        soc_percent=80,
    )

    assert enough_reserve.solar_to_load_w == 400
    assert enough_reserve.battery_to_load_w == 600
    assert enough_reserve.grid_to_load_w == 0
    assert enough_reserve.battery_provides_energy is True

    below_reserve = _decide(
        mode="self_consumption",
        load_w=1000,
        solar_w=400,
        soc_percent=25,
    )

    assert below_reserve.battery_to_load_w == 0
    assert below_reserve.grid_to_load_w == 600
    assert below_reserve.battery_provides_energy is False


def test_battery_priority_is_more_aggressive_but_respects_critical_protection() -> None:
    self_consumption = _decide(
        mode="self_consumption",
        load_w=1000,
        solar_w=400,
        soc_percent=25,
    )
    battery_priority = _decide(
        mode="battery_priority",
        load_w=1000,
        solar_w=400,
        soc_percent=25,
    )
    critical = _decide(
        mode="battery_priority",
        load_w=500,
        solar_w=0,
        soc_percent=10,
    )

    assert self_consumption.battery_to_load_w == 0
    assert self_consumption.grid_to_load_w == 600
    assert battery_priority.battery_to_load_w == 600
    assert battery_priority.grid_to_load_w == 0
    assert critical.battery_to_load_w == 0
    assert critical.grid_to_load_w == 500
    assert critical.battery_provides_energy is False
    assert EmsMode.BATTERY_PROTECTION.value in critical.flags


def test_backup_reserve_preserves_battery_and_requests_charge_to_backup_target() -> None:
    decision = _decide(
        mode="backup_reserve",
        load_w=500,
        solar_w=300,
        soc_percent=50,
    )

    assert decision.selected_mode == EmsMode.BACKUP_RESERVE
    assert decision.grid_to_load_w == 500
    assert decision.battery_to_load_w == 0
    assert decision.battery_provides_energy is False
    assert decision.solar_to_battery_w == 300
    assert decision.grid_to_battery_w > 0
    assert decision.requested_charge_power_w > 0


def test_force_charge_disables_discharge_and_requests_grid_charge_when_available() -> None:
    decision = _decide(
        mode="force_charge",
        load_w=500,
        solar_w=100,
        soc_percent=50,
    )

    assert decision.selected_mode == EmsMode.FORCE_CHARGE
    assert decision.grid_to_load_w == 500
    assert decision.battery_to_load_w == 0
    assert decision.battery_provides_energy is False
    assert decision.solar_to_battery_w == 100
    assert decision.grid_to_battery_w == 1100
    assert decision.requested_charge_power_w == 1200


def test_outage_normal_uses_solar_and_battery_when_safe() -> None:
    decision = _decide(
        mode="auto",
        grid_available=False,
        load_w=900,
        solar_w=300,
        soc_percent=80,
    )

    assert decision.selected_mode == EmsMode.OUTAGE_MODE
    assert decision.grid_to_load_w == 0
    assert decision.grid_to_battery_w == 0
    assert decision.solar_to_load_w == 300
    assert decision.battery_to_load_w == 600
    assert decision.effective_served_load_w == 900
    assert decision.battery_provides_energy is True
    assert decision.requested_battery_discharge_energy_wh_last_minute == pytest.approx(10)
    assert decision.protection_active is False


def test_outage_solar_only_serves_load_at_critical_soc_without_shutdown() -> None:
    decision = _decide(
        mode="auto",
        grid_available=False,
        load_w=500,
        solar_w=500,
        soc_percent=5,
    )

    assert decision.selected_mode == EmsMode.OUTAGE_MODE
    assert decision.effective_served_load_w == 500
    assert decision.inverter_output_enabled is True
    assert decision.solar_to_load_w == 500
    assert decision.battery_to_load_w == 0
    assert decision.battery_provides_energy is False
    assert decision.requested_battery_discharge_energy_wh_last_minute == 0
    assert decision.protection_active is False
    assert "shutdown" not in decision.reason


def test_outage_solar_only_serves_load_with_empty_battery_without_shutdown() -> None:
    decision = _decide(
        mode="auto",
        grid_available=False,
        load_w=500,
        solar_w=500,
        soc_percent=0,
        battery_status="empty",
        battery_energy_wh=0,
    )

    assert decision.selected_mode == EmsMode.OUTAGE_MODE
    assert decision.effective_served_load_w == 500
    assert decision.inverter_output_enabled is True
    assert decision.solar_to_load_w == 500
    assert decision.battery_to_load_w == 0
    assert decision.battery_provides_energy is False
    assert decision.requested_battery_discharge_energy_wh_last_minute == 0
    assert decision.protection_active is False


def test_outage_partial_solar_and_blocked_battery_shuts_down_fully() -> None:
    decision = _decide(
        mode="auto",
        grid_available=False,
        load_w=800,
        solar_w=500,
        soc_percent=5,
    )

    assert decision.selected_mode == EmsMode.INVERTER_PROTECTION_SHUTDOWN
    assert decision.effective_served_load_w == 0
    assert decision.inverter_output_enabled is False
    assert decision.solar_to_load_w == 0
    assert decision.battery_to_load_w == 0
    assert decision.battery_provides_energy is False
    assert decision.requested_battery_discharge_energy_wh_last_minute == 0
    assert decision.protection_active is True


def test_outage_load_above_inverter_limit_enters_shutdown_without_unmet_load_field() -> None:
    decision = _decide(
        mode="auto",
        grid_available=False,
        load_w=2500,
        solar_w=0,
        soc_percent=80,
    )

    assert decision.selected_mode == EmsMode.INVERTER_PROTECTION_SHUTDOWN
    assert decision.protection_active is True
    assert decision.inverter_output_enabled is False
    assert decision.effective_served_load_w == 0
    assert decision.battery_to_load_w == 0
    assert decision.battery_provides_energy is False
    assert decision.requested_battery_discharge_energy_wh_last_minute == 0
    assert "shutdown" in decision.reason
    assert not hasattr(decision, "unmet_load_w")
    assert "unmet_load_w" not in EmsDecision.__dataclass_fields__
    assert "unused_solar_w" not in EmsDecision.__dataclass_fields__


def test_outage_below_critical_soc_enters_protection_and_blocks_discharge() -> None:
    decision = _decide(
        mode="auto",
        grid_available=False,
        load_w=500,
        solar_w=0,
        soc_percent=5,
    )

    assert decision.selected_mode == EmsMode.INVERTER_PROTECTION_SHUTDOWN
    assert decision.protection_active is True
    assert decision.effective_served_load_w == 0
    assert decision.battery_to_load_w == 0
    assert decision.battery_provides_energy is False
    assert "battery protection" in decision.reason


def test_grid_available_load_above_inverter_limit_uses_grid_pass_through() -> None:
    decision = _decide(
        mode="grid_priority",
        grid_available=True,
        load_w=2500,
        solar_w=0,
        soc_percent=50,
    )

    assert decision.selected_mode == EmsMode.GRID_PRIORITY
    assert decision.effective_served_load_w == 2500
    assert decision.grid_to_load_w == 2500
    assert decision.battery_to_load_w == 0
    assert decision.inverter_output_enabled is True
    assert decision.protection_active is False


def test_auto_stable_grid_has_low_risk_and_selects_money_saving_mode() -> None:
    history = EmsHistorySummary(
        outage_minutes_last_6h=0,
        outage_minutes_last_24h=0,
        outage_count_last_24h=0,
        outage_count_last_72h=0,
        hours_since_last_outage=96,
        min_soc_last_24h=70,
        battery_recovered_to_full_after_last_outage=True,
    )
    decision = _decide(
        mode="auto",
        load_w=1000,
        solar_w=400,
        soc_percent=90,
        history_summary=history,
        timestamp=_local(12, 0),
    )

    assert decision.auto_risk_score <= 20
    assert decision.selected_mode in {
        EmsMode.SELF_CONSUMPTION,
        EmsMode.BATTERY_PRIORITY,
    }
    assert decision.battery_to_load_w > 0


def test_auto_recent_and_frequent_outages_select_backup_or_charge_behavior() -> None:
    history = EmsHistorySummary(
        outage_minutes_last_6h=30,
        outage_minutes_last_24h=300,
        outage_count_last_24h=3,
        outage_count_last_72h=5,
        hours_since_last_outage=2,
        min_soc_last_24h=25,
        battery_recovered_to_full_after_last_outage=False,
    )
    decision = _decide(
        mode="auto",
        load_w=500,
        solar_w=0,
        soc_percent=60,
        history_summary=history,
        timestamp=_local(12, 0),
    )

    assert decision.auto_risk_score >= 75
    assert decision.selected_mode in {EmsMode.BACKUP_RESERVE, EmsMode.FORCE_CHARGE}
    assert decision.battery_to_load_w == 0
    assert decision.requested_charge_power_w > 0


def test_auto_critical_soc_history_sets_high_risk_and_charge_behavior() -> None:
    history = EmsHistorySummary(
        min_soc_last_24h=5,
        battery_recovered_to_full_after_last_outage=False,
    )
    decision = _decide(
        mode="auto",
        load_w=500,
        solar_w=0,
        soc_percent=50,
        history_summary=history,
        timestamp=_local(12, 0),
    )

    assert decision.auto_risk_score >= 75
    assert decision.selected_mode in {EmsMode.BACKUP_RESERVE, EmsMode.FORCE_CHARGE}
    assert decision.battery_to_load_w == 0
    assert decision.requested_charge_power_w > 0


def test_cheap_tariff_window_enables_low_risk_grid_charging_at_night() -> None:
    config = EmsConfig(mode="auto")
    history = EmsHistorySummary(hours_since_last_outage=96, min_soc_last_24h=60)

    assert is_cheap_tariff_active(_local(23, 0), config) is True
    assert is_cheap_tariff_active(_local(6, 59), config) is True
    assert is_cheap_tariff_active(_local(7, 0), config) is False

    night = _decide(
        mode="auto",
        load_w=0,
        solar_w=0,
        soc_percent=50,
        history_summary=history,
        timestamp=_local(23, 30),
    )
    day = _decide(
        mode="auto",
        load_w=0,
        solar_w=0,
        soc_percent=50,
        history_summary=history,
        timestamp=_local(12, 0),
    )

    assert night.cheap_tariff_active is True
    assert night.grid_to_battery_w > 0
    assert night.requested_charge_power_w > 0
    assert day.cheap_tariff_active is False
    assert day.grid_to_battery_w == 0


def test_cheap_tariff_low_risk_auto_charges_above_normal_toward_backup_target() -> None:
    history = EmsHistorySummary(hours_since_last_outage=96, min_soc_last_24h=70)

    night = _decide(
        mode="auto",
        load_w=0,
        solar_w=0,
        soc_percent=90,
        history_summary=history,
        timestamp=_local(23, 30),
    )
    day = _decide(
        mode="auto",
        load_w=0,
        solar_w=0,
        soc_percent=90,
        history_summary=history,
        timestamp=_local(12, 0),
    )

    assert night.auto_risk_score == 0
    assert night.cheap_tariff_active is True
    assert night.selected_mode == EmsMode.GRID_PRIORITY
    assert night.grid_to_battery_w > 0
    assert night.requested_charge_power_w > 0
    assert day.cheap_tariff_active is False
    assert day.grid_to_battery_w == 0
    assert day.requested_charge_power_w == 0


def test_decision_schema_excludes_unmet_load_and_unused_solar_fields() -> None:
    decision = _decide(mode="auto", load_w=100, solar_w=100, soc_percent=80)

    assert not hasattr(decision, "unmet_load_w")
    assert not hasattr(decision, "unused_solar_w")
    assert "unmet_load_w" not in EmsDecision.__dataclass_fields__
    assert "unused_solar_w" not in EmsDecision.__dataclass_fields__


def test_ems_module_boundaries_and_input_immutability() -> None:
    source = inspect.getsource(ems_module)
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden_exact = {
        "sqlite3",
        "sqlmodel",
        "fastapi",
        "app.main",
        "backend.scripts.solar_data_scheduler",
    }
    forbidden_prefixes = (
        "app.storage",
        "app.controller",
        "app.simulation.battery",
        "app.simulation.grid",
        "app.simulation.load",
        "app.simulation.solar",
        "backend.scripts",
        "frontend",
    )

    assert imported_modules.isdisjoint(forbidden_exact)
    assert not any(
        module.startswith(forbidden_prefixes)
        for module in imported_modules
    )
    assert "outage_schedule" not in source
    assert "next_outage" not in source
    assert "unmet_load_w" not in source
    assert "unused_solar_w" not in source

    ems_input = _input(
        _config("self_consumption"),
        load_w=1000,
        solar_w=400,
        soc_percent=80,
    )
    before = asdict(ems_input)
    decision = EmsDecisionEngine().decide(ems_input)

    assert decision.battery_to_load_w == 600
    assert asdict(ems_input) == before


def _decide(
    *,
    mode: str,
    grid_available: bool = True,
    load_w: float,
    solar_w: float,
    soc_percent: float,
    history_summary: EmsHistorySummary | None = None,
    timestamp: datetime | None = None,
    battery_status: str = "idle",
    battery_energy_wh: float | None = None,
    max_charge_power_w: float = 1200.0,
) -> EmsDecision:
    config = _config(mode)
    return EmsDecisionEngine().decide(
        _input(
            config,
            grid_available=grid_available,
            load_w=load_w,
            solar_w=solar_w,
            soc_percent=soc_percent,
            history_summary=history_summary,
            timestamp=timestamp,
            battery_status=battery_status,
            battery_energy_wh=battery_energy_wh,
            max_charge_power_w=max_charge_power_w,
        )
    )


def _config(mode: str) -> EmsConfig:
    return EmsConfig(
        mode=mode,
        inverter_output_limit_w=2000,
        critical_soc_percent=10,
        reserve_soc_percent=30,
        normal_target_soc_percent=80,
        backup_target_soc_percent=100,
        cheap_tariff_start="23:00",
        cheap_tariff_end="07:00",
        cheap_tariff_price_factor=0.5,
        allow_grid_charging=True,
        recent_outage_recovery_minutes=60,
    )


def _input(
    config: EmsConfig,
    *,
    grid_available: bool = True,
    load_w: float,
    solar_w: float,
    soc_percent: float,
    history_summary: EmsHistorySummary | None = None,
    timestamp: datetime | None = None,
    battery_status: str = "idle",
    battery_energy_wh: float | None = None,
    max_charge_power_w: float = 1200.0,
) -> EmsInput:
    usable_capacity_wh = 2000.0
    resolved_battery_energy_wh = (
        usable_capacity_wh * soc_percent / 100.0
        if battery_energy_wh is None
        else battery_energy_wh
    )
    return EmsInput(
        timestamp=timestamp or _local(12, 0),
        grid_available=grid_available,
        solar_available_power_w=solar_w,
        load_power_w=load_w,
        battery_soc_percent=soc_percent,
        battery_soh_percent=100.0,
        battery_energy_wh=resolved_battery_energy_wh,
        battery_current_usable_capacity_wh=usable_capacity_wh,
        battery_voltage_v=24.0,
        battery_status=battery_status,
        battery_max_charge_power_w=max_charge_power_w,
        history_summary=history_summary,
        config=config,
    )


def _local(hour: int, minute: int) -> datetime:
    return datetime(2026, 1, 1, hour, minute, tzinfo=STATION_TIMEZONE)
