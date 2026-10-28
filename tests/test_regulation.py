"""Tests for Rvik Razor regulation logic."""

from __future__ import annotations

import asyncio
from collections import deque
import time

import pytest

from custom_components.rvik_razor.const import Load, LoadType
from custom_components.rvik_razor.coordinator import (
    RvikRazorCoordinator,
    _calculate_nominal_power_per_ampere,
    calculate_available_down_capacity,
    calculate_regulation_decision,
    is_hour_energy_reset_pending,
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

    def test_ev_charger_prefers_measured_power_over_nominal_capacity(self):
        load = create_test_load(
            load_type=LoadType.EV_AMPERE,
            current_ampere=16.0,
            current_power_kw=7.2,
            phases=3,
            voltage=400,
        )

        assert calculate_available_down_capacity([load]) == pytest.approx(7.2)


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

    def test_no_action_while_switch_loads_fit(self):
        load = create_test_load(current_switch_state="on")

        decision = decide([load], current_hour_kwh=3.9, house_power_kw=2.0)

        assert decision["action"] == "none"
        assert "stay as they are" in decision["reason"]

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

    def test_first_priority_load_stays_on_while_the_hour_holds_without_it(self):
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
        assert "Keeping Priority 1 on" in decision["reason"]

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
        assert "No switch load drawing power" in decision["reason"]

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

    def test_first_restore_priority_stays_off_until_there_is_room(self):
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

        decision = decide(loads, current_hour_kwh=4.4, house_power_kw=1.0)

        assert decision["action"] == "none"
        assert "Keeping High off" in decision["reason"]

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

    def test_ev_decision_prefers_measured_power_over_nominal(self):
        load = create_test_load(
            load_type=LoadType.EV_AMPERE,
            current_ampere=4,
            current_power_kw=2.0,
            phases=1,
            voltage=1000,
        )

        decision = decide([load], current_hour_kwh=4.0, house_power_kw=4.0)

        assert decision["action"] == "reduce"
        assert decision["loads_to_reduce"][0]["new_value"] == 0
        assert decision["loads_to_reduce"][0]["power_per_ampere"] == pytest.approx(
            0.5
        )
        assert decision["candidate_projected_end_kwh"] == pytest.approx(5.0)


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


class TestHourEnergyReset:
    """Test detection of an hour energy sensor that has not reset yet."""

    def test_reading_from_previous_hour_is_pending(self):
        assert is_hour_energy_reset_pending(4.62, 4.60, seconds_into_hour=16)

    def test_reset_reading_is_not_pending(self):
        assert not is_hour_energy_reset_pending(0.01, 4.60, seconds_into_hour=22)

    def test_gives_up_waiting_after_max_wait(self):
        assert not is_hour_energy_reset_pending(4.62, 4.60, seconds_into_hour=130)

    def test_not_pending_without_previous_hour(self):
        assert not is_hour_energy_reset_pending(4.0, None, seconds_into_hour=5)


def create_late_load(**kwargs) -> Load:
    """Create a late-hour EV switch load drawing about 4 kW."""
    defaults = {
        "name": "EV",
        "priority": 1,
        "late_in_hour": True,
        "assumed_power_kw": 4.0,
        "current_switch_state": "off",
        "current_power_kw": 0.0,
    }
    defaults.update(kwargs)
    return create_test_load(**defaults)


class TestLateHourLoads:
    """Test the late-hour start/stop rule for switch loads."""

    def test_waits_early_in_hour(self):
        load = create_late_load()

        decision = decide(
            [load],
            current_hour_kwh=0.2,
            house_power_kw=1.5,
            set_point_kwh=4.6,
            remaining_seconds=3000,
        )

        assert decision["action"] == "none"
        assert "Waiting to start EV late in hour" in decision["reason"]

    def test_starts_once_it_fits_until_end_of_hour(self):
        load = create_late_load()

        decision = decide(
            [load],
            current_hour_kwh=0.5,
            house_power_kw=1.5,
            set_point_kwh=4.6,
            remaining_seconds=1800,
        )

        assert decision["action"] == "restore"
        assert decision["loads_to_restore"][0]["load"].name == "EV"
        assert decision["loads_to_restore"][0]["new_value"] == "on"

    def test_start_respects_minimum_off_time(self):
        now = time.time()
        load = create_late_load(last_action_time=now - 60)

        decision = decide(
            [load],
            current_hour_kwh=0.5,
            house_power_kw=1.5,
            set_point_kwh=4.6,
            remaining_seconds=1800,
            current_time=now,
        )

        assert decision["action"] == "none"
        assert "minimum off time" in decision["reason"]

    def test_keeps_running_within_stop_tolerance(self):
        load = create_late_load(current_switch_state="on", current_power_kw=4.0)

        decision = decide(
            [load],
            current_hour_kwh=2.0,
            house_power_kw=5.5,
            set_point_kwh=4.6,
            remaining_seconds=1800,
        )

        assert decision["action"] == "none"
        assert "Running EV late in hour" in decision["reason"]

    def test_stops_when_short_after_minimum_on_time(self):
        load = create_late_load(current_switch_state="on", current_power_kw=4.0)

        decision = decide(
            [load],
            current_hour_kwh=3.86,
            house_power_kw=5.5,
            set_point_kwh=4.6,
            remaining_seconds=600,
        )

        assert decision["action"] == "reduce"
        assert decision["loads_to_reduce"][0]["new_value"] == "off"

    def test_minimum_on_time_delays_tolerance_stop(self):
        now = time.time()
        load = create_late_load(
            current_switch_state="on",
            current_power_kw=4.0,
            last_action_time=now - 60,
        )

        decision = decide(
            [load],
            current_hour_kwh=3.86,
            house_power_kw=5.5,
            set_point_kwh=4.6,
            remaining_seconds=600,
            current_time=now,
        )

        assert decision["action"] == "none"

    def test_overshoot_cap_stops_even_within_minimum_on_time(self):
        now = time.time()
        load = create_late_load(
            current_switch_state="on",
            current_power_kw=4.0,
            last_action_time=now - 60,
        )

        decision = decide(
            [load],
            current_hour_kwh=2.0,
            house_power_kw=7.0,
            set_point_kwh=4.6,
            remaining_seconds=1800,
            current_time=now,
        )

        assert decision["action"] == "reduce"
        assert decision["loads_to_reduce"][0]["load"].name == "EV"

    def test_idle_running_load_is_not_stopped(self):
        load = create_late_load(current_switch_state="on", current_power_kw=0.0)

        decision = decide(
            [load],
            current_hour_kwh=0.0,
            house_power_kw=1.5,
            set_point_kwh=4.6,
            remaining_seconds=3550,
        )

        assert decision["action"] == "none"

    def test_running_late_load_is_left_to_its_own_rule(self):
        late = create_late_load(current_switch_state="on", current_power_kw=4.0)
        heater = create_test_load(
            name="Heater",
            priority=10,
            current_switch_state="on",
            current_power_kw=0.5,
        )

        decision = decide(
            [late, heater],
            current_hour_kwh=4.08,
            house_power_kw=6.0,
            set_point_kwh=4.6,
            remaining_seconds=360,
        )

        # The late-hour load runs within its own stop tolerance, and the
        # heater is not cut to make room for it.
        assert decision["action"] == "none"

    def test_late_in_hour_round_trips_through_config(self):
        load = create_late_load()

        assert Load.from_dict(load.to_dict()).late_in_hour is True
        default = Load.from_dict({"name": "X", "type": "switch", "priority": 1})
        assert default.late_in_hour is False


def create_heater(**kwargs) -> Load:
    """Create a 2 kW water heater that is on and drawing power."""
    defaults = {
        "name": "Heater",
        "priority": 10,
        "current_switch_state": "on",
        "current_power_kw": 2.0,
        "timeout": 300,
    }
    defaults.update(kwargs)
    return create_test_load(**defaults)


class TestSwitchLoadsCutOnlyWhenNeeded:
    """Test that switch loads are cut only when the hour cannot hold without them."""

    def test_brief_spike_early_in_the_hour_keeps_the_heater_on(self):
        heater = create_heater()

        # 7 kW now, but the rest of the house averaged 3 kW over 5 minutes.
        decision = decide(
            [heater],
            current_hour_kwh=0.3,
            house_power_kw=7.0,
            set_point_kwh=4.8,
            remaining_seconds=3300,
            uncontrolled_power_kw=3.0,
        )

        assert decision["action"] == "none"
        assert "Keeping Heater on" in decision["reason"]

    def test_lasting_high_load_cuts_the_heater_early(self):
        heater = create_heater()

        decision = decide(
            [heater],
            current_hour_kwh=0.3,
            house_power_kw=7.0,
            set_point_kwh=4.8,
            remaining_seconds=3300,
            uncontrolled_power_kw=5.0,
        )

        assert decision["action"] == "reduce"
        assert decision["loads_to_reduce"][0]["load"].name == "Heater"
        assert decision["candidate_projected_end_kwh"] == pytest.approx(
            0.3 + 5.0 * 3300 / 3600
        )

    def test_load_that_just_stopped_does_not_cut_the_heater(self):
        heater = create_heater()

        # The last five minutes averaged 6 kW, but the rest of the house is
        # down to 1 kW now.
        decision = decide(
            [heater],
            current_hour_kwh=0.1,
            house_power_kw=3.0,
            set_point_kwh=4.8,
            remaining_seconds=3500,
            uncontrolled_power_kw=6.0,
        )

        assert decision["action"] == "none"

    def test_brief_dip_does_not_restore_the_heater(self):
        heater = create_heater(current_switch_state="off", current_power_kw=0.0)

        decision = decide(
            [heater],
            current_hour_kwh=3.0,
            house_power_kw=1.0,
            set_point_kwh=4.8,
            remaining_seconds=1800,
            uncontrolled_power_kw=3.4,
        )

        assert decision["action"] == "none"
        assert "Keeping Heater off" in decision["reason"]

    def test_heater_is_cut_late_in_the_hour_once_it_is_needed(self):
        heater = create_heater()

        kept = decide(
            [heater],
            current_hour_kwh=4.3,
            house_power_kw=4.0,
            set_point_kwh=4.8,
            remaining_seconds=600,
        )
        cut = decide(
            [heater],
            current_hour_kwh=4.4,
            house_power_kw=4.0,
            set_point_kwh=4.8,
            remaining_seconds=600,
        )

        assert kept["action"] == "none"
        assert cut["action"] == "reduce"

    def test_heater_drawing_nothing_is_not_cut(self):
        heater = create_heater(current_power_kw=0.0)

        decision = decide(
            [heater],
            current_hour_kwh=2.0,
            house_power_kw=8.0,
            set_point_kwh=4.8,
            remaining_seconds=1800,
        )

        assert decision["action"] == "none"
        assert "No switch load drawing power" in decision["reason"]

    def test_idle_first_priority_passes_the_cut_to_the_next_load(self):
        idle = create_heater(name="Idle", priority=1, current_power_kw=0.0)
        heat_pump = create_heater(name="Heat pump", priority=5)

        decision = decide(
            [idle, heat_pump],
            current_hour_kwh=2.0,
            house_power_kw=8.0,
            set_point_kwh=4.8,
            remaining_seconds=1800,
        )

        assert decision["action"] == "reduce"
        assert decision["loads_to_reduce"][0]["load"].name == "Heat pump"

    def test_running_late_load_is_cut_before_the_heater(self):
        late = create_late_load(current_switch_state="on", current_power_kw=5.5)
        heater = create_heater()

        decision = decide(
            [late, heater],
            current_hour_kwh=2.5,
            house_power_kw=9.5,
            set_point_kwh=4.8,
            remaining_seconds=1800,
            current_time=10_000.0,
        )

        assert decision["action"] == "reduce"
        assert decision["loads_to_reduce"][0]["load"].name == "EV"

    def test_heater_is_restored_once_the_hour_holds_without_it(self):
        heater = create_heater(current_switch_state="off", current_power_kw=0.0)

        restored = decide(
            [heater],
            current_hour_kwh=3.0,
            house_power_kw=2.8,
            set_point_kwh=4.8,
            remaining_seconds=1800,
        )
        kept_off = decide(
            [heater],
            current_hour_kwh=3.0,
            house_power_kw=3.4,
            set_point_kwh=4.8,
            remaining_seconds=1800,
        )

        assert restored["action"] == "restore"
        assert restored["loads_to_restore"][0]["new_value"] == "on"
        assert kept_off["action"] == "none"
        assert "Keeping Heater off" in kept_off["reason"]

    def test_heater_in_cooldown_is_left_alone(self):
        now = 10_000.0
        heater = create_heater(current_switch_state="off", last_action_time=now - 60)

        decision = decide(
            [heater],
            current_hour_kwh=1.0,
            house_power_kw=1.0,
            set_point_kwh=4.8,
            remaining_seconds=1800,
            current_time=now,
        )

        assert decision["action"] == "none"


class TestUncontrolledPowerAverage:
    """Test the averaged house power that switch loads are judged on."""

    def coordinator(self, loads):
        coordinator = RvikRazorCoordinator.__new__(RvikRazorCoordinator)
        coordinator.loads = loads
        coordinator._uncontrolled_samples = deque()
        return coordinator

    def test_leaves_out_controlled_loads(self):
        late = create_late_load(current_switch_state="on", current_power_kw=5.5)
        heater = create_heater()
        coordinator = self.coordinator([late, heater])

        coordinator._record_uncontrolled_power(1000.0, 9.0)

        assert coordinator._uncontrolled_average() == pytest.approx(1.5)

    def test_averages_the_last_five_minutes(self):
        coordinator = self.coordinator([])

        coordinator._record_uncontrolled_power(1000.0, 9.0)
        for now in range(1030, 1360, 30):
            coordinator._record_uncontrolled_power(float(now), 1.0)

        # The 9 kW sample is older than five minutes and has dropped out.
        assert coordinator._uncontrolled_average() == pytest.approx(1.0)

    def test_no_samples_falls_back_to_the_current_power(self):
        assert self.coordinator([])._uncontrolled_average() is None


class FakeState:
    """Minimal Home Assistant state."""

    def __init__(self, state: str, attributes: dict | None = None) -> None:
        self.state = state
        self.attributes = attributes or {}


class FakeHass:
    """Minimal Home Assistant core with states and services."""

    def __init__(self, states: dict[str, FakeState]) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.states = self
        self.services = self
        self._states = states

    def get(self, entity_id: str) -> FakeState | None:
        return self._states.get(entity_id)

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data))


class TestSwitchRestoreExecution:
    """Test that switch restores count as actions even at 0 W."""

    def test_restore_succeeds_while_power_sensor_reads_zero(self):
        coordinator = RvikRazorCoordinator.__new__(RvikRazorCoordinator)
        coordinator.last_action = ""
        coordinator.last_action_reason = ""
        coordinator.hass = FakeHass(
            {
                "switch.ev": FakeState("off"),
                "sensor.ev_power": FakeState("0.0", {"unit_of_measurement": "W"}),
            }
        )
        load = create_test_load(
            name="EV",
            switch_entity_id="switch.ev",
            power_sensor_entity_id="sensor.ev_power",
            assumed_power_kw=4.0,
        )

        asyncio.run(
            coordinator._async_execute_restorations(
                [{"load": load, "new_value": "on"}], 12345.0
            )
        )

        assert coordinator.hass.calls == [
            ("switch", "turn_on", {"entity_id": "switch.ev"})
        ]
        assert load.last_action_time == 12345.0
        assert coordinator.last_action == "Restored loads"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
