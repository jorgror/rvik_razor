"""Find the vehicle charging on a late-hour load.

Vehicles are described by their charge-current number entity. The other
entities RVik Razor reads (online status, charging state, charger power and
location) live on the same device and are found through the entity registry.
This works for the Tesla Fleet integration, whose vehicle data is only fetched
when RVik Razor asks for it (polling disabled).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import TRIM_POWER_MATCH_KW

# (domain, translation_key) of the sibling entities on the vehicle device
STATUS_KEY = ("binary_sensor", "state")
CHARGING_STATE_KEY = ("sensor", "charge_state_charging_state")
CHARGER_POWER_KEY = ("sensor", "charge_state_charger_power")
LOCATION_KEY = ("device_tracker", "location")

CHARGING_STATES = ("charging", "starting")
HOME = "home"


@dataclass
class VehicleTrimSession:
    """Amp-trim state for one late-hour load while a vehicle is plugged in."""

    ampere_entity_id: str | None = None  # Vehicle bound to this session
    ampere: float = 0.0  # Charge current last read or commanded
    min_ampere: float = 0.0
    max_ampere: float = 0.0
    changed_time: float = 0.0
    bound_time: float = 0.0
    charging_since: float | None = None  # Start of the current charging stretch
    # kW per ampere measured this charging stretch, at measured_ampere. The
    # same vehicle can draw a different rate after a current change or a pause,
    # for example when the charger switches phases.
    power_per_ampere: float = 0.0
    measured_ampere: float = 0.0
    previous_power_per_ampere: float = 0.0  # Rate at the end of the last stretch
    settle_until: float = 0.0  # Vehicle follows its last current change until then
    attempts: int = 0
    last_attempt_time: float = 0.0
    failed: bool = False  # Give up on amp trim until the vehicle is unplugged
    lowered: bool = False  # Current was lowered; restore it when unplugged
    unplugged_cycles: int = 0
    verify_after: float | None = None
    verify_max_power_kw: float = 0.0
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert the session to a dictionary kept across restarts.

        Timers of the current charging stretch are left out, since charging
        restarts its stretch after a restart.
        """
        data = asdict(self)
        for transient in SESSION_TRANSIENT_FIELDS:
            data.pop(transient)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VehicleTrimSession:
        """Restore a session saved by to_dict, ignoring unknown fields."""
        known = {field.name for field in fields(cls)} - set(SESSION_TRANSIENT_FIELDS)
        return cls(**{key: value for key, value in data.items() if key in known})


SESSION_TRANSIENT_FIELDS = ("charging_since", "settle_until", "unplugged_cycles")


@dataclass
class VehicleSnapshot:
    """What RVik Razor knows about one vehicle after a refresh."""

    ampere_entity_id: str
    online: bool
    charging_state: str | None
    charger_power_kw: float | None
    location: str | None


def select_charging_vehicle(
    vehicles: list[VehicleSnapshot], charger_power_kw: float
) -> tuple[str | None, str]:
    """Return the ampere entity of the one vehicle charging on this charger.

    A vehicle qualifies when it is online, charging, at home and reports about
    the power the charger measures. Anything but exactly one match returns
    None, so the load falls back to on/off control.
    """
    matches = []
    for vehicle in vehicles:
        if not vehicle.online:
            continue
        if (vehicle.charging_state or "").lower() not in CHARGING_STATES:
            continue
        if vehicle.location != HOME:
            continue
        if (
            vehicle.charger_power_kw is None
            or abs(vehicle.charger_power_kw - charger_power_kw) > TRIM_POWER_MATCH_KW
        ):
            continue
        matches.append(vehicle.ampere_entity_id)

    if len(matches) == 1:
        return matches[0], f"Charging vehicle is {matches[0]}"
    if not matches:
        return None, "No vehicle at home is charging at the charger's power"
    return None, f"Several vehicles match the charger: {', '.join(matches)}"


def find_vehicle_entities(
    hass: HomeAssistant, ampere_entity_id: str
) -> dict[tuple[str, str], str]:
    """Map (domain, translation_key) to entity_id for a vehicle's device."""
    registry = er.async_get(hass)
    entry = registry.async_get(ampere_entity_id)
    if entry is None or entry.device_id is None:
        return {}
    return {
        (sibling.domain, sibling.translation_key): sibling.entity_id
        for sibling in er.async_entries_for_device(registry, entry.device_id)
        if sibling.translation_key and not sibling.disabled_by
    }


def read_vehicle_snapshot(
    hass: HomeAssistant,
    ampere_entity_id: str,
    entities: dict[tuple[str, str], str],
) -> VehicleSnapshot:
    """Read a vehicle's state from its sibling entities."""

    def state_of(key: tuple[str, str]) -> str | None:
        entity_id = entities.get(key)
        state = hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        return state.state

    charger_power_kw: float | None = None
    power = state_of(CHARGER_POWER_KEY)
    if power is not None:
        try:
            charger_power_kw = float(power)
        except ValueError:
            pass
        else:
            power_state = hass.states.get(entities[CHARGER_POWER_KEY])
            if power_state.attributes.get("unit_of_measurement") == "W":
                charger_power_kw /= 1000.0

    return VehicleSnapshot(
        ampere_entity_id=ampere_entity_id,
        online=state_of(STATUS_KEY) == "on",
        charging_state=state_of(CHARGING_STATE_KEY),
        charger_power_kw=charger_power_kw,
        location=state_of(LOCATION_KEY),
    )
