# Mitsubishi WF-RAC Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

This is a Homeassistant integration for implementing the WF-RAC Airco modules into Homeassistant

`❗ Note: This is an experimental integration that is reversed engineert. Therefore there can by unwanted results ❗`

This is a fork of [jeatheak/Mitsubishi-WF-RAC-Integration](https://github.com/jeatheak/Mitsubishi-WF-RAC-Integration) with additional fixes merged in (account-eviction recovery, coordinator timeout, HTTPS session reuse, availability-check option key, detached-task error handling, core-readiness cleanup).

# Todo 📃 and Bug report 🐞

See [Github To Do & Bug List](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/issues)

# Support

Original integration by jeatheak — <a href="https://www.buymeacoffee.com/jeatheak" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" style="height: 40px !important;width: 145px !important;" ></a>

# Installation

Install using [HACS](https://hacs.xyz)
In HACS go to the three dots int the upper right corner choose add custom repository and add https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration to the list.

Install manually
Clone or copy this repository and copy the folder 'custom_components/mitsubishi-wf-rac' into '/custom_components/mitsubishi-wf-rac'
