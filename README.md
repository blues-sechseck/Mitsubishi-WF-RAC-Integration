# Mitsubishi WF-RAC Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/default)
[![Current version](https://img.shields.io/github/v/release/blues-sechseck/Mitsubishi-WF-RAC-Integration?style=for-the-badge)](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/releases/latest)
[![Total downloads](https://img.shields.io/github/downloads/blues-sechseck/Mitsubishi-WF-RAC-Integration/total?style=for-the-badge)](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/releases)
[![Latest release downloads](https://img.shields.io/github/downloads/blues-sechseck/Mitsubishi-WF-RAC-Integration/latest/total?sort=semver&style=for-the-badge)](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/releases/latest)
[![installbadge]][installs]
[<img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="28">](https://buymeacoffee.com/blues.sechseck)
[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-EA4AAA?style=for-the-badge&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/blues-sechseck)

This is a Home Assistant integration for **Mitsubishi Heavy Industries** air conditioners that use
the WF-RAC WiFi module and the **"Smart M-Air"** app.

> **Not compatible with Mitsubishi Electric systems** (e.g. those using a MAC-577IF2-E interface) or
> the MELCloud platform — those are a different manufacturer with a different app and protocol. If
> your unit uses MELCloud, see Home Assistant's built-in
> [MELCloud integration](https://www.home-assistant.io/integrations/melcloud/) instead.

# Supported devices

Any Mitsubishi Heavy Industries air conditioner that ships with the **WF-RAC** WiFi module — the
network interface controlled through the **Smart M-Air** app — should work. The integration talks
to the module's local HTTP API rather than to a specific indoor/outdoor unit model, and it probes
both plain HTTP and HTTPS on setup, so it doesn't matter which of the module's firmware branches
(`WF-RAC`, `WF-RAC-HTTPS`, `WCBN4612L`) yours happens to run.

Confirmed working on a `SRK20ZS-WF` + `SRK35ZS-WF` multi-split on an `SCM45ZS-W` outdoor unit. Some
entities are conditional on what the unit itself reports supporting — Occupancy and the Home Leave
Mode entities, for example, only appear on units that report the corresponding capability, and a
few diagnostic sensors depend on the model-identifier byte the unit sends back. An unsupported
feature simply doesn't create its entity, rather than failing.

## History

Created by [@jeatheak](https://github.com/jeatheak). In July 2026, jeatheak transferred ownership
of this repository to [@blues-sechseck](https://github.com/blues-sechseck), who continues to
maintain it. Thanks, jeatheak, for building this in the first place!

## How this is built

This integration is developed with AI assistance. Every change is reviewed by me
and tested on real hardware before it ships; bugs are mine, not the tool's.

## ⚠️ Coming from the original repo? Check your automations

Since [2026.8](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/releases/tag/2026.8), the `fan_mode`/`swing_mode`/`swing_horizontal_mode` state values were renamed to snake_case (e.g. `"Up/Down Auto"` → `"up_down_auto"`, `"Quiet"` → `"quiet"`, `"3D Auto"` → `"3d_auto"`) to satisfy Home Assistant's own validation rules — the old capitalized values were never actually valid. If you have automations, scripts, or dashboards that call `climate.set_fan_mode`, `select.select_option`, or the `set_horizontal_swing_mode`/`set_vertical_swing_mode` services with the old capitalized strings, update them to the new lowercase values. See the [2026.8 release notes](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/releases/tag/2026.8) for the full list.

# Todo 📃 and Bug report 🐞

See [Github To Do & Bug List](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/issues)

# Installation

### Install using [HACS](https://hacs.xyz)

This integration is part of the HACS default list — no custom repository needed. In HACS, go to
**Integrations**, search for **"Mitsubishi WF-RAC"**, and install it from there.

Already installed? Jump straight to setup:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=mitsubishi_wf_rac)

### Install manually

Clone or copy this repository and copy the folder `custom_components/mitsubishi_wf_rac` into
`/custom_components/mitsubishi_wf_rac`.

### Removing the integration

This integration follows standard integration removal — no extra steps (like disabling a cloud
account) are required.

1. Go to **Settings** > **Devices & Services**.
2. Find the **Mitsubishi WF-RAC** integration and select it.
3. Select the three-dot menu next to the entry, then **Delete**.

If you installed manually rather than through HACS, also delete the
`custom_components/mitsubishi_wf_rac` folder and restart Home Assistant.

# Entities

This integration creates one device per airco with the following entities.

## Climate

| Entity | Attribute | Available values | Description |
|---|---|---|---|
| `climate.<name>` | `hvac_mode` | `off`, `auto`, `cool`, `heat`, `dry`, `fan_only` | Operating mode of the unit. |
| | `fan_mode` | `auto`, `quiet`, `low`, `medium`, `high` | Fan speed. `quiet` is the lowest of the four steps, not the remote's ECO setting - ECO can't be set or read through the WLAN module, and while the remote has it running the unit reports the lowest step. |
| | `swing_mode` | `up_down_auto`, `highest`, `middle`, `normal`, `lowest`, `3d_auto` | Vertical louver position. `3d_auto` hands vertical *and* horizontal swing over to the unit's own automatic mode. |
| | `swing_horizontal_mode` | `left_right_auto`, `left_left`, `left_center`, `center_center`, `center_right`, `right_right`, `left_right`, `right_left`, `3d_auto` | Horizontal louver position. `3d_auto` behaves as above. |
| | `target_temperature` | 16–30 °C (cool), 18–30 °C (other modes) | Setpoint. Cooling accepts a lower minimum than heating/auto/dry in practice; heating below 18 °C isn't a reliable plain setpoint (see Home Leave Mode for that instead). |
| | `current_temperature` | °C | Indoor temperature as measured by the unit, corrected by the "Indoor Temp. Sensor Offset" option if set. |
| | `hvac_action` | `off`, `idle`, `cooling`, `heating`, `drying`, `fan` | What the unit is actually doing right now. `idle` means the unit is on but the compressor is stopped (e.g. setpoint satisfied) - same signal as the Compressor Demand binary sensor below. In `auto` mode, `cooling`/`heating` reflects the unit's own cool/heat decision, not just the configured mode. |

## Sensors

The diagnostic operation-data sensors below are disabled by default. Enabling a sensor makes the integration request its value; leaving all of them disabled makes no extra request at all - unless an external temperature override is armed, which asks for one segment on its own behalf (see [External temperature override](#indoor-temperature-override)).

Any one of them switches the request on, so pick one that always has something to show. **Indoor Coil Temperature** is the safest choice: it is per indoor unit and reads a temperature whatever the system is doing. **Compressor Frequency** is the more telling value where it works, but reads a constant 0 on older firmware ([#207](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/issues/207)). **Hot Gas Temperature** is a poor first choice: it reads unknown whenever the outdoor unit is idle, which looks like a broken sensor rather than a resting one.

| Entity | Values | Description |
|---|---|---|
| Indoor Temperature | °C | What the unit reports as the room temperature, exposed as its own sensor. Normally the same value as the climate entity's `current_temperature`; while an override is in effect the two part company, because this one keeps following the unit while the climate entity shows the temperature the unit was handed (see [Indoor temperature override](#indoor-temperature-override)). |
| Outdoor Temperature *(shared on multi-split)* | °C | Outdoor unit temperature, corrected by the "Outdoor Temp. Sensor Offset" option if set. On multi-split systems this is an outdoor-unit-level value - reads identically on every indoor unit sharing one outdoor unit, since there's only one outdoor sensor. |
| Target Temperature *(disabled by default)* | °C | Current setpoint, exposed as its own sensor. Off by default because the climate entity already carries the same value as its `target_temperature` attribute. |
| Energy Usage (current run) | kWh, increasing | Energy consumption of the **current run**, as reported by the unit in **0.25 kWh steps**. The unit clears this counter to 0 every time it is switched on, and holds the last value while it is off — so a low or zero reading is normal, not a fault, and a run consuming less than 0.25 kWh reads 0 throughout. For a lifetime figure use Energy Usage Total below. Only created if the unit actually reports this value — not all models do. |
| Energy Usage Total | kWh, increasing | Lifetime total, accumulated by the integration from the counter above and kept across restarts. This is the one to put on the Energy dashboard. Reset it with the "Reset Energy Usage Total" button on the device page, or set it to a specific value with the `mitsubishi_wf_rac.set_energy_total` action (useful when carrying a figure over from an existing meter). Resetting it does not erase the history already recorded in Home Assistant's long-term statistics. **Accuracy depends on run length:** whatever a run consumes above its last completed 0.25 kWh step is never reported by the unit and cannot be accumulated, which costs about 0.125 kWh per run on average regardless of how long the run is. A unit cycling every half hour therefore totals noticeably low; one running for hours at a time is off by little. Use an external energy meter if you need exact figures on a frequently cycling unit. |
| Compressor Frequency *(diagnostic, disabled by default, shared on multi-split)* | Hz | Actual compressor speed, not just on/off. Outdoor-unit-level - identical on every indoor unit sharing one outdoor unit. On older firmware (`mcu131`/`wireless010`) this reads a constant 0 even with the compressor confirmed running ([#207](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/issues/207)). |
| Compressor Frequency (raw) *(diagnostic, disabled by default)* | unitless | Undecoded operation-data value behind Compressor Frequency, useful for protocol work. Both bytes in this segment carry data. |
| Operating Current *(diagnostic, disabled by default, shared on multi-split)* | A | Compressor operating current. It has the same outdoor-unit-level sharing as Compressor Frequency and reads a constant 0 on older firmware ([#207](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/issues/207)). |
| Operating Current (raw) *(diagnostic, disabled by default)* | unitless | Undecoded operation-data byte behind Operating Current, useful for protocol work. |
| Hot Gas Temperature *(diagnostic, disabled by default, shared on multi-split)* | °C | Compressor discharge (hot gas) temperature. It has the same outdoor-unit-level sharing as Compressor Frequency. Below about 41 °C the sensor carries no resolution — it only means "30 °C or colder" — so the value reads unknown there rather than a number, which is where an idle outdoor unit sits. The raw byte keeps the distinction. |
| Hot Gas Temperature (raw) *(diagnostic, disabled by default)* | unitless | Undecoded operation-data byte behind Hot Gas Temperature, useful for protocol work. |
| EEV Pulses *(diagnostic, disabled by default)* | pulses | Electronic expansion valve position, raw pulse count (0-255). |
| EEV Position *(diagnostic, disabled by default)* | % | Same value as EEV Pulses, linearly mapped to 0-255=0-100%. The real full-open pulse count is unknown, so treat this as relative, not calibrated - useful for comparing indoor units on the same system. |
| Indoor Coil Temperature *(diagnostic, disabled by default)* | °C | Indoor heat-exchanger temperature (MHI's THI-R1). Per indoor unit, not shared. In cooling it drops as the coil gets cold and rises back to room temperature once the compressor stops - the clearest signal there is for what the unit is actually doing. Works in heating too, where the coil is the condenser and runs to 45 °C and beyond. |
| Indoor Coil Temperature (raw) *(diagnostic, disabled by default)* | unitless | Undecoded operation-data byte behind Indoor Coil Temperature, useful for protocol work. |
| Indoor Coil Outlet Temperature *(diagnostic, disabled by default)* | °C | Indoor heat-exchanger sensor on the gas line (MHI's THI-R3). The name describes cooling, where this point is the evaporator's outlet; in heating the flow reverses and the same point sits closer to the coil's inlet. Also per indoor unit. Equal to Indoor Coil Temperature while the compressor is off; while it runs the difference between the two is the evaporator superheat. |
| Indoor Coil Outlet Temperature (raw) *(diagnostic, disabled by default)* | unitless | Undecoded operation-data byte behind Indoor Coil Outlet Temperature, useful for protocol work. |
| Outdoor Coil Temperature (raw) *(diagnostic, disabled by default)* | unitless | Undecoded operation-data byte behind the outdoor coil temperature, useful for protocol work. |
| Discharge Superheat (raw) *(diagnostic, disabled by default)* | unitless | Undecoded operation-data byte behind discharge superheat, useful for protocol work. |
| Protection Number (raw) *(diagnostic, disabled by default)* | unitless | Undecoded operation-data byte behind the protection number, useful for protocol work. No module tested here has answered this code, so it may remain `unknown`. |
| Airco ID *(diagnostic, disabled by default)* | text | Internal ID of the airco. |
| Operator ID *(diagnostic, disabled by default)* | text | Internal operator/account ID. |
| Device ID *(diagnostic, disabled by default)* | text | Internal device ID. |
| IP *(diagnostic, disabled by default)* | text | Local IP address of the WF-RAC module. |
| Accounts *(diagnostic, disabled by default)* | number | Number of app accounts currently connected to the unit. |
| Error *(diagnostic)* | error code | Raw error code reported by the unit; `00` means no error. |
| Updated By *(diagnostic)* | text | Which account last changed the unit's settings (this integration or the Smart M-Air app). |
| Account Expires *(diagnostic, disabled by default)* | text | Expiry of the current operator session. |
| LED Status *(diagnostic, disabled by default)* | text | State of the unit's status LED. |
| Auto Heating *(diagnostic)* | text | State of the unit's automatic heating assist. |
| Model Nr *(diagnostic, disabled by default)* | number | Raw model-identifier byte reported by the unit. Used to gate which optional features (occupancy, Home Leave) are exposed; mostly useful for diagnosing unsupported models. |
| Cool Hot Judge *(diagnostic, disabled by default)* | `cooling`, `heating` | Raw cool/heat state reported by the unit's compressor, independent of the configured mode. `unknown` while off or in `fan_only`. Useful for detecting the "wait/hold" state on multi-split systems where one indoor unit is blocked because the outdoor unit is already committed to the opposite mode for a sibling unit. |

## Binary sensors

| Entity | Values | Description |
|---|---|---|
| Problem | on/off | On whenever the unit reports an error code (`error_code` attribute holds the raw code; `error_description` is added when the code is documented in the MHI service/user manuals). |
| Occupancy | on/off | Only created on units that report the "Vacant"/Home Leave bit (see Home Leave Mode below). This is *not* a physical presence/motion sensor - it just mirrors that bit, which is off unless Home Leave mode was actually entered. It will read "occupied" even in an empty room if Home Leave was never triggered. |
| Compressor Demand | on/off | Whether *this* indoor unit is currently calling for the compressor, as opposed to just being powered on (e.g. off while a setpoint is already satisfied). Comes from the same status poll as every other sensor - no extra request needed. On a single-split system that is the same thing as the compressor running. On a multi-split it is not: each indoor unit reports its own demand, so one can read "on" while a sibling on the same outdoor unit reads "off" at the same moment, and this sensor can go "off" while the shared compressor keeps running for the sibling. To tell whether the outdoor unit is running at all, either check this sensor across every indoor unit, or use Compressor Frequency, which is identical across all indoor units sharing one outdoor unit. |

## Update

| Entity | Values | Description |
|---|---|---|
| Firmware Update *(opt-in)* | on/off | Reports whether newer WF-RAC module firmware is available, by comparing the version reported locally against the manufacturer's `getFirmware` endpoint. Only created if "Check for firmware updates" is enabled in the integration's options - off by default, since it's the only call this integration makes outside the local network. Read-only; installing an update isn't offered here. |

## Home Leave Mode

The unit's own frost-protection/low-power standby mode for when nobody's home, with independent cooling and heating away-targets. Only created on units confirmed to support it.

| Entity | Values | Description |
|---|---|---|
| Home Leave Mode (select) | `off`, `away_cool`, `away_heat` | Enters/leaves Home Leave mode in either direction. |
| Home Leave Cooling/Heating Temp Rule *(number, disabled by default)* | 10–50 °C | Outdoor/room temperature threshold at which Home Leave engages for that mode. |
| Home Leave Cooling/Heating Temp Setting *(number, disabled by default)* | 10–50 °C | Target temperature while Home Leave is active for that mode. |
| Home Leave Cooling/Heating Airflow *(select, disabled by default)* | `auto`, `1`–`4` | Fan speed while Home Leave is active for that mode. |

The number/select entities above stay `unknown` until the climate entity's "Request Home Leave Mode status" action has been called once - the unit omits these values from a plain poll otherwise. Writing to them before that is refused rather than guessed at. See the `request_home_leave_mode_status`/`set_home_leave_mode` climate actions.

## Select

These duplicate the climate entity's swing/fan attributes as standalone entities, useful for dashboards or automations that prefer a plain `select` over a `climate` attribute. Installations that declined them during setup get them as disabled entities, to be enabled from the entity list when wanted.

| Entity | Values | Description |
|---|---|---|
| Horizontal Swing Direction | same as `swing_horizontal_mode` above | |
| Vertical Swing Direction | same as `swing_mode` above | |
| Fan Speed | same as `fan_mode` above | |

# Data updates

The integration polls the WF-RAC module directly over the local network every 60 seconds — there
is no cloud, push, or webhook involved. A single poll reads the unit's full state in one request
(mode, setpoint, temperatures, energy counter, and so on). The diagnostic operation-data sensors
described above (compressor frequency, coil temperatures, EEV position, etc.) cost one additional
request every second poll, made only for the segments an enabled sensor actually needs, and not at
all if none of them are enabled.

That second request is why those sensors update every two minutes rather than every minute: the
module grants whoever last sent it a command 60 seconds of exclusive control, and asking for
operation data counts as one. Requesting it every minute would hold that grip permanently and leave
the Smart M-Air app unable to control the unit at all. On the same principle the integration stops
asking for a few minutes when it notices another client controlling the unit — the External Control
sensor shows while that is happening, and the operation-data sensors hold their last values
meanwhile.

The only outbound *internet* request this integration ever makes is the optional firmware-version
check ("Check for firmware updates" under Options, off by default) — everything else, including
every poll and every command, stays on the local network.

# Use cases

- **Whole-home climate scheduling and automations** — set mode, fan speed, swing and setpoint like
  any other `climate` entity, from automations, scripts, or dashboards.
- **Energy dashboard tracking** — feed Energy Usage Total into Home Assistant's built-in Energy
  dashboard for a lifetime consumption figure per unit.
- **Presence-based energy saving** — drive Home Leave Mode from a `person`/zone trigger instead of
  a plain schedule, so the unit throttles back to a frost-protection setpoint while everyone's away
  and returns to normal the moment someone gets home.
- **Reacting to what the compressor is actually doing** — Compressor Demand and `hvac_action`
  distinguish "on but idle, setpoint satisfied" from "actively heating/cooling", useful for
  automations that should only fire while the unit is genuinely running.
- **Diagnosing multi-split behaviour** — the diagnostic operation-data sensors (compressor
  frequency, indoor coil temperature, EEV position) make short cycling, an oversubscribed outdoor
  unit, or a struggling indoor unit visible in history graphs, without a service call or the app.

# Examples

Set Home Leave Mode automatically when the last person leaves, back to normal when someone returns:

```yaml
automation:
  - alias: "AC: enable Home Leave Mode when everyone's away"
    trigger:
      - trigger: state
        entity_id: zone.home
        to: "0"
    action:
      - action: select.select_option
        target:
          entity_id: select.<name>_home_leave_mode
        data:
          option: away_cool
  - alias: "AC: back to normal when someone gets home"
    trigger:
      - trigger: state
        entity_id: zone.home
        from: "0"
    action:
      - action: select.select_option
        target:
          entity_id: select.<name>_home_leave_mode
        data:
          option: "off"
```

Notify when the unit reports a fault:

```yaml
automation:
  - alias: "AC: notify on error"
    trigger:
      - trigger: state
        entity_id: binary_sensor.<name>_problem
        to: "on"
    action:
      - action: notify.notify
        data:
          message: >-
            {{ state_attr('binary_sensor.<name>_problem', 'error_description')
               or state_attr('binary_sensor.<name>_problem', 'error_code') }}
```

# Options

Configurable via the integration's "Configure" (options) flow. The host/IP
address itself isn't here - it's connection-critical, so changing it goes
through "Reconfigure" instead, which re-validates the new address against the
device before saving it.

The dialog groups these into three sections: **Indoor temperature source**
(the source sensor and its two overshoot corrections), **Setpoint offsets**
(what gets sent to the unit) and **Sensor offsets** (what Home Assistant
shows). The overshoot fields only appear once a source is picked and saved -
without one there is no room temperature for them to correct.

| Option | Range | Description |
|---|---|---|
| Retry limit | 3 or higher, default 3 | Consecutive failed polls before the device is marked unavailable. At the 60 s poll interval, `3` is about 3 minutes - enough to ride through the module's hourly WiFi reassociation. Raise it on a weak link; it cannot be set lower. |
| Indoor Temp. Sensor Offset | -15..15 °C | Added to the unit's own indoor-sensor reading before it's shown as `current_temperature` / the Indoor Temperature sensor - display-only, doesn't change what the unit does. Suspended while an external temperature override is in effect: the unit is reporting the value it was given rather than measuring, so there is nothing to calibrate. |
| Outdoor Temp. Sensor Offset | -15..15 °C | Same, for the Outdoor Temperature sensor. |
| Target Temp. Offset | -5..5 °C | Calibrates the *setpoint sent to the unit* - see "Target Temp. Offset sign convention" below. Applies to every `hvac_mode` unless overridden by the two options below. |
| Target Temp. Offset (Cooling) | -5..5 °C, unset by default | Overrides Target Temp. Offset for `cool` and `dry` mode. Leave unset to keep using Target Temp. Offset for those modes too. |
| Target Temp. Offset (Heating) | -5..5 °C, unset by default | Overrides Target Temp. Offset for `heat` mode. Leave unset to keep using Target Temp. Offset for `heat` too. |
| Cooling overshoot | -3..3 °C | How far past your setting the room actually goes before the unit stops. Set 22 °C, room settles at 21 °C: enter 1. Only shown, and only has an effect, while an Indoor temperature source is configured. |
| Heating overshoot | -3..3 °C | The same for heating: how far above your setting the room ends up. Positive in both cases. |
| Check for firmware updates | on/off, off by default | Creates the Firmware Update entity (see Update above) and periodically checks the manufacturer's `getFirmware` endpoint. The only outbound internet call this integration makes - leave off to stay fully local. |

### Target Temp. Offset sign convention

The unit's internal temperature sensor is a **return-air sensor built into the indoor unit**, not a sensor sitting where you actually care about the temperature. It reads a biased version of the room - but which direction, and by how much, depends on your installation, not on cooling vs. heating alone:

- **Short-circuited airflow**: the unit's own outflow gets pulled straight back into the return before it mixes into the room. In cooling this reads *below* the true room temperature.
- **Stratification**: a high wall mount and a low fan speed let conditioned air pool near the ceiling instead of mixing down to where you live. In cooling this reads *above* the true room temperature - the opposite of the case above, and just as real. Fan speed matters here: on one multi-split installation, the unit capped at a low night-time fan speed showed a markedly larger bias than a sibling unit running medium/high in the same house.

There's no way to predict which case applies to your unit from its mode alone - you have to measure.

Target Temp. Offset corrects for this bias: `true_room ≈ PresetTemp + offset`. To land the *room* on the temperature you actually requested, the setpoint sent to the unit is `commanded PresetTemp = requested − offset`. Concretely: **a negative offset raises the setpoint actually sent to the unit** (a positive offset lowers it).

**Measuring it:** place a reference sensor away from the unit's own airflow, then average `current_temperature` (the Indoor Temperature sensor) minus that reference, split by the climate entity's `hvac_action`. Use the average while `cooling` (or `heating`) as your starting point for Target Temp. Offset (Cooling) / (Heating) - that's the state the thermostat loop actually regulates in, and it's not interchangeable with `idle` or `off`: on the installation above, the same unit's average bias moved by more than a kelvin between `cooling` and `off`. This is also why no single value is correct for both cool and heat at once, and why the offset isn't a fixed mounting/calibration error you can look up - it calibrates your installation's operating regime, and only a measurement of *your* unit, in the state it's actually controlling in, gets it right.

# Services

The climate entity exposes the following entity services (use as `mitsubishi_wf_rac.<service>`).

| Service | Fields | Description |
|---|---|---|
| `set_horizontal_swing_mode` | `swing_mode` | Set the horizontal (left/right) louver position. |
| `set_vertical_swing_mode` | `swing_mode` | Set the vertical (up/down) louver position. |
| `request_home_leave_mode_status` | — | Ask the unit to report its Home Leave Mode thresholds/airflow (only on supporting models). |
| `set_home_leave_mode` | `temp_rule_cooling`, `temp_setting_cooling`, `air_flow_cooling`, `temp_rule_heating`, `temp_setting_heating`, `air_flow_heating` | Write new Home Leave Mode thresholds/airflow (only on supporting models). |
| `set_external_temperature` | `temperature` (optional) | Provide a room temperature to the AC, replacing the one its own indoor sensor reads. Omit `temperature` or pass `null` to revert to the internal sensor. See below. |

## Indoor temperature override

`set_external_temperature` hands the unit a room temperature measured out in the room, and it regulates on that instead of the one its own return-air sensor reads.

The value is not a setting the unit stores under a flag of its own. It has no set-bit, which means it can only travel inside a frame sent for some other reason - and any frame that leaves it out sends the unit back to its internal sensor. Three things follow from that.

- **The action arms the value, it does not send it.** What carries it is the operation-data request, which the integration starts making for as long as an override is armed - the same request an enabled diagnostic sensor triggers, asked for on the override's own behalf, so nothing has to be switched on by hand. The value reaches the unit within a minute, or sooner if a command goes out for another reason in the meantime. Clearing takes effect the same way: the next frame carries "internal sensor" again.
- **Nothing is written just for the override.** A frame carrying it also re-asserts power, mode, fan speed, setpoint and both louver axes, and it takes the unit's 60-second write lock. Sending one per sensor reading would end any running self-clean cycle and keep the official app locked out for as long as the override is in use.
- **While the unit is off or in `fan_only`, the override is not written.** Neither mode regulates on a room temperature, and a request carrying the value ends a self-clean cycle started from the remote - self-clean runs from the off state. The unit falls back to its own sensor there, and the first frame after it returns to a regulating mode writes the value again.

The **Indoor temperature override** sensor (diagnostic) says whether the unit is actually regulating on your value right now, so an override that is armed but not yet in effect - after a restart, or while the unit is off - is visible rather than something to deduce.

You can select a sensor under **Indoor temperature source** in the integration options. The integration reads that temperature sensor immediately after setup and follows its state changes itself, converting its unit to °C and rounding to the protocol's 0.25 °C steps. If the source becomes `unavailable`, `unknown`, or disappears, it clears the override on the next frame so the unit returns to its internal sensor. When a source is configured, use of `set_external_temperature` with a value is refused to avoid two competing writers; calling it without `temperature` still clears the override, though only until the source reports again. The source cannot be one of this integration's own temperature sensors: while an override is armed those report the injected value back, so feeding one in would walk the override away from the room. Without a configured source, automations using the action remain supported as before. Clearing that option hands control back: the value it had armed is dropped rather than restored, and the unit is back on its own sensor.

What an armed override costs is one request per poll cycle, which holds the unit's write lock for part of the cycle exactly as an enabled operation-data sensor does.

**While an override is in effect, the unit stops reporting its own sensor.** Measured on hardware: the value you inject comes back on the next cycle as the unit's reported room temperature, half a kelvin above what you sent - the two protocol fields it travels in differ by that much with or without an override. So the Indoor Temperature sensor follows your override as well and is no longer a measurement of the room; the climate entity shows the value you supplied instead, and your own sensor remains the measurement. Clearing the override brings the real reading back within a cycle.

An action-driven override survives a restart and a reload. It is re-armed, not re-sent: the unit stays on whatever it last received until the next frame goes out. With a configured source, its current state is used instead of a restored value. The Indoor Temperature sensor shows what the unit reports throughout, so it keeps agreeing with the official app. `current_temperature` does not: with an **Indoor temperature source** configured, the climate entity shows that sensor's reading - including while the unit is off or in `fan_only`, where nothing is written and the unit's own reading means least. An override armed from an automation instead of a source is only shown once the unit is actually using it: there is no sensor behind it, so before then it is an intention rather than a measurement.

### When the room ends up past your setting

With a room temperature supplied, most units cool the room further than asked before stopping - measured between 0.6 and 1.3 K across four different models, three of them between 1.0 and 1.2, and the same figure repeats every cycle. That is the unit's own thermostat band, not a sensor error: its return-air sensor is out of the loop while it regulates on your value. Heating has so far looked correct.

Set **Cooling overshoot** to how far it goes: aim for 22 °C, watch where the room settles, and enter the difference as a positive number. The field opens on 1 because that is what the units measured so far need; it is a starting point, not a measurement of yours, and it only takes effect once you save the options. If your unit stops short instead and never quite gets there, a negative number corrects that the other way. The integration then hands the unit a room temperature that much lower, so the unit reaches its own stopping point exactly when your room is on target. Your setpoint and everything shown in Home Assistant stay the number you asked for.

Why here and not in Target Temp. Offset: on the units measured so far, a half-degree setpoint is rounded up to the next whole one, so that field is too coarse for a correction of less than a degree (owners of ZT and ZTL units report their models do take half degrees). The room temperature the unit is fed has 0.25 °C steps on every model, so correcting there is the finer of the two - and it leaves the setpoint matching what the official app shows.

While an override is in effect, the climate entity's `current_temperature` shows the room temperature you supplied and the Indoor Temperature sensor keeps showing what the unit reports - which is the value it was handed, plus the half kelvin it adds on the way back, minus any correction. The two therefore differ, and how far apart they sit is not a measurement of anything: it is the correction and that half kelvin, which cancel out at exactly 0.5.

# Known limitations

- **Self-clean cannot be started or monitored from Home Assistant.** The official app has no way to
  trigger it either - the unit's self-clean cycle can only be started from its own IR remote, and
  nothing on the wire distinguishes "self-clean running" from "off" (see
  [§6.4 of the protocol reference](docs/wf-rac-module-reference.md#64-self-clean-cannot-be-started-remotely)).
- **The Firmware Update entity is read-only.** It reports whether newer WF-RAC module firmware is
  available; installing it isn't offered through this integration - the module updates itself via
  the official app.
- **Not every entity appears on every unit.** Occupancy, Home Leave Mode, and a few diagnostic
  sensors only get created if the unit itself reports support for the underlying feature (see the
  notes under Entities above) - missing rather than `unavailable` is expected there, not a bug.
- **One device, one connection.** The WF-RAC module handles requests one at a time. This
  integration already serializes its own requests to respect that, but running a second tool (a
  custom script, another integration instance, the Smart M-Air app at the same moment) against the
  same unit can still cause slow or occasionally failed responses on either side.

# Troubleshooting

## Unit goes briefly unavailable about once an hour

The WF-RAC module drops and re-establishes its WiFi association roughly once an hour. This is
designed behaviour of the module, confirmed by MHI support under ticket reference 813958: the
interface does "connect / disconnect every one hour … to avoid too much cache by the communication"
([source](https://community.ui.com/questions/AC-Units-IOT-disconnecting-from-UniFi-Wi-Fi-at-regular-hourly-Intervals/821cd3e4-46a0-4d6b-8fd0-8d5cf182b90f)).
The reassociation takes seconds to about a minute and cannot be turned off.

This integration polls every 60 seconds, so a reassociation can cost a poll. That does not become a
visible outage: the device is only reported unavailable after three consecutive failed polls, about
three minutes, which rides through the reassociation. If your link is weak enough that this still
shows up, raise **Retry limit** in the options; three is the minimum, not a target.

A missed poll is not logged as a warning either — it is a debug line, and the log only speaks up
when the device actually crosses the threshold and again when it comes back. Turn on debug logging
for `custom_components.mitsubishi_wf_rac` if you want to watch the individual polls.

## Unit goes unavailable for about an hour

This is the same hourly reassociation as above, but the module fails to re-bind port `51443`
afterwards instead of coming back within a minute. The outage starts right on the hourly tick, not
at a random point in between, which is what distinguishes it from the WiFi roaming problem below.
There's no router setting that fixes this - it clears itself on the next hourly reassociation, so
it's a matter of waiting it out.

## Unit goes unavailable for 15-35 minutes

Outages in this range and starting at a random point (not on the hourly tick) are a network-side
problem: the module mishandles WiFi roaming and steering management frames. Recommended setup:

- A **dedicated 2.4 GHz-only SSID**. The module is 2.4 GHz only, and on a shared SSID band steering
  tries to push it onto a band it cannot join.
- **802.11r (Fast Roaming)** and **802.11v (BSS Transition / Handoff Suggestions)** off on that
  SSID. Band steering is itself implemented via the same 802.11v frames.
- **Plain WPA2**, not WPA2/WPA3 mixed mode. This also matters during initial pairing.
- On UniFi, "Force WiFi 4 Mode" and "DTIM Interval Lock" under IoT Optimization are safe to enable.
- Blocking the module's outbound internet access at the router removes these outages in some
  setups; this integration only needs LAN access. The hourly reassociation continues either way.

# Protocol reference

If you are writing your own client against the WF-RAC module — or building a CNS/SPI replacement
such as an ESP32 running MHI-AC-Ctrl — [**docs/wf-rac-module-reference.md**](docs/wf-rac-module-reference.md)
documents the interface end to end: mDNS discovery, the HTTPS API and its envelope rules, the
`airconStat` blob, the 18-byte state block and how it maps onto the CNS/SPI frame, the operation-data
channel, and what the module deliberately does not forward. Every non-obvious claim carries a
confidence tag saying whether it was observed on hardware, read out of a firmware image, or inferred.

It is written for people outside this project, so nothing in it assumes Home Assistant.

[installbadge]: https://img.shields.io/badge/dynamic/json?style=for-the-badge&logo=home-assistant&logoColor=ccc&label=usage&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.mitsubishi_wf_rac.total
[installs]: https://analytics.home-assistant.io/
