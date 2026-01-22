"""Constants for RVik Razor integration."""

from __future__ import annotations

from dataclasses import dataclass
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
CONF_LOAD_MIN_AMPERE = "min_ampere"
CONF_LOAD_MAX_AMPERE = "max_ampere"
CONF_LOAD_PHASES = "phases"
CONF_LOAD_VOLTAGE = "voltage"

# Switch-specific configuration
CONF_LOAD_SWITCH_ENTITY = "switch_entity"
CONF_LOAD_SWITCH_INVERTED = "switch_inverted"

# Default values
DEFAULT_MAX_HOUR_KWH = 5.0
DEFAULT_MODE = "monitor"
DEFAULT_UPDATE_INTERVAL = 30  # seconds
DEFAULT_COOLDOWN = 120  # seconds
DEFAULT_RESTORE_MARGIN = 0.1  # kWh
DEFAULT_MIN_AMPERE = 6
DEFAULT_MAX_AMPERE = 32
DEFAULT_PHASES = 3
DEFAULT_VOLTAGE = 400

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
    min_ampere: int = DEFAULT_MIN_AMPERE
    max_ampere: int = DEFAULT_MAX_AMPERE
    phases: int = DEFAULT_PHASES  # 1 or 3 phase
    voltage: int = DEFAULT_VOLTAGE  # 230 or 400 volt

    # Switch-specific fields
    switch_entity_id: str | None = None
    switch_inverted: bool = False  # If True, switch consumes power when OFF

    # Runtime state
    last_action_time: float = 0.0
    measured_power_per_ampere: float | None = (
        None  # Stored kW/A ratio from actual measurements
    )

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
            CONF_LOAD_MIN_AMPERE: self.min_ampere,
            CONF_LOAD_MAX_AMPERE: self.max_ampere,
            CONF_LOAD_PHASES: self.phases,
            CONF_LOAD_VOLTAGE: self.voltage,
            CONF_LOAD_SWITCH_ENTITY: self.switch_entity_id,
            CONF_LOAD_SWITCH_INVERTED: self.switch_inverted,
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
            min_ampere=data.get(CONF_LOAD_MIN_AMPERE, DEFAULT_MIN_AMPERE),
            max_ampere=data.get(CONF_LOAD_MAX_AMPERE, DEFAULT_MAX_AMPERE),
            phases=data.get(CONF_LOAD_PHASES, DEFAULT_PHASES),
            voltage=data.get(CONF_LOAD_VOLTAGE, DEFAULT_VOLTAGE),
            switch_entity_id=data.get(CONF_LOAD_SWITCH_ENTITY),
            switch_inverted=data.get(CONF_LOAD_SWITCH_INVERTED, False),
        )


@dataclass
class HouseConfig:
    """Configuration for house power monitoring."""

    hour_energy_entity_id: str
    house_power_entity_id: str | None = None
