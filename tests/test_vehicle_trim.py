"""Tests for trimming a vehicle's charge current on a late-hour load."""

from __future__ import annotations

import asyncio
from datetime import date
import time

import pytest

from custom_components.rvik_razor import coordinator as coordinator_module
from custom_components.rvik_razor.const import Load, LoadType
from custom_components.rvik_razor.coordinator import (
    RvikRazorCoordinator,
    calculate_regulation_decision,
)
from custom_components.rvik_razor.vehicle import (
    CHARGER_POWER_KEY,
    CHARGING_STATE_KEY,
    LOCATION_KEY,
    STATUS_KEY,
    VehicleSnapshot,
    select_charging_vehicle,
)

CAR_A = "number.car_a_charge_current"
CAR_B = "number.car_b_charge_current"


def create_trim_load(**kwargs) -> Load:
    """Create a late-hour charger load with a vehicle charging at 16 A (6.4 kW)."""
    defaults = {
        "name": "EV",
        "load_type": LoadType.SWITCH,
        "priority": 1,
        "late_in_hour": True,
        "assumed_power_kw": 6.4,
        "switch_entity_id": "switch.charger",
        "current_switch_state": "on",
        "current_power_kw": 6.3,
        "vehicle_ampere_entity_ids": [CAR_A, CAR_B],
        "vehicle_ampere": 16,
    }
    defaults.update(kwargs)
    return Load(**defaults)


def decide(loads, current_hour_kwh, house_power_kw, remaining_seconds, **kwargs):
    """Run the regulation decision with a 4.8 kWh set point."""
    return calculate_regulation_decision(
        loads=loads,
        current_hour_kwh=current_hour_kwh,
        house_power_kw=house_power_kw,
        hourly_set_point_kwh=4.8,
        remaining_seconds=remaining_seconds,
        current_time=kwargs.pop("current_time", time.time()),
        **kwargs,
    )


class TestTrimDecision:
    """Test fitting the vehicle current to the room left in the hour."""

    def test_lowers_current_when_full_power_no_longer_fits(self):
        load = create_trim_load()

        decision = decide(
            [load], current_hour_kwh=2.0, house_power_kw=7.3, remaining_seconds=1800
        )

        assert decision["action"] == "reduce"
        plan = decision["loads_to_reduce"][0]
        assert plan["type"] == "vehicle_ampere"
        assert plan["new_value"] == 11

    def test_without_vehicle_current_the_charger_stops_instead(self):
        load = create_trim_load(vehicle_ampere=None)

        decision = decide(
            [load], current_hour_kwh=2.0, house_power_kw=7.3, remaining_seconds=1800
        )

        assert decision["action"] == "reduce"
        assert decision["loads_to_reduce"][0]["new_value"] == "off"

    def test_keeps_current_while_it_fits(self):
        load = create_trim_load(vehicle_ampere=10, current_power_kw=4.0)

        decision = decide(
            [load], current_hour_kwh=2.0, house_power_kw=5.0, remaining_seconds=1800
        )

        assert decision["action"] == "none"
        assert "Running EV at 10A late in hour" in decision["reason"]

    def test_raises_when_a_full_step_fits(self):
        load = create_trim_load(vehicle_ampere=8, current_power_kw=3.2)

        decision = decide(
            [load], current_hour_kwh=1.5, house_power_kw=4.2, remaining_seconds=1800
        )

        assert decision["action"] == "restore"
        assert decision["loads_to_restore"][0]["new_value"] == 13

    def test_raise_waits_for_raise_interval(self):
        now = time.time()
        load = create_trim_load(
            vehicle_ampere=8,
            current_power_kw=3.2,
            vehicle_ampere_changed_time=now - 60,
        )

        decision = decide(
            [load],
            current_hour_kwh=1.5,
            house_power_kw=4.2,
            remaining_seconds=1800,
            current_time=now,
        )

        assert decision["action"] == "none"

    def test_lowers_to_minimum_before_stopping(self):
        load = create_trim_load(vehicle_ampere=10, current_power_kw=4.0)

        decision = decide(
            [load], current_hour_kwh=3.0, house_power_kw=9.0, remaining_seconds=900
        )

        assert decision["action"] == "reduce"
        assert decision["loads_to_reduce"][0]["new_value"] == 6

    def test_keeps_minimum_within_stop_tolerance(self):
        load = create_trim_load(vehicle_ampere=6, current_power_kw=2.4)

        decision = decide(
            [load], current_hour_kwh=3.0, house_power_kw=7.4, remaining_seconds=900
        )

        assert decision["action"] == "none"

    def test_stops_charger_when_minimum_does_not_fit(self):
        load = create_trim_load(vehicle_ampere=6, current_power_kw=2.4)

        decision = decide(
            [load], current_hour_kwh=3.0, house_power_kw=8.9, remaining_seconds=900
        )

        assert decision["action"] == "reduce"
        plan = decision["loads_to_reduce"][0]
        assert plan["type"] == "switch"
        assert plan["new_value"] == "off"

    def test_measured_power_per_ampere_drives_the_fit(self):
        load = create_trim_load(
            vehicle_ampere=14,
            current_power_kw=9.5,
            vehicle_power_per_ampere=0.68,
        )

        decision = decide(
            [load], current_hour_kwh=2.0, house_power_kw=11.5, remaining_seconds=1800
        )

        # 3.6 kW fits: under 6 A at the measured 0.68 kW/A, but 8 A at the
        # 0.4 kW/A the assumed 6.4 kW at 16 A implies.
        assert decision["loads_to_reduce"][0]["new_value"] == 6

    def test_start_uses_measured_full_power_over_assumed_power(self):
        measured = create_trim_load(
            assumed_power_kw=11.0,
            current_switch_state="off",
            current_power_kw=0.0,
            vehicle_ampere=None,
            vehicle_power_per_ampere=0.4,
        )
        unmeasured = create_trim_load(
            assumed_power_kw=11.0,
            current_switch_state="off",
            current_power_kw=0.0,
            vehicle_ampere=None,
        )

        # 7.5 kW available until the end of the hour.
        kwargs = dict(current_hour_kwh=0.85, house_power_kw=0.5, remaining_seconds=1800)
        assert decide([measured], **kwargs)["action"] == "restore"
        assert decide([unmeasured], **kwargs)["action"] == "none"

    def test_vehicle_entities_round_trip_through_config(self):
        load = create_trim_load()

        restored = Load.from_dict(load.to_dict())

        assert restored.vehicle_ampere_entity_ids == [CAR_A, CAR_B]
        default = Load.from_dict({"name": "X", "type": "switch", "priority": 1})
        assert default.vehicle_ampere_entity_ids == []


def snapshot(entity_id=CAR_A, **kwargs) -> VehicleSnapshot:
    """Create a vehicle charging at home at 6 kW."""
    defaults = {
        "online": True,
        "charging_state": "charging",
        "charger_power_kw": 6.0,
        "location": "home",
    }
    defaults.update(kwargs)
    return VehicleSnapshot(ampere_entity_id=entity_id, **defaults)


class TestSelectChargingVehicle:
    """Test choosing the one vehicle charging on the charger."""

    def test_selects_the_only_vehicle_charging_at_home(self):
        vehicles = [snapshot(CAR_A), snapshot(CAR_B, online=False)]

        assert select_charging_vehicle(vehicles, 6.3)[0] == CAR_A

    def test_ignores_vehicle_charging_away_from_home(self):
        vehicles = [snapshot(CAR_A, location="not_home")]

        assert select_charging_vehicle(vehicles, 6.3)[0] is None

    def test_ignores_vehicle_not_charging(self):
        vehicles = [snapshot(CAR_A, charging_state="complete")]

        assert select_charging_vehicle(vehicles, 6.3)[0] is None

    def test_ignores_vehicle_drawing_other_power_than_charger(self):
        vehicles = [snapshot(CAR_A, charger_power_kw=11.0)]

        assert select_charging_vehicle(vehicles, 6.3)[0] is None

    def test_two_matching_vehicles_select_none(self):
        vehicles = [snapshot(CAR_A), snapshot(CAR_B)]

        vehicle, reason = select_charging_vehicle(vehicles, 6.3)

        assert vehicle is None
        assert "Several vehicles" in reason


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


def vehicle_states(prefix: str, online: bool, charging: str, location: str) -> dict:
    """States of one vehicle's sibling entities."""
    return {
        f"binary_sensor.{prefix}_status": FakeState("on" if online else "off"),
        f"sensor.{prefix}_charging": FakeState(charging),
        f"sensor.{prefix}_charger_power": FakeState(
            "6", {"unit_of_measurement": "kW"}
        ),
        f"device_tracker.{prefix}_location": FakeState(location),
    }


@pytest.fixture
def coordinator(monkeypatch):
    """A coordinator with car A charging at home and car B asleep."""

    def fake_find_vehicle_entities(hass, ampere_entity_id):
        prefix = "car_a" if ampere_entity_id == CAR_A else "car_b"
        return {
            STATUS_KEY: f"binary_sensor.{prefix}_status",
            CHARGING_STATE_KEY: f"sensor.{prefix}_charging",
            CHARGER_POWER_KEY: f"sensor.{prefix}_charger_power",
            LOCATION_KEY: f"device_tracker.{prefix}_location",
        }

    monkeypatch.setattr(
        coordinator_module, "find_vehicle_entities", fake_find_vehicle_entities
    )
    coord = RvikRazorCoordinator.__new__(RvikRazorCoordinator)
    coord.last_action = ""
    coord.last_action_reason = ""
    coord._trim_sessions = {}
    coord._trim_commands_today = 0
    coord._trim_budget_day = date.today()
    coord.hass = FakeHass(
        {
            CAR_A: FakeState("16", {"min": 0, "max": 32}),
            CAR_B: FakeState("unknown", {"min": 0, "max": 32}),
            **vehicle_states("car_a", True, "charging", "home"),
            **vehicle_states("car_b", False, "unknown", "unknown"),
        }
    )
    coord.loads = [create_trim_load(vehicle_ampere=None)]
    return coord


def run(coro):
    return asyncio.run(coro)


class TestTrimSession:
    """Test binding a vehicle and commanding its charge current."""

    def test_binds_the_charging_vehicle_after_one_refresh(self, coordinator):
        load = coordinator.loads[0]

        run(coordinator._async_update_vehicle_trim(1000.0))

        assert coordinator.hass.calls == [
            ("homeassistant", "update_entity", {"entity_id": [CAR_A, CAR_B]})
        ]
        assert load.vehicle_ampere == 16
        assert load.vehicle_min_ampere == 6
        assert load.vehicle_max_ampere == 16

    def test_does_not_identify_before_charging(self, coordinator):
        load = coordinator.loads[0]
        load.current_power_kw = 0.0

        run(coordinator._async_update_vehicle_trim(1000.0))

        assert coordinator.hass.calls == []
        assert load.vehicle_ampere is None

    def test_trim_command_goes_to_bound_vehicle_without_restarting_timers(
        self, coordinator
    ):
        load = coordinator.loads[0]
        load.last_action_time = 500.0
        run(coordinator._async_update_vehicle_trim(1000.0))
        coordinator.hass.calls.clear()

        run(
            coordinator._async_execute_reductions(
                [{"load": load, "type": "vehicle_ampere", "new_value": 11}], 1030.0
            )
        )

        assert coordinator.hass.calls == [
            ("number", "set_value", {"entity_id": CAR_A, "value": 11})
        ]
        assert load.last_action_time == 500.0
        assert coordinator._trim_commands_today == 1

    def test_never_commands_a_vehicle_believed_asleep(self, coordinator):
        load = coordinator.loads[0]
        run(coordinator._async_update_vehicle_trim(1000.0))
        coordinator.hass._states["binary_sensor.car_a_status"] = FakeState("off")
        coordinator.hass.calls.clear()

        assert not run(coordinator._async_set_vehicle_ampere(load, 11))
        assert coordinator.hass.calls == []

    def test_charger_not_following_reduction_falls_back_to_on_off(self, coordinator):
        load = coordinator.loads[0]
        run(coordinator._async_update_vehicle_trim(1000.0))
        run(coordinator._async_set_vehicle_ampere(load, 8))
        session = coordinator._trim_sessions["EV"]
        session.verify_after = 1100.0

        run(coordinator._async_update_vehicle_trim(1200.0))

        assert load.vehicle_ampere is None
        assert session.failed

    def test_daily_budget_turns_trim_off(self, coordinator):
        load = coordinator.loads[0]
        coordinator._trim_commands_today = 10_000

        run(coordinator._async_update_vehicle_trim(1000.0))

        assert load.vehicle_ampere is None

    def test_learns_power_per_ampere_once_the_current_settles(self, coordinator):
        load = coordinator.loads[0]
        run(coordinator._async_update_vehicle_trim(1000.0))
        run(coordinator._async_update_vehicle_trim(1030.0))
        assert load.vehicle_power_per_ampere is None

        run(coordinator._async_update_vehicle_trim(1070.0))

        assert load.vehicle_power_per_ampere == pytest.approx(6.3 / 16)

    def test_tapering_does_not_lower_learned_power_per_ampere(self, coordinator):
        load = coordinator.loads[0]
        run(coordinator._async_update_vehicle_trim(1000.0))
        run(coordinator._async_update_vehicle_trim(1070.0))
        load.current_power_kw = 3.0

        run(coordinator._async_update_vehicle_trim(1100.0))

        assert load.vehicle_power_per_ampere == pytest.approx(6.3 / 16)

    def test_learned_power_per_ampere_is_kept_while_paused(self, coordinator):
        load = coordinator.loads[0]
        run(coordinator._async_update_vehicle_trim(1000.0))
        run(coordinator._async_update_vehicle_trim(1070.0))
        load.current_switch_state = "off"
        load.current_power_kw = 0.0

        run(coordinator._async_update_vehicle_trim(1100.0))

        assert load.vehicle_ampere is None
        assert load.vehicle_power_per_ampere == pytest.approx(6.3 / 16)

    def test_unplug_restores_lowered_current(self, coordinator):
        load = coordinator.loads[0]
        run(coordinator._async_update_vehicle_trim(1000.0))
        run(coordinator._async_set_vehicle_ampere(load, 8))
        coordinator.hass.calls.clear()
        load.current_switch_state = None

        run(coordinator._async_update_vehicle_trim(1030.0))
        assert coordinator.hass.calls == []
        run(coordinator._async_update_vehicle_trim(1060.0))

        assert coordinator.hass.calls == [
            ("number", "set_value", {"entity_id": CAR_A, "value": 16})
        ]
        assert coordinator._trim_sessions["EV"].ampere_entity_id is None
