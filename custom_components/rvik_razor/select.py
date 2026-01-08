"""Select platform for RVik Razor integration."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_MODE, DATA_COORDINATOR, DEFAULT_MODE, DOMAIN, OperationMode
from .coordinator import RvikRazorCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RVik Razor select entities."""
    coordinator: RvikRazorCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    async_add_entities([RvikRazorModeSelect(coordinator, entry)])


class RvikRazorModeSelect(SelectEntity):
    """Select entity for operation mode."""

    _attr_has_entity_name = True
    _attr_name = "Mode"
    _attr_icon = "mdi:power-settings"
    _attr_options = [mode.value for mode in OperationMode]

    def __init__(
        self,
        coordinator: RvikRazorCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the select entity."""
        self.coordinator = coordinator
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_mode"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "RVik Razor",
            "manufacturer": "RVik",
            "model": "Energy Limiter",
        }

    @property
    def current_option(self) -> str:
        """Return the current option."""
        return self.entry.data.get(CONF_MODE, DEFAULT_MODE)

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        _LOGGER.info("Setting mode to %s", option)

        # Update config entry
        new_data = {**self.entry.data, CONF_MODE: option}
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)

        # Update coordinator
        self.coordinator.update_config(new_data)
        await self.coordinator.async_request_refresh()

        # Update state
        self.async_write_ha_state()
