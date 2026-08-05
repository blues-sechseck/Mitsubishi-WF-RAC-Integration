# Mitsubishi WF-RAC Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/default)
[![installbadge]][installs]
[<img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="28">](https://buymeacoffee.com/blues.sechseck)

This is a Home Assistant integration for **Mitsubishi Heavy Industries** air conditioners that use
the WF-RAC WiFi module and the **"Smart M-Air"** app.

> **Not compatible with Mitsubishi Electric systems** (e.g. those using a MAC-577IF2-E interface) or
> the MELCloud platform — those are a different manufacturer with a different app and protocol. If
> your unit uses MELCloud, see Home Assistant's built-in
> [MELCloud integration](https://www.home-assistant.io/integrations/melcloud/) instead.

## History

Created by [@jeatheak](https://github.com/jeatheak). In July 2026, jeatheak transferred ownership
of this repository to [@blues-sechseck](https://github.com/blues-sechseck), who continues to
maintain it. Thanks, jeatheak, for building this in the first place!

## ⚠️ Coming from the original repo? Check your automations

Since [2026.8](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/releases/tag/2026.8), the `fan_mode`/`swing_mode`/`swing_horizontal_mode` state values were renamed to snake_case (e.g. `"Up/Down Auto"` → `"up_down_auto"`, `"Quiet"` → `"quiet"`, `"3D Auto"` → `"3d_auto"`) to satisfy Home Assistant's own validation rules — the old capitalized values were never actually valid. If you have automations, scripts, or dashboards that call `climate.set_fan_mode`, `select.select_option`, or the `set_horizontal_swing_mode`/`set_vertical_swing_mode` services with the old capitalized strings, update them to the new lowercase values. See the [2026.8 release notes](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/releases/tag/2026.8) for the full list.

# Todo 📃 and Bug report 🐞

See [Github To Do & Bug List](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/issues)

# Installation

### Install using [HACS](https://hacs.xyz)

This integration is part of the HACS default list — no custom repository needed. In HACS, go to
**Integrations**, search for **"Mitsubishi WF-RAC"**, and install it from there.

### Install manually

Clone or copy this repository and copy the folder `custom_components/mitsubishi_wf_rac` into
`/custom_components/mitsubishi_wf_rac`.

# Entities

This integration creates one device per airco with the following entities.

## Climate

| Entity | Attribute | Available values | Description |
|---|---|---|---|
| `climate.<name>` | `hvac_mode` | `off`, `auto`, `cool`, `heat`, `dry`, `fan_only` | Operating mode of the unit. |
| | `fan_mode` | `auto`, `quiet`, `low`, `medium`, `high` | Fan speed. |
| | `swing_mode` | `up_down_auto`, `highest`, `middle`, `normal`, `lowest`, `3d_auto` | Vertical louver position. `3d_auto` hands vertical *and* horizontal swing over to the unit's own automatic mode. |
| | `swing_horizontal_mode` | `left_right_auto`, `left_left`, `left_center`, `center_center`, `center_right`, `right_right`, `left_right`, `right_left`, `3d_auto` | Horizontal louver position. `3d_auto` behaves as above. |
| | `target_temperature` | 16–30 °C (cool), 18–30 °C (other modes) | Setpoint. Cooling accepts a lower minimum than heating/auto/dry in practice; heating below 18 °C isn't a reliable plain setpoint (see the Home Leave switch for that instead). |
| | `current_temperature` | °C | Indoor temperature as measured by the unit, corrected by the "Indoor Temp. Sensor Offset" option if set. |
| | `hvac_action` | `off`, `idle`, `cooling`, `heating`, `drying`, `fan` | What the unit is actually doing right now. In `auto` mode this reflects the unit's own cool/heat decision, not just the configured mode. |

## Sensors

| Entity | Values | Description |
|---|---|---|
| Indoor Temperature | °C | Same value as the climate entity's `current_temperature`, exposed as its own sensor. |
| Outdoor Temperature | °C | Outdoor unit temperature, corrected by the "Outdoor Temp. Sensor Offset" option if set. |
| Target Temperature | °C | Current setpoint, exposed as its own sensor. |
| Energy Usage | kWh, increasing | Cumulative energy consumption reported by the unit. Only created if the unit actually reports this value — not all models do. |
| Airco ID *(diagnostic)* | text | Internal ID of the airco. |
| Operator ID *(diagnostic, disabled by default)* | text | Internal operator/account ID. |
| Device ID *(diagnostic, disabled by default)* | text | Internal device ID. |
| IP *(diagnostic, disabled by default)* | text | Local IP address of the WF-RAC module. |
| Accounts *(diagnostic, disabled by default)* | number | Number of app accounts currently connected to the unit. |
| Error *(diagnostic)* | error code | Raw error code reported by the unit; `00` means no error. |
| Updated By *(diagnostic)* | text | Which account last changed the unit's settings (this integration or the Smart M-Air app). |
| Account Expires *(diagnostic)* | text | Expiry of the current operator session. |
| LED Status *(diagnostic)* | text | State of the unit's status LED. |
| Auto Heating *(diagnostic)* | text | State of the unit's automatic heating assist. |
| Model Nr *(diagnostic, disabled by default)* | number | Raw model-identifier byte reported by the unit. Used to gate which optional features (occupancy, Home Leave) are exposed; mostly useful for diagnosing unsupported models. |
| Cool Hot Judge *(diagnostic, disabled by default)* | `cooling`, `heating` | Raw cool/heat state reported by the unit's compressor, independent of the configured mode. `unknown` while off or in `fan_only`. Useful for detecting the "wait/hold" state on multi-split systems where one indoor unit is blocked because the outdoor unit is already committed to the opposite mode for a sibling unit. |

## Binary sensors

| Entity | Values | Description |
|---|---|---|
| Problem | on/off | On whenever the unit reports an error code (`error_code` attribute holds the raw code). |
| Occupancy | on/off | Only created on units that report the "Vacant"/Home Leave bit (see the Home Leave switch below). This is *not* a physical presence/motion sensor - it just mirrors that bit, which is off unless Home Leave mode was actually entered (e.g. via the Home Leave switch). It will read "occupied" even in an empty room if Home Leave was never triggered. |

## Switch

| Entity | Values | Description |
|---|---|---|
| Home Leave Mode | on/off | Enters/leaves the unit's own frost-protection/low-power standby mode for when nobody's home, by lowering the heat target temperature below the unit's Home Leave threshold. Only created on units confirmed to support it. |

## Select (optional)

Only created if "Whether to create an additional swing mode selectors" is enabled in the integration's options — off by default. These duplicate the climate entity's swing/fan attributes as standalone entities, useful for dashboards or automations that prefer a plain `select` over a `climate` attribute.

| Entity | Values | Description |
|---|---|---|
| Horizontal Swing Direction | same as `swing_horizontal_mode` above | |
| Vertical Swing Direction | same as `swing_mode` above | |
| Fan Speed | same as `fan_mode` above | |

## Options

Configurable via the integration's "Configure" (options) flow.

| Option | Range | Description |
|---|---|---|
| Host (IP) address | IP or hostname | Address of the WF-RAC module. |
| Check availability | on/off, on by default | Whether a failed poll is tolerated before the device is marked unavailable. Off marks it unavailable on the first failed poll. |
| Retry limit | 2 or higher, default 3 | Consecutive failed polls before the device is marked unavailable. At the 60 s poll interval, `3` is about 3 minutes. `0` and `1` mark it unavailable on the first failed poll, identical to Check availability being off. |
| Indoor Temp. Sensor Offset | -15..15 °C | Added to the unit's own indoor-sensor reading before it's shown as `current_temperature` / the Indoor Temperature sensor - display-only, doesn't change what the unit does. |
| Outdoor Temp. Sensor Offset | -15..15 °C | Same, for the Outdoor Temperature sensor. |
| Target Temp. Offset | -5..5 °C | Calibrates the *setpoint sent to the unit* - see "Target Temp. Offset sign convention" below. Applies to every `hvac_mode` unless overridden by the two options below. |
| Target Temp. Offset (Cooling) | -5..5 °C, unset by default | Overrides Target Temp. Offset for `cool` and `dry` mode. Leave unset to keep using Target Temp. Offset for those modes too. |
| Target Temp. Offset (Heating) | -5..5 °C, unset by default | Overrides Target Temp. Offset for `heat` mode. Leave unset to keep using Target Temp. Offset for `heat` too. |

### Target Temp. Offset sign convention

The unit's internal temperature sensor is a **return-air sensor built into the indoor unit**, not a sensor sitting where you actually care about the temperature. Its reading is therefore biased towards whatever the unit itself just blew out:

- **Cooling**: the sensor sits in the cold air the unit produces, so it reads *below* the true room temperature.
- **Heating**: the sensor sits in the warm air the unit produces, so it reads *above* the true room temperature.

Target Temp. Offset corrects for this bias: `true_room ≈ PresetTemp + offset`. To land the *room* on the temperature you actually requested, the setpoint sent to the unit is `commanded PresetTemp = requested − offset`.

Concretely: **a negative offset raises the setpoint actually sent to the unit** (a positive offset lowers it). Because the bias flips sign between cooling and heating, no single value is correct for both at once - this is exactly why Target Temp. Offset (Cooling) / (Heating) exist as separate overrides. The offset calibrates the unit's *operating regime* (the return-air bias above), not a fixed mounting/calibration error of the sensor - don't expect one number to be "the correct" offset independent of mode.

# Troubleshooting

## Unit goes briefly unavailable about once an hour

The WF-RAC module drops and re-establishes its WiFi association roughly once an hour. This is
designed behaviour of the module, confirmed by MHI support under ticket reference 813958: the
interface does "connect / disconnect every one hour … to avoid too much cache by the communication"
([source](https://community.ui.com/questions/AC-Units-IOT-disconnecting-from-UniFi-Wi-Fi-at-regular-hourly-Intervals/821cd3e4-46a0-4d6b-8fd0-8d5cf182b90f)).
The reassociation takes seconds to about a minute and cannot be turned off.

This integration polls every 60 seconds, so a reassociation can cost a poll. The availability
options decide whether that becomes a visible outage:

- **Check availability**: on (the default).
- **Retry limit**: `3` (the default) — the device is marked unavailable after 3 consecutive failed
  polls, about 3 minutes. `0` and `1` mark it unavailable on the first failed poll.

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

[installbadge]: https://img.shields.io/badge/dynamic/json?style=for-the-badge&logo=home-assistant&logoColor=ccc&label=usage&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.mitsubishi_wf_rac.total
[installs]: https://analytics.home-assistant.io/
