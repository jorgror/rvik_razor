"""Number platform for Rvik Razor integration."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_MAX_HOUR_KWH, DATA_COORDINATOR, DEFAULT_MAX_HOUR_KWH, DOMAIN
from .coordinator import RvikRazorCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Rvik Razor number entities."""
    coordinator: RvikRazorCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    async_add_entities([RvikRazorMaxHourKwhNumber(coordinator, entry)])


class RvikRazorMaxHourKwhNumber(NumberEntity):
    """Number entity for max hour kWh limit."""

    _attr_has_entity_name = True
    _attr_name = "Max hour kWh"
    _attr_icon = "mdi:gauge"
    _attr_native_min_value = 0.1
    _attr_native_max_value = 100.0
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = "kWh"

    def __init__(
        self,
        coordinator: RvikRazorCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the number entity."""
        self.coordinator = coordinator
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_max_hour_kwh"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Rvik Razor",
            "manufacturer": "Rvik",
            "model": "Energy Limiter",
        }

    @property
    def native_value(self) -> float:
        """Return the current value."""
        return self.entry.data.get(CONF_MAX_HOUR_KWH, DEFAULT_MAX_HOUR_KWH)

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        _LOGGER.info("Setting max hour kWh to %.2f", value)

        # Update config entry
        new_data = {**self.entry.data, CONF_MAX_HOUR_KWH: value}
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)

        # Update coordinator
        self.coordinator.update_config(new_data)
        await self.coordinator.async_request_refresh()

        # Update state
        self.async_write_ha_state()
