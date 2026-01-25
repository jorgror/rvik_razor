"""Config flow for Rvik Razor integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_HOUR_ENERGY_SENSOR,
    CONF_HOUSE_POWER_SENSOR,
    CONF_LOAD_AMPERE_ENTITY,
    CONF_LOAD_ASSUMED_POWER,
    CONF_LOAD_ENABLED,
    CONF_LOAD_ENABLED_ENTITY,
    CONF_LOAD_NAME,
    CONF_LOAD_PHASES,
    CONF_LOAD_POWER_SENSOR,
    CONF_LOAD_PRIORITY,
    CONF_LOAD_SWITCH_ENTITY,
    CONF_LOAD_SWITCH_INVERTED,
    CONF_LOAD_TIMEOUT,
    CONF_LOAD_TYPE,
    CONF_LOAD_VOLTAGE,
    CONF_LOADS,
    CONF_MAX_HOUR_KWH,
    CONF_MODE,
    DEFAULT_LOAD_TIMEOUT,
    DEFAULT_MAX_HOUR_KWH,
    DEFAULT_MODE,
    DEFAULT_PHASES,
    DEFAULT_VOLTAGE,
    DOMAIN,
    LoadType,
    OperationMode,
)


class RvikRazorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Rvik Razor."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the initial step - select energy sensor."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate that the sensor exists
            if not self.hass.states.get(user_input[CONF_HOUR_ENERGY_SENSOR]):
                errors[CONF_HOUR_ENERGY_SENSOR] = "entity_not_found"
            else:
                # Store for next step
                self.context["user_input"] = user_input
                return await self.async_step_power_sensor()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_HOUR_ENERGY_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="energy",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_power_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the power sensor step (optional)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Merge with previous input
            combined_input = {**self.context["user_input"], **user_input}

            # Validate power sensor if provided
            if user_input.get(CONF_HOUSE_POWER_SENSOR):
                if not self.hass.states.get(user_input[CONF_HOUSE_POWER_SENSOR]):
                    errors[CONF_HOUSE_POWER_SENSOR] = "entity_not_found"
                else:
                    self.context["user_input"] = combined_input
                    return await self.async_step_limits()
            else:
                self.context["user_input"] = combined_input
                return await self.async_step_limits()

        data_schema = vol.Schema(
            {
                vol.Optional(CONF_HOUSE_POWER_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="power",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="power_sensor",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_limits(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the limits configuration step."""
        if user_input is not None:
            # Merge all inputs
            final_input = {**self.context["user_input"], **user_input}

            # Initialize empty loads list
            final_input[CONF_LOADS] = []

            # Create the config entry
            return self.async_create_entry(
                title="Rvik Razor",
                data=final_input,
            )

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_MAX_HOUR_KWH, default=DEFAULT_MAX_HOUR_KWH
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.1,
                        max=100.0,
                        step=0.1,
                        unit_of_measurement="kWh",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_MODE, default=DEFAULT_MODE): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[mode.value for mode in OperationMode],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="limits",
            data_schema=data_schema,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> RvikRazorOptionsFlow:
        """Get the options flow for this handler."""
        return RvikRazorOptionsFlow(config_entry)


class RvikRazorOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Rvik Razor."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self.loads: list[dict[str, Any]] = list(config_entry.data.get(CONF_LOADS, []))
        self.current_load: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Manage the options - show load list and management options."""
        if user_input is not None:
            action = user_input.get("action")
            if action == "add_load":
                return await self.async_step_add_load()
            elif action == "edit_limits":
                return await self.async_step_edit_limits()
            elif action and action.startswith("edit_load_"):
                # Extract load index from action
                load_index = int(action.split("_")[-1])
                if 0 <= load_index < len(self.loads):
                    self.current_load = dict(self.loads[load_index])
                    self.current_load["_edit_index"] = load_index
                    if self.current_load[CONF_LOAD_TYPE] == LoadType.EV_AMPERE:
                        return await self.async_step_edit_ev_load()
                    else:
                        return await self.async_step_edit_switch_load()
            elif action and action.startswith("remove_load_"):
                # Extract load index from action
                load_index = int(action.split("_")[-1])
                if 0 <= load_index < len(self.loads):
                    self.loads.pop(load_index)
                    new_data = {**self._config_entry.data, CONF_LOADS: self.loads}
                    self.hass.config_entries.async_update_entry(
                        self._config_entry, data=new_data
                    )
                    # Show the menu again
                    return await self.async_step_init()

        # Build load list with edit/remove actions
        current_max = self._config_entry.data.get(
            CONF_MAX_HOUR_KWH, DEFAULT_MAX_HOUR_KWH
        )
        current_mode = self._config_entry.data.get(CONF_MODE, DEFAULT_MODE)

        # Create action options dynamically based on loads
        action_options = []

        # System actions at top
        action_options.append(
            {
                "value": "edit_limits",
                "label": f"⚙️  System settings │ Max: {current_max} kWh/h │ Mode: {current_mode}",
            }
        )
        action_options.append({"value": "add_load", "label": "➕ Add new load"})

        # Add loads in table format
        if self.loads:
            for i, load in enumerate(self.loads):
                load_name = load[CONF_LOAD_NAME]
                priority = load[CONF_LOAD_PRIORITY]
                load_type = "EV" if load[CONF_LOAD_TYPE] == LoadType.EV_AMPERE else "SW"
                enabled_icon = "✓" if load.get(CONF_LOAD_ENABLED, True) else "✗"

                # Create compact, aligned display
                # Format: Name (truncated) | Pri: X | Type | [Status] | Actions
                display_name = load_name[:18] if len(load_name) > 18 else load_name

                action_options.append(
                    {
                        "value": f"edit_load_{i}",
                        "label": f"✏️  [{enabled_icon}] {display_name.ljust(18)} │ Pri:{str(priority).rjust(2)} │ {load_type}",
                    }
                )
                action_options.append(
                    {
                        "value": f"remove_load_{i}",
                        "label": f"🗑️  [{enabled_icon}] {display_name.ljust(18)} │ Pri:{str(priority).rjust(2)} │ {load_type}",
                    }
                )

        data_schema = vol.Schema(
            {
                vol.Required("action"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=action_options,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            description_placeholders={
                "load_count": str(len(self.loads)),
                "max_kwh": str(current_max),
                "mode": current_mode,
            },
        )

    async def async_step_add_load(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Add a new load - select type."""
        if user_input is not None:
            self.current_load = user_input
            if user_input[CONF_LOAD_TYPE] == LoadType.EV_AMPERE:
                return await self.async_step_add_ev_load()
            else:
                return await self.async_step_add_switch_load()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_LOAD_NAME): selector.TextSelector(),
                vol.Required(CONF_LOAD_TYPE): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {
                                "value": LoadType.EV_AMPERE,
                                "label": "EV Charger (Ampere)",
                            },
                            {"value": LoadType.SWITCH, "label": "Switch (On/Off)"},
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_LOAD_PRIORITY, default=1): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=100,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_LOAD_ENABLED, default=True
                ): selector.BooleanSelector(),
                vol.Optional(CONF_LOAD_ENABLED_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain=["binary_sensor", "input_boolean"],
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="add_load",
            data_schema=data_schema,
        )

    async def async_step_add_ev_load(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Configure EV charger load."""
        if user_input is not None:
            # Convert phases and voltage to integers
            if CONF_LOAD_PHASES in user_input:
                user_input[CONF_LOAD_PHASES] = int(user_input[CONF_LOAD_PHASES])
            if CONF_LOAD_VOLTAGE in user_input:
                user_input[CONF_LOAD_VOLTAGE] = int(user_input[CONF_LOAD_VOLTAGE])

            # Convert empty/0.0 assumed power to None
            if (
                CONF_LOAD_ASSUMED_POWER in user_input
                and not user_input[CONF_LOAD_ASSUMED_POWER]
            ):
                user_input[CONF_LOAD_ASSUMED_POWER] = None

            # Merge with current load data
            self.current_load.update(user_input)
            self.loads.append(self.current_load)

            # Update config entry
            new_data = {**self._config_entry.data, CONF_LOADS: self.loads}
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )

            return self.async_create_entry(title="", data={})

        data_schema = vol.Schema(
            {
                vol.Required(CONF_LOAD_AMPERE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="number")
                ),
                vol.Required(
                    CONF_LOAD_PHASES, default=DEFAULT_PHASES
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": "1", "label": "1-phase"},
                            {"value": "3", "label": "3-phase"},
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_LOAD_VOLTAGE, default=DEFAULT_VOLTAGE
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": "230", "label": "230V"},
                            {"value": "400", "label": "400V"},
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_LOAD_POWER_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="power",
                    )
                ),
                vol.Optional(CONF_LOAD_ASSUMED_POWER): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.0,
                        max=50.0,
                        step=0.1,
                        unit_of_measurement="kW",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_LOAD_TIMEOUT, default=DEFAULT_LOAD_TIMEOUT
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=3600,
                        step=1,
                        unit_of_measurement="seconds",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="add_ev_load",
            data_schema=data_schema,
        )

    async def async_step_add_switch_load(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Configure switch load."""
        if user_input is not None:
            # Convert empty/0.0 assumed power to None
            if (
                CONF_LOAD_ASSUMED_POWER in user_input
                and not user_input[CONF_LOAD_ASSUMED_POWER]
            ):
                user_input[CONF_LOAD_ASSUMED_POWER] = None

            # Merge with current load data
            self.current_load.update(user_input)
            self.loads.append(self.current_load)

            # Update config entry
            new_data = {**self._config_entry.data, CONF_LOADS: self.loads}
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )

            return self.async_create_entry(title="", data={})

        data_schema = vol.Schema(
            {
                vol.Required(CONF_LOAD_SWITCH_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                ),
                vol.Optional(
                    CONF_LOAD_SWITCH_INVERTED, default=False
                ): selector.BooleanSelector(),
                vol.Optional(CONF_LOAD_POWER_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="power",
                    )
                ),
                vol.Optional(CONF_LOAD_ASSUMED_POWER): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.0,
                        max=50.0,
                        step=0.1,
                        unit_of_measurement="kW",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_LOAD_TIMEOUT, default=DEFAULT_LOAD_TIMEOUT
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=3600,
                        step=1,
                        unit_of_measurement="seconds",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="add_switch_load",
            data_schema=data_schema,
        )

    async def async_step_edit_ev_load(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Edit an existing EV charger load."""
        if user_input is not None:
            # Convert phases and voltage to integers
            if CONF_LOAD_PHASES in user_input:
                user_input[CONF_LOAD_PHASES] = int(user_input[CONF_LOAD_PHASES])
            if CONF_LOAD_VOLTAGE in user_input:
                user_input[CONF_LOAD_VOLTAGE] = int(user_input[CONF_LOAD_VOLTAGE])

            # Convert empty/0.0 assumed power to None
            if (
                CONF_LOAD_ASSUMED_POWER in user_input
                and not user_input[CONF_LOAD_ASSUMED_POWER]
            ):
                user_input[CONF_LOAD_ASSUMED_POWER] = None

            # Update the load at the stored index
            edit_index = self.current_load.pop("_edit_index")
            self.current_load.update(user_input)
            self.loads[edit_index] = self.current_load

            # Update config entry
            new_data = {**self._config_entry.data, CONF_LOADS: self.loads}
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )

            return self.async_create_entry(title="", data={})

        # Get current values for defaults
        current_ampere = self.current_load.get(CONF_LOAD_AMPERE_ENTITY) or ""
        current_phases = str(self.current_load.get(CONF_LOAD_PHASES, DEFAULT_PHASES))
        current_voltage = str(self.current_load.get(CONF_LOAD_VOLTAGE, DEFAULT_VOLTAGE))
        current_power = self.current_load.get(CONF_LOAD_POWER_SENSOR) or None
        current_assumed = self.current_load.get(CONF_LOAD_ASSUMED_POWER)
        current_name = self.current_load.get(CONF_LOAD_NAME) or ""
        current_priority = self.current_load.get(CONF_LOAD_PRIORITY, 1)
        current_enabled = self.current_load.get(CONF_LOAD_ENABLED, True)
        current_enabled_entity = self.current_load.get(CONF_LOAD_ENABLED_ENTITY) or None
        current_timeout = self.current_load.get(CONF_LOAD_TIMEOUT, DEFAULT_LOAD_TIMEOUT)

        # Build schema dynamically to handle None values properly
        schema_dict: dict[Any, Any] = {
            vol.Required(CONF_LOAD_NAME, default=current_name): selector.TextSelector(),
            vol.Required(
                CONF_LOAD_PRIORITY, default=current_priority
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=100,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_LOAD_ENABLED, default=current_enabled
            ): selector.BooleanSelector(),
        }

        # Only add default for enabled_entity if it has a valid value
        if current_enabled_entity and current_enabled_entity != "None":
            schema_dict[
                vol.Optional(CONF_LOAD_ENABLED_ENTITY, default=current_enabled_entity)
            ] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["binary_sensor", "input_boolean"],
                )
            )
        else:
            schema_dict[vol.Optional(CONF_LOAD_ENABLED_ENTITY)] = (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain=["binary_sensor", "input_boolean"],
                    )
                )
            )

        schema_dict[vol.Required(CONF_LOAD_AMPERE_ENTITY, default=current_ampere)] = (
            selector.EntitySelector(selector.EntitySelectorConfig(domain="number"))
        )

        schema_dict[vol.Required(CONF_LOAD_PHASES, default=current_phases)] = (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "1", "label": "1-phase"},
                        {"value": "3", "label": "3-phase"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        )

        schema_dict[vol.Required(CONF_LOAD_VOLTAGE, default=current_voltage)] = (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "230", "label": "230V"},
                        {"value": "400", "label": "400V"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        )

        # Only add default for power_sensor if it has a valid value
        if current_power and current_power != "None":
            schema_dict[vol.Optional(CONF_LOAD_POWER_SENSOR, default=current_power)] = (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="power",
                    )
                )
            )
        else:
            schema_dict[vol.Optional(CONF_LOAD_POWER_SENSOR)] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class="power",
                )
            )

        # Only add default for assumed_power if it has a valid value
        if current_assumed is not None and current_assumed != 0:
            schema_dict[
                vol.Optional(CONF_LOAD_ASSUMED_POWER, default=current_assumed)
            ] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0,
                    max=50.0,
                    step=0.1,
                    unit_of_measurement="kW",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
        else:
            schema_dict[vol.Optional(CONF_LOAD_ASSUMED_POWER)] = (
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.0,
                        max=50.0,
                        step=0.1,
                        unit_of_measurement="kW",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                )
            )

        # Add timeout field
        schema_dict[vol.Optional(CONF_LOAD_TIMEOUT, default=current_timeout)] = (
            selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=3600,
                    step=1,
                    unit_of_measurement="seconds",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
        )

        data_schema = vol.Schema(schema_dict)

        return self.async_show_form(
            step_id="edit_ev_load",
            data_schema=data_schema,
        )

    async def async_step_edit_switch_load(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Edit an existing switch load."""
        if user_input is not None:
            # Convert empty/0.0 assumed power to None
            if (
                CONF_LOAD_ASSUMED_POWER in user_input
                and not user_input[CONF_LOAD_ASSUMED_POWER]
            ):
                user_input[CONF_LOAD_ASSUMED_POWER] = None

            # Update the load at the stored index
            edit_index = self.current_load.pop("_edit_index")
            self.current_load.update(user_input)
            self.loads[edit_index] = self.current_load

            # Update config entry
            new_data = {**self._config_entry.data, CONF_LOADS: self.loads}
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )

            return self.async_create_entry(title="", data={})

        # Get current values for defaults
        current_switch = self.current_load.get(CONF_LOAD_SWITCH_ENTITY) or ""
        current_inverted = self.current_load.get(CONF_LOAD_SWITCH_INVERTED, False)
        current_power = self.current_load.get(CONF_LOAD_POWER_SENSOR) or None
        current_assumed = self.current_load.get(CONF_LOAD_ASSUMED_POWER)
        current_name = self.current_load.get(CONF_LOAD_NAME) or ""
        current_priority = self.current_load.get(CONF_LOAD_PRIORITY, 1)
        current_enabled = self.current_load.get(CONF_LOAD_ENABLED, True)
        current_enabled_entity = self.current_load.get(CONF_LOAD_ENABLED_ENTITY) or None
        current_timeout = self.current_load.get(CONF_LOAD_TIMEOUT, DEFAULT_LOAD_TIMEOUT)

        # Build schema dynamically to handle None values properly
        schema_dict: dict[Any, Any] = {
            vol.Required(CONF_LOAD_NAME, default=current_name): selector.TextSelector(),
            vol.Required(
                CONF_LOAD_PRIORITY, default=current_priority
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=100,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_LOAD_ENABLED, default=current_enabled
            ): selector.BooleanSelector(),
        }

        # Only add default for enabled_entity if it has a valid value
        if current_enabled_entity and current_enabled_entity != "None":
            schema_dict[
                vol.Optional(CONF_LOAD_ENABLED_ENTITY, default=current_enabled_entity)
            ] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["binary_sensor", "input_boolean"],
                )
            )
        else:
            schema_dict[vol.Optional(CONF_LOAD_ENABLED_ENTITY)] = (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain=["binary_sensor", "input_boolean"],
                    )
                )
            )

        schema_dict[vol.Required(CONF_LOAD_SWITCH_ENTITY, default=current_switch)] = (
            selector.EntitySelector(selector.EntitySelectorConfig(domain="switch"))
        )

        schema_dict[
            vol.Optional(CONF_LOAD_SWITCH_INVERTED, default=current_inverted)
        ] = selector.BooleanSelector()

        # Only add default for power_sensor if it has a valid value
        if current_power and current_power != "None":
            schema_dict[vol.Optional(CONF_LOAD_POWER_SENSOR, default=current_power)] = (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="power",
                    )
                )
            )
        else:
            schema_dict[vol.Optional(CONF_LOAD_POWER_SENSOR)] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class="power",
                )
            )

        # Only add default for assumed_power if it has a valid value
        if current_assumed is not None and current_assumed != 0:
            schema_dict[
                vol.Optional(CONF_LOAD_ASSUMED_POWER, default=current_assumed)
            ] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0,
                    max=50.0,
                    step=0.1,
                    unit_of_measurement="kW",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
        else:
            schema_dict[vol.Optional(CONF_LOAD_ASSUMED_POWER)] = (
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.0,
                        max=50.0,
                        step=0.1,
                        unit_of_measurement="kW",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                )
            )

        # Add timeout field
        schema_dict[vol.Optional(CONF_LOAD_TIMEOUT, default=current_timeout)] = (
            selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=3600,
                    step=1,
                    unit_of_measurement="seconds",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
        )

        data_schema = vol.Schema(schema_dict)

        return self.async_show_form(
            step_id="edit_switch_load",
            data_schema=data_schema,
        )

    async def async_step_edit_limits(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Edit max hour kWh limit and mode."""
        if user_input is not None:
            # Update config entry
            new_data = {**self._config_entry.data, **user_input}
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )

            return self.async_create_entry(title="", data={})

        current_max = self._config_entry.data.get(
            CONF_MAX_HOUR_KWH, DEFAULT_MAX_HOUR_KWH
        )
        current_mode = self._config_entry.data.get(CONF_MODE, DEFAULT_MODE)

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_MAX_HOUR_KWH, default=current_max
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.1,
                        max=100.0,
                        step=0.1,
                        unit_of_measurement="kWh",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_MODE, default=current_mode): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[mode.value for mode in OperationMode],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="edit_limits",
            data_schema=data_schema,
        )
