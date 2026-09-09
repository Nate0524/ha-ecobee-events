"""Config flow for Ecobee Utility Events.

There is nothing to configure: the integration reads the core ecobee
integration's existing data, so it needs no credentials and no options. The flow
is a single confirmation step that records which thermostat identifier the
entity unique_ids are pinned to.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from . import find_thermostat_list, match_thermostat
from .const import CONF_THERMOSTAT_ID, DEFAULT_THERMOSTAT_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)


class EcobeeEventsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ecobee Utility Events."""

    VERSION = 1

    def _discover(self) -> tuple[str, str | None, str]:
        """Return (identifier, thermostat name, human readable status).

        Never raises. A thermostat that cannot be found right now is not a
        failure: the integration sets up anyway and its entities go available
        as soon as the core ecobee integration is readable.
        """
        try:
            thermostats = find_thermostat_list(self.hass)
        except Exception as err:  # noqa: BLE001 - the flow must never crash
            _LOGGER.debug("Could not inspect the ecobee integration: %s", err)
            thermostats = None

        if not thermostats:
            return (
                DEFAULT_THERMOSTAT_ID,
                None,
                "The core ecobee integration is not readable yet. Setup will "
                "continue and the entities will populate once it is.",
            )

        thermostat = match_thermostat(thermostats, DEFAULT_THERMOSTAT_ID)
        if thermostat is None:
            thermostat = next(
                (item for item in thermostats if isinstance(item, dict) and item.get("identifier")),
                None,
            )
        if thermostat is None:
            return (
                DEFAULT_THERMOSTAT_ID,
                None,
                "No usable thermostat was found on the ecobee account.",
            )

        identifier = str(thermostat.get("identifier"))
        name = thermostat.get("name") if isinstance(thermostat.get("name"), str) else None
        events = thermostat.get("events")
        count = len(events) if isinstance(events, list) else 0
        return (
            identifier,
            name,
            f"Found thermostat {name or identifier} ({identifier}) with "
            f"{count} event(s) in its current events array.",
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the single confirmation step."""
        identifier, name, status = self._discover()

        if user_input is not None:
            await self.async_set_unique_id(identifier)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"{name} utility events" if name else "Ecobee Utility Events",
                data={CONF_THERMOSTAT_ID: identifier},
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
            description_placeholders={
                "thermostat": name or identifier,
                "identifier": identifier,
                "status": status,
            },
        )
