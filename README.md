# Ecobee Utility Events

A Home Assistant custom integration that surfaces the `events` array your ecobee
thermostat already sends to Home Assistant every three minutes, and that Home
Assistant throws away.

It is read-only. It opens no connection to `api.ecobee.com`, needs no API key,
and consumes none of your ecobee rate limit.

---

## Why this exists

Your utility can push time-of-use curtailment straight into the thermostat. On
an Xcel Energy time-of-use plan in Colorado this shows up as two event types
stacked into the thermostat object:

| Event type | What it does |
| --- | --- |
| `touPrecool` | Drops the cooling setpoint before the on-peak window (for example `coolRelativeTemp: -40`, meaning -4.0 F) |
| `touSetback` | Raises it during the on-peak window (for example `coolRelativeTemp: 20`, meaning +2.0 F, 17:00 to 21:00) |

Home Assistant's core `ecobee` integration fetches all of this. Its
`Thermostat.preset_mode` property in `homeassistant/components/ecobee/climate.py`
then iterates the array and recognises exactly three event types: `hold`,
anything starting with `auto`, and `vacation`. A `touSetback` matches none of
them, so it falls out of the loop with no logging, no attribute, and no state.

Worse, the very first line of that loop is:

```python
if not event["running"]:
    continue
```

The thermostat queues the next event into the array long before it starts, with
`running: false`. On a real thermostat a 17:00 `touSetback` was already visible
and queued at 15:45 - 75 minutes of advance warning. Core discards that too.

The result is that a utility can hold your compressor off for two hours and
raise the room temperature 6-7 F, and nothing in Home Assistant's state machine
can tell you it happened. `climate.my_ecobee` looks completely normal: the
setpoint simply moves, and `preset_mode` reports whatever hold happens to be in
place. There is no back door either - `extra_state_attributes` on the climate
entity exposes only fan, climate mode, equipment status, fan minimum on time and
sensor lists. The events array never reaches the state machine at all.

This integration reads that array out of the core integration's memory and turns
it into entities.

---

## Entities

All three exclude any event of type `hold` - the one seen in the field is
`hold` / `auto`, which Home Assistant surfaces as `preset_mode: temp`. It is an
ordinary setpoint hold, not a curtailment, and letting it trip these entities
would make them useless. The match is on the type alone, never on the name, so
a hold named anything else is still excluded and still feeds the
`permanent_hold_*` re-assert detector below.

A "utility event" here means any event whose `type` starts with `tou`
(case-insensitive), or whose type is exactly `demandResponse`. Both are matched
because `touPrecool` / `touSetback` is what is actually observed in the field,
while `demandResponse` is the type the API documents for the same purpose.

### `binary_sensor.ecobee_utility_event_active`

`on` while a utility event is in force on the thermostat.

"In force" means its `end` has not passed **and** either the thermostat has
flagged it `running`, or its scheduled `start` has passed. Both halves matter.
Selecting on `running` alone latches the sensor `on` after the window closes,
because the upstream data is up to three minutes old (so at 17:01 the cached
array still says the 17:00 `touPrecool` is running) and because `pyecobee`
freezes its last good copy when a request fails (so an expired token would pin
the sensor `on` forever and the "curtailment ended" trigger would never fire).
Promoting an event at its scheduled start closes the matching gap at the
`touPrecool` to `touSetback` handover, so there is no spurious `off` blip
between the two.

Attributes: `event_type`, `event_name`, `start` (ISO 8601, local), `end`,
`cool_relative_f`, `heat_relative_f`, `is_temperature_relative`, `link_ref`,
`minutes_until_end`, `raw_event`, plus the shared diagnostics below. Every key
is always present and is `null` when no event is running.

### `binary_sensor.ecobee_utility_event_upcoming`

`on` while a utility event is queued (`running: false`) with a start time still
in the future. This is the advance-warning signal, and it does not exist
anywhere else in Home Assistant.

Same attributes, with `minutes_until_start` in place of `minutes_until_end`.

### `sensor.ecobee_active_event`

State is the running event's type (`touSetback`, `touPrecool`,
`demandResponse`) or the string `none`.

Attributes flatten the running event and the queued event for convenient
templating, and carry the whole decoded array so everything is inspectable from
Developer Tools:

- `event_type`, `event_name`, `start`, `end`, `cool_relative_f`,
  `heat_relative_f`, `is_temperature_relative`, `link_ref`, `minutes_until_end`
  for the running event
- `upcoming_event_type`, `upcoming_event_name`, `upcoming_start`,
  `upcoming_end`, `minutes_until_start` for the queued event
- `events` - every decoded event except the excluded hold
- `utility_events` - the curtailment subset of the above, minus each entry's
  `raw_event` copy (it is already in `events`; carrying it twice only doubled
  the size of every recorder row)
- `event_count`, `utility_event_count`
- `permanent_hold_present`, `permanent_hold_name`, `permanent_hold_started`,
  `permanent_hold_cool_f`, `permanent_hold_heat_f` - see "Watching the
  thermostat re-assert" below

### Shared diagnostic attributes

Present on all three entities:

`thermostat_identifier`, `thermostat_name`, `thermostat_rev`,
`thermostat_time`, `source_data_updated`, `source_data_stale`.

`source_data_updated` is the thermostat's own `utcTime`, as an ISO 8601
timestamp. It is deliberately a timestamp rather than an age in seconds: an
age recomputed from the wall clock would make the attribute dict different on
every 30-second tick, and Home Assistant only skips a state write when the
state **and** attributes are byte-identical - so one live-computed key would
write three new states, and three recorder rows, every 30 seconds forever,
even with an idle thermostat and no events at all. Compute the age in a
template instead:

```jinja
{{ (now() - as_datetime(state_attr('sensor.ecobee_active_event',
                                   'source_data_updated'))).total_seconds() }}
```

### Decoded event shape

Each entry in `events` / `utility_events` looks like this:

```yaml
event_type: touSetback
event_name: sbk170000
running: true
start: "2026-09-09T17:00:00-06:00"
end: "2026-09-09T21:00:00-06:00"
cool_relative_f: 2.0
heat_relative_f: 4.0
cool_hold_f: null
heat_hold_f: null
is_temperature_relative: true
is_temperature_absolute: false
is_indefinite: false
is_optional: true
link_ref: "087743da68000292384"
hold_climate_ref: null
is_utility_event: true
minutes_until_start: -9
minutes_until_end: 231
raw_event: { ... the untouched dict from the API ... }
```

Two decoding rules are worth knowing:

1. **Temperatures are tenths of a degree F in the API.** `786` is 78.6 F,
   `coolRelativeTemp: 20` is +2.0 F, `-40` is -4.0 F. Everything ending in `_f`
   here has already been divided by ten.
2. **Absolute and relative fields are mutually exclusive.** On a relative event
   (`is_temperature_relative: true`) the API still populates `coolHoldTemp` and
   `heatHoldTemp`, but with inert placeholders equal to the thermostat's range
   limits - not the setpoint being applied. This integration therefore reports
   `cool_hold_f` / `heat_hold_f` as `null` on relative events and
   `cool_relative_f` / `heat_relative_f` as `null` on absolute ones. The
   untouched values are always in `raw_event` if you want them.

---

## Installation

### HACS (custom repository)

1. HACS, three-dot menu, Custom repositories.
2. Add this repository URL with category **Integration**.
3. Find "Ecobee Utility Events" in HACS and download it.
4. **Restart Home Assistant.** A newly downloaded custom component is only
   picked up on a full restart - reloading a config entry is not enough.
5. Settings, Devices and services, Add integration, "Ecobee Utility Events".
   There is nothing to fill in; just confirm.

### Manual

Copy `custom_components/ecobee_events/` into your Home Assistant `config`
directory, restart, then add the integration from the UI.

### Requirements

- Home Assistant 2025.2.0 or newer (the point at which the core ecobee
  integration moved its data to `entry.runtime_data`; an older fallback path is
  included but untested on those versions).
- The core `ecobee` integration set up and working. This integration reads its
  data; it does not replace it.

---

## Example automations

Pre-cool the house before the utility raises your setpoint:

```yaml
automation:
  - alias: Pre-cool before ecobee utility setback
    triggers:
      - trigger: numeric_state
        entity_id: binary_sensor.ecobee_utility_event_upcoming
        attribute: minutes_until_start
        below: 60
    conditions:
      - condition: state
        entity_id: binary_sensor.ecobee_utility_event_upcoming
        state: "on"
      - condition: template
        value_template: >
          {{ 'setback' in
             (state_attr('binary_sensor.ecobee_utility_event_upcoming',
                         'event_type') | lower) }}
    actions:
      - action: climate.set_temperature
        target:
          entity_id: climate.my_ecobee
        data:
          temperature: 74
```

Notify when curtailment starts and ends:

```yaml
automation:
  - alias: Notify on ecobee utility curtailment
    triggers:
      - trigger: state
        entity_id: binary_sensor.ecobee_utility_event_active
        to: "on"
        id: start
      - trigger: state
        entity_id: binary_sensor.ecobee_utility_event_active
        to: "off"
        id: end
    actions:
      - action: notify.persistent_notification
        data:
          message: >
            {% if trigger.id == 'start' %}
              Utility curtailment started:
              {{ state_attr('binary_sensor.ecobee_utility_event_active',
                            'event_type') }}
              until
              {{ state_attr('binary_sensor.ecobee_utility_event_active',
                            'end') }}
              ({{ state_attr('binary_sensor.ecobee_utility_event_active',
                             'cool_relative_f') }} F on cooling)
            {% else %}
              Utility curtailment ended.
            {% endif %}
```

### Watching the thermostat re-assert

A one-shot `climate.set_temperature` during a `touSetback` does not stick. The
thermostat destroys and recreates its `hold` / `auto` event with a fresh start
time and a fresh absolute setpoint, observed pushing a setpoint back up within
about seven minutes with nobody at the thermostat.

That is why `permanent_hold_started`, `permanent_hold_cool_f` and
`permanent_hold_name` are exposed on `sensor.ecobee_active_event`, even though
the hold itself is excluded from the event lists. Any event of type `hold`
feeds these, whatever it is called, so the detector cannot be blinded by the
thermostat naming a hold something other than `auto`.
Treat the pair `(permanent_hold_started, permanent_hold_cool_f)` as
a change detector: when it changes while
`binary_sensor.ecobee_utility_event_active` is `on`, the thermostat has just
re-asserted and any correction you applied is gone. Any automation that fights a
setback must re-apply on that trigger, not fire once.

### Recorder

None of these entities writes on a timer. Every attribute is derived from the
upstream data or from an event's own schedule, so a new state is written only
when ecobee's data actually changes (about every three minutes) or when a
minute ticks over inside a live event window. With no events on the
thermostat, the entities are quiet.

`raw_event` is still a full copy of the API's event dict, and
`sensor.ecobee_active_event` carries one per event, so that entity's attribute
blob is a few kilobytes while a curtailment is running. If you would rather
not store it:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.ecobee_active_event
```

The binary sensors are much smaller and worth keeping in history - that is
where the "when was I curtailed" answer lives. If you want them out too, add
`binary_sensor.ecobee_utility_event_*` to the same list.

---

## How it works, and where it is fragile

This integration does something Home Assistant integrations are not normally
supposed to do: **it reads another integration's internal, private data
structures.** That is a deliberate trade, and it is the only option short of
opening a second authenticated connection to the ecobee cloud. Understand the
trade before you rely on it.

### The access path

On each 30-second tick it walks, from scratch:

```
hass.config_entries.async_loaded_entries("ecobee")
  -> entry.runtime_data            (an EcobeeData instance, HA 2025.2+)
  -> .ecobee                       (a pyecobee.Ecobee)
  -> .thermostats                  (list[dict], verbatim from the API)
  -> the dict whose "identifier" matches
  -> ["events"]
```

Design decisions that follow from that:

- **Nothing is ever cached.** `pyecobee.get_thermostats()` rebinds
  `self.thermostats` to a brand new list of brand new dicts on every refresh, so
  a cached reference to the list, a thermostat, or the events array would freeze
  silently with no error and no staleness signal. The whole chain is re-walked
  on every read.
- **The thermostat is matched by `identifier`, never by index.** The index is
  positional and `get_thermostat()` raises `IndexError` on a bad one.
- **Nothing is imported from `homeassistant.components.ecobee`.** Not the
  domain, not `EcobeeData`, not `pyecobee`. Everything is duck-typed against the
  string `"ecobee"`, so a rename or restructure upstream degrades this
  integration to "unavailable" instead of crashing it.
- **The array is iterated and filtered by type, never indexed.** The order is
  not stable between polls; the same three events were observed in two different
  orders 20 minutes apart.
- **No extra API calls, ever.** The core integration already refreshes every
  ~180 seconds (a `util.Throttle` on `EcobeeData.update`), driven by the climate
  platform's 60-second entity poll. This integration free-rides that. It never
  calls `update(no_throttle=True)`, which would bypass the shared throttle and
  double your account's API rate against ecobee's limits.

### Failure behaviour

The integration is built to never break Home Assistant startup:

- Setup always succeeds. If the core ecobee integration is not loaded yet, the
  entities are created **unavailable** and go available on a later tick, rather
  than the entry failing and the entities not existing at all.
- Every failure mode logs a warning exactly once and then drops to debug, so a
  30-second timer cannot spam your log. Recovery clears the flag, including for
  the catch-all "unexpected" case, so a fault that is fixed and later recurs
  warns again instead of staying invisible.
- Any unexpected exception inside the read is caught, converted to an
  `UpdateFailed`, and turns the entities unavailable. Nothing propagates.
- If the configured thermostat identifier is not found but the account has
  exactly one thermostat, that one is used and a warning is logged once.

### Staleness

`pyecobee` leaves its previous thermostat list in place when a request fails, so
an expired token freezes the data with no exception reaching anyone. The
integration therefore compares the thermostat's own `utcTime` against the
current time and exposes `source_data_updated` (that `utcTime`, as an ISO 8601
timestamp) and `source_data_stale` on every entity. Age over ten minutes sets
`source_data_stale` and logs once.

Stale data does **not** mark the entities unavailable, on the grounds that
slightly old information about a running curtailment beats no information. If
you want to be strict, add a condition on `source_data_stale` to your
automations.

Frozen data cannot, however, leave `binary_sensor.ecobee_utility_event_active`
stuck `on`: an event whose `end` has passed is never selected as active, so a
frozen `running: true` expires on its own schedule.

### What will break this, ranked

1. **The core ecobee integration migrating to a `DataUpdateCoordinator`.** It is
   one of the last core holdouts still on `@Throttle` plus entity polling, and
   this is the most likely modernisation. When it lands, `entry.runtime_data`
   becomes a coordinator and `.ecobee` may move to `.api` or
   `.coordinator.ecobee`. The resolver here already probes `.ecobee`, `.api`,
   `.client` and `.coordinator` at two levels of nesting, plus a coordinator's
   `.data`, so there is a fair chance it survives. If it does not, the entities
   go unavailable with one warning in the log - they do not crash.
2. **The 180-second throttle or the 60-second climate scan interval changing.**
   Handled: staleness is measured from the thermostat's own clock, not assumed.
3. **`hass.data["ecobee"]["thermostats"]` being removed.** It carries a
   `pylint: disable-next=home-assistant-use-runtime-data` marker upstream, which
   is how core flags code queued for deletion. It is only ever used here as a
   last-resort fallback.
4. **The `events` key disappearing.** Unlikely: `includeEvents` is hardcoded
   `true` in `pyecobee.get_thermostats()` with no configuration knob. Guarded
   anyway.

### One thing to watch out for on your own system

If your thermostat is also exposed to Home Assistant through
`homekit_controller` (a second `climate.*` entity for the same physical unit),
you have two independent control paths. Automations built on these entities
should write to the **core ecobee** climate entity only. Two integrations
writing setpoints to one thermostat will fight.

---

## What this integration does not do

It does not write to the thermostat, does not change `drAccept`, does not
override or cancel a utility event, and does not create any helpers. It reports.
Acting on what it reports is left to your own automations, deliberately: an
automation you wrote is one you can see, disable and reason about at 5 pm on a
95-degree day.

## License

MIT
