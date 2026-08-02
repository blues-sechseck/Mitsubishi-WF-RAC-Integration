"""for Climate integration."""

from __future__ import annotations
import logging
from typing import Any

from . import MitsubishiWfRacConfigEntry
import voluptuous as vol

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import HVACMode, HVACAction, FAN_AUTO
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE
from homeassistant.helpers import config_validation as cv, entity_platform

from .entity import WfRacEntity
from .wfrac.device import Device
from .wfrac.models.aircon import AirconCommands
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
    SUPPORT_SWING_HORIZONTAL_MODES,
    CONF_INDOOR_OFFSET,
    CONF_TARGET_OFFSET,
    CONF_TARGET_OFFSET_COOL,
    CONF_TARGET_OFFSET_HEAT,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry: MitsubishiWfRacConfigEntry, async_add_entities):
    """Setup climate entities"""
    device: Device = entry.runtime_data.device
    _LOGGER.info("Setup climate for: %s, %s", device.device_name, device.airco_id)
    async_add_entities([AircoClimate(device)])

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


class AircoClimate(WfRacEntity, ClimateEntity):
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
    # models SRK60ZSX-W/A and SRK100ZR-W): indoor unit only accepts 18-30C.
    # Cooling reliably goes lower than that in practice (see #113), but heating
    # below 18C only works through the unit's own Vacant/Home Leave mechanism
    # (see the Home Leave switch) - as a plain setpoint it silently resets to
    # 18 after a power cycle, so it isn't offered as a free value here.
    _attr_max_temp: float = 30
    _attr_swing_horizontal_mode: str | None = SWING_HORIZONTAL_AUTO
    _attr_swing_horizontal_modes: list[str] | None = SUPPORT_SWING_HORIZONTAL_MODES
    _enable_turn_on_off_backwards_compatibility = False  # Remove after HA 2025.1
    _attr_translation_key = "mitsubishi_wf_rac"

    def __init__(self, device: Device) -> None:
        super().__init__(device)
        self._attr_name = device.device_name
        self._attr_unique_id = f"{DOMAIN}-{self._device.airco_id}-climate"
        self._update_state()

    @staticmethod
    def _min_temp_for_mode(hvac_mode: HVACMode) -> float:
        """Minimum setpoint depends on hvac_mode - see #113."""
        return 16 if hvac_mode == HVACMode.COOL else 18

    @property
    def min_temp(self) -> float:
        return self._min_temp_for_mode(self._attr_hvac_mode)

    def _resolve_target_offset(self, hvac_mode: HVACMode) -> float:
        """Resolve the effective target_offset for a given hvac_mode.

        COOL/DRY fall back to CONF_TARGET_OFFSET_COOL, HEAT to
        CONF_TARGET_OFFSET_HEAT, everything else always uses the global
        CONF_TARGET_OFFSET - and so does COOL/HEAT when its per-mode option
        is unset (None), which is what keeps single-target_offset installs
        unchanged. Called from both the write and read-back path so they can
        never resolve a different offset for the same mode (see beta2: that
        divergence is what caused the target_temperature re-send loop).
        """
        options = self._device.config_entry.options
        base_offset = options.get(CONF_TARGET_OFFSET, 0.0)
        if hvac_mode in (HVACMode.COOL, HVACMode.DRY):
            override = options.get(CONF_TARGET_OFFSET_COOL)
        elif hvac_mode == HVACMode.HEAT:
            override = options.get(CONF_TARGET_OFFSET_HEAT)
        else:
            override = None
        return base_offset if override is None else override

    async def async_set_temperature(self, **kwargs) -> None:
        """Set new target temperature."""
        set_temp = kwargs.get(ATTR_TEMPERATURE)
        if set_temp is None:
            raise ValueError("Temperature is required")

        # If this call also switches hvac_mode, the minimum must reflect the mode
        # being switched to, not the (still stale until the next poll) current one.
        target_hvac_mode = kwargs.get("hvac_mode", self._attr_hvac_mode)
        target_hvac_mode = HVACMode.OFF if target_hvac_mode is None else target_hvac_mode
        min_temp = self._min_temp_for_mode(target_hvac_mode)

        if set_temp < min_temp:
            raise ValueError(f"Temperature {set_temp} is below minimum {min_temp}")

        if set_temp > self._attr_max_temp:
            raise ValueError(f"Temperature {set_temp} is above maximum {self._attr_max_temp}")

        # The AC unit's own thermostat logic uses its own indoor sensor reading,
        # subject to the same calibration bias CONF_INDOOR_OFFSET corrects for
        # display (see sensor.py). To make the unit actually reach the
        # user-requested real room temperature despite that bias, the offset is
        # subtracted from the commanded setpoint before sending - the displayed
        # target_temperature itself is unaffected. Resolved against the mode
        # the unit will be in after this command (target_hvac_mode), since
        # cooling and heating have opposite-sign return-air bias.
        target_offset = self._resolve_target_offset(target_hvac_mode)
        target_temp = set_temp - target_offset
        target_temp = max(min_temp, min(self._attr_max_temp, target_temp))

        opts: dict[str, Any] = {AirconCommands.PresetTemp: target_temp}

        if "hvac_mode" in kwargs:
            opts.update(
                {
                    AirconCommands.OperationMode: self._device.airco.OperationMode
                    if target_hvac_mode == HVACMode.OFF
                    else HVAC_TRANSLATION[target_hvac_mode],
                    AirconCommands.Operation: target_hvac_mode != HVACMode.OFF,
                }
            )

        await self._device.async_queue_command(opts)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode."""
        await self._device.async_queue_command({AirconCommands.AirFlow: FAN_MODE_TRANSLATION[fan_mode]})

    async def async_turn_on(self) -> None:
        """Turn the entity on."""
        await self._device.async_queue_command({AirconCommands.Operation: True})

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        await self._device.async_queue_command(
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
            await self._device.async_queue_command(
                {
                    AirconCommands.Entrust: _swing_auto,
                }
            )
        else:
            await self._device.async_queue_command(
                {
                    AirconCommands.WindDirectionUD: SWING_MODE_TRANSLATION[swing_mode],
                    AirconCommands.Entrust: False,
                }
            )

    async def async_set_swing_horizontal_mode(self, swing_mode: str) -> None:
        """Set new target horizontal swing operation."""
        _swing_auto = swing_mode == SWING_3D_AUTO
        if _swing_auto:
            await self._device.async_queue_command(
                {
                    AirconCommands.Entrust: _swing_auto,
                }
            )
        else:
            await self._device.async_queue_command(
                {
                    AirconCommands.WindDirectionLR: SWING_HORIZONTAL_MODE_TRANSLATION[swing_mode],
                    AirconCommands.Entrust: False,
                }
            )

    async def async_turn_off(self) -> None:
        """Turn the entity off."""
        await self._device.async_queue_command({AirconCommands.Operation: False})

    def _update_state(self) -> None:
        """Private update attributes"""
        airco = self._device.airco

        # Apply indoor offset
        indoor_offset = self._device.config_entry.options.get(CONF_INDOOR_OFFSET, 0.0)
        # airco.OperationMode reports the underlying cool/heat mode even
        # while the unit is off (self._attr_hvac_mode gets forced to OFF
        # below in that case) - both the displayed hvac_mode and the
        # target_offset resolution need that underlying mode, so it's
        # computed once here and shared between them.
        mode_from_operation = list(HVAC_TRANSLATION.keys())[airco.OperationMode]
        # Mirror the subtraction in async_set_temperature() so the displayed
        # target_temperature agrees with what the user set - PresetTemp itself
        # holds the offset-lowered value that was actually sent to the device.
        target_offset = self._resolve_target_offset(mode_from_operation)

        self._attr_target_temperature = airco.PresetTemp + target_offset
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
        self._attr_hvac_mode = mode_from_operation

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