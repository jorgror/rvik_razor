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
SESSION_ENERGY = "sensor.charger_session_energy"


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

    def test_raises_when_a_full_step_fits_for_a_minute(self):
        now = time.time()
        load = create_trim_load(vehicle_ampere=8, current_power_kw=3.2)
        kwargs = dict(current_hour_kwh=1.5, house_power_kw=4.2, remaining_seconds=1800)

        assert decide([load], **kwargs, current_time=now)["action"] == "none"
        decision = decide([load], **kwargs, current_time=now + 60)

        # 13 A fits; the raise keeps an ampere spare.
        assert decision["action"] == "restore"
        assert decision["loads_to_restore"][0]["new_value"] == 12

    def test_raise_restarts_its_minute_when_the_room_goes(self):
        now = time.time()
        load = create_trim_load(vehicle_ampere=8, current_power_kw=3.2)
        room = dict(current_hour_kwh=1.5, house_power_kw=4.2, remaining_seconds=1800)
        busy = dict(current_hour_kwh=1.5, house_power_kw=6.2, remaining_seconds=1800)

        decide([load], **room, current_time=now)
        decide([load], **busy, current_time=now + 30)
        decide([load], **room, current_time=now + 60)

        assert decide([load], **room, current_time=now + 90)["action"] == "none"
        assert decide([load], **room, current_time=now + 120)["action"] == "restore"

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
            [load], current_hour_kwh=1.9, house_power_kw=11.5, remaining_seconds=1800
        )

        # 3.8 kW fits: under 6 A at the measured 0.68 kW/A, but 9 A at the
        # 0.4 kW/A the assumed 6.4 kW at 16 A implies.
        assert decision["loads_to_reduce"][0]["new_value"] == 6

    def test_stops_without_lowering_first_when_minimum_does_not_fit(self):
        load = create_trim_load(vehicle_ampere=10, current_power_kw=4.0)

        decision = decide(
            [load], current_hour_kwh=3.0, house_power_kw=10.4, remaining_seconds=900
        )

        plan = decision["loads_to_reduce"][0]
        assert plan["type"] == "switch"
        assert plan["new_value"] == "off"

    def test_stop_rule_at_minimum_uses_what_the_vehicle_draws(self):
        # At 6 A the vehicle draws 2.3 kW, not the 4.1 kW the last stretch's
        # 0.68 kW/A predicts.
        load = create_trim_load(
            vehicle_ampere=6, current_power_kw=2.3, vehicle_power_per_ampere=0.68
        )

        decision = decide(
            [load], current_hour_kwh=3.0, house_power_kw=7.4, remaining_seconds=900
        )

        assert decision["action"] == "none"

    def test_holds_while_the_vehicle_follows_its_current(self):
        now = time.time()
        load = create_trim_load(
            vehicle_ampere=8, current_power_kw=6.3, vehicle_settle_until=now + 30
        )
        kwargs = dict(current_hour_kwh=3.0, house_power_kw=12.0, remaining_seconds=900)

        held = decide([load], **kwargs, current_time=now)
        assert held["action"] == "none"
        assert "follow its current" in held["reason"]
        assert decide([load], **kwargs, current_time=now + 30)["action"] == "reduce"

    def test_cuts_one_ampere_deeper_near_the_end_of_the_hour(self):
        early = create_trim_load(vehicle_ampere=10, current_power_kw=4.0)
        late = create_trim_load(vehicle_ampere=10, current_power_kw=4.0)

        # 3.8 kW available: 9 A fits at 0.4 kW/A.
        early_decision = decide(
            [early], current_hour_kwh=3.35, house_power_kw=6.0, remaining_seconds=900
        )
        late_decision = decide(
            [late], current_hour_kwh=3.93, house_power_kw=6.0, remaining_seconds=540
        )

        assert early_decision["loads_to_reduce"][0]["new_value"] == 9
        assert late_decision["loads_to_reduce"][0]["new_value"] == 8

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

    def paused(self, **kwargs) -> Load:
        """A paused charger with a bound vehicle at 0.39 kW/A, 6-14 A."""
        defaults = dict(
            assumed_power_kw=11.0,
            current_switch_state="off",
            current_power_kw=0.0,
            vehicle_ampere=None,
            vehicle_power_per_ampere=0.39,
            vehicle_min_ampere=6,
            vehicle_max_ampere=14,
            vehicle_trim_ready=True,
        )
        defaults.update(kwargs)
        return create_trim_load(**defaults)

    def test_restarts_at_reduced_current_once_it_fits_for_three_minutes(self):
        # 4.5 kW available for half an hour: 14 A (5.5 kW) does not fit, 8 A does.
        load = self.paused()
        kwargs = dict(current_hour_kwh=2.05, house_power_kw=1.0, remaining_seconds=1800)

        first = decide([load], current_time=1000.0, **kwargs)
        confirmed = decide([load], current_time=1180.0, **kwargs)

        assert first["action"] == "none"
        assert confirmed["action"] == "restore"
        assert confirmed["loads_to_restore"][0]["new_value"] == "on"
        assert "reduced current" in confirmed["reason"]

    def test_reduced_restart_confirmation_starts_over_when_the_room_goes(self):
        load = self.paused()
        kwargs = dict(house_power_kw=1.0, remaining_seconds=1800)

        decide([load], current_hour_kwh=2.05, current_time=1000.0, **kwargs)
        decide([load], current_hour_kwh=3.0, current_time=1090.0, **kwargs)
        decision = decide([load], current_hour_kwh=2.05, current_time=1180.0, **kwargs)

        assert decision["action"] == "none"

    def test_waits_for_full_power_without_a_vehicle_to_trim(self):
        load = self.paused(vehicle_trim_ready=False)
        kwargs = dict(current_hour_kwh=2.05, house_power_kw=1.0, remaining_seconds=1800)

        decide([load], current_time=1000.0, **kwargs)
        decision = decide([load], current_time=1180.0, **kwargs)

        assert decision["action"] == "none"

    def test_reduced_restart_leaves_room_for_the_ramp_at_full_power(self):
        # 3.8 kW covers 8 A plus the start slack, but not two minutes at 14 A.
        load = self.paused()
        kwargs = dict(current_hour_kwh=2.4, house_power_kw=1.0, remaining_seconds=1800)

        decide([load], current_time=1000.0, **kwargs)
        decision = decide([load], current_time=1180.0, **kwargs)

        assert decision["action"] == "none"

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
        self.refreshed: dict[str, FakeState] = {}  # States a refresh brings

    def get(self, entity_id: str) -> FakeState | None:
        return self._states.get(entity_id)

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data))
        if service == "update_entity":
            self._states.update(self.refreshed)


class FakeStore:
    """Minimal Home Assistant storage that writes at once."""

    def __init__(self, data: dict | None = None) -> None:
        self.data = data
        self.saves = 0

    async def async_load(self) -> dict | None:
        return self.data

    def async_delay_save(self, data_func, delay: float = 0) -> None:
        self.data = data_func()
        self.saves += 1


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
    monkeypatch.setattr(
        coordinator_module,
        "find_charger_energy_entities",
        lambda hass, switch_entity_id: [SESSION_ENERGY],
    )
    # Most tests bind on the first update; TestSteadyBinding covers the wait.
    monkeypatch.setattr(coordinator_module, "TRIM_IDENTIFY_SETTLE", 0)
    coord = RvikRazorCoordinator.__new__(RvikRazorCoordinator)
    coord.last_action = ""
    coord.last_action_reason = ""
    coord._charger_energy = {}
    coord._trim_sessions = {}
    coord._trim_commands_today = 0
    coord._trim_budget_day = date.today()
    coord._trim_store = FakeStore()
    coord._trim_saved = None
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

    def test_does_not_learn_while_the_vehicle_ramps_up_after_a_start(
        self, coordinator
    ):
        load = coordinator.loads[0]
        run(coordinator._async_update_vehicle_trim(1000.0))
        load.current_switch_state = "off"
        load.current_power_kw = 0.0
        run(coordinator._async_update_vehicle_trim(1030.0))

        # Charger starts again long after the last current change
        load.current_switch_state = "on"
        load.current_power_kw = 1.7
        run(coordinator._async_update_vehicle_trim(3000.0))
        load.current_power_kw = 6.3
        run(coordinator._async_update_vehicle_trim(3030.0))
        assert load.vehicle_power_per_ampere is None

        run(coordinator._async_update_vehicle_trim(3060.0))

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

    def test_each_charging_stretch_learns_its_own_rate(self, coordinator):
        load = coordinator.loads[0]
        run(coordinator._async_update_vehicle_trim(1000.0))
        run(coordinator._async_update_vehicle_trim(1070.0))
        load.current_switch_state = "off"
        load.current_power_kw = 0.0
        run(coordinator._async_update_vehicle_trim(1100.0))

        # Resumed on fewer phases: 4.0 kW at 16 A
        load.current_switch_state = "on"
        load.current_power_kw = 4.0
        run(coordinator._async_update_vehicle_trim(3000.0))
        assert load.vehicle_power_per_ampere == pytest.approx(6.3 / 16)
        run(coordinator._async_update_vehicle_trim(3060.0))

        assert load.vehicle_power_per_ampere == pytest.approx(4.0 / 16)

    def test_rate_is_measured_again_after_a_current_change(self, coordinator):
        load = coordinator.loads[0]
        load.current_power_kw = 6.8
        run(coordinator._async_update_vehicle_trim(1000.0))
        run(coordinator._async_update_vehicle_trim(1070.0))
        assert load.vehicle_power_per_ampere == pytest.approx(6.8 / 16)

        # Lowered to 8 A, the vehicle draws far less per ampere
        session = coordinator._trim_sessions["EV"]
        session.ampere = 8.0
        session.changed_time = 1100.0
        load.current_power_kw = 2.4
        run(coordinator._async_update_vehicle_trim(1130.0))
        assert load.vehicle_power_per_ampere == pytest.approx(6.8 / 16)
        run(coordinator._async_update_vehicle_trim(1160.0))

        assert load.vehicle_power_per_ampere == pytest.approx(2.4 / 8)

    def test_stretch_drawing_more_than_the_last_is_taken_at_its_draw(
        self, coordinator
    ):
        load = coordinator.loads[0]
        load.current_power_kw = 4.6
        run(coordinator._async_update_vehicle_trim(1000.0))
        run(coordinator._async_update_vehicle_trim(1070.0))
        load.current_switch_state = "off"
        load.current_power_kw = 0.0
        run(coordinator._async_update_vehicle_trim(1100.0))

        load.current_switch_state = "on"
        load.current_power_kw = 9.0
        run(coordinator._async_update_vehicle_trim(3000.0))

        assert load.vehicle_power_per_ampere == pytest.approx(9.0 / 16)

    def test_current_change_holds_decisions_while_the_vehicle_follows(
        self, coordinator
    ):
        load = coordinator.loads[0]
        run(coordinator._async_update_vehicle_trim(1000.0))
        run(coordinator._async_set_vehicle_ampere(load, 8))
        changed = coordinator._trim_sessions["EV"].changed_time

        run(coordinator._async_update_vehicle_trim(changed + 30))

        assert load.vehicle_settle_until == pytest.approx(changed + 60)

    def test_paused_bound_vehicle_is_ready_for_a_reduced_restart(self, coordinator):
        load = coordinator.loads[0]
        run(coordinator._async_update_vehicle_trim(1000.0))
        load.current_switch_state = "off"
        load.current_power_kw = 0.0

        run(coordinator._async_update_vehicle_trim(1030.0))

        assert load.vehicle_trim_ready
        assert load.vehicle_ampere is None

    def test_used_budget_rules_out_a_reduced_restart(self, coordinator):
        load = coordinator.loads[0]
        run(coordinator._async_update_vehicle_trim(1000.0))
        coordinator._trim_commands_today = 10_000

        run(coordinator._async_update_vehicle_trim(1030.0))

        assert not load.vehicle_trim_ready

    def test_start_holds_decisions_while_the_vehicle_ramps_up(self, coordinator):
        load = coordinator.loads[0]

        run(coordinator._async_update_vehicle_trim(1000.0))

        assert load.vehicle_settle_until == pytest.approx(1060.0)

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


class TestSteadyBinding:
    """Test that the vehicle is looked for once the charger's power has settled."""

    def test_waits_a_minute_of_charging_before_the_refresh(
        self, coordinator, monkeypatch
    ):
        monkeypatch.setattr(coordinator_module, "TRIM_IDENTIFY_SETTLE", 60)
        load = coordinator.loads[0]

        run(coordinator._async_update_vehicle_trim(1000.0))
        run(coordinator._async_update_vehicle_trim(1030.0))
        assert coordinator.hass.calls == []

        run(coordinator._async_update_vehicle_trim(1060.0))
        assert coordinator.hass.calls == [
            ("homeassistant", "update_entity", {"entity_id": [CAR_A, CAR_B]})
        ]
        assert coordinator._trim_sessions["EV"].ampere_entity_id == CAR_A
        assert coordinator._trim_sessions["EV"].attempts == 1
        assert load.vehicle_ampere == 16

    def test_a_stop_before_the_minute_starts_the_wait_again(
        self, coordinator, monkeypatch
    ):
        monkeypatch.setattr(coordinator_module, "TRIM_IDENTIFY_SETTLE", 60)
        load = coordinator.loads[0]

        run(coordinator._async_update_vehicle_trim(1000.0))
        load.current_power_kw = 0.0
        run(coordinator._async_update_vehicle_trim(1030.0))
        load.current_power_kw = 6.3
        run(coordinator._async_update_vehicle_trim(1060.0))

        assert coordinator.hass.calls == []


class TestReplug:
    """Test noticing a replug that the charger switch never showed."""

    def session_energy(self, coordinator, kwh):
        coordinator.hass._states[SESSION_ENERGY] = FakeState(
            str(kwh), {"unit_of_measurement": "kWh"}
        )

    def test_new_session_forgets_a_failed_vehicle(self, coordinator):
        load = coordinator.loads[0]
        self.session_energy(coordinator, 8.9)
        run(coordinator._async_update_vehicle_trim(1000.0))
        session = coordinator._trim_sessions["EV"]
        session.ampere_entity_id = None
        session.failed = True
        coordinator.hass.calls.clear()

        self.session_energy(coordinator, 0.7)
        run(coordinator._async_update_vehicle_trim(1030.0))

        session = coordinator._trim_sessions["EV"]
        assert not session.failed
        assert session.ampere_entity_id == CAR_A
        assert session.attempts == 1
        assert load.vehicle_ampere == 16

    def test_replug_leaves_a_lowered_current_for_trim(self, coordinator):
        load = coordinator.loads[0]
        self.session_energy(coordinator, 8.9)
        run(coordinator._async_update_vehicle_trim(1000.0))
        run(coordinator._async_set_vehicle_ampere(load, 8))
        coordinator.hass._states[CAR_A] = FakeState("8", {"min": 0, "max": 32})
        coordinator.hass.calls.clear()

        self.session_energy(coordinator, 0.7)
        run(coordinator._async_update_vehicle_trim(1400.0))

        # Refreshed and bound again, but no command to set 16 A.
        assert ("number", "set_value", {"entity_id": CAR_A, "value": 16}) not in (
            coordinator.hass.calls
        )
        assert coordinator._trim_sessions["EV"].ampere_entity_id == CAR_A
        assert load.vehicle_ampere == 8

    def test_small_correction_of_session_energy_is_not_a_replug(self, coordinator):
        self.session_energy(coordinator, 2.32)
        run(coordinator._async_update_vehicle_trim(1000.0))
        bound_time = coordinator._trim_sessions["EV"].bound_time

        self.session_energy(coordinator, 2.12)
        run(coordinator._async_update_vehicle_trim(1030.0))

        assert coordinator._trim_sessions["EV"].bound_time == bound_time

    def test_first_reading_after_a_restart_is_not_a_replug(self, coordinator):
        self.session_energy(coordinator, 0.7)
        run(coordinator._async_update_vehicle_trim(1000.0))
        bound_time = coordinator._trim_sessions["EV"].bound_time

        run(coordinator._async_update_vehicle_trim(1030.0))

        assert coordinator._trim_sessions["EV"].bound_time == bound_time


class TestStaleRange:
    """Test a charge-current range left stale by a refresh while paused."""

    def bound(self, coordinator):
        """Car A bound and measured, then left with a stale 0-5 A range."""
        load = coordinator.loads[0]
        run(coordinator._async_update_vehicle_trim(1000.0))
        run(coordinator._async_update_vehicle_trim(1070.0))
        coordinator.hass._states[CAR_A] = FakeState("5", {"min": 0, "max": 5})
        coordinator.hass.calls.clear()
        return load

    def test_refreshes_a_stale_range_before_the_command(self, coordinator):
        load = self.bound(coordinator)
        coordinator.hass.refreshed = {CAR_A: FakeState("16", {"min": 0, "max": 32})}

        assert run(coordinator._async_set_vehicle_ampere(load, 6))
        assert coordinator.hass.calls == [
            ("homeassistant", "update_entity", {"entity_id": [CAR_A]}),
            ("number", "set_value", {"entity_id": CAR_A, "value": 6}),
        ]

    def test_range_still_too_low_keeps_the_rate_for_on_off(self, coordinator):
        load = self.bound(coordinator)

        assert not run(coordinator._async_set_vehicle_ampere(load, 6))
        load.current_switch_state = "off"
        load.current_power_kw = 0.0
        run(coordinator._async_update_vehicle_trim(1100.0))

        assert ("number", "set_value", {"entity_id": CAR_A, "value": 6}) not in (
            coordinator.hass.calls
        )
        assert load.vehicle_power_per_ampere == pytest.approx(6.3 / 16)
        assert not load.vehicle_trim_ready
        assert coordinator._trim_commands_today == 0

    def test_vehicle_is_found_again_at_its_next_charging_stretch(self, coordinator):
        load = self.bound(coordinator)
        run(coordinator._async_set_vehicle_ampere(load, 6))
        coordinator.hass._states[CAR_A] = FakeState("16", {"min": 0, "max": 32})

        run(coordinator._async_update_vehicle_trim(1400.0))

        assert load.vehicle_ampere == 16
        assert load.vehicle_max_ampere == 16


class TestTrimRestart:
    """Test keeping the bound vehicle and its rate across a restart."""

    def restarted(self, coordinator):
        """A new coordinator restored from what the old one saved."""
        new = RvikRazorCoordinator.__new__(RvikRazorCoordinator)
        new.last_action = new.last_action_reason = ""
        new._charger_energy = {}
        new._trim_sessions = {}
        new._trim_commands_today = 0
        new._trim_budget_day = date.today()
        new._trim_store = FakeStore(coordinator._trim_store.data)
        new._trim_saved = None
        new.hass = coordinator.hass
        new.loads = [create_trim_load(vehicle_ampere=None)]
        run(new.async_restore_trim_state())
        return new

    def test_measured_rate_survives_a_restart_while_paused(self, coordinator):
        load = coordinator.loads[0]
        run(coordinator._async_update_vehicle_trim(1000.0))
        run(coordinator._async_update_vehicle_trim(1070.0))
        load.current_switch_state = "off"
        load.current_power_kw = 0.0
        run(coordinator._async_update_vehicle_trim(1100.0))
        coordinator._async_save_trim_state()

        new = self.restarted(coordinator)
        load = new.loads[0]
        load.current_switch_state = "off"
        load.current_power_kw = 0.0
        run(new._async_update_vehicle_trim(1200.0))

        assert load.vehicle_power_per_ampere == pytest.approx(6.3 / 16)
        assert new._trim_sessions["EV"].ampere_entity_id == CAR_A

    def test_lowered_current_is_restored_when_unplugged_after_a_restart(
        self, coordinator
    ):
        load = coordinator.loads[0]
        run(coordinator._async_update_vehicle_trim(1000.0))
        run(coordinator._async_set_vehicle_ampere(load, 8))
        coordinator._async_save_trim_state()

        new = self.restarted(coordinator)
        new.hass.calls.clear()
        new.loads[0].current_switch_state = None
        run(new._async_update_vehicle_trim(1030.0))
        run(new._async_update_vehicle_trim(1060.0))

        assert new.hass.calls == [
            ("number", "set_value", {"entity_id": CAR_A, "value": 16})
        ]

    def test_commands_count_toward_the_same_day_only(self, coordinator):
        coordinator._trim_commands_today = 40
        coordinator._async_save_trim_state()
        assert self.restarted(coordinator)._trim_commands_today == 40

        coordinator._trim_store.data["day"] = "2000-01-01"
        assert self.restarted(coordinator)._trim_commands_today == 0

    def test_saves_only_when_something_changed(self, coordinator):
        run(coordinator._async_update_vehicle_trim(1000.0))
        coordinator._async_save_trim_state()
        coordinator._async_save_trim_state()

        assert coordinator._trim_store.saves == 1
