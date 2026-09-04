"""Entity service actions of the WF-RAC integration.

Registered from async_setup rather than from the platforms themselves: with
the platform-level API the actions only existed once a config entry had
finished setting up its climate/sensor platform, so a device that was
unreachable at startup left the actions missing from the UI and from any
automation that referenced them. Registering here makes them independent of
that - the entities a call resolves to are still restricted to this
integration's own, the helper takes care of that.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_platform_entity_service

from pywfrac.parser import EXTERNAL_TEMPERATURE_MAX, EXTERNAL_TEMPERATURE_MIN

from .const import (
    DOMAIN,
    SERVICE_REQUEST_HOME_LEAVE_MODE_STATUS,
    SERVICE_SET_ENERGY_TOTAL,
    SERVICE_SET_EXTERNAL_TEMPERATURE,
    SERVICE_SET_HOME_LEAVE_MODE,
    SERVICE_SET_HORIZONTAL_SWING_MODE,
    SERVICE_SET_VERTICAL_SWING_MODE,
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register this integration's entity service actions."""
    # Imported here, not at module level: sensor.py imports the config entry
    # type from __init__.py, which imports this module - at import time that
    # is a cycle, by the time async_setup runs it is not.
    from .sensor import async_set_energy_total  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_HORIZONTAL_SWING_MODE,
        entity_domain=Platform.CLIMATE,
        func="async_set_swing_horizontal_mode",
        schema={vol.Required("swing_mode"): cv.string},
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_VERTICAL_SWING_MODE,
        entity_domain=Platform.CLIMATE,
        func="async_set_swing_mode",
        schema={vol.Required("swing_mode"): cv.string},
    )

    # HomeLeaveMode (Tag 248, capability index 7) - deliberately actions, not
    # switch/number entities, until confirmed on real hardware: no dashboard
    # tile to accidentally trigger before that.
    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_REQUEST_HOME_LEAVE_MODE_STATUS,
        entity_domain=Platform.CLIMATE,
        func="async_request_home_leave_mode_status",
        schema={},
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_HOME_LEAVE_MODE,
        entity_domain=Platform.CLIMATE,
        func="async_set_home_leave_mode",
        schema={
            vol.Required("temp_rule_cooling"): vol.Coerce(float),
            vol.Required("temp_setting_cooling"): vol.Coerce(float),
            # The select selector in services.yaml submits its value as a
            # string ("0".."4") - coerce before checking range so both that
            # and a programmatic int call work.
            vol.Required("air_flow_cooling"): vol.All(
                vol.Coerce(int), vol.In([0, 1, 2, 3, 4])
            ),
            vol.Required("temp_rule_heating"): vol.Coerce(float),
            vol.Required("temp_setting_heating"): vol.Coerce(float),
            vol.Required("air_flow_heating"): vol.All(
                vol.Coerce(int), vol.In([0, 1, 2, 3, 4])
            ),
        },
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_EXTERNAL_TEMPERATURE,
        entity_domain=Platform.CLIMATE,
        func="async_set_external_temperature",
        schema={
            vol.Optional("temperature"): vol.Any(
                vol.Range(min=EXTERNAL_TEMPERATURE_MIN, max=EXTERNAL_TEMPERATURE_MAX),
                None,
            ),
        },
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_ENERGY_TOTAL,
        entity_domain=Platform.SENSOR,
        func=async_set_energy_total,
        schema={vol.Required("value"): vol.All(vol.Coerce(float), vol.Range(min=0))},
    )
