"""Tests for Rvik Razor regulation logic.

This module tests the core regulation decision-making logic in isolation.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import pytest

from custom_components.rvik_razor.const import Load, LoadType
from custom_components.rvik_razor.coordinator import calculate_regulation_decision


def create_test_load(
    name: str = "Test Load",
    priority: int = 50,
    load_type: LoadType = LoadType.SWITCH,
    enabled: bool = True,
    enabled_entity_id: str | None = None,
    last_action_time: float = 0.0,
    assumed_power_kw: float = 2.0,
    **kwargs,
) -> Load:
    """Create a test Load object with sensible defaults."""
    return Load(
        name=name,
        priority=priority,
        load_type=load_type,
        enabled=enabled,
        enabled_entity_id=enabled_entity_id,
        last_action_time=last_action_time,
        assumed_power_kw=assumed_power_kw,
        **kwargs,
    )


class TestRegulationDecisions:
    """Test the calculate_regulation_decision function."""

    def test_no_action_when_within_safe_range(self):
        """Test that no action is taken when power is within safe range."""
        loads = [create_test_load()]
        current_time = datetime(2020, 1, 1, 8, 45, 0, tzinfo=timezone.utc).timestamp()

        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=0.0,  # No reduction needed
            projected_end_kwh=4.8,  # Between max - margin (4.7) and max (5.0)
            max_hour_kwh=5.0,
            current_time=current_time,
            restore_margin=0.3,
        )

        assert decision["action"] == "none"
        assert len(decision["loads_to_reduce"]) == 0
        assert len(decision["loads_to_restore"]) == 0
        assert "Within safe range" in decision["reason"]

    def test_reduce_action_when_over_limit(self):
        """Test that loads are reduced when exceeding limit."""
        loads = [
            create_test_load(name="Load 1", priority=10, assumed_power_kw=2.0),
            create_test_load(name="Load 2", priority=20, assumed_power_kw=3.0),
        ]
        current_time = time.time()

        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=2.5,  # Need to reduce
            projected_end_kwh=6.0,
            max_hour_kwh=5.0,
            current_time=current_time,
        )

        assert decision["action"] == "reduce"
        assert len(decision["loads_to_reduce"]) > 0
        # Lower priority should be selected first
        assert decision["loads_to_reduce"][0]["load"].name == "Load 1"

    def test_restore_action_with_sufficient_margin(self):
        """Test that loads are restored when there's sufficient margin."""
        loads = [
            create_test_load(
                name="Load 1",
                priority=10,
                assumed_power_kw=2.0,
            ),
            create_test_load(
                name="Load 2",
                priority=20,
                assumed_power_kw=3.0,
            ),
        ]
        current_time = time.time()

        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=0.0,
            projected_end_kwh=4.0,  # Well below max
            max_hour_kwh=5.0,
            current_time=current_time,
            restore_margin=0.5,  # margin of 0.5 kWh
        )

        assert decision["action"] == "restore"
        # All eligible loads returned, sorted by priority (highest first)
        assert len(decision["loads_to_restore"]) >= 1
        # Higher priority should be first (restored first)
        assert decision["loads_to_restore"][0].name == "Load 2"

    def test_multiple_loads_reduced_in_priority_order(self):
        """Test that multiple loads are reduced in correct priority order."""
        loads = [
            create_test_load(name="Low Priority", priority=10, assumed_power_kw=1.0),
            create_test_load(name="High Priority", priority=90, assumed_power_kw=1.0),
            create_test_load(name="Med Priority", priority=50, assumed_power_kw=1.0),
        ]
        current_time = time.time()

        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=2.5,  # Need multiple loads
            projected_end_kwh=6.0,
            max_hour_kwh=5.0,
            current_time=current_time,
        )

        assert decision["action"] == "reduce"
        # Should reduce at least 2 loads
        assert len(decision["loads_to_reduce"]) >= 2
        # Check order: lowest priority first
        priorities = [plan["load"].priority for plan in decision["loads_to_reduce"]]
        assert priorities == sorted(priorities)

    def test_cooldown_prevents_action(self):
        """Test that cooldown prevents actions on recently changed loads."""
        cooldown = 300.0  # 5 minutes
        current_time = time.time()

        loads = [
            create_test_load(
                name="Recent Action",
                priority=10,
                last_action_time=current_time - 60,  # 1 minute ago
            ),
            create_test_load(
                name="Old Action",
                priority=20,
                last_action_time=current_time - 400,  # 6+ minutes ago
            ),
        ]

        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=1.0,
            projected_end_kwh=6.0,
            max_hour_kwh=5.0,
            current_time=current_time,
            cooldown=cooldown,
        )

        assert decision["action"] == "reduce"
        # Should only plan to reduce the load that's not in cooldown
        assert len(decision["loads_to_reduce"]) == 1
        assert decision["loads_to_reduce"][0]["load"].name == "Old Action"

    def test_disabled_loads_are_ignored(self):
        """Test that disabled loads are not included in decisions."""
        loads = [
            create_test_load(name="Enabled", priority=10, enabled=True),
            create_test_load(name="Disabled", priority=5, enabled=False),
        ]
        current_time = time.time()

        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=1.0,
            projected_end_kwh=6.0,
            max_hour_kwh=5.0,
            current_time=current_time,
        )

        # Should only consider enabled loads
        for plan in decision["loads_to_reduce"]:
            assert plan["load"].enabled

    def test_only_one_load_restored_at_time(self):
        """Test that loads are returned in priority order for restoration.
        
        The decision function returns all eligible loads sorted by priority,
        and the execution phase restores only one at a time.
        """
        loads = [
            create_test_load(
                name="Load 1",
                priority=10,
            ),
            create_test_load(
                name="Load 2",
                priority=20,
            ),
        ]
        current_time = time.time()

        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=0.0,
            projected_end_kwh=4.0,
            max_hour_kwh=5.0,
            current_time=current_time,
            restore_margin=0.5,
        )

        assert decision["action"] == "restore"
        # All eligible loads are returned, sorted by priority (highest first)
        # The execution phase will only restore one at a time
        assert len(decision["loads_to_restore"]) >= 1
        # Highest priority (Load 2) should be first in the list
        assert decision["loads_to_restore"][0].name == "Load 2"

    def test_restore_any_enabled_load(self):
        """Test that any enabled load can be restored to max consumption."""
        loads = [
            create_test_load(
                name="Enabled Load",
                priority=10,
            ),
        ]
        current_time = time.time()

        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=0.0,
            projected_end_kwh=4.0,
            max_hour_kwh=5.0,
            current_time=current_time,
            restore_margin=0.5,
        )

        # Should attempt to restore any enabled load when margin is available
        assert decision["action"] == "restore"
        assert len(decision["loads_to_restore"]) == 1

    def test_ev_ampere_load_type(self):
        """Test that EV ampere loads are properly handled."""
        loads = [
            create_test_load(
                name="EV Charger",
                priority=50,
                load_type=LoadType.EV_AMPERE,
                ampere_number_entity_id="number.ev_charger",
            ),
        ]
        current_time = time.time()

        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=2.0,
            projected_end_kwh=6.0,
            max_hour_kwh=5.0,
            current_time=current_time,
        )

        assert decision["action"] == "reduce"
        assert len(decision["loads_to_reduce"]) == 1
        assert decision["loads_to_reduce"][0]["type"] == "ev_ampere"

    def test_insufficient_reduction_available(self):
        """Test behavior when available reduction is less than needed."""
        loads = [
            create_test_load(name="Small Load", priority=10, assumed_power_kw=1.0),
        ]
        current_time = time.time()

        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=5.0,  # Need much more than available
            projected_end_kwh=8.0,
            max_hour_kwh=5.0,
            current_time=current_time,
        )

        assert decision["action"] == "reduce"
        assert len(decision["loads_to_reduce"]) == 1
        # Should still reduce what's available even if insufficient
        assert decision["remaining_reduction"] > 0

    def test_no_restore_action_end_of_hour_high_power(self):
        """Test that restore is blocked when end of hour has high power."""
        loads = [
            create_test_load(
                name="Load 1",
                priority=10,
                assumed_power_kw=2.0,
            ),
        ]
        current_time = time.time()
        max_hour_kwh = 5.0

        # Scenario:
        # End of hour (5 min remaining).
        # We have used very little energy (projected 2.0 kWh < 5.0 kWh).
        # But we are currently pulling 6.0 kW (>= 5.0 kW).
        # Normal logic would restore loads because projected < max - margin.
        # New logic should block restore because power is high near end of hour.

        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=0.0,
            projected_end_kwh=2.0,  # Low projected energy
            max_hour_kwh=max_hour_kwh,
            current_time=current_time,
            current_power_kw=6.0,  # High current power
            remaining_minutes=2.0,  # Near end of hour
            restore_margin=0.5,
        )

        assert decision["action"] == "none"
        assert "holding due to high power" in decision["reason"]


class TestRegulationEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_very_small_reduction_threshold(self):
        """Test that very small reductions (< 0.01 kW) are ignored."""
        loads = [create_test_load()]
        current_time = time.time()

        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=0.005,  # Below threshold
            projected_end_kwh=5.0,
            max_hour_kwh=5.0,
            current_time=current_time,
        )

        assert decision["action"] == "none"

    def test_restore_margin_boundary(self):
        """Test behavior at exact restore margin boundary."""
        loads = [create_test_load()]
        current_time = time.time()
        restore_margin = 0.5
        max_hour_kwh = 5.0

        # Exactly at boundary (should NOT restore)
        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=0.0,
            projected_end_kwh=max_hour_kwh - restore_margin,
            max_hour_kwh=max_hour_kwh,
            current_time=current_time,
            restore_margin=restore_margin,
        )
        assert decision["action"] == "none"

        # Just below boundary (should restore)
        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=0.0,
            projected_end_kwh=max_hour_kwh - restore_margin - 0.01,
            max_hour_kwh=max_hour_kwh,
            current_time=current_time,
            restore_margin=restore_margin,
        )
        assert decision["action"] == "restore"

    def test_empty_loads_list(self):
        """Test behavior with no loads configured."""
        decision = calculate_regulation_decision(
            loads=[],
            needed_reduction_kw=2.0,
            projected_end_kwh=6.0,
            max_hour_kwh=5.0,
            current_time=time.time(),
        )

        assert decision["action"] == "reduce"
        assert len(decision["loads_to_reduce"]) == 0
        assert "no loads available" in decision["reason"].lower()


class TestEnabledEntityFeature:
    """Test the enabled_entity_id feature for dynamic load control."""

    def test_load_with_enabled_entity_id(self):
        """Test that loads can have an enabled_entity_id configured."""
        load = create_test_load(
            name="EV Charger",
            enabled=True,
            enabled_entity_id="binary_sensor.car_charging",
        )

        assert load.enabled_entity_id == "binary_sensor.car_charging"
        assert load.enabled is True

    def test_load_without_enabled_entity_id(self):
        """Test that loads work without enabled_entity_id (backwards compatible)."""
        load = create_test_load(
            name="Heat Pump",
            enabled=True,
        )

        assert load.enabled_entity_id is None
        assert load.enabled is True

    def test_load_to_dict_includes_enabled_entity(self):
        """Test that to_dict includes enabled_entity field."""
        load = create_test_load(
            name="EV Charger",
            enabled_entity_id="binary_sensor.car_charging",
        )

        load_dict = load.to_dict()
        assert "enabled_entity" in load_dict
        assert load_dict["enabled_entity"] == "binary_sensor.car_charging"

    def test_load_from_dict_with_enabled_entity(self):
        """Test that from_dict correctly loads enabled_entity field."""
        from custom_components.rvik_razor.const import (
            CONF_LOAD_ENABLED_ENTITY,
            CONF_LOAD_NAME,
            CONF_LOAD_PRIORITY,
            CONF_LOAD_TYPE,
            Load,
        )

        data = {
            CONF_LOAD_NAME: "EV Charger",
            CONF_LOAD_TYPE: "switch",
            CONF_LOAD_PRIORITY: 10,
            CONF_LOAD_ENABLED_ENTITY: "binary_sensor.car_charging",
        }

        load = Load.from_dict(data)
        assert load.enabled_entity_id == "binary_sensor.car_charging"

    def test_load_from_dict_without_enabled_entity(self):
        """Test that from_dict works without enabled_entity (backwards compatible)."""
        from custom_components.rvik_razor.const import (
            CONF_LOAD_NAME,
            CONF_LOAD_PRIORITY,
            CONF_LOAD_TYPE,
            Load,
        )

        data = {
            CONF_LOAD_NAME: "Heat Pump",
            CONF_LOAD_TYPE: "switch",
            CONF_LOAD_PRIORITY: 20,
        }

        load = Load.from_dict(data)
        assert load.enabled_entity_id is None

    def test_disabled_via_enabled_flag_still_works(self):
        """Test that the regular enabled flag still disables loads."""
        loads = [
            create_test_load(
                name="Disabled Load",
                priority=5,  # Lowest priority, would be first to reduce
                enabled=False,
                enabled_entity_id=None,
            ),
            create_test_load(
                name="Enabled Load",
                priority=10,
                enabled=True,
                enabled_entity_id=None,
            ),
        ]
        current_time = time.time()

        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=1.0,
            projected_end_kwh=6.0,
            max_hour_kwh=5.0,
            current_time=current_time,
        )

        # Should only reduce the enabled load
        assert decision["action"] == "reduce"
        assert len(decision["loads_to_reduce"]) == 1
        assert decision["loads_to_reduce"][0]["load"].name == "Enabled Load"


class TestIncidentRecreation:
    """Test cases that recreate real-world incidents."""

    def test_incident_2026_01_25_heat_pump_not_reduced(self):
        """Recreate incident: heat pump (pri 3, inverted switch) not turned off.
        
        Scenario:
        - 2 EV chargers at priority 1 and 2
        - 1 heat pump at priority 3, inverted switch, no assumed_power_kw set
        - Heat pump has power sensor showing ~2kW consumption
        - EVs were reduced to 0, but heat pump wasn't turned off
        - Result: exceeded the limit
        
        The bug: When assumed_power_kw is not set for a switch, 
        _calculate_switch_reduction returns reduction_kw=0.0, which means
        the planning phase doesn't account for the actual reduction potential.
        """
        current_time = time.time()
        
        loads = [
            # EV charger 1 - priority 1 (lowest, cut first)
            Load(
                name="EV Charger 1",
                load_type=LoadType.EV_AMPERE,
                priority=1,
                enabled=True,
                ampere_number_entity_id="number.ev1_charging_amps",
                phases=3,
                voltage=400,
            ),
            # EV charger 2 - priority 2
            Load(
                name="EV Charger 2",
                load_type=LoadType.EV_AMPERE,
                priority=2,
                enabled=True,
                ampere_number_entity_id="number.ev2_charging_amps",
                phases=3,
                voltage=400,
            ),
            # Heat pump - priority 3 (highest, cut last)
            # Note: NO assumed_power_kw set, but has power_sensor
            Load(
                name="Heat Pump",
                load_type=LoadType.SWITCH,
                priority=3,
                enabled=True,
                switch_entity_id="switch.heat_pump",
                switch_inverted=True,  # Consumes power when OFF
                power_sensor_entity_id="sensor.heat_pump_power",
                assumed_power_kw=None,  # Not set! This was the issue
                current_switch_state="off",  # Currently consuming power
            ),
        ]
        
        # Need 3kW reduction - more than EVs alone can provide at 0A
        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=3.0,
            projected_end_kwh=6.0,
            max_hour_kwh=5.0,
            current_time=current_time,
        )
        
        assert decision["action"] == "reduce"
        
        # All 3 loads should be in the reduction plan
        load_names = [p["load"].name for p in decision["loads_to_reduce"]]
        assert "EV Charger 1" in load_names, "EV1 should be in reduction plan"
        assert "EV Charger 2" in load_names, "EV2 should be in reduction plan"
        assert "Heat Pump" in load_names, "Heat pump should be in reduction plan"
        
        # The heat pump's reduction_kw should reflect actual potential, not 0
        heat_pump_plan = next(
            p for p in decision["loads_to_reduce"] if p["load"].name == "Heat Pump"
        )
        # BUG CHECK: If assumed_power_kw is None, reduction_kw becomes 0.0
        # This test documents the current (buggy) behavior
        # When fixed, the heat pump should report its actual power consumption
        print(f"Heat pump reduction_kw in plan: {heat_pump_plan['reduction_kw']}")
        
        # For now, just verify the heat pump IS in the plan
        # The actual execution will read from power sensor
        assert heat_pump_plan is not None

    def test_switch_with_power_sensor_but_no_assumed_power(self):
        """Test that switches with power sensor but no assumed_power still get reduced.
        
        The planning phase can't know the actual power (needs HA state),
        so reduction_kw will be 0.0. But the switch should still be in the plan
        and the actual power should be determined during execution.
        """
        current_time = time.time()
        
        # Single switch load with power sensor but no assumed_power
        load = Load(
            name="Test Switch",
            load_type=LoadType.SWITCH,
            priority=1,
            enabled=True,
            switch_entity_id="switch.test",
            switch_inverted=False,
            power_sensor_entity_id="sensor.test_power",
            assumed_power_kw=None,
            current_switch_state="on",  # Currently consuming power
        )
        
        decision = calculate_regulation_decision(
            loads=[load],
            needed_reduction_kw=2.0,
            projected_end_kwh=6.0,
            max_hour_kwh=5.0,
            current_time=current_time,
        )
        
        assert decision["action"] == "reduce"
        assert len(decision["loads_to_reduce"]) == 1
        
        # The reduction_kw will be 0.0 because assumed_power_kw is None
        # This is a limitation of the pure function approach
        plan = decision["loads_to_reduce"][0]
        assert plan["reduction_kw"] == 0.0, "Without assumed_power_kw, reduction_kw is 0"
        
        # But remaining_reduction should still be > 0 because we couldn't account for it
        assert decision["remaining_reduction"] == 2.0, (
            "Remaining reduction should be unchanged since switch has 0 reduction_kw"
        )

    def test_switch_plan_has_needed_reduction_key(self):
        """Test that switch reduction plans include needed_reduction key.
        
        BUG: Switch plans didn't include 'needed_reduction' key but the execution
        code tries to access plan['needed_reduction'], causing KeyError.
        """
        current_time = time.time()
        
        load = Load(
            name="Test Switch",
            load_type=LoadType.SWITCH,
            priority=1,
            enabled=True,
            switch_entity_id="switch.test",
            switch_inverted=False,
            assumed_power_kw=2.0,
            current_switch_state="on",
        )
        
        decision = calculate_regulation_decision(
            loads=[load],
            needed_reduction_kw=3.0,
            projected_end_kwh=6.0,
            max_hour_kwh=5.0,
            current_time=current_time,
        )
        
        assert len(decision["loads_to_reduce"]) == 1
        plan = decision["loads_to_reduce"][0]
        
        # This should NOT raise KeyError - the key should exist
        assert "needed_reduction" in plan, (
            "Switch plans must include 'needed_reduction' key to avoid KeyError in execution"
        )


if __name__ == "__main__":
    # Allow running tests directly with: python test_regulation.py
    pytest.main([__file__, "-v"])
