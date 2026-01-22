"""Coordinator for Rvik Razor integration."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_HOUSE_POWER_SENSOR,
    CONF_HOUR_ENERGY_SENSOR,
    CONF_LOADS,
    CONF_MAX_HOUR_KWH,
    CONF_MODE,
    DEFAULT_COOLDOWN,
    DEFAULT_RESTORE_MARGIN,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    Load,
    LoadType,
    OperationMode,
)

_LOGGER = logging.getLogger(__name__)


def calculate_regulation_decision(
    loads: list[Load],
    needed_reduction_kw: float,
    projected_end_kwh: float,
    max_hour_kwh: float,
    current_time: float,
    current_power_kw: float | None = None,
    remaining_minutes: float | None = None,
    restore_margin: float = DEFAULT_RESTORE_MARGIN,
    cooldown: float = DEFAULT_COOLDOWN,
) -> dict[str, Any]:
    """Calculate what regulation actions should be taken.

    This is a pure function that contains the core regulation logic.
    It doesn't interact with Home Assistant - it just makes decisions.

    Args:
        loads: List of Load objects with current state
        needed_reduction_kw: How much power needs to be reduced (kW)
        projected_end_kwh: Projected energy usage at end of hour (kWh)
        max_hour_kwh: Maximum allowed energy for the hour (kWh)
        current_time: Current timestamp for cooldown calculations
        current_power_kw: Current instant power consumption in kW
        remaining_minutes: Minutes remaining in the current hour
        restore_margin: Margin below max before restoring loads (kWh)
        cooldown: Minimum time between actions on same load (seconds)

    Returns:
        Dictionary with:
        - action: "reduce", "restore", or "none"
        - loads_to_reduce: List of (load, new_value) tuples for reduction
        - loads_to_restore: List of loads to restore
        - remaining_reduction: kW still needed after planned actions
        - reason: Human-readable explanation
    """
    result = {
        "action": "none",
        "loads_to_reduce": [],
        "loads_to_restore": [],
        "remaining_reduction": 0.0,
        "reason": "",
    }

    # Safety check for end of hour
    # If we are close to end of hour (e.g. < 10 mins) and current power is already
    # exceeding the hourly average limit, preventing restorations is prudent.
    # This avoids the "jumping" behavior where we max out power at end of hour
    # only to immediately cut it at start of next hour.
    end_of_hour_safety_trigger = False
    if (
        current_power_kw is not None
        and remaining_minutes is not None
        and remaining_minutes < 5  # Last 5 minutes
        and current_power_kw >= max_hour_kwh
    ):
        end_of_hour_safety_trigger = True

    # Check if we need to reduce or restore
    if needed_reduction_kw > 0.01:  # Need to reduce
        result["action"] = "reduce"
        result["remaining_reduction"] = needed_reduction_kw

        # Sort loads by priority (lowest first = cut first)
        sorted_loads = sorted(
            [load for load in loads if load.enabled],
            key=lambda x: x.priority,
        )

        remaining_reduction = needed_reduction_kw
        loads_to_reduce = []

        for load in sorted_loads:
            if remaining_reduction <= 0.01:
                break

            # Check cooldown
            if current_time - load.last_action_time < cooldown:
                _LOGGER.debug(
                    "Load %s in cooldown, skipping (%.0fs remaining)",
                    load.name,
                    cooldown - (current_time - load.last_action_time),
                )
                continue

            # Calculate what reduction this load can provide
            reduction_info = _calculate_load_reduction(load, remaining_reduction)
            if reduction_info:
                loads_to_reduce.append(reduction_info)
                remaining_reduction -= reduction_info["reduction_kw"]

        result["loads_to_reduce"] = loads_to_reduce
        result["remaining_reduction"] = remaining_reduction

        if loads_to_reduce:
            result["reason"] = (
                f"Need {needed_reduction_kw:.2f}kW reduction, planning to reduce {len(loads_to_reduce)} load(s)"
            )
        else:
            result["reason"] = (
                f"Need {needed_reduction_kw:.2f}kW but no loads available to reduce"
            )

    elif projected_end_kwh < (max_hour_kwh - restore_margin):
        # Enough margin to restore any enabled load

        if end_of_hour_safety_trigger:
            result["reason"] = (
                f"Margin available ({max_hour_kwh - projected_end_kwh:.2f}kWh), but holding due to "
                f"high power ({current_power_kw:.2f}kW >= {max_hour_kwh:.2f}kW) near end of hour"
            )
            return result

        # Consider all enabled loads - we'll check if they can be restored
        # (i.e., are currently below max consumption)
        restorable_loads = [load for load in loads if load.enabled]

        if restorable_loads:
            result["action"] = "restore"

            # Sort loads by priority (highest first = restore first)
            sorted_loads = sorted(
                restorable_loads, key=lambda x: x.priority, reverse=True
            )

            # Only restore one at a time to avoid overshooting
            for load in sorted_loads:
                # Check cooldown
                if current_time - load.last_action_time < cooldown:
                    continue

                result["loads_to_restore"] = [load]
                result["reason"] = (
                    f"Sufficient margin ({max_hour_kwh - projected_end_kwh:.2f}kWh), restoring {load.name}"
                )
                break

            if not result["loads_to_restore"]:
                result["action"] = "none"
                result["reason"] = "Margin available but all loads in cooldown"
        else:
            # No loads to restore, just within safe range
            result["reason"] = (
                f"Within safe range (projected: {projected_end_kwh:.2f}kWh, max: {max_hour_kwh:.2f}kWh)"
            )
    else:
        result["reason"] = (
            f"Within safe range (projected: {projected_end_kwh:.2f}kWh, max: {max_hour_kwh:.2f}kWh)"
        )

    return result


def _calculate_load_reduction(
    load: Load,
    needed_reduction: float,
) -> dict[str, Any] | None:
    """Calculate how to reduce a single load.

    Returns a dict with reduction details or None if load cannot be reduced.
    """
    if load.load_type == LoadType.EV_AMPERE:
        return _calculate_ev_reduction(load, needed_reduction)
    elif load.load_type == LoadType.SWITCH:
        return _calculate_switch_reduction(load)
    return None


def _calculate_ev_reduction(
    load: Load,
    needed_reduction: float,
) -> dict[str, Any] | None:
    """Calculate EV charger reduction."""
    # This requires actual sensor values, so we'll return a structure
    # that the async code can use to make the actual determination
    return {
        "load": load,
        "type": "ev_ampere",
        "needed_reduction": needed_reduction,
        "reduction_kw": 0.0,  # Will be calculated with actual values
    }


def _calculate_switch_reduction(load: Load) -> dict[str, Any] | None:
    """Calculate switch reduction."""
    # For switches, we know the reduction from config
    reduction_kw = load.assumed_power_kw if load.assumed_power_kw else 0.0
    # For inverted switches, we turn ON to reduce power (OFF consumes power)
    new_value = "on" if load.switch_inverted else "off"
    return {
        "load": load,
        "type": "switch",
        "reduction_kw": reduction_kw,
        "new_value": new_value,
    }


class RvikRazorCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to manage Rvik Razor data and control."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: dict[str, Any],
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_UPDATE_INTERVAL),
        )
        self.entry_id = entry_id
        self.config = config
        self.loads: list[Load] = []
        self._load_config()

        # Runtime state
        self.last_hour: int = datetime.now().hour
        self.last_action = "Initialized"
        self.last_action_reason = ""

    def _load_config(self) -> None:
        """Load configuration and create Load objects."""
        loads_data = self.config.get(CONF_LOADS, [])
        self.loads = [Load.from_dict(load_data) for load_data in loads_data]
        _LOGGER.debug("Loaded %d loads from config", len(self.loads))

    def update_config(self, config: dict[str, Any]) -> None:
        """Update configuration and reload loads."""
        self.config = config
        self._load_config()
        _LOGGER.info("Configuration updated, reloaded %d loads", len(self.loads))

    def _update_loads_enabled_state(self) -> None:
        """Update enabled state for loads based on their enabled_entity_id.

        If a load has an enabled_entity_id configured, check the entity's state
        to determine if the load should be considered enabled for regulation.
        This allows dynamic control of load participation (e.g., only regulate
        an EV charger when the car is actually charging).
        """
        for load in self.loads:
            if load.enabled_entity_id:
                state = self.hass.states.get(load.enabled_entity_id)
                if state is None or state.state in ("unknown", "unavailable"):
                    # Entity not found or unavailable - default to disabled
                    _LOGGER.debug(
                        "Enabled entity %s for load %s is unavailable, treating as disabled",
                        load.enabled_entity_id,
                        load.name,
                    )
                    load.enabled = False
                else:
                    # Check if entity is "on" or "true"
                    entity_enabled = state.state.lower() in ("on", "true", "1")
                    _LOGGER.debug(
                        "Load %s: enabled_entity %s is %s, effective enabled=%s",
                        load.name,
                        load.enabled_entity_id,
                        state.state,
                        entity_enabled,
                    )
                    load.enabled = entity_enabled

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from sensors and calculate needed actions."""
        try:
            # Get current hour energy
            hour_energy_sensor = self.config[CONF_HOUR_ENERGY_SENSOR]
            hour_energy_state = self.hass.states.get(hour_energy_sensor)

            if not hour_energy_state or hour_energy_state.state in (
                "unknown",
                "unavailable",
            ):
                raise UpdateFailed(f"Energy sensor {hour_energy_sensor} unavailable")

            current_hour_kwh = float(hour_energy_state.state)

            # Get house power if available
            house_power_kw: float | None = None
            house_power_sensor = self.config.get(CONF_HOUSE_POWER_SENSOR)
            if house_power_sensor:
                house_power_state = self.hass.states.get(house_power_sensor)
                if house_power_state and house_power_state.state not in (
                    "unknown",
                    "unavailable",
                ):
                    # Convert W to kW if needed
                    power_value = float(house_power_state.state)
                    if house_power_state.attributes.get("unit_of_measurement") == "W":
                        house_power_kw = power_value / 1000.0
                    else:
                        house_power_kw = power_value

            # Calculate remaining seconds in current hour
            now = datetime.now()
            next_hour = (now + timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0
            )
            remaining_seconds = (next_hour - now).total_seconds()

            # Check for hour rollover
            if now.hour != self.last_hour:
                _LOGGER.info(
                    "Hour rollover detected: %d -> %d", self.last_hour, now.hour
                )
                self.last_hour = now.hour
                self.last_action = "Hour rollover"
                self.last_action_reason = "New hour started, resetting calculations"

            # Calculate projected end of hour kWh
            if house_power_kw is not None and remaining_seconds > 0:
                projected_end_kwh = current_hour_kwh + (
                    house_power_kw * remaining_seconds / 3600.0
                )
            else:
                # Conservative: assume current is final
                projected_end_kwh = current_hour_kwh

            # Get max limit and mode
            max_hour_kwh = self.config.get(CONF_MAX_HOUR_KWH, 5.0)
            mode = OperationMode(self.config.get(CONF_MODE, OperationMode.MONITOR))

            # Calculate needed reduction
            if remaining_seconds > 0:
                needed_reduction_kw = max(
                    0.0, (projected_end_kwh - max_hour_kwh) * 3600.0 / remaining_seconds
                )
            else:
                needed_reduction_kw = 0.0

            # Execute control actions if in control mode
            if mode == OperationMode.CONTROL:
                remaining_minutes = (
                    remaining_seconds / 60.0 if remaining_seconds else 0.0
                )
                await self._async_execute_control(
                    needed_reduction_kw,
                    max_hour_kwh,
                    projected_end_kwh,
                    house_power_kw,
                    remaining_minutes,
                )
            elif mode == OperationMode.OFF:
                # In OFF mode, restore all loads to original values
                await self._async_restore_all_loads("Mode is OFF")

            return {
                "current_hour_kwh": current_hour_kwh,
                "projected_end_kwh": projected_end_kwh,
                "needed_reduction_kw": needed_reduction_kw,
                "house_power_kw": house_power_kw,
                "remaining_seconds": remaining_seconds,
                "max_hour_kwh": max_hour_kwh,
                "mode": mode,
                "last_action": self.last_action,
                "last_action_reason": self.last_action_reason,
            }

        except ValueError as err:
            raise UpdateFailed(f"Error parsing sensor values: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Error updating data: {err}") from err

    async def _async_execute_control(
        self,
        needed_reduction_kw: float,
        max_hour_kwh: float,
        projected_end_kwh: float,
        current_power_kw: float | None = None,
        remaining_minutes: float | None = None,
    ) -> None:
        """Execute control actions based on needed reduction."""
        current_time = time.time()

        # Update effective enabled state for each load based on enabled_entity_id
        self._update_loads_enabled_state()

        # Use the pure function to calculate what to do
        decision = calculate_regulation_decision(
            loads=self.loads,
            needed_reduction_kw=needed_reduction_kw,
            projected_end_kwh=projected_end_kwh,
            max_hour_kwh=max_hour_kwh,
            current_time=current_time,
            current_power_kw=current_power_kw,
            remaining_minutes=remaining_minutes,
        )

        # Execute the decision
        if decision["action"] == "reduce":
            await self._async_execute_reductions(
                decision["loads_to_reduce"], current_time
            )
        elif decision["action"] == "restore":
            await self._async_execute_restorations(
                decision["loads_to_restore"],
                current_time,
                max_hour_kwh,
                projected_end_kwh,
            )

    async def _async_execute_reductions(
        self, reduction_plans: list[dict[str, Any]], current_time: float
    ) -> None:
        """Execute load reductions based on calculated plans."""
        actions_taken = []

        for plan in reduction_plans:
            load = plan["load"]

            # Execute the reduction
            reduction_achieved = await self._async_reduce_single_load(
                load, plan["needed_reduction"]
            )

            if reduction_achieved > 0:
                load.last_action_time = current_time
                actions_taken.append(f"{load.name}: -{reduction_achieved:.2f}kW")

        if actions_taken:
            self.last_action = "Reduced loads"
            self.last_action_reason = f"Actions: {', '.join(actions_taken)}"
            _LOGGER.info("Reduction actions: %s", self.last_action_reason)
        else:
            self.last_action = "Cannot reduce further"
            self.last_action_reason = "No loads could be reduced"
            _LOGGER.warning(self.last_action_reason)

    async def _async_reduce_single_load(
        self, load: Load, needed_reduction: float
    ) -> float:
        """Reduce a single load and return achieved reduction in kW."""
        if load.load_type == LoadType.EV_AMPERE:
            return await self._async_reduce_ev_load(load, needed_reduction)
        elif load.load_type == LoadType.SWITCH:
            return await self._async_reduce_switch_load(load)
        return 0.0

    async def _async_reduce_ev_load(self, load: Load, needed_reduction: float) -> float:
        """Reduce EV charger amperage."""
        if not load.ampere_number_entity_id:
            return 0.0

        state = self.hass.states.get(load.ampere_number_entity_id)
        if not state or state.state in ("unknown", "unavailable"):
            return 0.0

        current_ampere = float(state.state)

        # Get the actual min/max from the entity to avoid out_of_range errors
        entity_min = state.attributes.get("min", load.min_ampere)
        entity_max = state.attributes.get("max", load.max_ampere)

        if current_ampere <= entity_min:
            return 0.0  # Already at minimum

        # Get actual current power from power sensor if available
        current_power_kw = None
        power_per_ampere = None

        if load.power_sensor_entity_id:
            power_state = self.hass.states.get(load.power_sensor_entity_id)
            if power_state and power_state.state not in ("unknown", "unavailable"):
                try:
                    power_value = float(power_state.state)
                    unit = power_state.attributes.get("unit_of_measurement", "W")
                    current_power_kw = (
                        power_value / 1000.0 if unit == "W" else power_value
                    )
                    # Calculate and store power per ampere from actual measurement
                    # Only calculate if we have both power and amperage > 0
                    if current_ampere > 0 and current_power_kw > 0.1:
                        power_per_ampere = current_power_kw / current_ampere
                        # Store this measurement for future use
                        load.measured_power_per_ampere = power_per_ampere
                        _LOGGER.debug(
                            "%s: Measured %.2fkW at %dA = %.2fkW/A (stored for future use)",
                            load.name,
                            current_power_kw,
                            current_ampere,
                            power_per_ampere,
                        )
                    elif load.measured_power_per_ampere is not None:
                        # Use previously measured ratio if current power is zero/low
                        power_per_ampere = load.measured_power_per_ampere
                        current_power_kw = power_per_ampere * current_ampere
                        _LOGGER.debug(
                            "%s: Using stored power ratio %.2fkW/A (current power too low)",
                            load.name,
                            power_per_ampere,
                        )
                except (ValueError, TypeError) as err:
                    _LOGGER.warning(
                        "Failed to read power sensor for %s: %s", load.name, err
                    )

        # Use stored measured ratio if available, otherwise calculate from config
        if power_per_ampere is None:
            if load.measured_power_per_ampere is not None:
                power_per_ampere = load.measured_power_per_ampere
                current_power_kw = power_per_ampere * current_ampere
                _LOGGER.debug(
                    "%s: Using stored power ratio %.2fkW/A",
                    load.name,
                    power_per_ampere,
                )
            else:
                # Calculate from configured phases and voltage
                # For 1-phase: P = V × I
                # For 3-phase: P = √3 × V × I
                if load.phases == 3:
                    power_per_ampere = (load.voltage * 1.732) / 1000.0  # √3 ≈ 1.732
                else:
                    power_per_ampere = load.voltage / 1000.0
                current_power_kw = power_per_ampere * current_ampere
                _LOGGER.debug(
                    "%s: Using configured %dV %d-phase (%.2fkW/A)",
                    load.name,
                    load.voltage,
                    load.phases,
                    power_per_ampere,
                )

        # Calculate target ampere based on needed reduction
        # Target power = current power - needed reduction
        target_power_kw = max(0, current_power_kw - needed_reduction)
        target_ampere = (
            target_power_kw / power_per_ampere
            if power_per_ampere > 0
            else load.min_ampere
        )

        # Ensure we stay within bounds and round to integer
        new_ampere = max(entity_min, round(target_ampere))

        # Ensure we actually reduce (don't increase)
        if new_ampere >= current_ampere:
            # If proportional calc says don't reduce, reduce by 1A
            new_ampere = max(entity_min, current_ampere - 1)

        # Final bounds check to ensure we're within entity limits
        new_ampere = min(entity_max, max(entity_min, new_ampere))

        # Calculate actual reduction achieved using the power per ampere ratio
        actual_reduction_kw = (current_ampere - new_ampere) * power_per_ampere

        # Set new value
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {
                    "entity_id": load.ampere_number_entity_id,
                    "value": new_ampere,
                },
                blocking=True,
            )
            _LOGGER.info(
                "Reduced %s from %dA to %dA (needed %.2fkW, reducing %.2fkW)",
                load.name,
                current_ampere,
                new_ampere,
                needed_reduction,
                actual_reduction_kw,
            )
            return actual_reduction_kw
        except Exception as err:
            _LOGGER.error("Failed to reduce %s: %s", load.name, err)
            return 0.0

    async def _async_reduce_switch_load(self, load: Load) -> float:
        """Reduce switch load (turn off normal, turn on inverted)."""
        if not load.switch_entity_id:
            return 0.0

        state = self.hass.states.get(load.switch_entity_id)
        if not state:
            return 0.0

        # For inverted switches: power is consumed when OFF, so we turn ON to reduce
        # For normal switches: power is consumed when ON, so we turn OFF to reduce
        if load.switch_inverted:
            # Inverted: check if already ON (already reduced)
            if state.state == "on":
                return 0.0
            target_state = "on"
            target_service = "turn_on"
        else:
            # Normal: check if already OFF (already reduced)
            if state.state != "on":
                return 0.0
            target_state = "off"
            target_service = "turn_off"

        # Determine power reduction
        power_kw = 0.0
        if load.power_sensor_entity_id:
            power_state = self.hass.states.get(load.power_sensor_entity_id)
            if power_state and power_state.state not in ("unknown", "unavailable"):
                power_value = float(power_state.state)
                unit = power_state.attributes.get("unit_of_measurement", "W")
                power_kw = power_value / 1000.0 if unit == "W" else power_value
        elif load.assumed_power_kw:
            power_kw = load.assumed_power_kw

        # Execute switch action
        try:
            await self.hass.services.async_call(
                "switch",
                target_service,
                {"entity_id": load.switch_entity_id},
                blocking=True,
            )
            _LOGGER.info(
                "Switched %s to %s (estimated %.2fkW reduction)",
                load.name,
                target_state,
                power_kw,
            )
            return power_kw
        except Exception as err:
            _LOGGER.error("Failed to switch %s: %s", load.name, err)
            return 0.0

    async def _async_execute_restorations(
        self,
        loads_to_restore: list[Load],
        current_time: float,
        max_hour_kwh: float,
        projected_end_kwh: float,
    ) -> None:
        """Execute load restorations based on calculated plans."""
        actions_taken = []
        available_margin_kwh = max_hour_kwh - projected_end_kwh

        for load in loads_to_restore:
            # Try to restore this load (maximize consumption)
            if await self._async_restore_single_load(load, available_margin_kwh):
                load.last_action_time = current_time
                actions_taken.append(load.name)
                # Only restore one at a time to avoid overshooting
                break

        if actions_taken:
            self.last_action = "Restored loads"
            self.last_action_reason = (
                f"Sufficient margin, restored: {', '.join(actions_taken)}"
            )
            _LOGGER.info("Restore actions: %s", self.last_action_reason)

    async def _async_restore_single_load(
        self, load: Load, available_margin_kwh: float
    ) -> bool:
        """Restore a single load optimally based on available margin."""
        try:
            if load.load_type == LoadType.EV_AMPERE and load.ampere_number_entity_id:
                return await self._async_restore_ev_load(load, available_margin_kwh)
            elif load.load_type == LoadType.SWITCH and load.switch_entity_id:
                # Restore to max consumption state (ON for normal, OFF for inverted)
                state = self.hass.states.get(load.switch_entity_id)
                if not state:
                    return False

                # Determine target state for max consumption
                if load.switch_inverted:
                    # Inverted: consumes power when OFF
                    if state.state == "off":
                        return False  # Already at max consumption
                    restore_service = "turn_off"
                    target_state = "OFF"
                else:
                    # Normal: consumes power when ON
                    if state.state == "on":
                        return False  # Already at max consumption
                    restore_service = "turn_on"
                    target_state = "ON"

                await self.hass.services.async_call(
                    "switch",
                    restore_service,
                    {"entity_id": load.switch_entity_id},
                    blocking=True,
                )
                _LOGGER.info(
                    "Restored %s to %s (max consumption)", load.name, target_state
                )
                return True
        except Exception as err:
            _LOGGER.error("Failed to restore %s: %s", load.name, err)

        return False

    async def _async_restore_all_loads(self, reason: str) -> None:
        """Restore all loads to max consumption (used when mode is OFF)."""
        restored = []
        for load in self.loads:
            # Pass large margin since we're restoring everything to max
            if await self._async_restore_single_load(load, available_margin_kwh=999.0):
                restored.append(load.name)

        if restored:
            self.last_action = "Restored all loads"
            self.last_action_reason = f"{reason}. Restored: {', '.join(restored)}"
            _LOGGER.info(self.last_action_reason)

    async def _async_restore_ev_load(
        self, load: Load, available_margin_kwh: float
    ) -> bool:
        """Restore EV charger amperage optimally based on available margin."""
        if not load.ampere_number_entity_id:
            return False

        state = self.hass.states.get(load.ampere_number_entity_id)
        if not state or state.state in ("unknown", "unavailable"):
            return False

        current_ampere = float(state.state)

        # Get the actual min/max from the entity to avoid out_of_range errors
        entity_min = state.attributes.get("min", load.min_ampere)
        entity_max = state.attributes.get("max", load.max_ampere)

        # Get power per ampere ratio
        power_per_ampere = None

        # Try to measure current power if sensor is available
        if load.power_sensor_entity_id:
            power_state = self.hass.states.get(load.power_sensor_entity_id)
            if power_state and power_state.state not in ("unknown", "unavailable"):
                try:
                    power_value = float(power_state.state)
                    unit = power_state.attributes.get("unit_of_measurement", "W")
                    current_power_kw = (
                        power_value / 1000.0 if unit == "W" else power_value
                    )
                    # Only calculate ratio if we have meaningful readings
                    if current_ampere > 0 and current_power_kw > 0.1:
                        power_per_ampere = current_power_kw / current_ampere
                        # Update stored measurement
                        load.measured_power_per_ampere = power_per_ampere
                except (ValueError, TypeError) as err:
                    _LOGGER.warning(
                        "Failed to read power sensor for %s: %s", load.name, err
                    )

        # Use stored measured ratio if available and current measurement failed
        if power_per_ampere is None and load.measured_power_per_ampere is not None:
            power_per_ampere = load.measured_power_per_ampere
            _LOGGER.debug(
                "%s: Using stored power ratio %.2fkW/A for restoration",
                load.name,
                power_per_ampere,
            )

        # Fallback: use configured phases and voltage
        if power_per_ampere is None:
            power_per_ampere = (load.voltage * load.phases) / 1000.0  # kW per ampere
            _LOGGER.debug(
                "%s: Using configured %dV %d-phase (%.2fkW/A) for restoration",
                load.name,
                load.voltage,
                load.phases,
                power_per_ampere,
            )

        # Calculate time remaining in hour to convert margin to power
        now = datetime.now()
        next_hour = (now + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
        remaining_seconds = (next_hour - now).total_seconds()

        if remaining_seconds <= 0:
            remaining_seconds = 3600  # Default to full hour if calculation fails

        # Convert available margin (kWh) to available power (kW)
        # available_margin_kwh = power_kw * (remaining_seconds / 3600)
        # So: power_kw = available_margin_kwh * 3600 / remaining_seconds
        available_power_kw = (available_margin_kwh * 3600.0) / remaining_seconds

        # Calculate target amperage based on available power
        if power_per_ampere > 0:
            target_ampere = available_power_kw / power_per_ampere
        else:
            target_ampere = entity_max

        # Clamp to entity's min/max range and round to integer
        target_ampere = min(entity_max, max(entity_min, target_ampere))
        new_ampere = round(target_ampere)

        # Final bounds check to ensure we're within entity limits
        new_ampere = min(entity_max, max(entity_min, new_ampere))

        # Ensure we're actually increasing (don't decrease when restoring)
        if new_ampere <= current_ampere:
            _LOGGER.debug(
                "%s: Already at or above calculated target (%dA >= %dA)",
                load.name,
                current_ampere,
                new_ampere,
            )
            return False

        # Set new value
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {
                    "entity_id": load.ampere_number_entity_id,
                    "value": new_ampere,
                },
                blocking=True,
            )
            _LOGGER.info(
                "Restored %s from %dA to %dA (margin: %.2fkWh, available power: %.2fkW)",
                load.name,
                current_ampere,
                new_ampere,
                available_margin_kwh,
                available_power_kw,
            )
            return True
        except Exception as err:
            _LOGGER.error("Failed to restore %s: %s", load.name, err)
            return False
