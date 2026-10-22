"""Constants for RVik Razor integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

DOMAIN = "rvik_razor"

# Config flow constants
CONF_HOUR_ENERGY_SENSOR = "hour_energy_sensor"
CONF_HOUSE_POWER_SENSOR = "house_power_sensor"
CONF_MAX_HOUR_KWH = "max_hour_kwh"
CONF_MODE = "mode"
CONF_LOADS = "loads"

# Load configuration keys
CONF_LOAD_NAME = "name"
CONF_LOAD_TYPE = "type"
CONF_LOAD_PRIORITY = "priority"
CONF_LOAD_ENABLED = "enabled"
CONF_LOAD_ENABLED_ENTITY = "enabled_entity"
CONF_LOAD_POWER_SENSOR = "power_sensor"
CONF_LOAD_ASSUMED_POWER = "assumed_power_kw"

# EV-specific configuration
CONF_LOAD_AMPERE_ENTITY = "ampere_entity"
CONF_LOAD_PHASES = "phases"
CONF_LOAD_VOLTAGE = "voltage"

# Switch-specific configuration
CONF_LOAD_SWITCH_ENTITY = "switch_entity"
CONF_LOAD_SWITCH_INVERTED = "switch_inverted"
CONF_LOAD_LATE_IN_HOUR = "late_in_hour"
CONF_LOAD_VEHICLE_AMPERE_ENTITIES = "vehicle_ampere_entities"

# Regulation timing configuration
CONF_LOAD_TIMEOUT = "timeout"

# Default values
DEFAULT_MAX_HOUR_KWH = 5.0
DEFAULT_MODE = "monitor"
DEFAULT_UPDATE_INTERVAL = 30  # seconds
DEFAULT_COOLDOWN = 120  # seconds
DEFAULT_ACCURACY_DEADBAND_KWH = 0.02  # Minimum improvement before changing a load
DEFAULT_PHASES = 3
DEFAULT_VOLTAGE = 400
# Load timeout configuration
DEFAULT_LOAD_TIMEOUT = 120  # seconds between regulations for a load
# Wait this long after the hour for the hour energy sensor to reset
HOUR_ENERGY_RESET_MAX_WAIT = 120  # seconds

# Late-hour load rule (see docs/adr/0002-run-binary-loads-late-in-the-hour.md)
DEFAULT_LATE_START_SLACK_KW = 0.6  # Spare power required before starting
DEFAULT_LATE_STOP_TOLERANCE_KW = 1.0  # Shortfall accepted before stopping
DEFAULT_LATE_OVERSHOOT_CAP_KWH = 0.2  # Max projected overshoot while running
DEFAULT_LATE_MIN_ON = 300  # seconds
DEFAULT_LATE_MIN_OFF = 300  # seconds

# Vehicle amp trim for late-hour loads (see docs/adr/0003-trim-vehicle-charge-current.md)
DEFAULT_TRIM_MIN_AMPERE = 6
DEFAULT_TRIM_MAX_AMPERE = 16  # Charger installation limit
DEFAULT_TRIM_MARGIN_KW = 0.2  # Power kept spare when fitting the current
DEFAULT_TRIM_RAISE_STEP = 2  # Only raise when this many more amperes fit
DEFAULT_TRIM_RAISE_INTERVAL = 300  # seconds between raises
DEFAULT_TRIM_DAILY_COMMAND_BUDGET = 150  # Vehicle API commands per day
TRIM_CHARGING_THRESHOLD_KW = 1.0  # Load power that counts as charging
TRIM_IDENTIFY_INTERVAL = 300  # seconds between attempts to find the vehicle
TRIM_IDENTIFY_MAX_ATTEMPTS = 3  # per plug-in session
TRIM_POWER_MATCH_KW = 1.5  # Max difference between vehicle and charger power
TRIM_VERIFY_DELAY = 120  # seconds before checking that a reduction took effect
TRIM_VERIFY_TOLERANCE_KW = 1.0  # Excess power that means the wrong vehicle

# Fallback ampere limits when entity doesn't provide them
FALLBACK_MIN_AMPERE = 0
FALLBACK_MAX_AMPERE = 16

# Coordinator data keys
DATA_COORDINATOR = "coordinator"
DATA_UNSUB = "unsub"

# Entity unique ID prefixes
ENTITY_ID_FORMAT = "{domain}_{entry_id}_{suffix}"


class OperationMode(StrEnum):
    """Operation modes for RVik Razor."""

    OFF = "off"
    MONITOR = "monitor"
    CONTROL = "control"


class LoadType(StrEnum):
    """Load types supported by RVik Razor."""

    EV_AMPERE = "ev_ampere"
    SWITCH = "switch"


@dataclass
class Load:
    """Represents a controllable load."""

    name: str
    load_type: LoadType
    priority: int
    enabled: bool = True
    enabled_entity_id: str | None = None  # Optional entity to check if load is active
    power_sensor_entity_id: str | None = None
    assumed_power_kw: float | None = None

    # EV-specific fields
    ampere_number_entity_id: str | None = None
    phases: int = DEFAULT_PHASES  # 1 or 3 phase
    voltage: int = DEFAULT_VOLTAGE  # 230 or 400 volt

    # Switch-specific fields
    switch_entity_id: str | None = None
    switch_inverted: bool = False  # If True, switch consumes power when OFF
    late_in_hour: bool = False  # Run late in the hour instead of to the set point
    # Charge-current number entities of vehicles that may charge on this load
    vehicle_ampere_entity_ids: list[str] = field(default_factory=list)

    # Regulation timing
    timeout: int = DEFAULT_LOAD_TIMEOUT  # Seconds between regulations

    # Runtime state
    last_action_time: float = 0.0
    measured_power_per_ampere: float | None = (
        None  # Stored kW/A ratio from actual measurements
    )
    current_switch_state: str | None = (
        None  # Current state of switch ("on", "off", etc.)
    )
    current_ampere: float | None = None  # Current ampere setting for EV chargers
    current_min_ampere: float | None = None  # Runtime min from EV ampere entity
    current_max_ampere: float | None = None  # Runtime max from EV ampere entity
    current_power_kw: float | None = None  # Measured power from power_sensor_entity_id
    # Charge current of the vehicle charging on a late-hour load; None when
    # amp trim is not available and the load runs on/off only.
    vehicle_ampere: float | None = None
    vehicle_min_ampere: float = DEFAULT_TRIM_MIN_AMPERE
    vehicle_max_ampere: float = DEFAULT_TRIM_MAX_AMPERE
    vehicle_ampere_changed_time: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert load to dictionary for storage."""
        return {
            CONF_LOAD_NAME: self.name,
            CONF_LOAD_TYPE: self.load_type,
            CONF_LOAD_PRIORITY: self.priority,
            CONF_LOAD_ENABLED: self.enabled,
            CONF_LOAD_ENABLED_ENTITY: self.enabled_entity_id,
            CONF_LOAD_POWER_SENSOR: self.power_sensor_entity_id,
            CONF_LOAD_ASSUMED_POWER: self.assumed_power_kw,
            CONF_LOAD_AMPERE_ENTITY: self.ampere_number_entity_id,
            CONF_LOAD_PHASES: self.phases,
            CONF_LOAD_VOLTAGE: self.voltage,
            CONF_LOAD_SWITCH_ENTITY: self.switch_entity_id,
            CONF_LOAD_SWITCH_INVERTED: self.switch_inverted,
            CONF_LOAD_LATE_IN_HOUR: self.late_in_hour,
            CONF_LOAD_VEHICLE_AMPERE_ENTITIES: list(self.vehicle_ampere_entity_ids),
            CONF_LOAD_TIMEOUT: self.timeout,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Load:
        """Create load from dictionary."""
        return Load(
            name=data[CONF_LOAD_NAME],
            load_type=LoadType(data[CONF_LOAD_TYPE]),
            priority=data[CONF_LOAD_PRIORITY],
            enabled=data.get(CONF_LOAD_ENABLED, True),
            enabled_entity_id=data.get(CONF_LOAD_ENABLED_ENTITY),
            power_sensor_entity_id=data.get(CONF_LOAD_POWER_SENSOR),
            assumed_power_kw=data.get(CONF_LOAD_ASSUMED_POWER),
            ampere_number_entity_id=data.get(CONF_LOAD_AMPERE_ENTITY),
            phases=data.get(CONF_LOAD_PHASES, DEFAULT_PHASES),
            voltage=data.get(CONF_LOAD_VOLTAGE, DEFAULT_VOLTAGE),
            switch_entity_id=data.get(CONF_LOAD_SWITCH_ENTITY),
            switch_inverted=data.get(CONF_LOAD_SWITCH_INVERTED, False),
            late_in_hour=data.get(CONF_LOAD_LATE_IN_HOUR, False),
            vehicle_ampere_entity_ids=list(
                data.get(CONF_LOAD_VEHICLE_AMPERE_ENTITIES) or []
            ),
            timeout=data.get(CONF_LOAD_TIMEOUT, DEFAULT_LOAD_TIMEOUT),
        )


@dataclass
class HouseConfig:
    """Configuration for house power monitoring."""

    hour_energy_entity_id: str
    house_power_entity_id: str | None = None
