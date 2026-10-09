"""Entity service actions of the WF-RAC integration.

Registered from async_setup, not from the platforms: an action registered by
a platform is missing from the UI and from automations until a config entry
finishes setting that platform up, which a device unreachable at startup
never does. Calls still resolve only to this integration's entities.
"""

from __future__ import annotations

from pywfrac.parser import EXTERNAL_TEMPERATURE_MAX, EXTERNAL_TEMPERATURE_MIN

# HA 2026.10 types flow and service schemas against probatio. voluptuous
# stays until the floor in hacs.json reaches that release, so the call
# sites handing it over carry a type: ignore.
import voluptuous as vol

from homeassistant.components.climate import ClimateEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.service import async_register_platform_entity_service

from .const import (
    DOMAIN,
    SERVICE_REQUEST_HOME_LEAVE_MODE_STATUS,
    SERVICE_SET_ENERGY_TOTAL,
    SERVICE_SET_EXTERNAL_TEMPERATURE,
    SERVICE_SET_HOME_LEAVE_MODE,
    SERVICE_SET_HORIZONTAL_SWING_MODE,
    SERVICE_SET_VERTICAL_SWING_MODE,
    SUPPORT_SWING_HORIZONTAL_MODES,
    SUPPORT_SWING_MODES,
)

# Home Leave thresholds go out as int(value * 2) in a single byte, masked
# rather than refused, so anything outside this comes back as a different
# temperature.
_home_leave_temperature = vol.All(vol.Coerce(float), vol.Range(min=0, max=127.5))


async def _async_set_horizontal_swing_mode(
    entity: ClimateEntity, call: ServiceCall
) -> None:
    """Hand the action's swing_mode field to the climate method.

    The field keeps the name both swing actions share, so existing automations
    stay valid; the method takes it as swing_horizontal_mode.
    """
    await entity.async_set_swing_horizontal_mode(call.data["swing_mode"])


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register this integration's entity service actions."""
    # Imported here, not at module level: sensor.py imports the config entry
    # type from __init__.py, which imports this module - at import time that
    # is a cycle, by the time async_setup runs it is not.
    from .sensor import async_set_energy_total  # noqa: PLC0415

    # HACS only: climate.set_swing_mode and climate.set_swing_horizontal_mode
    # already do this, and binding func= directly skips the base class's own
    # mode validation - hence vol.In here, which it would otherwise do.
    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_HORIZONTAL_SWING_MODE,
        entity_domain=Platform.CLIMATE,
        func=_async_set_horizontal_swing_mode,
        schema={vol.Required("swing_mode"): vol.In(SUPPORT_SWING_HORIZONTAL_MODES)},  # type: ignore[dict-item]
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_VERTICAL_SWING_MODE,
        entity_domain=Platform.CLIMATE,
        func="async_set_swing_mode",
        schema={vol.Required("swing_mode"): vol.In(SUPPORT_SWING_MODES)},  # type: ignore[dict-item]
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
            vol.Required("temp_rule_cooling"): _home_leave_temperature,  # type: ignore[dict-item]
            vol.Required("temp_setting_cooling"): _home_leave_temperature,  # type: ignore[dict-item]
            # The select selector in services.yaml submits its value as a
            # string ("0".."4") - coerce before checking range so both that
            # and a programmatic int call work.
            vol.Required("air_flow_cooling"): vol.All(  # type: ignore[dict-item]
                vol.Coerce(int), vol.In([0, 1, 2, 3, 4])
            ),
            vol.Required("temp_rule_heating"): _home_leave_temperature,  # type: ignore[dict-item]
            vol.Required("temp_setting_heating"): _home_leave_temperature,  # type: ignore[dict-item]
            vol.Required("air_flow_heating"): vol.All(  # type: ignore[dict-item]
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
            vol.Optional("temperature"): vol.Any(  # type: ignore[dict-item]
                vol.All(
                    vol.Coerce(float),
                    vol.Range(
                        min=EXTERNAL_TEMPERATURE_MIN, max=EXTERNAL_TEMPERATURE_MAX
                    ),
                ),
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
        schema={vol.Required("value"): vol.All(vol.Coerce(float), vol.Range(min=0))},  # type: ignore[dict-item]
    )
