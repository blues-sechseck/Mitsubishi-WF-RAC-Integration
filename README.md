# Mitsubishi WF-RAC Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
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

Install using [HACS](https://hacs.xyz)
In HACS go to the three dots int the upper right corner choose add custom repository and add https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration to the list.

Install manually
Clone or copy this repository and copy the folder 'custom_components/mitsubishi-wf-rac' into '/custom_components/mitsubishi-wf-rac'

# Troubleshooting

## Unit becomes briefly unavailable / drops connection every so often

This is a long-standing, widely reported issue (see #106, #146, #173) that appears to originate in
the WF-RAC WiFi module itself rather than in this integration. Community-gathered findings so far:

- Lower "WF-RAC AC Connection" → "Availability retry limit" to `0` or `1`, and disable "Check
  availability" in the integration's options. Retrying aggressively appears to make the module's own
  reconnect behavior worse rather than better.
- Several users who additionally blocked the unit's outbound internet access (router/firewall rule —
  LAN access for this integration still works fine, only WAN is blocked) report the drops disappearing
  entirely over several weeks. The working theory is that whatever the module does when it reaches out
  to Mitsubishi's cloud service periodically interferes with its local HTTP API.
- This has not been confirmed against the module's firmware/protocol directly, just observed
  empirically by several users — if it doesn't help in your case, please say so on one of the issues
  above so we can keep the guidance accurate.
- If none of the above helps and it's a dealbreaker for you, some users have switched to
  [MHI-AC-Ctrl-ESPHome](https://github.com/ginkage/MHI-AC-Ctrl-ESPHome) instead, which replaces the
  WF-RAC WiFi module with your own ESP hardware and reports fewer stability issues — at the cost of a
  DIY hardware install.

# Entities

This integration creates one device per airco with the following entities.

## Climate

| Entity | Attribute | Available values | Description |
|---|---|---|---|
| `climate.<name>` | `hvac_mode` | `off`, `auto`, `cool`, `heat`, `dry`, `fan_only` | Operating mode of the unit. |
| | `fan_mode` | `auto`, `quiet`, `low`, `medium`, `high` | Fan speed. |
| | `swing_mode` | `up_down_auto`, `highest`, `middle`, `normal`, `lowest`, `3d_auto` | Vertical louver position. `3d_auto` hands vertical *and* horizontal swing over to the unit's own automatic mode. |
| | `swing_horizontal_mode` | `left_right_auto`, `left_left`, `left_center`, `center_center`, `center_right`, `right_right`, `left_right`, `right_left`, `3d_auto` | Horizontal louver position. `3d_auto` behaves as above. |
| | `target_temperature` | 18–30 °C | Setpoint. The AC unit itself only accepts this range. |
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

## Binary sensors

| Entity | Values | Description |
|---|---|---|
| Problem | on/off | On whenever the unit reports an error code (`error_code` attribute holds the raw code). |
| Occupancy | on/off | Only created on units that support presence detection. Reflects whether the unit currently sees the room as occupied. |

## Switch

| Entity | Values | Description |
|---|---|---|
| Self Clean | on/off | Starts/stops the unit's self-clean cycle. Only created on units that support it. After toggling, the real state is re-read from the unit after a short delay, since the unit's own response can briefly still show the old state. |

## Select (optional)

Only created if "Whether to create an additional swing mode selectors" is enabled in the integration's options — off by default. These duplicate the climate entity's swing/fan attributes as standalone entities, useful for dashboards or automations that prefer a plain `select` over a `climate` attribute.

| Entity | Values | Description |
|---|---|---|
| Horizontal Swing Direction | same as `swing_horizontal_mode` above | |
| Vertical Swing Direction | same as `swing_mode` above | |
| Fan Speed | same as `fan_mode` above | |

[installbadge]: https://img.shields.io/badge/dynamic/json?style=for-the-badge&logo=home-assistant&logoColor=ccc&label=usage&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.mitsubishi_wf_rac.total
[installs]: https://analytics.home-assistant.io/
