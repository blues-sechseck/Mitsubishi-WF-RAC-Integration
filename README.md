# Mitsubishi WF-RAC Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
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
