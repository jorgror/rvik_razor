"""Tests for Rvik Razor regulation logic."""

from __future__ import annotations

import asyncio
import time

import pytest

from custom_components.rvik_razor.const import Load, LoadType
from custom_components.rvik_razor.coordinator import (
    RvikRazorCoordinator,
    _calculate_nominal_power_per_ampere,
    calculate_available_down_capacity,
    calculate_regulation_decision,
)


def create_test_load(
    name: str = "Test Load",
    priority: int = 50,
    load_type: LoadType = LoadType.SWITCH,
    enabled: bool = True,
    last_action_time: float = 0.0,
    assumed_power_kw: float | None = 2.0,
    **kwargs,
) -> Load:
    """Create a test Load object with sensible defaults."""
    return Load(
        name=name,
        priority=priority,
        load_type=load_type,
        enabled=enabled,
        last_action_time=last_action_time,
        assumed_power_kw=assumed_power_kw,
        **kwargs,
    )


def decide(
    loads: list[Load],
    current_hour_kwh: float,
    house_power_kw: float | None,
    set_point_kwh: float = 5.0,
    remaining_seconds: float = 1800.0,
    **kwargs,
) -> dict:
    """Call the decision function with common defaults."""
    return calculate_regulation_decision(
        loads=loads,
        current_hour_kwh=current_hour_kwh,
        house_power_kw=house_power_kw,
        hourly_set_point_kwh=set_point_kwh,
        remaining_seconds=remaining_seconds,
        current_time=kwargs.pop("current_time", time.time()),
        **kwargs,
    )


class TestAvailableDownCapacity:
    """Test calculation of available downward control capacity."""

    def test_switch_on_provides_capacity(self):
        load = create_test_load(current_switch_state="on", assumed_power_kw=2.0)

        assert calculate_available_down_capacity([load]) == 2.0

    def test_inverted_switch_off_provides_capacity(self):
        load = create_test_load(
            current_switch_state="off",
            switch_inverted=True,
            assumed_power_kw=1.5,
        )

        assert calculate_available_down_capacity([load]) == 1.5

    def test_measured_power_preferred_over_assumed(self):
        load = create_test_load(
            current_switch_state="on",
            assumed_power_kw=2.0,
            current_power_kw=1.8,
        )

        assert calculate_available_down_capacity([load]) == 1.8

    def test_ev_charger_uses_nominal_ampere_power(self):
        load = create_test_load(
            load_type=LoadType.EV_AMPERE,
            current_ampere=16.0,
            phases=3,
            voltage=400,
        )

        assert calculate_available_down_capacity([load]) == pytest.approx(
            11.08, rel=0.02
        )


class TestPowerPerAmpere:
    """Test nominal power-per-ampere fallback calculations."""

    def test_nominal_power_per_ampere_single_phase(self):
        load = create_test_load(load_type=LoadType.EV_AMPERE, phases=1, voltage=230)

        assert _calculate_nominal_power_per_ampere(load) == pytest.approx(0.23)

    def test_nominal_power_per_ampere_three_phase(self):
        load = create_test_load(load_type=LoadType.EV_AMPERE, phases=3, voltage=400)

        assert _calculate_nominal_power_per_ampere(load) == pytest.approx(
            0.6928, rel=1e-3
        )


class TestHorizonRegulationDecisions:
    """Test projection-based horizon set-point control."""

    def test_no_action_within_accuracy_deadband(self):
        load = create_test_load(current_switch_state="on")

        decision = decide([load], current_hour_kwh=4.99, house_power_kw=0.0)

        assert decision["action"] == "none"
        assert "deadband" in decision["reason"]

    def test_no_control_without_house_power(self):
        load = create_test_load(current_switch_state="on")

        decision = decide([load], current_hour_kwh=4.0, house_power_kw=None)

        assert decision["action"] == "none"
        assert "house power unavailable" in decision["reason"].lower()

    def test_reduce_first_priority_load_when_it_improves_accuracy(self):
        loads = [
            create_test_load(
                name="Priority 1",
                priority=1,
                assumed_power_kw=1.0,
                current_switch_state="on",
            ),
            create_test_load(
                name="Priority 2",
                priority=2,
                assumed_power_kw=0.5,
                current_switch_state="on",
            ),
        ]

        decision = decide(loads, current_hour_kwh=4.0, house_power_kw=3.0)

        assert decision["action"] == "reduce"
        assert decision["loads_to_reduce"][0]["load"].name == "Priority 1"
        assert decision["loads_to_reduce"][0]["new_value"] == "off"
        assert decision["candidate_projected_end_kwh"] == pytest.approx(5.0)

    def test_bad_fit_at_first_eligible_reduce_priority_waits(self):
        loads = [
            create_test_load(
                name="Priority 1",
                priority=1,
                assumed_power_kw=2.0,
                current_switch_state="on",
            ),
            create_test_load(
                name="Priority 2",
                priority=2,
                assumed_power_kw=0.2,
                current_switch_state="on",
            ),
        ]

        decision = decide(loads, current_hour_kwh=4.0, house_power_kw=2.2)

        assert decision["action"] == "none"
        assert "Priority 1" in decision["reason"]
        assert "waiting" in decision["reason"]

    def test_cooldown_is_ineligible_and_next_priority_can_reduce(self):
        now = time.time()
        loads = [
            create_test_load(
                name="Cooldown",
                priority=1,
                assumed_power_kw=1.0,
                current_switch_state="on",
                last_action_time=now - 30,
                timeout=120,
            ),
            create_test_load(
                name="Next",
                priority=2,
                assumed_power_kw=1.0,
                current_switch_state="on",
            ),
        ]

        decision = decide(
            loads, current_hour_kwh=4.0, house_power_kw=3.0, current_time=now
        )

        assert decision["action"] == "reduce"
        assert decision["loads_to_reduce"][0]["load"].name == "Next"

    def test_unknown_switch_power_is_ineligible(self):
        load = create_test_load(
            assumed_power_kw=None,
            current_power_kw=None,
            current_switch_state="on",
        )

        decision = decide([load], current_hour_kwh=4.0, house_power_kw=3.0)

        assert decision["action"] == "none"
        assert "No eligible loads" in decision["reason"]

    def test_restore_highest_priority_load_when_it_improves_accuracy(self):
        loads = [
            create_test_load(
                name="Low",
                priority=1,
                assumed_power_kw=1.0,
                current_switch_state="off",
            ),
            create_test_load(
                name="High",
                priority=10,
                assumed_power_kw=1.0,
                current_switch_state="off",
            ),
        ]

        decision = decide(loads, current_hour_kwh=4.0, house_power_kw=1.0)

        assert decision["action"] == "restore"
        assert decision["loads_to_restore"][0]["load"].name == "High"
        assert decision["loads_to_restore"][0]["new_value"] == "on"
        assert decision["candidate_projected_end_kwh"] == pytest.approx(5.0)

    def test_bad_fit_at_first_eligible_restore_priority_waits(self):
        loads = [
            create_test_load(
                name="High",
                priority=10,
                assumed_power_kw=2.0,
                current_switch_state="off",
            ),
            create_test_load(
                name="Low",
                priority=1,
                assumed_power_kw=1.0,
                current_switch_state="off",
            ),
        ]

        decision = decide(loads, current_hour_kwh=4.0, house_power_kw=1.0)

        assert decision["action"] == "none"
        assert "High" in decision["reason"]
        assert "waiting" in decision["reason"]

    def test_inverted_switch_reduce_turns_on(self):
        load = create_test_load(
            switch_inverted=True,
            current_switch_state="off",
            assumed_power_kw=1.0,
        )

        decision = decide([load], current_hour_kwh=4.0, house_power_kw=3.0)

        assert decision["action"] == "reduce"
        assert decision["loads_to_reduce"][0]["new_value"] == "on"

    def test_inverted_switch_restore_turns_off(self):
        load = create_test_load(
            switch_inverted=True,
            current_switch_state="on",
            assumed_power_kw=1.0,
        )

        decision = decide([load], current_hour_kwh=4.0, house_power_kw=1.0)

        assert decision["action"] == "restore"
        assert decision["loads_to_restore"][0]["new_value"] == "off"

    def test_ev_reduce_chooses_best_lower_ampere(self):
        load = create_test_load(
            load_type=LoadType.EV_AMPERE,
            current_ampere=4,
            phases=1,
            voltage=1000,
        )

        decision = decide([load], current_hour_kwh=4.0, house_power_kw=4.0)

        assert decision["action"] == "reduce"
        assert decision["loads_to_reduce"][0]["new_value"] == 2
        assert decision["candidate_projected_end_kwh"] == pytest.approx(5.0)

    def test_ev_restore_chooses_best_higher_ampere(self):
        load = create_test_load(
            load_type=LoadType.EV_AMPERE,
            current_ampere=0,
            phases=1,
            voltage=1000,
        )

        decision = decide([load], current_hour_kwh=4.0, house_power_kw=1.0)

        assert decision["action"] == "restore"
        assert decision["loads_to_restore"][0]["new_value"] == 1
        assert decision["candidate_projected_end_kwh"] == pytest.approx(5.0)

    def test_ev_restore_uses_runtime_entity_max_ampere(self):
        load = create_test_load(
            load_type=LoadType.EV_AMPERE,
            current_ampere=16,
            current_max_ampere=32,
            phases=1,
            voltage=1000,
        )

        decision = decide(
            [load],
            current_hour_kwh=4.0,
            house_power_kw=8.0,
            set_point_kwh=6.0,
            remaining_seconds=450.0,
        )

        assert decision["action"] == "restore"
        assert decision["loads_to_restore"][0]["new_value"] == 32


class TestEnabledEntityFeature:
    """Test the enabled_entity_id feature for dynamic load control."""

    def test_load_to_dict_includes_enabled_entity(self):
        load = create_test_load(enabled_entity_id="binary_sensor.car_charging")

        load_dict = load.to_dict()

        assert load_dict["enabled_entity"] == "binary_sensor.car_charging"

    def test_load_from_dict_with_enabled_entity(self):
        data = {
            "name": "EV Charger",
            "type": "switch",
            "priority": 10,
            "enabled_entity": "binary_sensor.car_charging",
        }

        load = Load.from_dict(data)

        assert load.enabled_entity_id == "binary_sensor.car_charging"

    def test_disabled_loads_are_ignored(self):
        loads = [
            create_test_load(
                name="Disabled",
                priority=1,
                enabled=False,
                current_switch_state="on",
            ),
            create_test_load(
                name="Enabled",
                priority=2,
                enabled=True,
                current_switch_state="on",
                assumed_power_kw=1.0,
            ),
        ]

        decision = decide(loads, current_hour_kwh=4.0, house_power_kw=3.0)

        assert decision["action"] == "reduce"
        assert decision["loads_to_reduce"][0]["load"].name == "Enabled"


class TestReductionExecution:
    """Test reduction execution behavior."""

    def test_execute_reductions_stops_after_first_success(self):
        coordinator = RvikRazorCoordinator.__new__(RvikRazorCoordinator)
        coordinator.last_action = ""
        coordinator.last_action_reason = ""

        reduced_loads = []

        async def fake_reduce(plan: dict) -> float:
            reduced_loads.append(plan["load"].name)
            return 1.0

        coordinator._async_reduce_single_load = fake_reduce

        load1 = create_test_load(name="Load 1")
        load2 = create_test_load(name="Load 2")
        plans = [{"load": load1}, {"load": load2}]

        current_time = 12345.0
        asyncio.run(coordinator._async_execute_reductions(plans, current_time))

        assert reduced_loads == ["Load 1"]
        assert load1.last_action_time == current_time
        assert load2.last_action_time == 0.0

    def test_execute_reductions_tries_next_plan_after_failed_attempt(self):
        coordinator = RvikRazorCoordinator.__new__(RvikRazorCoordinator)
        coordinator.last_action = ""
        coordinator.last_action_reason = ""

        call_order = []

        async def fake_reduce(plan: dict) -> float:
            call_order.append(plan["load"].name)
            return 0.0 if plan["load"].name == "Load 1" else 1.5

        coordinator._async_reduce_single_load = fake_reduce

        load1 = create_test_load(name="Load 1")
        load2 = create_test_load(name="Load 2")
        plans = [{"load": load1}, {"load": load2}]

        current_time = 12345.0
        asyncio.run(coordinator._async_execute_reductions(plans, current_time))

        assert call_order == ["Load 1", "Load 2"]
        assert load1.last_action_time == 0.0
        assert load2.last_action_time == current_time


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
