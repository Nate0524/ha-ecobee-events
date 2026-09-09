"""Constants for the Ecobee Utility Events integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "ecobee_events"

# Domain of the CORE integration we read from. Deliberately a hardcoded string:
# importing anything from homeassistant.components.ecobee would create a real
# dependency edge and a hard crash surface if that integration is restructured.
ECOBEE_DOMAIN: Final = "ecobee"

# The ecobee thermostat this integration watches. Stored in the config entry so
# unique_ids stay stable even if the account later gains a second thermostat.
CONF_THERMOSTAT_ID: Final = "thermostat_id"
DEFAULT_THERMOSTAT_ID: Final = "531672042411"

# Reading the events array is a dictionary lookup in another integration's
# memory: no network traffic and no ecobee API quota consumed. 30s is cheap and
# gives the on-peak setback boundary sub-minute resolution.
SCAN_INTERVAL: Final = timedelta(seconds=30)

# The core ecobee integration refreshes every ~180s (util.Throttle in
# homeassistant/components/ecobee/__init__.py, MIN_TIME_BETWEEN_UPDATES). Older
# than this means the upstream poll is failing silently: pyecobee leaves the
# previous thermostat list in place when a request fails, so stale data never
# raises. Surfaced as an attribute, never as unavailability.
STALE_AFTER: Final = timedelta(minutes=10)

# A utility curtailment event is any event whose type starts with "tou"
# (touPrecool / touSetback, what Xcel Energy actually sends) or the literal
# "demandResponse" type, in case the utility ever switches to it. Matched
# case-insensitively.
UTILITY_EVENT_PREFIX: Final = "tou"
UTILITY_EVENT_TYPES: Final = frozenset({"demandresponse"})

# A plain "hold" event (the observed one is hold/auto, which HA surfaces as
# preset_mode "temp") is a user or thermostat setpoint hold, not a curtailment,
# and must never trip these entities or appear in the event lists. Matched on
# type alone: a hold under any other name still must not leak into the lists,
# and still must feed the permanent_hold_* re-assert change detector.
EXCLUDED_EVENT_TYPE: Final = "hold"

MANUFACTURER: Final = "ecobee"
MODEL: Final = "Thermostat utility events"

# Attribute keys shared by the entities.
ATTR_EVENT_TYPE: Final = "event_type"
ATTR_EVENT_NAME: Final = "event_name"
ATTR_START: Final = "start"
ATTR_END: Final = "end"
ATTR_COOL_RELATIVE_F: Final = "cool_relative_f"
ATTR_HEAT_RELATIVE_F: Final = "heat_relative_f"
ATTR_IS_TEMPERATURE_RELATIVE: Final = "is_temperature_relative"
ATTR_LINK_REF: Final = "link_ref"
ATTR_RAW_EVENT: Final = "raw_event"
ATTR_MINUTES_UNTIL_START: Final = "minutes_until_start"
