"""for Climate integration."""

from __future__ import annotations
import asyncio
from datetime import timedelta
import logging
from typing import Any

from . import MitsubishiWfRacConfigEntry
import voluptuous as vol

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import HVACMode, HVACAction, FAN_AUTO
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.util import Throttle
from homeassistant.helpers import config_validation as cv, entity_platform

from .wfrac.device import Device
from .wfrac.models.aircon import AirconCommands
from .wfrac.repository import AirconApiError
from .const import (
    DOMAIN,
    FAN_MODE_TRANSLATION,
    HVAC_TRANSLATION,
    SERVICE_SET_HORIZONTAL_SWING_MODE,
    SERVICE_SET_VERTICAL_SWING_MODE,
    SUPPORT_FLAGS,
    SWING_HORIZONTAL_AUTO,
    SWING_VERTICAL_AUTO,
    SUPPORT_SWING_MODES,
    SUPPORTED_FAN_MODES,
    SUPPORTED_HVAC_MODES,
    SWING_3D_AUTO,
    SWING_MODE_TRANSLATION,
    SWING_HORIZONTAL_MODE_TRANSLATION,
    MIN_TIME_BETWEEN_UPDATES,
    SUPPORT_SWING_HORIZONTAL_MODES,
    CONF_INDOOR_OFFSET,
    CONF_TARGET_OFFSET
)

_LOGGER = logging.getLogger(__name__)
UPDATE_CONSOLIDATION_PERIOD = timedelta(milliseconds=500)


async def async_setup_entry(hass, entry: MitsubishiWfRacConfigEntry, async_add_entities):
    """Setup climate entities"""
    device: Device = entry.runtime_data.device
    _LOGGER.info("Setup climate for: %s, %s", device.device_name, device.airco_id)
    async_add_entities([AircoClimate(device, hass)])

    platform = entity_platform.async_get_current_platform()

    platform.async_register_entity_service(
        SERVICE_SET_HORIZONTAL_SWING_MODE,
        {
            vol.Required("swing_mode"): cv.string,
        },
        "async_set_swing_horizontal_mode",
    )

    platform.async_register_entity_service(
        SERVICE_SET_VERTICAL_SWING_MODE,
        {
            vol.Required("swing_mode"): cv.string,
        },
        "async_set_swing_mode",
    )


class AircoClimate(ClimateEntity):
    """Representation of a climate entity"""

    _attr_supported_features: int = SUPPORT_FLAGS
    _attr_temperature_unit: str = UnitOfTemperature.CELSIUS
    _attr_hvac_modes: list[HVACMode] = SUPPORTED_HVAC_MODES
    _attr_fan_modes: list[str] = SUPPORTED_FAN_MODES
    _attr_hvac_mode: HVACMode = HVACMode.OFF
    _attr_hvac_action: HVACAction | None = None
    _attr_fan_mode: str = FAN_AUTO
    _attr_swing_mode: str | None = SWING_VERTICAL_AUTO
    _attr_swing_modes: list[str] | None = SUPPORT_SWING_MODES
    # Per Mitsubishi Heavy Industries' official operable table ('21 SRK-T-324,
    # models SRK60ZSX-W/A and SRK100ZR-W): indoor unit only accepts 18-30C:
    # values below 18 are not a documented WF-RAC capability.
    _attr_min_temp: float = 18
    _attr_max_temp: float = 30
    _attr_swing_horizontal_mode: str | None = SWING_HORIZONTAL_AUTO
    _attr_swing_horizontal_modes: list[str] | None = SUPPORT_SWING_HORIZONTAL_MODES
    _enable_turn_on_off_backwards_compatibility = False  # Remove after HA 2025.1
    _attr_translation_key = "mitsubishi_wf_rac"

    def __init__(self, device: Device, hass: HomeAssistant) -> None:
        self._device = device
        self._hass = hass

        self._attr_name = device.device_name
        self._attr_device_info = device.device_info
        self._attr_unique_id = f"{DOMAIN}-{self._device.airco_id}-climate"
        self._consolidated_params = {}
        self._update_state()

    async def async_set_temperature(self, **kwargs) -> None:
        """Set new target temperature."""
        set_temp = kwargs.get(ATTR_TEMPERATURE)
        if set_temp is None:
            raise ValueError("Temperature is required")

        if set_temp < self._attr_min_temp:
            raise ValueError(f"Temperature {set_temp} is below minimum {self._attr_min_temp}")

        if set_temp > self._attr_max_temp:
            raise ValueError(f"Temperature {set_temp} is above maximum {self._attr_max_temp}")

        # The AC unit's own thermostat logic uses its own indoor sensor reading,
        # subject to the same calibration bias CONF_INDOOR_OFFSET corrects for
        # display (see sensor.py). To make the unit actually reach the
        # user-requested real room temperature despite that bias, the offset is
        # subtracted from the commanded setpoint before sending - the displayed
        # target_temperature itself is unaffected.
        target_offset = self._device.config_entry.options.get(CONF_TARGET_OFFSET, 0.0)
        target_temp = set_temp - target_offset
        target_temp = max(self._attr_min_temp, min(self._attr_max_temp, target_temp))

        opts: dict[str, Any] = {AirconCommands.PresetTemp: target_temp}

        if "hvac_mode" in kwargs:
            hvac_mode: HVACMode | None = kwargs.get("hvac_mode")
            hvac_mode = HVACMode.OFF if hvac_mode is None else hvac_mode
            opts.update(
                {
                    AirconCommands.OperationMode: self._device.airco.OperationMode
                    if hvac_mode == HVACMode.OFF
                    else HVAC_TRANSLATION[hvac_mode],
                    AirconCommands.Operation: hvac_mode != HVACMode.OFF,
                }
            )

        await self._set_airco(opts)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode."""
        await self._set_airco({AirconCommands.AirFlow: FAN_MODE_TRANSLATION[fan_mode]})

    async def async_turn_on(self) -> None:
        """Turn the entity on."""
        await self._set_airco({AirconCommands.Operation: True})

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        await self._set_airco(
            {
                AirconCommands.OperationMode: self._device.airco.OperationMode
                if hvac_mode == HVACMode.OFF
                else HVAC_TRANSLATION[hvac_mode],
                AirconCommands.Operation: hvac_mode != HVACMode.OFF,
            }
        )

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set new target swing operation."""
        _swing_auto = swing_mode == SWING_3D_AUTO
        if _swing_auto:
            await self._set_airco(
                {
                    AirconCommands.Entrust: _swing_auto,
                }
            )
        else:
            await self._set_airco(
                {
                    AirconCommands.WindDirectionUD: SWING_MODE_TRANSLATION[swing_mode],
                    AirconCommands.Entrust: False,
                }
            )

    async def async_set_swing_horizontal_mode(self, swing_mode: str) -> None:
        """Set new target horizontal swing operation."""
        _swing_auto = swing_mode == SWING_3D_AUTO
        if _swing_auto:
            await self._set_airco(
                {
                    AirconCommands.Entrust: _swing_auto,
                }
            )
        else:
            await self._set_airco(
                {
                    AirconCommands.WindDirectionLR: SWING_HORIZONTAL_MODE_TRANSLATION[swing_mode],
                    AirconCommands.Entrust: False,
                }
            )

    async def async_turn_off(self) -> None:
        """Turn the entity off."""
        await self._set_airco({AirconCommands.Operation: False})

    async def _set_airco(self, params: dict[str, Any]) -> None:
        will_do_update = not self._consolidated_params
        self._consolidated_params.update(params)

        if will_do_update:
            self._hass.async_create_task(self._set_airco_after_delay())

    async def _set_airco_after_delay(self):
        await asyncio.sleep(UPDATE_CONSOLIDATION_PERIOD.total_seconds())
        params = self._consolidated_params.copy()
        self._consolidated_params.clear()
        try:
            await self._device.set_airco(params)
        except (AirconApiError, KeyError, TypeError, ValueError):
            # This runs as a detached task (async_create_task, nothing awaits it), so
            # without this the re-raised error from Device.set_airco() (already logged
            # there) becomes an orphaned "Task exception was never retrieved" with zero
            # HA-visible feedback that the command never reached the unit. Still refresh
            # below so the entity picks up self._device.available if the same failure
            # already flipped it.
            pass
        self._update_state()
        self.async_write_ha_state()

    def _update_state(self) -> None:
        """Private update attributes"""
        airco = self._device.airco

        # Apply indoor offset
        indoor_offset = self._device.config_entry.options.get(CONF_INDOOR_OFFSET, 0.0)

        self._attr_target_temperature = airco.PresetTemp
        self._attr_current_temperature = airco.IndoorTemp + indoor_offset
        self._attr_fan_mode = list(FAN_MODE_TRANSLATION.keys())[airco.AirFlow]
        self._attr_swing_mode = (
            SWING_3D_AUTO
            if airco.Entrust
            else list(SWING_MODE_TRANSLATION.keys())[airco.WindDirectionUD]
        )
        self._attr_swing_horizontal_mode = (
            SWING_3D_AUTO
            if airco.Entrust
            else list(
                SWING_HORIZONTAL_MODE_TRANSLATION.keys()
            )[airco.WindDirectionLR]
        )
        self._attr_available = self._device.available
        self._attr_hvac_mode = list(HVAC_TRANSLATION.keys())[airco.OperationMode]

        if airco.Operation is False:
            self._attr_hvac_mode = HVACMode.OFF
            self._attr_hvac_action = HVACAction.OFF
        else:
            _new_mode: HVACMode = HVACMode.OFF
            _mode = airco.OperationMode
            if _mode == 0:
                _new_mode = HVACMode.AUTO
            elif _mode == 1:
                _new_mode = HVACMode.COOL
            elif _mode == 2:
                _new_mode = HVACMode.HEAT
            elif _mode == 3:
                _new_mode = HVACMode.FAN_ONLY
            elif _mode == 4:
                _new_mode = HVACMode.DRY
            self._attr_hvac_mode = _new_mode

            # Determine hvac_action based on operation mode and state
            self._attr_hvac_action = self._determine_hvac_action(airco)

    def _determine_hvac_action(self, airco) -> HVACAction:
        """Determine the current HVAC action based on operation mode and state.

        CoolHotJudge logic (from rac_parser.py line 267):
        - CoolHotJudge = (content[8] & 8) <= 0
        - When bit is SET (1): CoolHotJudge = False → COOLING
        - When bit is NOT SET (0): CoolHotJudge = True → HEATING

        The CoolHotJudge flag is set by the AC unit itself and indicates what
        action the unit is currently performing in AUTO mode.
        """
        if not airco.Operation:
            return HVACAction.OFF

        _mode = airco.OperationMode

        # FAN_ONLY mode
        if _mode == 3:
            return HVACAction.FAN

        # DRY mode
        if _mode == 4:
            return HVACAction.DRYING

        # AUTO mode - use CoolHotJudge directly (unit tells us what it's doing)
        # CoolHotJudge is set by the AC unit based on its internal logic
        if _mode == 0:
            # CoolHotJudge = True means HEATING (bit NOT set in byte 8)
            # CoolHotJudge = False means COOLING (bit IS set in byte 8)
            if airco.CoolHotJudge:
                return HVACAction.HEATING
            else:
                return HVACAction.COOLING

        # COOL mode - unit is actively cooling when on
        # The AC unit manages its own cycles, so we report COOLING when on
        if _mode == 1:
            return HVACAction.COOLING

        # HEAT mode - unit is actively heating when on
        # The AC unit manages its own cycles, so we report HEATING when on
        if _mode == 2:
            return HVACAction.HEATING

        # Default to idle if mode is unknown
        return HVACAction.IDLE

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    async def async_update(self):
        """Retrieve latest state."""
        try:
            await self._device.update()
            self._update_state()
        except (IndexError, KeyError, AttributeError, ValueError):
            _LOGGER.warning("Could not update the airco values")
            self._attr_available = False
            self._device.set_available(False)
            self.async_write_ha_state()