# Mitsubishi WF-RAC Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

This is a Homeassistant integration for implementing the WF-RAC Airco modules into Homeassistant

`❗ Note: This is an experimental integration that is reversed engineert. Therefore there can by unwanted results ❗`

This is a fork of [jeatheak/Mitsubishi-WF-RAC-Integration](https://github.com/jeatheak/Mitsubishi-WF-RAC-Integration) with additional fixes merged in (account-eviction recovery, coordinator timeout, HTTPS session reuse, availability-check option key, detached-task error handling, core-readiness cleanup).

## ⚠️ Coming from the original repo? Check your automations

Since [2026.8](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/releases/tag/2026.8), the `fan_mode`/`swing_mode`/`swing_horizontal_mode` state values were renamed to snake_case (e.g. `"Up/Down Auto"` → `"up_down_auto"`, `"Quiet"` → `"quiet"`, `"3D Auto"` → `"3d_auto"`) to satisfy Home Assistant's own validation rules — the old capitalized values were never actually valid. If you have automations, scripts, or dashboards that call `climate.set_fan_mode`, `select.select_option`, or the `set_horizontal_swing_mode`/`set_vertical_swing_mode` services with the old capitalized strings, update them to the new lowercase values. See the [2026.8 release notes](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/releases/tag/2026.8) for the full list.

# Todo 📃 and Bug report 🐞

See [Github To Do & Bug List](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/issues)

# Installation

Install using [HACS](https://hacs.xyz)
In HACS go to the three dots int the upper right corner choose add custom repository and add https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration to the list.

Install manually
Clone or copy this repository and copy the folder 'custom_components/mitsubishi-wf-rac' into '/custom_components/mitsubishi-wf-rac'
