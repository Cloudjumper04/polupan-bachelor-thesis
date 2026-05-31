from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from app.config_loader import load_config
from app.simulation import battery as battery_module
from app.simulation.battery import (
    BatteryChemistry,
    BatteryConfig,
    BatterySimulator,
    BatteryState,
    BatteryStepInput,
    BatteryStatus,
    chemistry_profile,
)


CONFIG_PATH = "backend/config/station.default.yaml"


def test_config_validation_accepts_only_supported_chemistry_voltage_and_capacity() -> None:
    for chemistry in ("lead_acid", "lifepo4", "li_ion"):
        config = BatteryConfig(chemistry, 24, 100, "2026-01-01")
        assert config.chemistry == BatteryChemistry(chemistry)

    BatteryConfig("lifepo4", 12, 100, date(2026, 1, 1))

    with pytest.raises(ValueError, match="chemistry"):
        BatteryConfig("nickel", 24, 100, date(2026, 1, 1))
    with pytest.raises(ValueError, match="12 or 24"):
        BatteryConfig("lifepo4", 48, 100, date(2026, 1, 1))
    with pytest.raises(ValueError, match="positive"):
        BatteryConfig("lifepo4", 24, 0, date(2026, 1, 1))
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        BatteryConfig("lifepo4", 24, 100, "2026-99-99")


def test_station_default_yaml_contains_valid_minimal_battery_config() -> None:
    config = load_config(CONFIG_PATH)
    battery_config = config.station.battery

    assert battery_config is not None
    assert battery_config.chemistry in {"lead_acid", "lifepo4", "li_ion"}
    assert battery_config.nominal_voltage_v in {12, 24}
    assert battery_config.capacity_ah > 0
    date.fromisoformat(battery_config.installation_date)

    simulator = BatterySimulator.from_station_config(config)

    assert simulator.state.nominal_energy_wh == pytest.approx(
        battery_config.nominal_voltage_v * battery_config.capacity_ah,
    )
    assert simulator.state.soc_percent == pytest.approx(50.0)
    assert simulator.state.usable_capacity_wh > 0.0


def test_nominal_energy_uses_allowed_pack_voltage_and_capacity() -> None:
    assert BatterySimulator(_config(voltage=24, capacity=100)).state.nominal_energy_wh == 2400.0
    assert BatterySimulator(_config(voltage=12, capacity=100)).state.nominal_energy_wh == 1200.0


def test_initial_state_starts_new_at_half_usable_capacity() -> None:
    simulator = BatterySimulator(_config(chemistry="lifepo4", voltage=24, capacity=100))

    state = simulator.state
    assert state.soh_percent == 100.0
    assert state.usable_capacity_wh == pytest.approx(1920.0)
    assert state.energy_wh == pytest.approx(state.usable_capacity_wh * 0.50)
    assert state.soc_percent == pytest.approx(50.0)
    assert state.equivalent_cycles_today == 0.0
    assert state.total_equivalent_cycles == 0.0
    assert state.status == BatteryStatus.IDLE


def test_usable_capacity_depends_on_chemistry_profile() -> None:
    lead_acid = BatterySimulator(_config(chemistry="lead_acid")).state
    lifepo4 = BatterySimulator(_config(chemistry="lifepo4")).state
    li_ion = BatterySimulator(_config(chemistry="li_ion")).state

    assert lead_acid.usable_capacity_wh == pytest.approx(2400.0 * 0.50 * 0.80)
    assert lifepo4.usable_capacity_wh == pytest.approx(2400.0 * 1.00 * 0.80)
    assert li_ion.usable_capacity_wh == pytest.approx(2400.0 * 0.90 * 0.80)
    assert lead_acid.usable_capacity_wh < li_ion.usable_capacity_wh < lifepo4.usable_capacity_wh


def test_discharge_only_happens_when_ems_requests_battery_supply() -> None:
    provides = BatterySimulator(_config())
    bypasses = BatterySimulator(_config())

    discharge_result = provides.step(
        BatteryStepInput(
            timestamp=_ts(10, 0),
            consumed_energy_wh_last_minute=60.0,
            battery_provides_energy=True,
        )
    )
    no_discharge_result = bypasses.step(
        BatteryStepInput(
            timestamp=_ts(10, 0),
            consumed_energy_wh_last_minute=60.0,
            battery_provides_energy=False,
        )
    )

    assert discharge_result.applied_discharge_energy_wh == pytest.approx(60.0)
    assert discharge_result.removed_discharge_energy_wh == pytest.approx(60.0 / 0.95)
    assert discharge_result.state.energy_wh == pytest.approx(960.0 - 60.0 / 0.95)
    assert discharge_result.state.status == BatteryStatus.DISCHARGING
    assert no_discharge_result.applied_discharge_energy_wh == 0.0
    assert no_discharge_result.state.energy_wh == pytest.approx(960.0)


def test_charging_increases_energy_and_clamps_by_charge_rate_and_remaining_capacity() -> None:
    normal = BatterySimulator(_config())
    normal_result = normal.step(
        BatteryStepInput(timestamp=_ts(10, 0), requested_charge_power_w=600.0)
    )

    assert normal_result.applied_charge_power_w == pytest.approx(600.0)
    assert normal_result.stored_charge_energy_wh == pytest.approx(600.0 * 0.95 / 60.0)
    assert normal_result.state.energy_wh == pytest.approx(960.0 + 9.5)

    rate_limited = BatterySimulator(_config())
    rate_result = rate_limited.step(
        BatteryStepInput(timestamp=_ts(10, 0), requested_charge_power_w=2400.0)
    )

    assert rate_limited.max_charge_power_w == pytest.approx(1200.0)
    assert rate_result.applied_charge_power_w == pytest.approx(1200.0)
    assert rate_result.status == BatteryStatus.CHARGE_LIMITED

    near_full = BatterySimulator(
        _config(),
        _state_with_energy(BatterySimulator(_config()), 1919.0),
    )
    full_result = near_full.step(
        BatteryStepInput(timestamp=_ts(10, 0), requested_charge_power_w=1200.0)
    )

    assert full_result.stored_charge_energy_wh == pytest.approx(1.0)
    assert full_result.applied_charge_power_w == pytest.approx(60.0 / 0.95)
    assert full_result.state.energy_wh == pytest.approx(full_result.state.current_usable_capacity_wh)
    assert full_result.state.status == BatteryStatus.FULL


def test_empty_and_full_limits_clamp_energy() -> None:
    emptying = BatterySimulator(
        _config(),
        _state_with_energy(BatterySimulator(_config()), 5.0),
    )
    empty_result = emptying.step(
        BatteryStepInput(
            timestamp=_ts(10, 0),
            consumed_energy_wh_last_minute=100.0,
            battery_provides_energy=True,
        )
    )

    assert empty_result.applied_discharge_energy_wh == pytest.approx(5.0 * 0.95)
    assert empty_result.state.energy_wh == 0.0
    assert empty_result.state.status == BatteryStatus.EMPTY

    full = BatterySimulator(
        _config(),
        _state_with_energy(BatterySimulator(_config()), 1920.0),
    )
    full_result = full.step(
        BatteryStepInput(timestamp=_ts(10, 0), requested_charge_power_w=600.0)
    )

    assert full_result.applied_charge_power_w == 0.0
    assert full_result.state.energy_wh == pytest.approx(1920.0)
    assert full_result.state.status == BatteryStatus.FULL


def test_voltage_changes_with_soc_but_soc_is_energy_based() -> None:
    base = BatterySimulator(_config())
    low_soc = BatterySimulator(_config(), _state_with_energy(base, 192.0))
    high_soc = BatterySimulator(_config(), _state_with_energy(base, 1728.0))

    assert low_soc.state.soc_percent == pytest.approx(10.0)
    assert high_soc.state.soc_percent == pytest.approx(90.0)
    assert low_soc.state.voltage_v < high_soc.state.voltage_v

    impossible_voltage_state = replace(base.state, energy_wh=960.0, voltage_v=999.0)
    normalized = BatterySimulator(_config(), impossible_voltage_state)

    assert normalized.state.soc_percent == pytest.approx(50.0)
    assert normalized.state.voltage_v != 999.0


def test_cycle_degradation_counts_discharged_wh_and_reduces_capacity() -> None:
    simulator = BatterySimulator(
        _config(),
        _state_with_energy(BatterySimulator(_config()), 1920.0),
    )

    result = simulator.step(
        BatteryStepInput(
            timestamp=_ts(10, 0),
            consumed_energy_wh_last_minute=960.0,
            battery_provides_energy=True,
        )
    )
    expected_removed_wh = 960.0 / 0.95
    expected_cycles = expected_removed_wh / 1920.0

    assert result.removed_discharge_energy_wh == pytest.approx(expected_removed_wh)
    assert result.state.equivalent_cycles_today == pytest.approx(expected_cycles)

    finalized = simulator.finalize_day(date(2026, 1, 1))

    assert finalized.equivalent_cycles == pytest.approx(expected_cycles)
    assert finalized.soh_loss_percent == pytest.approx(expected_cycles * (20.0 / 3000.0))
    assert finalized.state.soh_percent < 100.0
    assert finalized.state.current_usable_capacity_wh < finalized.state.usable_capacity_wh
    assert finalized.state.total_equivalent_cycles == pytest.approx(expected_cycles)
    assert finalized.state.equivalent_cycles_today == 0.0


def test_manual_finalize_then_next_day_step_does_not_double_count_degradation() -> None:
    simulator = BatterySimulator(
        _config(),
        _state_with_energy(BatterySimulator(_config()), 1920.0),
    )
    simulator.step(
        BatteryStepInput(
            timestamp=_ts(10, 0),
            consumed_energy_wh_last_minute=960.0,
            battery_provides_energy=True,
        )
    )

    finalized = simulator.finalize_day(date(2026, 1, 1))
    soh_after_finalize = simulator.state.soh_percent
    cycles_after_finalize = simulator.state.total_equivalent_cycles
    next_day = simulator.step(BatteryStepInput(timestamp=_ts_on(2026, 1, 2, 0, 0)))

    assert finalized.applied is True
    assert next_day.state.soh_percent == pytest.approx(soh_after_finalize)
    assert next_day.state.total_equivalent_cycles == pytest.approx(cycles_after_finalize)
    assert next_day.state.equivalent_cycles_today == 0.0
    assert simulator.active_day == date(2026, 1, 2)


def test_auto_finalize_then_manual_previous_day_finalize_is_noop() -> None:
    simulator = BatterySimulator(
        _config(),
        _state_with_energy(BatterySimulator(_config()), 1920.0),
    )
    simulator.step(
        BatteryStepInput(
            timestamp=_ts(10, 0),
            consumed_energy_wh_last_minute=960.0,
            battery_provides_energy=True,
        )
    )
    simulator.step(BatteryStepInput(timestamp=_ts_on(2026, 1, 2, 0, 0)))
    soh_after_auto_finalize = simulator.state.soh_percent
    cycles_after_auto_finalize = simulator.state.total_equivalent_cycles

    simulator.step(
        BatteryStepInput(
            timestamp=_ts_on(2026, 1, 2, 0, 1),
            consumed_energy_wh_last_minute=10.0,
            battery_provides_energy=True,
        )
    )
    cycles_today_before_stale_finalize = simulator.state.equivalent_cycles_today
    stale_finalize = simulator.finalize_day(date(2026, 1, 1))

    assert stale_finalize.applied is False
    assert stale_finalize.equivalent_cycles == 0.0
    assert stale_finalize.soh_loss_percent == 0.0
    assert simulator.active_day == date(2026, 1, 2)
    assert simulator.state.soh_percent == pytest.approx(soh_after_auto_finalize)
    assert simulator.state.total_equivalent_cycles == pytest.approx(cycles_after_auto_finalize)
    assert simulator.state.equivalent_cycles_today == pytest.approx(
        cycles_today_before_stale_finalize,
    )


def test_finalize_day_is_idempotent_for_same_day() -> None:
    simulator = BatterySimulator(
        _config(),
        _state_with_energy(BatterySimulator(_config()), 1920.0),
    )
    simulator.step(
        BatteryStepInput(
            timestamp=_ts(10, 0),
            consumed_energy_wh_last_minute=960.0,
            battery_provides_energy=True,
        )
    )

    first = simulator.finalize_day(date(2026, 1, 1))
    state_after_first = simulator.state
    second = simulator.finalize_day(date(2026, 1, 1))

    assert first.applied is True
    assert second.applied is False
    assert second.equivalent_cycles == 0.0
    assert second.soh_loss_percent == 0.0
    assert simulator.state == state_after_first


def test_multi_day_timestamp_jump_finalizes_current_day_and_skips_empty_days() -> None:
    simulator = BatterySimulator(
        _config(),
        _state_with_energy(BatterySimulator(_config()), 1920.0),
    )
    simulator.step(
        BatteryStepInput(
            timestamp=_ts(10, 0),
            consumed_energy_wh_last_minute=960.0,
            battery_provides_energy=True,
        )
    )
    expected_cycles = (960.0 / 0.95) / 1920.0
    expected_soh = 100.0 - expected_cycles * (20.0 / 3000.0)

    result = simulator.step(BatteryStepInput(timestamp=_ts_on(2026, 1, 5, 0, 0)))

    assert simulator.active_day == date(2026, 1, 5)
    assert result.state.soh_percent == pytest.approx(expected_soh)
    assert result.state.total_equivalent_cycles == pytest.approx(expected_cycles)
    assert result.state.equivalent_cycles_today == 0.0


def test_active_day_never_moves_backward_for_stale_or_future_finalize() -> None:
    simulator = BatterySimulator(_config())
    simulator.step(BatteryStepInput(timestamp=_ts_on(2026, 1, 3, 0, 0)))
    assert simulator.active_day == date(2026, 1, 3)

    stale_finalize = simulator.finalize_day(date(2026, 1, 1))

    assert stale_finalize.applied is False
    assert simulator.active_day == date(2026, 1, 3)
    with pytest.raises(ValueError, match="future battery day"):
        simulator.finalize_day(date(2026, 1, 4))
    assert simulator.active_day == date(2026, 1, 3)


def test_soh_degradation_clamps_energy_to_reduced_usable_capacity() -> None:
    base = BatterySimulator(_config(chemistry="lead_acid"))
    profile = chemistry_profile("lead_acid")
    initial_state = replace(
        base.state,
        energy_wh=960.0,
        equivalent_cycles_today=profile.reference_cycle_life_to_80_soh * 5.0,
    )
    simulator = BatterySimulator(_config(chemistry="lead_acid"), initial_state)

    finalized = simulator.finalize_day(date(2026, 1, 1))

    assert finalized.state.soh_percent == pytest.approx(0.0)
    assert finalized.state.current_usable_capacity_wh == pytest.approx(0.0)
    assert finalized.state.energy_wh == pytest.approx(0.0)
    assert finalized.state.status == BatteryStatus.EMPTY


def test_one_minute_energy_input_from_600_w_load_is_10_wh_before_efficiency() -> None:
    simulator = BatterySimulator(_config())
    result = simulator.step(
        BatteryStepInput(
            timestamp=_ts(10, 0),
            consumed_energy_wh_last_minute=600.0 / 60.0,
            battery_provides_energy=True,
        )
    )

    assert result.requested_discharge_energy_wh == pytest.approx(10.0)
    assert result.applied_discharge_energy_wh == pytest.approx(10.0)
    assert result.removed_discharge_energy_wh == pytest.approx(10.0 / 0.95)
    assert result.state.energy_wh == pytest.approx(960.0 - 10.0 / 0.95)


def test_battery_module_does_not_import_load_solar_grid_db_api_or_scheduler() -> None:
    source = inspect.getsource(battery_module)
    tree = ast.parse(source)
    imported_modules = set()
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
        "app.simulation.load",
        "app.simulation.solar",
        "app.simulation.grid",
        "app.storage",
        "app.controller",
        "backend.scripts",
    )

    assert imported_modules.isdisjoint(forbidden_exact)
    assert not any(
        module.startswith(forbidden_prefixes)
        for module in imported_modules
    )
    assert "LoadSimulator" not in source
    assert "IdealSolarGenerator" not in source
    assert "SmartEnergyRepository" not in source


def _config(
    chemistry: str = "lifepo4",
    voltage: int = 24,
    capacity: float = 100.0,
) -> BatteryConfig:
    return BatteryConfig(
        chemistry=chemistry,
        nominal_voltage_v=voltage,
        capacity_ah=capacity,
        installation_date=date(2026, 1, 1),
    )


def _state_with_energy(simulator: BatterySimulator, energy_wh: float) -> BatteryState:
    return replace(simulator.state, energy_wh=energy_wh)


def _ts(hour: int, minute: int) -> datetime:
    return _ts_on(2026, 1, 1, hour, minute)


def _ts_on(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
