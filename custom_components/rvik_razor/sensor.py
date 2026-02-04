"""Sensor platform for RVik Razor integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import RvikRazorCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RvikRazorSensorEntityDescription(SensorEntityDescription):
    """Describes RVik Razor sensor entity."""

    value_fn: Callable[[dict[str, Any]], float | None] = lambda data: None
    attributes_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


SENSORS: tuple[RvikRazorSensorEntityDescription, ...] = (
    RvikRazorSensorEntityDescription(
        key="current_hour_kwh",
        name="Current hour kWh",
        icon="mdi:lightning-bolt",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: data.get("current_hour_kwh"),
    ),
    RvikRazorSensorEntityDescription(
        key="projected_end_hour_kwh",
        name="Projected end hour kWh",
        icon="mdi:chart-line",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("projected_end_kwh"),
        attributes_fn=lambda data: {
            "remaining_seconds": data.get("remaining_seconds"),
            "house_power_kw": data.get("house_power_kw"),
            "last_action": data.get("last_action"),
            "last_action_reason": data.get("last_action_reason"),
            "mode": data.get("mode"),
        },
    ),
    RvikRazorSensorEntityDescription(
        key="needed_reduction_kw",
        name="Needed reduction",
        icon="mdi:arrow-down-bold",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("needed_reduction_kw"),
    ),
    RvikRazorSensorEntityDescription(
        key="effective_target_kwh",
        name="Effective target",
        icon="mdi:target",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("effective_target_kwh"),
        attributes_fn=lambda data: {
            "max_hour_kwh": data.get("max_hour_kwh"),
            "target_fraction": (
                f"{data.get('target_fraction', 0) * 100:.0f}%"
                if data.get("target_fraction") is not None
                else None
            ),
            "available_down_capacity_kw": data.get("available_down_capacity_kw"),
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RVik Razor sensor entities."""
    coordinator: RvikRazorCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    async_add_entities(
        RvikRazorSensor(coordinator, entry, description) for description in SENSORS
    )


class RvikRazorSensor(CoordinatorEntity[RvikRazorCoordinator], SensorEntity):
    """Sensor entity for RVik Razor."""

    entity_description: RvikRazorSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RvikRazorCoordinator,
        entry: ConfigEntry,
        description: RvikRazorSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "RVik Razor",
            "manufacturer": "RVik",
            "model": "Energy Limiter",
        }

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        if (
            self.coordinator.data is None
            or self.entity_description.attributes_fn is None
        ):
            return None
        return self.entity_description.attributes_fn(self.coordinator.data)
