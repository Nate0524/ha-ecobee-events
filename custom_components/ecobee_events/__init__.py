"""Ecobee Utility Events.

Home Assistant's core ecobee integration fetches the full thermostat object from
api.ecobee.com every ~3 minutes, including an "events" array, and then discards
it. climate.py's preset_mode property recognises exactly three event types --
"hold", anything starting with "auto", and "vacation" -- so the "touPrecool" and
"touSetback" events a utility uses for time-of-use curtailment fall through the
loop with no logging, no attribute and no state. It also skips any event with
running == False, which throws away the advance warning that a queued event
provides.

This integration re-reads that array out of the core integration's in-memory
data. It opens no second connection to api.ecobee.com, consumes no additional
API quota and needs no credentials of its own.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_THERMOSTAT_ID,
    DEFAULT_THERMOSTAT_ID,
    DOMAIN,
    ECOBEE_DOMAIN,
    EXCLUDED_EVENT_TYPE,
    MANUFACTURER,
    MODEL,
    SCAN_INTERVAL,
    STALE_AFTER,
    UTILITY_EVENT_PREFIX,
    UTILITY_EVENT_TYPES,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

type EcobeeEventsConfigEntry = ConfigEntry[EcobeeEventsCoordinator]


# ---------------------------------------------------------------------------
# Decoding helpers
# ---------------------------------------------------------------------------


def tenths_to_degrees(value: Any) -> float | None:
    """Convert an ecobee tenths-of-a-degree-F integer to degrees F.

    786 -> 78.6, 20 -> 2.0, -40 -> -4.0. Returns None for anything that is not
    an integer-ish value.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(int(value) / 10.0, 1)
    except (TypeError, ValueError):
        return None


def combine_local(date_str: Any, time_str: Any) -> datetime | None:
    """Combine ecobee's split date and time into an aware local datetime.

    The thermostat reports "startDate": "2026-09-09" and "startTime":
    "17:00:00" in the thermostat's own local time, which is the same zone the
    user configured in Home Assistant. dt_util.DEFAULT_TIME_ZONE is a
    zoneinfo.ZoneInfo, so replace() is DST-correct here.
    """
    if not isinstance(date_str, str) or not date_str.strip():
        return None

    raw_time = time_str.strip() if isinstance(time_str, str) and time_str.strip() else "00:00:00"
    parts = raw_time.split(":")
    while len(parts) < 3:
        parts.append("00")

    try:
        naive = datetime.strptime(
            f"{date_str.strip()} {parts[0]}:{parts[1]}:{parts[2]}", "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        return None

    return naive.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)


def parse_thermostat_utc(value: Any) -> datetime | None:
    """Parse the thermostat's "utcTime"/"lastModified" string into an aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def is_utility_event(raw: dict[str, Any]) -> bool:
    """Return True for a utility time-of-use / demand-response curtailment event."""
    event_type = raw.get("type")
    if not isinstance(event_type, str):
        return False
    lowered = event_type.strip().lower()
    return lowered.startswith(UTILITY_EVENT_PREFIX) or lowered in UTILITY_EVENT_TYPES


def is_permanent_hold(raw: dict[str, Any]) -> bool:
    """Return True for a plain setpoint "hold" event.

    The one observed in the field is type "hold" / name "auto", which HA
    surfaces as preset_mode "temp". It is a plain hold, not a curtailment, and
    is excluded from every entity here.

    Matched on TYPE ONLY, deliberately. Keying on the name as well would let a
    hold named anything else ("hold", "sched", "", None) leak into the event
    lists and, worse, silently blank out the permanent_hold_* change detector
    that the re-assert automation depends on. The name is surfaced as the
    permanent_hold_name diagnostic instead of being used as a match condition.
    """
    event_type = raw.get("type")
    return isinstance(event_type, str) and event_type.strip().lower() == EXCLUDED_EVENT_TYPE


def _minutes_between(now: datetime, target: datetime | None) -> int | None:
    """Whole minutes from now until target; negative once target is in the past."""
    if target is None:
        return None
    return int(round((target - now).total_seconds() / 60.0))


@dataclass(slots=True)
class DecodedEvent:
    """One decoded entry from the thermostat's events array."""

    event_type: str
    event_name: str | None
    running: bool
    is_utility: bool
    start: datetime | None
    end: datetime | None
    attributes: dict[str, Any] = field(default_factory=dict)


def decode_event(raw: dict[str, Any], now: datetime) -> DecodedEvent:
    """Decode one raw ecobee event dict into display-ready values."""
    event_type = raw.get("type") if isinstance(raw.get("type"), str) else ""
    event_name = raw.get("name") if isinstance(raw.get("name"), str) else None
    running = bool(raw.get("running"))
    relative = bool(raw.get("isTemperatureRelative"))
    absolute = bool(raw.get("isTemperatureAbsolute"))
    start = combine_local(raw.get("startDate"), raw.get("startTime"))
    end = combine_local(raw.get("endDate"), raw.get("endTime"))
    link_ref = raw.get("linkRef") if isinstance(raw.get("linkRef"), str) else None
    hold_ref = raw.get("holdClimateRef") if isinstance(raw.get("holdClimateRef"), str) else None

    attributes: dict[str, Any] = {
        "event_type": event_type,
        "event_name": event_name,
        "running": running,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        # Relative offsets are only meaningful on a relative event.
        "cool_relative_f": tenths_to_degrees(raw.get("coolRelativeTemp")) if relative else None,
        "heat_relative_f": tenths_to_degrees(raw.get("heatRelativeTemp")) if relative else None,
        # And absolute hold temps are only meaningful on an absolute event: on a
        # relative touSetback, coolHoldTemp/heatHoldTemp are inert placeholders
        # equal to the thermostat's range limits, NOT the setpoint being applied.
        "cool_hold_f": tenths_to_degrees(raw.get("coolHoldTemp")) if not relative else None,
        "heat_hold_f": tenths_to_degrees(raw.get("heatHoldTemp")) if not relative else None,
        "is_temperature_relative": relative,
        "is_temperature_absolute": absolute,
        "is_indefinite": bool(raw.get("isIndefinite")),
        "is_optional": bool(raw.get("isOptional")),
        "link_ref": link_ref or None,
        "hold_climate_ref": hold_ref or None,
        "is_utility_event": is_utility_event(raw),
        "minutes_until_start": _minutes_between(now, start),
        "minutes_until_end": _minutes_between(now, end),
        # A snapshot copy: pyecobee rebinds its thermostat list on every refresh,
        # so holding the original dict would be a silent staleness trap.
        "raw_event": dict(raw),
    }

    return DecodedEvent(
        event_type=event_type,
        event_name=event_name,
        running=running,
        is_utility=attributes["is_utility_event"],
        start=start,
        end=end,
        attributes=attributes,
    )


# ---------------------------------------------------------------------------
# Locating the core ecobee integration's in-memory thermostat list
# ---------------------------------------------------------------------------


def _probe(obj: Any) -> Iterator[Any]:
    """Yield objects that might expose a `.thermostats` list.

    Deliberately duck-typed. As of HA 2025.2+ the shape is
    entry.runtime_data (EcobeeData) -> .ecobee (pyecobee.Ecobee) -> .thermostats,
    but the ecobee integration is one of the last core holdouts still on
    @Throttle + entity polling. When it migrates to a DataUpdateCoordinator the
    object will move to .api / .coordinator, so probe for those too.
    """
    if obj is None:
        return
    yield obj
    for attr in ("ecobee", "api", "client", "coordinator"):
        child = getattr(obj, attr, None)
        if child is None:
            continue
        yield child
        for sub in ("ecobee", "api", "client"):
            grandchild = getattr(child, sub, None)
            if grandchild is not None:
                yield grandchild


def _thermostat_list(obj: Any) -> list[Any] | None:
    """Return a non-empty list of thermostat dicts from a candidate object."""
    for attr in ("thermostats", "data"):
        value = getattr(obj, attr, None)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return None


def find_thermostat_list(hass: HomeAssistant) -> list[Any] | None:
    """Return the core ecobee integration's live thermostat list, or None.

    Re-walked on every read. pyecobee's get_thermostats() rebinds
    self.thermostats to a brand new list of brand new dicts on every refresh, so
    any cached reference silently freezes at the moment it was taken.
    """
    # Primary path: HA >= 2025.2, entry.runtime_data. async_loaded_entries()
    # already filters to ConfigEntryState.LOADED, and runtime_data is physically
    # deleted on unload, hence getattr with a default rather than bare access.
    for entry in hass.config_entries.async_loaded_entries(ECOBEE_DOMAIN):
        for candidate in _probe(getattr(entry, "runtime_data", None)):
            if (thermostats := _thermostat_list(candidate)) is not None:
                return thermostats

    legacy = hass.data.get(ECOBEE_DOMAIN)

    # Fallback A: HA <= 2025.1 stored the bare EcobeeData at hass.data["ecobee"].
    if legacy is not None and not isinstance(legacy, dict):
        for candidate in _probe(legacy):
            if (thermostats := _thermostat_list(candidate)) is not None:
                return thermostats

    # Fallback B (emergency only): hass.data["ecobee"]["thermostats"] is a list
    # of live climate ENTITY objects kept for a legacy service target. It is
    # pylint-flagged in core as queued for deletion; each entity still exposes
    # .data (EcobeeData), which gets us back to the real list.
    if isinstance(legacy, dict):
        for entity in legacy.get("thermostats") or []:
            for candidate in _probe(getattr(entity, "data", None)):
                if (thermostats := _thermostat_list(candidate)) is not None:
                    return thermostats

    return None


def match_thermostat(thermostats: list[Any], identifier: str) -> dict[str, Any] | None:
    """Return the thermostat dict with this identifier.

    Matched by identifier, never by index: the index is positional and
    get_thermostat() raises IndexError on a bad one.
    """
    for thermostat in thermostats:
        if isinstance(thermostat, dict) and thermostat.get("identifier") == identifier:
            return thermostat
    return None


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EcobeeEventsData:
    """Everything the entities need for one read of the events array."""

    identifier: str
    thermostat_name: str | None
    thermostat_rev: str | None
    thermostat_time: str | None
    source_updated: datetime | None
    data_age_seconds: int | None
    data_stale: bool
    events: list[DecodedEvent]
    utility_events: list[DecodedEvent]
    active: DecodedEvent | None
    upcoming: DecodedEvent | None
    permanent_hold_present: bool
    permanent_hold_name: str | None
    permanent_hold_started: str | None
    permanent_hold_cool_f: float | None
    permanent_hold_heat_f: float | None

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Common diagnostic attributes shared by every entity.

        Every key here must change ONLY when the upstream data changes. An
        attribute recomputed from the wall clock (the age in seconds this used
        to publish) makes the attribute dict unique on every 30-second tick,
        and HA's StateMachine.async_set only short-circuits when the state and
        attributes are byte-identical -- so a single live-computed key forces a
        state write, and a recorder row, on all three entities every 30
        seconds forever, even with an idle thermostat and zero events.
        source_data_updated is the same information as a fixed timestamp: it
        moves only when ecobee actually refreshes, roughly every 3 minutes.
        Templates can still get the age with
        `now() - as_datetime(state_attr(..., 'source_data_updated'))`.
        """
        return {
            "thermostat_identifier": self.identifier,
            "thermostat_name": self.thermostat_name,
            "thermostat_rev": self.thermostat_rev,
            "thermostat_time": self.thermostat_time,
            "source_data_updated": (
                self.source_updated.isoformat() if self.source_updated else None
            ),
            "source_data_stale": self.data_stale,
        }


class EcobeeEventsCoordinator(DataUpdateCoordinator[EcobeeEventsData]):
    """Poll the core ecobee integration's in-memory thermostat data.

    This "poll" is a dictionary read, not a network call. The underlying data is
    refreshed by the core integration roughly every 3 minutes for free.
    """

    def __init__(
        self, hass: HomeAssistant, entry: EcobeeEventsConfigEntry, identifier: str
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {identifier}",
            update_interval=SCAN_INTERVAL,
        )
        self.identifier = identifier
        self._warned: set[str] = set()

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        """Log a warning the first time a condition appears, debug thereafter."""
        if key in self._warned:
            _LOGGER.debug(message, *args)
            return
        self._warned.add(key)
        _LOGGER.warning(message, *args)

    def _clear_warning(self, key: str) -> None:
        """Allow a condition to warn again after it has recovered."""
        self._warned.discard(key)

    async def _async_update_data(self) -> EcobeeEventsData:
        """Read and decode the events array. Never raises anything but UpdateFailed."""
        try:
            data = self._read()
        except UpdateFailed:
            raise
        except Exception as err:  # noqa: BLE001 - must never break HA
            self._warn_once(
                "unexpected",
                "Unexpected error reading the ecobee events array (%s: %s). "
                "The core ecobee integration's data shape may have changed; "
                "%s entities will be unavailable",
                type(err).__name__,
                err,
                DOMAIN,
            )
            raise UpdateFailed(f"Unexpected error reading ecobee event data: {err}") from err

        # Cleared on the success path like every other warn-once key, so a
        # breakage that is fixed and then recurs in a later HA release warns
        # again instead of hiding at debug level forever.
        self._clear_warning("unexpected")
        return data

    def _read(self) -> EcobeeEventsData:
        """Walk the accessor chain from scratch and decode. Caches nothing."""
        thermostats = find_thermostat_list(self.hass)
        if not thermostats:
            self._warn_once(
                "no_ecobee",
                "Could not reach the core 'ecobee' integration's thermostat data "
                "(not loaded, still setting up, or its internals changed); %s "
                "entities will be unavailable until it is readable",
                DOMAIN,
            )
            raise UpdateFailed("ecobee integration data not available")
        self._clear_warning("no_ecobee")

        thermostat = match_thermostat(thermostats, self.identifier)
        if thermostat is None:
            only = thermostats[0] if len(thermostats) == 1 else None
            if isinstance(only, dict) and only.get("identifier"):
                self._warn_once(
                    "wrong_identifier",
                    "Configured ecobee thermostat %s was not found; using the "
                    "only thermostat on the account instead (%s)",
                    self.identifier,
                    only.get("identifier"),
                )
                thermostat = only
            else:
                self._warn_once(
                    "missing_identifier",
                    "ecobee thermostat %s is not present in the account's %d "
                    "thermostat(s); %s entities will be unavailable",
                    self.identifier,
                    len(thermostats),
                    DOMAIN,
                )
                raise UpdateFailed(f"thermostat {self.identifier} not found")
        else:
            self._clear_warning("wrong_identifier")
            self._clear_warning("missing_identifier")

        now = dt_util.now()

        # Staleness. pyecobee leaves self.thermostats untouched when a request
        # fails, so a broken token freezes the data with no exception anywhere.
        source_updated = parse_thermostat_utc(thermostat.get("utcTime"))
        if source_updated is None:
            source_updated = parse_thermostat_utc(thermostat.get("lastModified"))
        age_seconds: int | None = None
        stale = False
        if source_updated is not None:
            age_seconds = max(0, int((dt_util.utcnow() - source_updated).total_seconds()))
            stale = age_seconds > STALE_AFTER.total_seconds()
        if stale:
            self._warn_once(
                "stale",
                "ecobee thermostat data is %d seconds old (expected a refresh "
                "every ~180s); the upstream poll may be failing silently",
                age_seconds,
            )
        else:
            self._clear_warning("stale")

        raw_events = thermostat.get("events")
        if not isinstance(raw_events, list):
            raw_events = []

        events: list[DecodedEvent] = []
        holds: list[dict[str, Any]] = []

        for raw in raw_events:
            if not isinstance(raw, dict):
                continue
            if is_permanent_hold(raw):
                # Excluded from every entity, but its (startTime, coolHoldTemp)
                # tuple is the cheapest way to see the thermostat re-assert a
                # setpoint, so it is kept as scalar diagnostics only.
                holds.append(raw)
                continue
            events.append(decode_event(raw, now))

        # If the thermostat somehow carries more than one hold, report the one
        # that started most recently: that is the one currently in force, and
        # the array order is not stable between polls.
        hold_present = bool(holds)
        hold_name: str | None = None
        hold_started: str | None = None
        hold_cool: float | None = None
        hold_heat: float | None = None
        if holds:
            def _hold_start(raw: dict[str, Any]) -> tuple[bool, datetime]:
                """Sort key: a hold with a real start beats one without."""
                started = combine_local(raw.get("startDate"), raw.get("startTime"))
                return (started is not None, started or now)

            hold = max(holds, key=_hold_start)
            started = combine_local(hold.get("startDate"), hold.get("startTime"))
            hold_name = hold.get("name") if isinstance(hold.get("name"), str) else None
            hold_started = started.isoformat() if started else None
            hold_cool = tenths_to_degrees(hold.get("coolHoldTemp"))
            hold_heat = tenths_to_degrees(hold.get("heatHoldTemp"))

        # Sort by start time so "first" is deterministic; the array order from
        # the API is NOT stable between polls.
        events.sort(key=lambda item: (item.start is None, item.start or now))
        utility_events = [item for item in events if item.is_utility]

        # Active: the utility event currently shaping the setpoint.
        #
        # An event is only a candidate if its window has not closed. Selecting
        # on the `running` flag alone latches the sensor ON after the event is
        # over, in two ways that both really happen:
        #   1. Upstream refreshes only every ~180s, so at 17:01 the in-memory
        #      array can still be the 16:59 one, in which touPrecool (which
        #      ended at 17:00) is running:true and touSetback is running:false.
        #      Every weekday, for up to three minutes, the sensor would report
        #      the precool offset while the thermostat is applying the setback.
        #   2. pyecobee leaves self.thermostats untouched when a request fails,
        #      so an expired token freezes running:true forever and the sensor
        #      never turns off -- no "curtailment ended" trigger ever fires.
        #
        # So: drop anything whose end has passed, then treat an event as active
        # once its scheduled start has passed even if the cached copy has not
        # been flagged running yet. That closes the trailing-ON latch AND the
        # leading gap, with no OFF blip at the precool/setback handover.
        not_ended = [item for item in utility_events if item.end is None or item.end > now]
        in_window = [item for item in not_ended if item.start is not None and item.start <= now]
        active: DecodedEvent | None = None
        if in_window:
            # If several overlap, the one that started most recently is the one
            # actually in force.
            active = max(in_window, key=lambda item: item.start)
        else:
            # Last resort: flagged running but with no parseable start time.
            # A running event whose start is still in the future is NOT active
            # -- it is reported by the "upcoming" sensor instead.
            undated = [item for item in not_ended if item.running and item.start is None]
            active = undated[0] if undated else None

        # Upcoming: anything whose start is still in the future. In practice
        # that means queued with running == False, which is the whole
        # early-warning signal and exactly what core's preset_mode discards
        # with `if not event["running"]: continue`. The running flag is not
        # part of the test, so an event the thermostat flags running ahead of
        # its own start is still reported here rather than falling into the
        # gap between the two sensors.
        queued = [item for item in utility_events if item.start is not None and item.start > now]
        upcoming = min(queued, key=lambda item: item.start) if queued else None

        return EcobeeEventsData(
            identifier=str(thermostat.get("identifier") or self.identifier),
            thermostat_name=thermostat.get("name"),
            thermostat_rev=thermostat.get("thermostatRev"),
            thermostat_time=thermostat.get("thermostatTime"),
            source_updated=source_updated,
            data_age_seconds=age_seconds,
            data_stale=stale,
            events=events,
            utility_events=utility_events,
            active=active,
            upcoming=upcoming,
            permanent_hold_present=hold_present,
            permanent_hold_name=hold_name,
            permanent_hold_started=hold_started,
            permanent_hold_cool_f=hold_cool,
            permanent_hold_heat_f=hold_heat,
        )


# ---------------------------------------------------------------------------
# Shared entity base
# ---------------------------------------------------------------------------


class EcobeeEventsEntity(CoordinatorEntity[EcobeeEventsCoordinator]):
    """Base class for the entities in this integration.

    has_entity_name is deliberately False so the object_ids are the flat,
    predictable ones documented in the README rather than being prefixed with
    the device name.
    """

    _attr_has_entity_name = False

    def __init__(self, coordinator: EcobeeEventsCoordinator, key: str, name: str) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.identifier}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.identifier)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name="Ecobee Utility Events",
        )

    @property
    def _data(self) -> EcobeeEventsData | None:
        """Return the last successful read, if there is one."""
        return self.coordinator.data if self.coordinator.last_update_success else None

    def _base_attributes(self) -> dict[str, Any]:
        """Return diagnostics common to every entity."""
        data = self._data
        return dict(data.diagnostics) if data else {}


# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: EcobeeEventsConfigEntry) -> bool:
    """Set up Ecobee Utility Events from a config entry."""
    identifier = str(entry.data.get(CONF_THERMOSTAT_ID) or DEFAULT_THERMOSTAT_ID)
    coordinator = EcobeeEventsCoordinator(hass, entry, identifier)

    # Deliberately async_refresh(), not async_config_entry_first_refresh(): the
    # latter raises ConfigEntryNotReady when the core ecobee integration has not
    # finished setting up, which would leave these entities missing entirely
    # rather than merely unavailable. Setup must always succeed.
    await coordinator.async_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _refresh_when_started(_hass: HomeAssistant) -> None:
        """Pick the data up as soon as HA has finished starting."""
        await coordinator.async_request_refresh()

    entry.async_on_unload(async_at_started(hass, _refresh_when_started))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EcobeeEventsConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
