"""Coordinator for Rvik Razor integration."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
import math
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_HOUSE_POWER_SENSOR,
    CONF_HOUR_ENERGY_SENSOR,
    CONF_LOADS,
    CONF_MAX_HOUR_KWH,
    CONF_MODE,
    DEFAULT_ACCURACY_DEADBAND_KWH,
    DEFAULT_COOLDOWN,
    DEFAULT_LATE_MIN_OFF,
    DEFAULT_LATE_MIN_ON,
    DEFAULT_LATE_OVERSHOOT_CAP_KWH,
    DEFAULT_LATE_START_SLACK_KW,
    DEFAULT_LATE_STOP_TOLERANCE_KW,
    DEFAULT_TRIM_DAILY_COMMAND_BUDGET,
    DEFAULT_TRIM_MARGIN_KW,
    DEFAULT_TRIM_MAX_AMPERE,
    DEFAULT_TRIM_MIN_AMPERE,
    DEFAULT_TRIM_RAISE_INTERVAL,
    DEFAULT_TRIM_RAISE_STEP,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    FALLBACK_MAX_AMPERE,
    FALLBACK_MIN_AMPERE,
    HOUR_ENERGY_RESET_MAX_WAIT,
    Load,
    LoadType,
    OperationMode,
    TRIM_CHARGING_THRESHOLD_KW,
    TRIM_END_OF_HOUR,
    TRIM_FOLLOW_HOLD,
    TRIM_IDENTIFY_INTERVAL,
    TRIM_IDENTIFY_MAX_ATTEMPTS,
    TRIM_LEARN_SETTLE,
    TRIM_RAISE_CONFIRM,
    TRIM_RAISE_HEADROOM,
    TRIM_VERIFY_DELAY,
    TRIM_VERIFY_TOLERANCE_KW,
)
from .vehicle import (
    STATUS_KEY,
    VehicleTrimSession,
    find_vehicle_entities,
    read_vehicle_snapshot,
    select_charging_vehicle,
)

_LOGGER = logging.getLogger(__name__)


def _calculate_nominal_power_per_ampere(load: Load) -> float:
    """Calculate nominal kW per ampere from configured electrical setup.

    For 1-phase AC: P = V * I
    For 3-phase AC: P = sqrt(3) * V * I
    """
    if load.phases == 3:
        return (load.voltage * 1.732) / 1000.0  # sqrt(3) ≈ 1.732
    return load.voltage / 1000.0


def _calculate_ev_power_per_ampere(load: Load) -> float:
    """Calculate EV kW per ampere, preferring current measured load power."""
    if (
        load.current_power_kw is not None
        and load.current_power_kw > 0
        and load.current_ampere is not None
        and load.current_ampere > 0
    ):
        return load.current_power_kw / load.current_ampere

    nominal_ratio = _calculate_nominal_power_per_ampere(load)
    if nominal_ratio > 0:
        return nominal_ratio

    if load.measured_power_per_ampere is not None:
        return load.measured_power_per_ampere

    return nominal_ratio


def is_hour_energy_reset_pending(
    current_hour_kwh: float,
    previous_hour_kwh: float | None,
    seconds_into_hour: float,
    max_wait: float = HOUR_ENERGY_RESET_MAX_WAIT,
) -> bool:
    """Return True while the hour energy sensor still shows the previous hour.

    Meters often reset their hour counter some seconds after the clock hour.
    Until the reading drops below the previous hour's last value, it belongs
    to the previous hour and would project far above the set point.
    """
    if previous_hour_kwh is None or previous_hour_kwh <= 0:
        return False
    if seconds_into_hour >= max_wait:
        return False
    return current_hour_kwh >= previous_hour_kwh


def calculate_available_down_capacity(loads: list[Load]) -> float:
    """Calculate total available downward control capacity.

    This determines how much power we could reduce if needed, based on
    currently active/consuming loads.

    Uses measured power from power_sensor_entity_id when available,
    falling back to assumed_power_kw or calculated power for EV chargers.

    Args:
        loads: List of Load objects with current state

    Returns:
        Total potential reduction capacity in kW
    """
    total_capacity = 0.0

    for load in loads:
        if not load.enabled:
            continue

        if load.load_type == LoadType.SWITCH:
            # For switches, check if it's in a state that can be reduced
            is_consuming = False
            if load.switch_inverted:
                # Inverted: OFF = consuming, ON = reduced
                is_consuming = load.current_switch_state == "off"
            else:
                # Normal: ON = consuming, OFF = reduced
                is_consuming = load.current_switch_state == "on"

            if is_consuming:
                # Prefer measured power, fall back to assumed power
                if load.current_power_kw is not None and load.current_power_kw > 0:
                    total_capacity += load.current_power_kw
                    _LOGGER.debug(
                        "Load %s: using measured power %.3f kW for capacity",
                        load.name,
                        load.current_power_kw,
                    )
                else:
                    total_capacity += load.assumed_power_kw or 0.0

        elif load.load_type == LoadType.EV_AMPERE:
            # For EV chargers, prefer measured power, then nominal calculation
            # from configured electrical setup.
            if load.current_ampere is not None and load.current_ampere > 0:
                power_per_ampere = _calculate_ev_power_per_ampere(load)
                if power_per_ampere > 0:
                    total_capacity += load.current_ampere * power_per_ampere
            elif load.current_power_kw is not None and load.current_power_kw > 0:
                total_capacity += load.current_power_kw
                _LOGGER.debug(
                    "Load %s: using measured power %.3f kW for capacity (ampere unavailable)",
                    load.name,
                    load.current_power_kw,
                )

    return total_capacity


def calculate_regulation_decision(
    loads: list[Load],
    current_hour_kwh: float,
    house_power_kw: float | None,
    hourly_set_point_kwh: float,
    remaining_seconds: float,
    current_time: float,
    cooldown: float = DEFAULT_COOLDOWN,
    accuracy_deadband_kwh: float = DEFAULT_ACCURACY_DEADBAND_KWH,
    late_start_slack_kw: float = DEFAULT_LATE_START_SLACK_KW,
    late_stop_tolerance_kw: float = DEFAULT_LATE_STOP_TOLERANCE_KW,
    late_overshoot_cap_kwh: float = DEFAULT_LATE_OVERSHOOT_CAP_KWH,
    late_min_on: float = DEFAULT_LATE_MIN_ON,
    late_min_off: float = DEFAULT_LATE_MIN_OFF,
    trim_margin_kw: float = DEFAULT_TRIM_MARGIN_KW,
    trim_raise_step: float = DEFAULT_TRIM_RAISE_STEP,
    trim_raise_interval: float = DEFAULT_TRIM_RAISE_INTERVAL,
) -> dict[str, Any]:
    """Calculate a projection-based regulation action.

    Late-hour loads are decided first by their own start/stop rule, which also
    trims the charge current of a vehicle charging on them. Otherwise
    the controller chooses direction from the current end-of-hour projection,
    then evaluates the first eligible load in priority order for that direction.
    It only acts when that load improves accuracy by more than the deadband.
    """
    remaining_hours = max(0.0, remaining_seconds / 3600.0)
    projected_end_kwh = (
        current_hour_kwh + house_power_kw * remaining_hours
        if house_power_kw is not None
        else current_hour_kwh
    )
    result = {
        "action": "none",
        "loads_to_reduce": [],
        "loads_to_restore": [],
        "remaining_reduction": 0.0,
        "projected_end_kwh": projected_end_kwh,
        "candidate_projected_end_kwh": None,
        "reason": "",
    }

    if house_power_kw is None:
        result["reason"] = "Cannot regulate: house power unavailable"
        return result

    if remaining_hours <= 0:
        result["reason"] = "No time remaining in current hour"
        return result

    late_plan, late_status = _calculate_late_hour_action(
        loads=[load for load in loads if load.enabled and _is_late_hour_load(load)],
        current_hour_kwh=current_hour_kwh,
        house_power_kw=house_power_kw,
        remaining_hours=remaining_hours,
        hourly_set_point_kwh=hourly_set_point_kwh,
        current_time=current_time,
        start_slack_kw=late_start_slack_kw,
        stop_tolerance_kw=late_stop_tolerance_kw,
        overshoot_cap_kwh=late_overshoot_cap_kwh,
        min_on=late_min_on,
        min_off=late_min_off,
        trim_margin_kw=trim_margin_kw,
        trim_raise_step=trim_raise_step,
        trim_raise_interval=trim_raise_interval,
    )
    if late_plan is not None:
        direction = late_plan["direction"]
        result["action"] = direction
        result["candidate_projected_end_kwh"] = late_plan["candidate_projected_end_kwh"]
        result["reason"] = late_status
        if direction == "reduce":
            result["loads_to_reduce"] = [late_plan]
            result["remaining_reduction"] = max(
                0.0, house_power_kw - late_plan["candidate_house_power_kw"]
            )
        else:
            result["loads_to_restore"] = [late_plan]
        return result

    decision = _calculate_horizon_decision(
        result=result,
        loads=[
            load for load in loads if load.enabled and not _is_late_hour_load(load)
        ],
        current_hour_kwh=current_hour_kwh,
        house_power_kw=house_power_kw,
        hourly_set_point_kwh=hourly_set_point_kwh,
        remaining_hours=remaining_hours,
        current_time=current_time,
        accuracy_deadband_kwh=accuracy_deadband_kwh,
    )
    if decision["action"] == "none" and late_status:
        decision["reason"] = f"{late_status}; {decision['reason']}"
    return decision


def _calculate_horizon_decision(
    result: dict[str, Any],
    loads: list[Load],
    current_hour_kwh: float,
    house_power_kw: float,
    hourly_set_point_kwh: float,
    remaining_hours: float,
    current_time: float,
    accuracy_deadband_kwh: float,
) -> dict[str, Any]:
    """Choose one horizon set-point action among non-late-hour loads."""
    projected_end_kwh = result["projected_end_kwh"]
    projection_delta = projected_end_kwh - hourly_set_point_kwh
    current_error = abs(projection_delta)
    if current_error <= accuracy_deadband_kwh:
        result["reason"] = (
            f"Within accuracy deadband (projected: {projected_end_kwh:.2f}kWh, "
            f"set point: {hourly_set_point_kwh:.2f}kWh)"
        )
        return result

    direction = "reduce" if projection_delta > 0 else "restore"
    sorted_loads = sorted(
        loads,
        key=lambda load: load.priority,
        reverse=direction == "restore",
    )

    for load in sorted_loads:
        load_timeout = load.timeout
        if current_time - load.last_action_time < load_timeout:
            _LOGGER.debug(
                "Load %s in cooldown for %s (%.0fs remaining)",
                load.name,
                direction,
                load_timeout - (current_time - load.last_action_time),
            )
            continue

        plan = _calculate_horizon_candidate(
            load=load,
            direction=direction,
            current_hour_kwh=current_hour_kwh,
            house_power_kw=house_power_kw,
            remaining_hours=remaining_hours,
            hourly_set_point_kwh=hourly_set_point_kwh,
        )
        if plan is None:
            continue

        candidate_error = abs(
            plan["candidate_projected_end_kwh"] - hourly_set_point_kwh
        )
        improvement = current_error - candidate_error
        if improvement > accuracy_deadband_kwh:
            result["action"] = direction
            result["candidate_projected_end_kwh"] = plan["candidate_projected_end_kwh"]
            result["reason"] = (
                f"{direction.title()} {load.name}: projected "
                f"{projected_end_kwh:.2f}kWh -> {plan['candidate_projected_end_kwh']:.2f}kWh"
            )
            if direction == "reduce":
                result["loads_to_reduce"] = [plan]
                result["remaining_reduction"] = max(
                    0.0, house_power_kw - plan["candidate_house_power_kw"]
                )
            else:
                result["loads_to_restore"] = [plan]
            return result

        result["reason"] = (
            f"First eligible load {load.name} would not improve accuracy enough "
            f"({improvement:.3f}kWh <= {accuracy_deadband_kwh:.3f}kWh); waiting"
        )
        return result

    result["reason"] = f"No eligible loads available to {direction}"

    return result


def _is_late_hour_load(load: Load) -> bool:
    """Return True if the load follows the late-hour rule."""
    return load.late_in_hour and load.load_type == LoadType.SWITCH


def _calculate_late_hour_action(
    loads: list[Load],
    current_hour_kwh: float,
    house_power_kw: float,
    remaining_hours: float,
    hourly_set_point_kwh: float,
    current_time: float,
    start_slack_kw: float,
    stop_tolerance_kw: float,
    overshoot_cap_kwh: float,
    min_on: float,
    min_off: float,
    trim_margin_kw: float = DEFAULT_TRIM_MARGIN_KW,
    trim_raise_step: float = DEFAULT_TRIM_RAISE_STEP,
    trim_raise_interval: float = DEFAULT_TRIM_RAISE_INTERVAL,
) -> tuple[dict[str, Any] | None, str]:
    """Decide whether a late-hour load should start, stop or trim.

    A late-hour load only starts once it can run at full power until the end of
    the hour, which pushes its consumption late in the hour. It stops when the
    rest of the house no longer leaves room for it, so it stays available to
    absorb consumption that turns up late in the hour.

    While a vehicle with a known charge current charges on the load, its
    current is fitted to the room instead, and the stop rule applies at the
    vehicle's minimum current. Nothing is decided for the load for a minute
    after its current changed or it started charging, while the vehicle
    follows.

    Returns the plan for at most one action (stops, then current reductions,
    then starts, then current raises) and a status describing what the
    late-hour loads are doing.
    """
    stops: list[dict[str, Any]] = []
    starts: list[dict[str, Any]] = []
    trim_reductions: list[dict[str, Any]] = []
    trim_raises: list[dict[str, Any]] = []
    statuses: list[str] = []

    for load in sorted(loads, key=lambda load: load.priority):
        if load.current_switch_state not in ("on", "off"):
            continue
        run_power = _estimate_late_load_run_power(load)
        if run_power is None:
            continue

        consuming_state = "off" if load.switch_inverted else "on"
        reduced_state = "on" if load.switch_inverted else "off"
        running = load.current_switch_state == consuming_state

        # A running load drawing less than its run power (finished, ramping) is
        # projected at what it draws now; the next cycle sees any increase.
        load_power_now = (
            load.current_power_kw
            if load.current_power_kw is not None
            else (run_power if running else 0.0)
        )
        base_power_kw = max(0.0, house_power_kw - load_power_now)
        room_kwh = hourly_set_point_kwh - (
            current_hour_kwh + base_power_kw * remaining_hours
        )
        available_kw = room_kwh / remaining_hours
        since_action = current_time - load.last_action_time

        plan_args = {
            "load": load,
            "plan_type": "switch",
            "current_load_power_kw": load_power_now,
            "current_hour_kwh": current_hour_kwh,
            "house_power_kw": house_power_kw,
            "remaining_hours": remaining_hours,
            "hourly_set_point_kwh": hourly_set_point_kwh,
        }

        trimmed = running and load.vehicle_ampere is not None
        if not trimmed:
            load.vehicle_raise_fits_since = None
        if trimmed and current_time < load.vehicle_settle_until:
            # The charger's power can lag the meter while the vehicle follows
            # a new current or ramps up, which skews the rest of the house.
            statuses.append(
                f"Waiting for {load.name} to follow its current "
                f"({load.vehicle_settle_until - current_time:.0f}s)"
            )
            continue
        lower_to_minimum = None
        if trimmed:
            trim_plan, trim_status = _calculate_vehicle_trim(
                load=load,
                plan_args=plan_args,
                available_kw=available_kw,
                current_time=current_time,
                margin_kw=trim_margin_kw,
                raise_step=trim_raise_step,
                raise_interval=trim_raise_interval,
            )
            if trim_plan is not None:
                (
                    trim_reductions
                    if trim_plan["direction"] == "reduce"
                    else trim_raises
                ).append(trim_plan)
                continue
            if trim_status is not None:
                statuses.append(trim_status)
                continue
            # Even the minimum current does not fit: apply the stop rule at
            # the minimum current, before spending a command to lower it.
            # Once at the minimum, what the vehicle draws is known.
            power_per_ampere = _vehicle_power_per_ampere(load)
            if load.vehicle_ampere > load.vehicle_min_ampere and power_per_ampere > 0:
                stop_power_kw = load.vehicle_min_ampere * power_per_ampere
                lower_to_minimum = _build_vehicle_trim_plan(
                    load,
                    plan_args,
                    int(round(load.vehicle_min_ampere)),
                    available_kw,
                )
            else:
                stop_power_kw = load_power_now
        else:
            stop_power_kw = load_power_now

        if running:
            projected_with_load_kwh = (
                current_hour_kwh + (base_power_kw + stop_power_kw) * remaining_hours
            )
            over_cap = (
                projected_with_load_kwh > hourly_set_point_kwh + overshoot_cap_kwh
            )
            short = available_kw < stop_power_kw - stop_tolerance_kw
            if over_cap or (short and since_action >= min_on):
                stops.append(
                    {
                        **_build_horizon_plan(
                            **plan_args,
                            candidate_load_power_kw=0.0,
                            new_value=reduced_state,
                            reduction_kw=load_power_now,
                        ),
                        "direction": "reduce",
                        "reason": (
                            f"Late-hour stop {load.name}: projected "
                            f"{projected_with_load_kwh:.2f}kWh while running, "
                            f"{available_kw:.2f}kW available"
                        ),
                    }
                )
            elif lower_to_minimum is not None:
                trim_reductions.append(lower_to_minimum)
            else:
                statuses.append(f"Running {load.name} late in hour")
            continue

        if since_action < min_off:
            statuses.append(
                f"{load.name} off for minimum off time "
                f"({min_off - since_action:.0f}s remaining)"
            )
        elif available_kw >= run_power + start_slack_kw:
            starts.append(
                {
                    **_build_horizon_plan(
                        **plan_args,
                        candidate_load_power_kw=run_power,
                        new_value=consuming_state,
                        reduction_kw=0.0,
                    ),
                    "direction": "restore",
                    "reason": (
                        f"Late-hour start {load.name}: {available_kw:.2f}kW "
                        f"available until end of hour"
                    ),
                }
            )
        else:
            statuses.append(
                f"Waiting to start {load.name} late in hour "
                f"(needs {run_power + start_slack_kw:.2f}kW, "
                f"{available_kw:.2f}kW available)"
            )

    if stops:
        return stops[0], stops[0]["reason"]
    if trim_reductions:
        return trim_reductions[0], trim_reductions[0]["reason"]
    if starts:
        # Highest priority number is restored first, as for horizon control.
        plan = starts[-1]
        return plan, plan["reason"]
    if trim_raises:
        plan = trim_raises[-1]
        return plan, plan["reason"]
    return None, "; ".join(statuses)


def _vehicle_power_per_ampere(load: Load) -> float:
    """Estimate kW per ampere for a vehicle charging on a late-hour load.

    Vehicles differ a lot per ampere on the same charger, so the value
    measured for the bound vehicle wins. Until it is measured, the load's
    assumed power is taken as its power at the maximum current, which errs
    high when the assumed power is sized for the most powerful vehicle.
    """
    if load.vehicle_power_per_ampere:
        return load.vehicle_power_per_ampere
    if load.assumed_power_kw and load.vehicle_max_ampere > 0:
        return load.assumed_power_kw / load.vehicle_max_ampere
    if load.current_power_kw and load.vehicle_ampere:
        return load.current_power_kw / load.vehicle_ampere
    return 0.0


def _session_power_per_ampere(session: VehicleTrimSession) -> float:
    """kW per ampere measured this charging stretch, else in the last one."""
    return session.power_per_ampere or session.previous_power_per_ampere


def _calculate_vehicle_trim(
    load: Load,
    plan_args: dict[str, Any],
    available_kw: float,
    current_time: float,
    margin_kw: float,
    raise_step: float,
    raise_interval: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Fit a charging vehicle's current to the room left in the hour.

    Lowers the current at once when it no longer fits, one ampere deeper near
    the end of the hour. Raises it, keeping an ampere spare, only when a full
    raise step has fitted for a minute and the last change is old enough, since
    every change is a billed vehicle command.

    Returns a trim plan, or no plan with a status while the current is kept.
    Returns neither when even the minimum current does not fit, so the caller
    applies the stop rule.
    """
    power_per_ampere = _vehicle_power_per_ampere(load)
    if power_per_ampere <= 0:
        return None, None

    current = int(round(load.vehicle_ampere or 0))
    minimum = int(round(load.vehicle_min_ampere))
    maximum = int(round(load.vehicle_max_ampere))
    # The small epsilon keeps an exact fit from rounding down a whole ampere.
    fitting = math.floor((available_kw - margin_kw) / power_per_ampere + 1e-9)

    # A raise keeps an ampere spare, so a small rise in the rest of the house
    # does not cut it straight back.
    raise_to = fitting - TRIM_RAISE_HEADROOM
    raise_fits = raise_to >= current + raise_step and current < maximum
    if not raise_fits:
        load.vehicle_raise_fits_since = None
    elif load.vehicle_raise_fits_since is None:
        load.vehicle_raise_fits_since = current_time

    if fitting < minimum:
        return None, None
    if fitting < current:
        # Near the end of the hour each ampere is worth less energy, so cut
        # deeper rather than follow the room down one command at a time.
        if plan_args["remaining_hours"] * 3600 < TRIM_END_OF_HOUR:
            fitting -= 1
        target = max(minimum, fitting)
    elif (
        raise_fits
        and current_time - load.vehicle_raise_fits_since >= TRIM_RAISE_CONFIRM
        and current_time - load.vehicle_ampere_changed_time >= raise_interval
    ):
        target = min(maximum, raise_to)
    else:
        return None, (
            f"Running {load.name} at {current}A late in hour "
            f"({available_kw:.2f}kW available)"
        )
    return _build_vehicle_trim_plan(load, plan_args, target, available_kw), None


def _build_vehicle_trim_plan(
    load: Load, plan_args: dict[str, Any], target: int, available_kw: float
) -> dict[str, Any]:
    """Build the plan that sets a charging vehicle's current to target."""
    current = int(round(load.vehicle_ampere or 0))
    candidate_power_kw = target * _vehicle_power_per_ampere(load)
    current_power_kw = plan_args["current_load_power_kw"]
    return {
        **_build_horizon_plan(
            **plan_args,
            candidate_load_power_kw=candidate_power_kw,
            new_value=target,
            reduction_kw=max(0.0, current_power_kw - candidate_power_kw),
        ),
        "type": "vehicle_ampere",
        "direction": "reduce" if target < current else "restore",
        "reason": (
            f"Late-hour trim {load.name}: {current}A -> {target}A, "
            f"{available_kw:.2f}kW available"
        ),
    }


def _estimate_late_load_run_power(load: Load) -> float | None:
    """Estimate a late-hour load's power when running, erring high.

    For a bound vehicle whose kW per ampere is measured, full power is that
    rate at its maximum current rather than the load's assumed power.
    """
    full_power_kw = (
        load.vehicle_power_per_ampere * load.vehicle_max_ampere
        if load.vehicle_power_per_ampere
        else load.assumed_power_kw
    )
    estimates = [
        value
        for value in (load.current_power_kw, full_power_kw)
        if value is not None and value > 0
    ]
    return max(estimates) if estimates else None


def _estimate_switch_consuming_power(load: Load) -> float | None:
    """Estimate switch power when it is in its consuming state."""
    if load.current_power_kw is not None and load.current_power_kw > 0:
        return load.current_power_kw
    if load.assumed_power_kw is not None and load.assumed_power_kw > 0:
        return load.assumed_power_kw
    return None


def _calculate_switch_power_for_state(load: Load, state: str) -> float | None:
    """Estimate switch load power for a given switch state."""
    if state not in ("on", "off"):
        return None

    consuming_state = "off" if load.switch_inverted else "on"
    if state == consuming_state:
        return _estimate_switch_consuming_power(load)
    return 0.0


def _calculate_horizon_candidate(
    load: Load,
    direction: str,
    current_hour_kwh: float,
    house_power_kw: float,
    remaining_hours: float,
    hourly_set_point_kwh: float,
) -> dict[str, Any] | None:
    """Calculate the best candidate state for one load in one direction."""
    if load.load_type == LoadType.SWITCH:
        return _calculate_switch_horizon_candidate(
            load,
            direction,
            current_hour_kwh,
            house_power_kw,
            remaining_hours,
            hourly_set_point_kwh,
        )
    if load.load_type == LoadType.EV_AMPERE:
        return _calculate_ev_horizon_candidate(
            load,
            direction,
            current_hour_kwh,
            house_power_kw,
            remaining_hours,
            hourly_set_point_kwh,
        )
    return None


def _build_horizon_plan(
    load: Load,
    plan_type: str,
    current_load_power_kw: float,
    candidate_load_power_kw: float,
    current_hour_kwh: float,
    house_power_kw: float,
    remaining_hours: float,
    hourly_set_point_kwh: float,
    **extra: Any,
) -> dict[str, Any]:
    """Build a regulation plan from a load power candidate."""
    candidate_house_power_kw = max(
        0.0, house_power_kw - current_load_power_kw + candidate_load_power_kw
    )
    candidate_projected_end_kwh = (
        current_hour_kwh + candidate_house_power_kw * remaining_hours
    )
    return {
        "load": load,
        "type": plan_type,
        "current_load_power_kw": current_load_power_kw,
        "candidate_load_power_kw": candidate_load_power_kw,
        "candidate_house_power_kw": candidate_house_power_kw,
        "candidate_projected_end_kwh": candidate_projected_end_kwh,
        "candidate_error_kwh": abs(
            candidate_projected_end_kwh - hourly_set_point_kwh
        ),
        **extra,
    }


def _calculate_switch_horizon_candidate(
    load: Load,
    direction: str,
    current_hour_kwh: float,
    house_power_kw: float,
    remaining_hours: float,
    hourly_set_point_kwh: float,
) -> dict[str, Any] | None:
    """Calculate a binary switch candidate for horizon control."""
    if load.current_switch_state not in ("on", "off"):
        return None

    if direction == "reduce":
        target_state = "on" if load.switch_inverted else "off"
    else:
        target_state = "off" if load.switch_inverted else "on"

    if load.current_switch_state == target_state:
        return None

    current_power = _calculate_switch_power_for_state(load, load.current_switch_state)
    candidate_power = _calculate_switch_power_for_state(load, target_state)
    if current_power is None or candidate_power is None:
        return None

    return _build_horizon_plan(
        load=load,
        plan_type="switch",
        current_load_power_kw=current_power,
        candidate_load_power_kw=candidate_power,
        current_hour_kwh=current_hour_kwh,
        house_power_kw=house_power_kw,
        remaining_hours=remaining_hours,
        hourly_set_point_kwh=hourly_set_point_kwh,
        new_value=target_state,
        reduction_kw=max(0.0, current_power - candidate_power),
    )


def _calculate_ev_horizon_candidate(
    load: Load,
    direction: str,
    current_hour_kwh: float,
    house_power_kw: float,
    remaining_hours: float,
    hourly_set_point_kwh: float,
) -> dict[str, Any] | None:
    """Calculate the best EV ampere candidate for horizon control."""
    if load.current_ampere is None:
        return None

    power_per_ampere = _calculate_ev_power_per_ampere(load)
    if power_per_ampere <= 0:
        return None

    current_ampere = int(round(load.current_ampere))
    current_power = load.current_ampere * power_per_ampere
    entity_min = int(round(load.current_min_ampere or FALLBACK_MIN_AMPERE))
    entity_max = int(round(load.current_max_ampere or FALLBACK_MAX_AMPERE))

    if direction == "reduce":
        candidate_values = range(entity_min, current_ampere)
    else:
        candidate_values = range(current_ampere + 1, entity_max + 1)

    best_plan: dict[str, Any] | None = None
    for candidate_ampere in candidate_values:
        candidate_power = candidate_ampere * power_per_ampere
        plan = _build_horizon_plan(
            load=load,
            plan_type="ev_ampere",
            current_load_power_kw=current_power,
            candidate_load_power_kw=candidate_power,
            current_hour_kwh=current_hour_kwh,
            house_power_kw=house_power_kw,
            remaining_hours=remaining_hours,
            hourly_set_point_kwh=hourly_set_point_kwh,
            new_value=candidate_ampere,
            reduction_kw=max(0.0, current_power - candidate_power),
            power_per_ampere=power_per_ampere,
        )
        if (
            best_plan is None
            or plan["candidate_error_kwh"] < best_plan["candidate_error_kwh"]
        ):
            best_plan = plan

    return best_plan


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
        self._last_hour_kwh: float | None = None
        self._previous_hour_kwh: float | None = None
        self._trim_sessions: dict[str, VehicleTrimSession] = {}
        self._trim_commands_today = 0
        self._trim_budget_day = date.today()

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

    def _update_loads_runtime_state(self) -> None:
        """Update runtime state for loads from Home Assistant entities.

        Updates:
        - enabled state from enabled_entity_id
        - current_switch_state for switch loads
        - current_ampere for EV loads

        This ensures the pure regulation functions have current state information.
        """
        for load in self.loads:
            # Update enabled state from enabled_entity_id
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

            # Update current switch state for switch loads
            if load.load_type == LoadType.SWITCH and load.switch_entity_id:
                switch_state = self.hass.states.get(load.switch_entity_id)
                if switch_state and switch_state.state not in (
                    "unknown",
                    "unavailable",
                ):
                    load.current_switch_state = switch_state.state
                    _LOGGER.debug(
                        "Load %s: switch state is %s (inverted=%s)",
                        load.name,
                        load.current_switch_state,
                        load.switch_inverted,
                    )
                else:
                    load.current_switch_state = None
                    _LOGGER.debug(
                        "Load %s: switch entity %s unavailable",
                        load.name,
                        load.switch_entity_id,
                    )

            # Update current ampere for EV loads
            if load.load_type == LoadType.EV_AMPERE and load.ampere_number_entity_id:
                ampere_state = self.hass.states.get(load.ampere_number_entity_id)
                if ampere_state and ampere_state.state not in (
                    "unknown",
                    "unavailable",
                ):
                    try:
                        load.current_ampere = float(ampere_state.state)
                        load.current_min_ampere = float(
                            ampere_state.attributes.get("min", FALLBACK_MIN_AMPERE)
                        )
                        load.current_max_ampere = float(
                            ampere_state.attributes.get("max", FALLBACK_MAX_AMPERE)
                        )
                    except (ValueError, TypeError):
                        load.current_ampere = None
                        load.current_min_ampere = None
                        load.current_max_ampere = None
                else:
                    load.current_ampere = None
                    load.current_min_ampere = None
                    load.current_max_ampere = None

            # Update current measured power from power sensor if configured
            if load.power_sensor_entity_id:
                power_state = self.hass.states.get(load.power_sensor_entity_id)
                if power_state and power_state.state not in (
                    "unknown",
                    "unavailable",
                ):
                    try:
                        power_value = float(power_state.state)
                        # Convert W to kW if needed
                        if power_state.attributes.get("unit_of_measurement") == "W":
                            load.current_power_kw = power_value / 1000.0
                        else:
                            load.current_power_kw = power_value
                        _LOGGER.debug(
                            "Load %s: measured power = %.3f kW",
                            load.name,
                            load.current_power_kw,
                        )
                    except (ValueError, TypeError):
                        load.current_power_kw = None
                else:
                    load.current_power_kw = None
            else:
                load.current_power_kw = None

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
                self._previous_hour_kwh = self._last_hour_kwh
                self.last_action = "Hour rollover"
                self.last_action_reason = "New hour started, resetting calculations"

            hour_reset_pending = is_hour_energy_reset_pending(
                current_hour_kwh,
                self._previous_hour_kwh,
                seconds_into_hour=3600.0 - remaining_seconds,
            )
            self._last_hour_kwh = current_hour_kwh

            # Calculate projected end of hour kWh
            if house_power_kw is not None and remaining_seconds > 0:
                projected_end_kwh = current_hour_kwh + (
                    house_power_kw * remaining_seconds / 3600.0
                )
            else:
                # Projection is unavailable without a current power baseline.
                projected_end_kwh = current_hour_kwh

            # Get hourly set point and mode
            max_hour_kwh = self.config.get(CONF_MAX_HOUR_KWH, 5.0)
            mode = OperationMode(self.config.get(CONF_MODE, OperationMode.MONITOR))

            # Update runtime state to get current load states for capacity calculation
            self._update_loads_runtime_state()

            available_down_capacity_kw = calculate_available_down_capacity(self.loads)

            _LOGGER.debug(
                "Set-point strategy: set_point=%.2fkWh, projected=%.2fkWh, down_capacity=%.2fkW",
                max_hour_kwh,
                projected_end_kwh,
                available_down_capacity_kw,
            )

            projection_error_kwh = projected_end_kwh - max_hour_kwh

            # Execute control actions if in control mode
            if mode == OperationMode.CONTROL:
                if house_power_kw is None:
                    self.last_action = "Cannot regulate"
                    self.last_action_reason = "House power sensor unavailable"
                elif hour_reset_pending:
                    self.last_action = "Waiting for hour reset"
                    self.last_action_reason = (
                        f"Hour energy sensor still shows the previous hour "
                        f"({current_hour_kwh:.2f}kWh)"
                    )
                else:
                    await self._async_update_vehicle_trim(time.time())
                    await self._async_execute_control(
                        current_hour_kwh,
                        max_hour_kwh,
                        house_power_kw,
                        remaining_seconds,
                    )
            elif mode == OperationMode.OFF:
                # In OFF mode, restore all loads to original values
                await self._async_restore_all_loads("Mode is OFF")

            return {
                "current_hour_kwh": current_hour_kwh,
                "projected_end_kwh": projected_end_kwh,
                "projection_error_kwh": projection_error_kwh,
                "house_power_kw": house_power_kw,
                "remaining_seconds": remaining_seconds,
                "max_hour_kwh": max_hour_kwh,
                "available_down_capacity_kw": available_down_capacity_kw,
                "mode": mode,
                "last_action": self.last_action,
                "last_action_reason": self.last_action_reason,
                "vehicle_trim": "; ".join(
                    f"{name}: {session.status}"
                    for name, session in self._trim_sessions.items()
                    if session.status
                ),
                "vehicle_commands_today": self._trim_commands_today,
            }

        except ValueError as err:
            raise UpdateFailed(f"Error parsing sensor values: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Error updating data: {err}") from err

    async def _async_execute_control(
        self,
        current_hour_kwh: float,
        hourly_set_point_kwh: float,
        house_power_kw: float,
        remaining_seconds: float,
    ) -> None:
        """Execute horizon set-point control actions.

        Args:
            current_hour_kwh: Energy used so far this hour
            hourly_set_point_kwh: Target energy consumption for the hour
            house_power_kw: Current instant power consumption
            remaining_seconds: Seconds remaining in the current hour
        """
        current_time = time.time()
        projected_end_kwh = current_hour_kwh + house_power_kw * (
            remaining_seconds / 3600.0
        )

        # Log current state before decision
        _LOGGER.debug(
            "Control state: projected=%.2fkWh, set_point=%.2fkWh, error=%.2fkWh, "
            "current_power=%.2fkW, remaining_mins=%.1f",
            projected_end_kwh,
            hourly_set_point_kwh,
            projected_end_kwh - hourly_set_point_kwh,
            house_power_kw,
            remaining_seconds / 60.0,
        )

        # Use the pure function to calculate what to do
        decision = calculate_regulation_decision(
            loads=self.loads,
            current_hour_kwh=current_hour_kwh,
            house_power_kw=house_power_kw,
            hourly_set_point_kwh=hourly_set_point_kwh,
            remaining_seconds=remaining_seconds,
            current_time=current_time,
        )

        # Log decision details
        _LOGGER.debug(
            "Regulation decision: action=%s, reason=%s",
            decision["action"],
            decision["reason"],
        )
        if decision["loads_to_restore"]:
            load_names = [p["load"].name for p in decision["loads_to_restore"]]
            _LOGGER.debug("Loads selected for restoration: %s", load_names)
        if decision["loads_to_reduce"]:
            load_names = [p["load"].name for p in decision["loads_to_reduce"]]
            _LOGGER.debug("Loads selected for reduction: %s", load_names)

        # Execute the decision
        if decision["action"] == "reduce":
            await self._async_execute_reductions(
                decision["loads_to_reduce"], current_time
            )
        elif decision["action"] == "restore":
            await self._async_execute_restorations(
                decision["loads_to_restore"],
                current_time,
            )
        else:
            self.last_action = "No action"
            self.last_action_reason = decision["reason"]

    async def _async_update_vehicle_trim(self, now: float) -> None:
        """Track which vehicle charges on each late-hour load and expose its current.

        Sets load.vehicle_ampere only while a bound vehicle is drawing power, so
        commands only reach an awake vehicle, and only while the daily command
        budget lasts. Otherwise the load runs on/off only.
        """
        if date.today() != self._trim_budget_day:
            self._trim_budget_day = date.today()
            self._trim_commands_today = 0

        for load in self.loads:
            load.vehicle_ampere = None
            load.vehicle_power_per_ampere = None
            if not (
                load.enabled
                and _is_late_hour_load(load)
                and load.vehicle_ampere_entity_ids
            ):
                continue
            session = self._trim_sessions.setdefault(load.name, VehicleTrimSession())

            # The charger switch is unavailable while no vehicle is plugged in.
            if load.current_switch_state is None:
                session.unplugged_cycles += 1
                if session.unplugged_cycles >= 2:
                    await self._async_end_trim_session(load, session)
                continue
            session.unplugged_cycles = 0

            charging = (load.current_power_kw or 0.0) >= TRIM_CHARGING_THRESHOLD_KW
            if not charging:
                # The stretch is over. Its rate predicts the next start.
                if session.power_per_ampere:
                    session.previous_power_per_ampere = session.power_per_ampere
                    session.power_per_ampere = 0.0
                session.charging_since = None
            elif session.charging_since is None:
                session.charging_since = now
            if session.ampere_entity_id is None:
                if (
                    charging
                    and not session.failed
                    and session.attempts < TRIM_IDENTIFY_MAX_ATTEMPTS
                    and now - session.last_attempt_time >= TRIM_IDENTIFY_INTERVAL
                ):
                    await self._async_identify_vehicle(load, session, now)
                if session.ampere_entity_id is None:
                    continue

            if session.verify_after is not None and now >= session.verify_after:
                session.verify_after = None
                if (load.current_power_kw or 0.0) > session.verify_max_power_kw:
                    _LOGGER.warning(
                        "%s did not follow the charge current of %s; "
                        "using on/off control until unplugged",
                        load.name,
                        session.ampere_entity_id,
                    )
                    session.ampere_entity_id = None
                    session.failed = True
                    session.status = "Charger did not follow the vehicle current"
                    continue

            # Measure kW per ampere once the current has settled and the
            # vehicle has ramped up after the charger started. At an unchanged
            # current keep the highest value, since a vehicle tapering off
            # draws less than its current allows but never more.
            settled = charging and now - session.changed_time >= TRIM_LEARN_SETTLE
            if (
                settled
                and session.ampere > 0
                and now - session.bound_time >= TRIM_LEARN_SETTLE
                and now - session.charging_since >= TRIM_LEARN_SETTLE
            ):
                measured = (load.current_power_kw or 0.0) / session.ampere
                if session.measured_ampere == session.ampere:
                    measured = max(session.power_per_ampere, measured)
                session.power_per_ampere = measured
                session.measured_ampere = session.ampere
            power_per_ampere = _session_power_per_ampere(session)
            if (
                settled
                and not session.power_per_ampere
                and power_per_ampere
                and session.ampere > 0
            ):
                # Until this stretch is measured, a vehicle drawing more than
                # the last stretch's rate is taken at what it draws.
                power_per_ampere = max(
                    power_per_ampere, (load.current_power_kw or 0.0) / session.ampere
                )
            load.vehicle_power_per_ampere = power_per_ampere or None
            load.vehicle_settle_until = max(
                session.settle_until,
                (session.charging_since or 0.0) + TRIM_FOLLOW_HOLD,
            )
            load.vehicle_min_ampere = session.min_ampere
            load.vehicle_max_ampere = session.max_ampere

            if not charging:
                continue
            if self._trim_commands_today >= DEFAULT_TRIM_DAILY_COMMAND_BUDGET:
                session.status = "Daily vehicle command budget used"
                continue

            load.vehicle_ampere = session.ampere
            load.vehicle_ampere_changed_time = session.changed_time

    async def _async_identify_vehicle(
        self, load: Load, session: VehicleTrimSession, now: float
    ) -> None:
        """Refresh the vehicles once and bind the one charging on this load."""
        session.attempts += 1
        session.last_attempt_time = now
        try:
            await self.hass.services.async_call(
                "homeassistant",
                "update_entity",
                {"entity_id": list(load.vehicle_ampere_entity_ids)},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001
            session.status = f"Could not refresh vehicles: {err}"
            _LOGGER.warning("%s: %s", load.name, session.status)
            return

        snapshots = [
            read_vehicle_snapshot(
                self.hass, entity_id, find_vehicle_entities(self.hass, entity_id)
            )
            for entity_id in load.vehicle_ampere_entity_ids
        ]
        entity_id, reason = select_charging_vehicle(
            snapshots, load.current_power_kw or 0.0
        )
        session.status = reason
        if entity_id is None:
            _LOGGER.info("%s: %s", load.name, reason)
            return

        state = self.hass.states.get(entity_id)
        try:
            ampere = float(state.state)
        except (AttributeError, TypeError, ValueError):
            session.status = f"Charge current of {entity_id} is unknown"
            return

        session.ampere_entity_id = entity_id
        session.ampere = ampere
        session.min_ampere = max(
            DEFAULT_TRIM_MIN_AMPERE,
            float(state.attributes.get("min", DEFAULT_TRIM_MIN_AMPERE)),
        )
        session.max_ampere = min(
            DEFAULT_TRIM_MAX_AMPERE,
            float(state.attributes.get("max", DEFAULT_TRIM_MAX_AMPERE)),
        )
        session.changed_time = 0.0
        session.bound_time = now
        _LOGGER.info("%s: %s at %.0fA", load.name, reason, ampere)

    async def _async_set_vehicle_ampere(self, load: Load, target: int) -> bool:
        """Send a charge current command to the vehicle bound to a load."""
        session = self._trim_sessions.get(load.name)
        if session is None or session.ampere_entity_id is None:
            return False

        # Commanding a vehicle the integration believes is asleep would wake
        # it first, which is billed far higher than a command.
        status_entity = find_vehicle_entities(
            self.hass, session.ampere_entity_id
        ).get(STATUS_KEY)
        status = self.hass.states.get(status_entity) if status_entity else None
        if status is not None and status.state == "off":
            session.status = "Vehicle is asleep; not sending a command"
            return False

        self._trim_commands_today += 1
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": session.ampere_entity_id, "value": target},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to set %s to %sA: %s; using on/off control until unplugged",
                session.ampere_entity_id,
                target,
                err,
            )
            session.ampere_entity_id = None
            session.failed = True
            session.status = f"Charge current command failed: {err}"
            return False

        previous = session.ampere
        session.ampere = float(target)
        session.changed_time = time.time()
        session.settle_until = session.changed_time + TRIM_FOLLOW_HOLD
        session.status = f"{session.ampere_entity_id} at {target}A"
        if power_per_ampere := _session_power_per_ampere(session):
            session.status += f" ({power_per_ampere:.2f}kW/A)"
        if target < previous:
            session.lowered = True
            session.verify_after = session.changed_time + TRIM_VERIFY_DELAY
            session.verify_max_power_kw = (
                target * _vehicle_power_per_ampere(load) + TRIM_VERIFY_TOLERANCE_KW
            )
        _LOGGER.info(
            "Set %s charge current %.0fA -> %dA", load.name, previous, target
        )
        return True

    async def _async_end_trim_session(
        self, load: Load, session: VehicleTrimSession
    ) -> None:
        """Forget the bound vehicle once unplugged, restoring a lowered current.

        Vehicles remember their charge current per location, so a lowered
        current would otherwise apply to the next unmanaged charge at home.
        """
        if (
            session.ampere_entity_id is not None
            and session.lowered
            and session.ampere < session.max_ampere
        ):
            self._trim_commands_today += 1
            try:
                await self.hass.services.async_call(
                    "number",
                    "set_value",
                    {
                        "entity_id": session.ampere_entity_id,
                        "value": int(session.max_ampere),
                    },
                    blocking=True,
                )
                _LOGGER.info(
                    "%s unplugged; restored %s to %.0fA",
                    load.name,
                    session.ampere_entity_id,
                    session.max_ampere,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.info(
                    "Could not restore %s after unplug: %s",
                    session.ampere_entity_id,
                    err,
                )
        self._trim_sessions[load.name] = VehicleTrimSession()

    async def _async_execute_reductions(
        self, reduction_plans: list[dict[str, Any]], current_time: float
    ) -> None:
        """Execute load reductions based on calculated plans."""
        actions_taken = []
        failed_actions = []

        _LOGGER.info(
            "Evaluating %d reduction plans (max 1 successful action this loop): %s",
            len(reduction_plans),
            [p["load"].name for p in reduction_plans],
        )

        for plan in reduction_plans:
            load = plan["load"]

            if await self._async_reduce_single_load(plan):
                if plan.get("type") != "vehicle_ampere":
                    load.last_action_time = current_time
                actions_taken.append(
                    f"{load.name}: -{plan.get('reduction_kw', 0.0):.2f}kW"
                )
                # Only reduce one load per control loop to avoid overshooting.
                break
            else:
                failed_actions.append(load.name)
                _LOGGER.warning(
                    "Load %s was planned for reduction but achieved 0kW (already reduced, unavailable, or failed)",
                    load.name,
                )

        if actions_taken:
            self.last_action = "Reduced loads"
            self.last_action_reason = f"Actions: {', '.join(actions_taken)}"
            _LOGGER.info("Reduction actions: %s", self.last_action_reason)
        else:
            self.last_action = "Cannot reduce further"
            self.last_action_reason = "No loads could be reduced"
            _LOGGER.warning(self.last_action_reason)

        if failed_actions:
            _LOGGER.warning(
                "Failed/skipped reductions: %s - these loads were planned but achieved 0kW",
                failed_actions,
            )

    async def _async_reduce_single_load(self, plan: dict[str, Any]) -> bool:
        """Reduce a single load and return whether it was changed."""
        load = plan["load"]
        if plan.get("type") == "vehicle_ampere":
            return await self._async_set_vehicle_ampere(load, plan["new_value"])
        if load.load_type == LoadType.EV_AMPERE:
            return (
                await self._async_set_ev_load(load, plan["new_value"], "reduce") > 0
            )
        elif load.load_type == LoadType.SWITCH:
            return await self._async_set_switch_load(load, plan["new_value"])
        return False

    async def _async_set_ev_load(
        self, load: Load, target_ampere: float, direction: str
    ) -> float:
        """Set EV charger amperage and return absolute power change in kW."""
        if not load.ampere_number_entity_id:
            return 0.0

        state = self.hass.states.get(load.ampere_number_entity_id)
        if not state or state.state in ("unknown", "unavailable"):
            return 0.0

        current_ampere = float(state.state)

        # Get the actual min/max from the entity (these can change dynamically)
        entity_min = state.attributes.get("min", FALLBACK_MIN_AMPERE)
        entity_max = state.attributes.get("max", FALLBACK_MAX_AMPERE)

        if current_ampere <= entity_min:
            if direction == "reduce":
                return 0.0  # Already at minimum

        load.current_ampere = current_ampere

        if load.power_sensor_entity_id:
            power_state = self.hass.states.get(load.power_sensor_entity_id)
            if power_state and power_state.state not in ("unknown", "unavailable"):
                try:
                    power_value = float(power_state.state)
                    unit = power_state.attributes.get("unit_of_measurement", "W")
                    load.current_power_kw = (
                        power_value / 1000.0 if unit == "W" else power_value
                    )
                except (ValueError, TypeError) as err:
                    _LOGGER.warning(
                        "Failed to read power sensor for %s: %s", load.name, err
                    )

        power_per_ampere = _calculate_ev_power_per_ampere(load)
        if power_per_ampere <= 0:
            return 0.0

        _LOGGER.debug(
            "%s: Using %.2fkW/A for %s",
            load.name,
            power_per_ampere,
            direction,
        )

        new_ampere = min(entity_max, max(entity_min, round(target_ampere)))
        if direction == "reduce" and new_ampere >= current_ampere:
            return 0.0
        if direction == "restore" and new_ampere <= current_ampere:
            return 0.0

        # Calculate actual reduction achieved using the power per ampere ratio
        actual_change_kw = abs(current_ampere - new_ampere) * power_per_ampere

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
                "%s %s from %dA to %dA (%.2fkW change)",
                direction.title(),
                load.name,
                current_ampere,
                new_ampere,
                actual_change_kw,
            )
            return actual_change_kw
        except Exception as err:
            _LOGGER.error("Failed to %s %s: %s", direction, load.name, err)
            return 0.0

    async def _async_set_switch_load(self, load: Load, target_state: str) -> bool:
        """Set switch load to a target state and return whether it was changed.

        Success does not depend on measured power: a switch being turned on
        typically reads 0 W until it starts consuming.
        """
        if not load.switch_entity_id:
            _LOGGER.warning(
                "Load %s has no switch_entity_id configured, cannot regulate",
                load.name,
            )
            return False

        state = self.hass.states.get(load.switch_entity_id)
        if not state:
            _LOGGER.warning(
                "Load %s: switch entity %s not found or unavailable",
                load.name,
                load.switch_entity_id,
            )
            return False

        _LOGGER.debug(
            "Load %s: switch_entity=%s, state=%s, inverted=%s",
            load.name,
            load.switch_entity_id,
            state.state,
            load.switch_inverted,
        )

        if state.state == target_state:
            return False

        target_service = "turn_on" if target_state == "on" else "turn_off"
        power_kw = _estimate_switch_consuming_power(load) or 0.0

        # Execute switch action
        try:
            await self.hass.services.async_call(
                "switch",
                target_service,
                {"entity_id": load.switch_entity_id},
                blocking=True,
            )
            _LOGGER.info(
                "Switched %s to %s (estimated %.2fkW change)",
                load.name,
                target_state,
                power_kw,
            )
            return True
        except Exception as err:
            _LOGGER.error("Failed to switch %s: %s", load.name, err)
            return False

    async def _async_execute_restorations(
        self,
        loads_to_restore: list[dict[str, Any]],
        current_time: float,
    ) -> None:
        """Execute load restorations based on calculated plans."""
        actions_taken = []

        _LOGGER.debug(
            "Executing restorations: %d loads to restore",
            len(loads_to_restore),
        )

        for plan in loads_to_restore:
            load = plan["load"]
            _LOGGER.debug(
                "Attempting to restore load %s (type=%s)",
                load.name,
                load.load_type,
            )
            if await self._async_restore_single_load(plan):
                if plan.get("type") != "vehicle_ampere":
                    load.last_action_time = current_time
                actions_taken.append(load.name)
                # Only restore one at a time to avoid overshooting
                break
            else:
                _LOGGER.debug(
                    "Load %s was not restored (already at max or restore failed)",
                    load.name,
                )

        if actions_taken:
            self.last_action = "Restored loads"
            self.last_action_reason = (
                f"Sufficient margin, restored: {', '.join(actions_taken)}"
            )
            _LOGGER.info("Restore actions: %s", self.last_action_reason)

    async def _async_restore_single_load(self, plan: dict[str, Any]) -> bool:
        """Restore a single load to the planned target."""
        load = plan["load"]
        try:
            if plan.get("type") == "vehicle_ampere":
                return await self._async_set_vehicle_ampere(load, plan["new_value"])
            if load.load_type == LoadType.EV_AMPERE and load.ampere_number_entity_id:
                return (
                    await self._async_set_ev_load(load, plan["new_value"], "restore")
                    > 0
                )
            elif load.load_type == LoadType.SWITCH and load.switch_entity_id:
                return await self._async_set_switch_load(load, plan["new_value"])
        except Exception as err:
            _LOGGER.error("Failed to restore %s: %s", load.name, err)

        return False

    async def _async_restore_all_loads(self, reason: str) -> None:
        """Restore all loads to max consumption (used when mode is OFF)."""
        restored = []
        for load in self.loads:
            if load.load_type == LoadType.EV_AMPERE:
                if await self._async_restore_ev_load(load, available_margin_kwh=999.0):
                    restored.append(load.name)
            elif load.load_type == LoadType.SWITCH:
                target_state = "off" if load.switch_inverted else "on"
                if await self._async_set_switch_load(load, target_state):
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

        # Get the actual min/max from the entity (these can change dynamically)
        entity_min = state.attributes.get("min", FALLBACK_MIN_AMPERE)
        entity_max = state.attributes.get("max", FALLBACK_MAX_AMPERE)

        load.current_ampere = current_ampere

        # Try to measure current power if sensor is available
        if load.power_sensor_entity_id:
            power_state = self.hass.states.get(load.power_sensor_entity_id)
            if power_state and power_state.state not in ("unknown", "unavailable"):
                try:
                    power_value = float(power_state.state)
                    unit = power_state.attributes.get("unit_of_measurement", "W")
                    load.current_power_kw = (
                        power_value / 1000.0 if unit == "W" else power_value
                    )
                except (ValueError, TypeError) as err:
                    _LOGGER.warning(
                        "Failed to read power sensor for %s: %s", load.name, err
                    )

        power_per_ampere = _calculate_ev_power_per_ampere(load)
        if power_per_ampere <= 0:
            return False

        _LOGGER.debug(
            "%s: Using %.2fkW/A for restoration",
            load.name,
            power_per_ampere,
        )

        _LOGGER.debug(
            "%s restore check: current=%dA, entity_min=%d, entity_max=%d, power_per_A=%.2fkW/A",
            load.name,
            current_ampere,
            entity_min,
            entity_max,
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

        # Convert available margin (kWh) to additional power we can add (kW)
        # available_margin_kwh = additional_power_kw * (remaining_seconds / 3600)
        # So: additional_power_kw = available_margin_kwh * 3600 / remaining_seconds
        additional_power_kw = (available_margin_kwh * 3600.0) / remaining_seconds

        # Calculate current power consumption
        current_power_kw = current_ampere * power_per_ampere

        # Target is current + additional power we can use (up to entity max)
        if power_per_ampere > 0:
            # We can add additional_power_kw worth of amperes to current
            additional_ampere = additional_power_kw / power_per_ampere
            target_ampere = current_ampere + additional_ampere
        else:
            target_ampere = entity_max

        _LOGGER.debug(
            "%s restore calc: margin=%.2fkWh, remaining_secs=%.0f, additional_power=%.2fkW, "
            "current=%.1fA + additional=%.1fA = target=%.1fA",
            load.name,
            available_margin_kwh,
            remaining_seconds,
            additional_power_kw,
            current_ampere,
            additional_power_kw / power_per_ampere if power_per_ampere > 0 else 0,
            target_ampere,
        )

        # Clamp to entity's min/max range and round to integer
        target_ampere = min(entity_max, max(entity_min, target_ampere))
        new_ampere = round(target_ampere)

        # Final bounds check to ensure we're within entity limits
        new_ampere = min(entity_max, max(entity_min, new_ampere))

        _LOGGER.debug(
            "%s restore target: clamped_target=%.1fA, new_ampere=%d, current=%d",
            load.name,
            target_ampere,
            new_ampere,
            current_ampere,
        )

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
                "Restored %s from %dA to %dA (margin: %.2fkWh, additional power: %.2fkW)",
                load.name,
                current_ampere,
                new_ampere,
                available_margin_kwh,
                additional_power_kw,
            )
            return True
        except Exception as err:
            _LOGGER.error("Failed to restore %s: %s", load.name, err)
            return False
