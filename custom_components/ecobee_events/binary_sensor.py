"""Binary sensors for ecobee utility time-of-use curtailment events."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DecodedEvent, EcobeeEventsConfigEntry, EcobeeEventsCoordinator, EcobeeEventsEntity

# The attribute contract. Every key is always present, set to None when there is
# no matching event, so templates and automations never hit an undefined key.
_ACTIVE_KEYS: tuple[str, ...] = (
    "event_type",
    "event_name",
    "start",
    "end",
    "cool_relative_f",
    "heat_relative_f",
    "is_temperature_relative",
    "link_ref",
    "minutes_until_end",
    "raw_event",
)

_UPCOMING_KEYS: tuple[str, ...] = (
    "event_type",
    "event_name",
    "start",
    "end",
    "cool_relative_f",
    "heat_relative_f",
    "is_temperature_relative",
    "link_ref",
    "minutes_until_start",
    "raw_event",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcobeeEventsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the ecobee utility event binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            EcobeeUtilityEventActive(coordinator),
            EcobeeUtilityEventUpcoming(coordinator),
        ]
    )


class _EcobeeUtilityEventBinarySensor(EcobeeEventsEntity, BinarySensorEntity):
    """Shared attribute plumbing for the two event binary sensors."""

    _keys: tuple[str, ...] = ()

    def _event(self) -> DecodedEvent | None:
        """Return the event this entity reports on, if any."""
        raise NotImplementedError

    @property
    def is_on(self) -> bool | None:
        """Return True when the relevant event exists."""
        if self._data is None:
            return None
        return self._event() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the decoded event, plus shared diagnostics."""
        attributes: dict[str, Any] = {key: None for key in self._keys}
        event = self._event() if self._data is not None else None
        if event is not None:
            for key in self._keys:
                attributes[key] = event.attributes.get(key)
        attributes.update(self._base_attributes())
        return attributes


class EcobeeUtilityEventActive(_EcobeeUtilityEventBinarySensor):
    """ON while a utility curtailment event is running on the thermostat.

    Keyed on an event type starting with "tou" (touPrecool / touSetback) or the
    literal "demandResponse" type, with running == True. The permanent
    "hold"/"auto" event is excluded: it is a plain setpoint hold, not a
    curtailment.
    """

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:transmission-tower"
    _keys = _ACTIVE_KEYS

    def __init__(self, coordinator: EcobeeEventsCoordinator) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, "utility_event_active", "Ecobee Utility Event Active")

    def _event(self) -> DecodedEvent | None:
        data = self._data
        return data.active if data else None


class EcobeeUtilityEventUpcoming(_EcobeeUtilityEventBinarySensor):
    """ON while a utility curtailment event is queued but not yet running.

    The thermostat publishes the next event into the array well before it
    starts, with running == False: a touSetback for 17:00 was observed queued
    and visible from 15:45. Core's climate.preset_mode throws that away with
    `if not event["running"]: continue`, which is why this early warning does
    not exist anywhere else in Home Assistant.
    """

    _attr_icon = "mdi:clock-alert-outline"
    _keys = _UPCOMING_KEYS

    def __init__(self, coordinator: EcobeeEventsCoordinator) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, "utility_event_upcoming", "Ecobee Utility Event Upcoming")

    def _event(self) -> DecodedEvent | None:
        data = self._data
        return data.upcoming if data else None
