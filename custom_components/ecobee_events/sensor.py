"""Sensor exposing the whole decoded ecobee events array."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcobeeEventsConfigEntry, EcobeeEventsCoordinator, EcobeeEventsEntity

STATE_NONE = "none"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcobeeEventsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the ecobee active event sensor."""
    async_add_entities([EcobeeActiveEventSensor(entry.runtime_data)])


class EcobeeActiveEventSensor(EcobeeEventsEntity, SensorEntity):
    """The running utility event's type, with the full decoded array attached.

    State is the running curtailment event's type -- "touSetback",
    "touPrecool", "demandResponse" -- or "none". The permanent "hold"/"auto"
    event never appears: it is a plain setpoint hold, not a curtailment.
    """

    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: EcobeeEventsCoordinator) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, "active_event", "Ecobee Active Event")

    @property
    def native_value(self) -> str | None:
        """Return the running event type, or "none"."""
        data = self._data
        if data is None:
            return None
        if data.active is None:
            return STATE_NONE
        return data.active.event_type or STATE_NONE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return every decoded event so the whole array is inspectable."""
        data = self._data
        if data is None:
            return {}

        active = data.active
        upcoming = data.upcoming

        attributes: dict[str, Any] = {
            # The running event, flattened for convenient templating.
            "event_type": active.event_type if active else None,
            "event_name": active.event_name if active else None,
            "start": active.attributes.get("start") if active else None,
            "end": active.attributes.get("end") if active else None,
            "cool_relative_f": active.attributes.get("cool_relative_f") if active else None,
            "heat_relative_f": active.attributes.get("heat_relative_f") if active else None,
            "is_temperature_relative": (
                active.attributes.get("is_temperature_relative") if active else None
            ),
            "link_ref": active.attributes.get("link_ref") if active else None,
            "minutes_until_end": active.attributes.get("minutes_until_end") if active else None,
            # The queued event, if one is waiting.
            "upcoming_event_type": upcoming.event_type if upcoming else None,
            "upcoming_event_name": upcoming.event_name if upcoming else None,
            "upcoming_start": upcoming.attributes.get("start") if upcoming else None,
            "upcoming_end": upcoming.attributes.get("end") if upcoming else None,
            "minutes_until_start": (
                upcoming.attributes.get("minutes_until_start") if upcoming else None
            ),
            # Everything, decoded. "events" is every event on the thermostat
            # except the excluded hold; "utility_events" is the curtailment
            # subset of it, minus the raw_event copy, which is already in
            # "events" and would otherwise be stored twice in every recorder
            # row for no benefit.
            "event_count": len(data.events),
            "utility_event_count": len(data.utility_events),
            "events": [item.attributes for item in data.events],
            "utility_events": [
                {key: value for key, value in item.attributes.items() if key != "raw_event"}
                for item in data.utility_events
            ],
            # The excluded hold, as scalars only. It is not a curtailment, but
            # the thermostat destroys and recreates it with a new startTime and
            # a new absolute setpoint every time it re-asserts, so this pair is
            # the cheapest way to watch that happen.
            "permanent_hold_present": data.permanent_hold_present,
            "permanent_hold_name": data.permanent_hold_name,
            "permanent_hold_started": data.permanent_hold_started,
            "permanent_hold_cool_f": data.permanent_hold_cool_f,
            "permanent_hold_heat_f": data.permanent_hold_heat_f,
        }
        attributes.update(self._base_attributes())
        return attributes
