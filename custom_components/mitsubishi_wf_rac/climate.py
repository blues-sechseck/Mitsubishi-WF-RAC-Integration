"""for Climate integration."""

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any

from . import MitsubishiWfRacConfigEntry
import voluptuous as vol

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
    FAN_AUTO,
)
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from .entity import WfRacEntity
from .coordinator import Device
from .wfrac.models.aircon import Aircon, AirconCommands, HomeLeaveModeSetting
from .wfrac.rac_parser import (
    EXTERNAL_TEMPERATURE_MAX,
    EXTERNAL_TEMPERATURE_MIN,
    is_external_temperature_mode,
)
from .const import (
    DOMAIN,
    FAN_MODE_TRANSLATION,
    HVAC_TRANSLATION,
    SERVICE_REQUEST_HOME_LEAVE_MODE_STATUS,
    SERVICE_SET_HOME_LEAVE_MODE,
    SERVICE_SET_EXTERNAL_TEMPERATURE,
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
)

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MitsubishiWfRacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
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

    # HomeLeaveMode (Tag 248, capability index 7) - deliberately services, not
    # switch/number entities, until confirmed on real hardware: no dashboard
    # tile to accidentally trigger before that.
    platform.async_register_entity_service(
        SERVICE_REQUEST_HOME_LEAVE_MODE_STATUS,
        {},
        "async_request_home_leave_mode_status",
    )

    platform.async_register_entity_service(
        SERVICE_SET_HOME_LEAVE_MODE,
        {
            vol.Required("temp_rule_cooling"): vol.Coerce(float),
            vol.Required("temp_setting_cooling"): vol.Coerce(float),
            # The select selector in services.yaml submits its value as a
            # string ("0".."4") - coerce before checking range so both that
            # and a programmatic int call work.
            vol.Required("air_flow_cooling"): vol.All(vol.Coerce(int), vol.In([0, 1, 2, 3, 4])),
            vol.Required("temp_rule_heating"): vol.Coerce(float),
            vol.Required("temp_setting_heating"): vol.Coerce(float),
            vol.Required("air_flow_heating"): vol.All(vol.Coerce(int), vol.In([0, 1, 2, 3, 4])),
        },
        "async_set_home_leave_mode",
    )

    platform.async_register_entity_service(
        SERVICE_SET_EXTERNAL_TEMPERATURE,
        {
            vol.Optional("temperature"): vol.Any(
                vol.Range(min=EXTERNAL_TEMPERATURE_MIN, max=EXTERNAL_TEMPERATURE_MAX),
                None,
            ),
        },
        "async_set_external_temperature",
    )


@dataclass
class ExternalTemperatureOverrideData(ExtraStoredData):
    """The armed external temperature override, stored next to the entity state.

    Deliberately not carried by a state attribute: attributes are only merged
    into a state while the entity is available, and this module goes
    unreachable for about a minute every hour, so an override armed before that
    gap would quietly disappear from the restored state.
    """

    external_temperature_override: float | None

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the restore-state store."""
        return {"external_temperature_override": self.external_temperature_override}


class AircoClimate(WfRacEntity, ClimateEntity, RestoreEntity):
    """Representation of a climate entity"""

    _external_temperature_override: float | None = None

    _attr_supported_features: ClimateEntityFeature = SUPPORT_FLAGS
    _attr_temperature_unit: str = UnitOfTemperature.CELSIUS
    _attr_hvac_modes: list[HVACMode] = SUPPORTED_HVAC_MODES
    _attr_fan_modes: list[str] = SUPPORTED_FAN_MODES
    _attr_hvac_mode: HVACMode = HVACMode.OFF
    _attr_hvac_action: HVACAction | None = None
    _attr_fan_mode: str = FAN_AUTO
    _attr_swing_mode: str | None = SWING_VERTICAL_AUTO
    _attr_swing_modes: list[str] | None = SUPPORT_SWING_MODES
    _attr_swing_horizontal_mode: str | None = SWING_HORIZONTAL_AUTO
    _attr_swing_horizontal_modes: list[str] | None = SUPPORT_SWING_HORIZONTAL_MODES
    _enable_turn_on_off_backwards_compatibility = False  # Remove after HA 2025.1
    _attr_translation_key = "mitsubishi_wf_rac"

    def __init__(self, device: Device) -> None:
        super().__init__(device)
        self._attr_name = device.device_name
        self._attr_unique_id = f"{DOMAIN}-{self._device.airco_id}-climate"
        self._update_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._restore_external_temperature_override(await self.async_get_last_extra_data())
        # No async_write_ha_state() here: the platform writes the state itself
        # once this returns, and until then the entity is still being added,
        # which makes the call a no-op.
        self._update_state()

    def _restore_external_temperature_override(self, stored: ExtraStoredData | None) -> None:
        """Re-arm the override recorded before the last restart or reload.

        Restoring only arms it integration-side - the unit is not told anything
        here. The next outgoing frame carries the value, and until one has, the
        override counts as unapplied (Device.external_temperature_applied).
        """
        if stored is None:
            return
        restored = stored.as_dict().get("external_temperature_override")
        if restored is None:
            return
        try:
            value = float(restored)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "Ignoring unreadable stored external temperature override: %r", restored
            )
            return
        # Range-checked here rather than left to the encoder: a value outside
        # the encodable span raises on every frame it is put into, which would
        # take down the command path and the operation-data request with it -
        # and it would do so on every restart, with nothing pointing at the
        # stored value as the cause.
        if not EXTERNAL_TEMPERATURE_MIN <= value <= EXTERNAL_TEMPERATURE_MAX:
            _LOGGER.warning(
                "Ignoring stored external temperature override %s °C: outside the "
                "encodable range of %s..%s °C",
                value,
                EXTERNAL_TEMPERATURE_MIN,
                EXTERNAL_TEMPERATURE_MAX,
            )
            return
        self._external_temperature_override = value
        self._device.set_external_temperature_override(value)

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """What async_added_to_hass() reads back after a restart or reload."""
        return ExternalTemperatureOverrideData(self._external_temperature_override)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the armed override, so it is visible without calling the
        action to find out. Display only - the value that survives a restart is
        the one in extra_restore_state_data above.
        """
        attrs = dict(super().extra_state_attributes or {})
        if self._external_temperature_override is not None:
            attrs["external_temperature_override"] = self._external_temperature_override
        return attrs

    def _min_temp_for_mode(self, hvac_mode: HVACMode) -> float:
        """Minimum setpoint depends on hvac_mode.

        Per Mitsubishi Heavy Industries' official operable table ('21
        SRK-T-324, models SRK60ZSX-W/A and SRK100ZR-W): indoor unit only
        accepts 18-30C. Cooling reliably goes lower than that in practice
        regardless of model, so that override applies unconditionally.
        Models with the app's PresetTempRange2 capability (`ModelNoType`/
        `TempItemType` in the app, see wfrac/capabilities.py) go further,
        per the app's own table (Constants.java TempItemType.getMin/getMax):
        Auto/Cool/Dry down to 16, Heat down to 10. That 10C heating floor is
        unconfirmed on real hardware - the plain-setpoint reset to 18C after a
        power cycle that's documented for the default range was only ever
        observed on hardware without this capability.
        """
        if self._device.airco.Capabilities.preset_temp_range_2:
            if hvac_mode == HVACMode.HEAT:
                return 10
            if hvac_mode in (HVACMode.COOL, HVACMode.DRY, HVACMode.AUTO):
                return 16
        return 16 if hvac_mode == HVACMode.COOL else 18

    def _max_temp_for_mode(self, hvac_mode: HVACMode) -> float:
        """Maximum setpoint depends on hvac_mode for PresetTempRange2 models -
        see _min_temp_for_mode."""
        if self._device.airco.Capabilities.preset_temp_range_2 and hvac_mode in (
            HVACMode.COOL,
            HVACMode.DRY,
        ):
            return 33
        return 30

    @property
    def min_temp(self) -> float:
        return self._min_temp_for_mode(self._attr_hvac_mode)

    @property
    def max_temp(self) -> float:
        return self._max_temp_for_mode(self._attr_hvac_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        set_temp = kwargs.get(ATTR_TEMPERATURE)
        if set_temp is None:
            raise ServiceValidationError(
                "Temperature is required",
                translation_domain=DOMAIN,
                translation_key="temperature_required",
            )

        # If this call also switches hvac_mode, the minimum must reflect the mode
        # being switched to, not the (still stale until the next poll) current one.
        target_hvac_mode = kwargs.get("hvac_mode", self._attr_hvac_mode)
        target_hvac_mode = HVACMode.OFF if target_hvac_mode is None else target_hvac_mode
        min_temp = self._min_temp_for_mode(target_hvac_mode)
        max_temp = self._max_temp_for_mode(target_hvac_mode)

        if set_temp < min_temp:
            raise ServiceValidationError(
                f"Temperature {set_temp} is below minimum {min_temp}",
                translation_domain=DOMAIN,
                translation_key="temperature_below_minimum",
                translation_placeholders={
                    "temperature": str(set_temp),
                    "min_temp": str(min_temp),
                },
            )

        if set_temp > max_temp:
            raise ServiceValidationError(
                f"Temperature {set_temp} is above maximum {max_temp}",
                translation_domain=DOMAIN,
                translation_key="temperature_above_maximum",
                translation_placeholders={
                    "temperature": str(set_temp),
                    "max_temp": str(max_temp),
                },
            )

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
        target_temp = max(min_temp, min(max_temp, target_temp))

        opts: dict[AirconCommands, Any] = {AirconCommands.PresetTemp: target_temp}

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

    async def async_set_external_temperature(self, temperature: float | None = None) -> None:
        """Arm an external room temperature override, or revert to the unit's
        internal sensor. The valid range is enforced by the service schema.

        Arming only: nothing is sent from here. The value rides along on the
        next frame that goes out anyway - the operation-data request once a
        cycle, or any command in between - and the same is true of clearing
        it, since a frame without an override carries 0xFF and puts the unit
        back on its own sensor.

        A command frame of this action's own would cost more than the wait.
        Byte 5 has no set-bit, so it cannot be written on its own: the frame
        carrying it also re-asserts power, mode, fan speed, setpoint and both
        vane axes, which is what ends a running self-clean cycle. It would
        also take the unit's 60-second write lock, and a source sensor
        reporting every minute would hold that lock permanently, locking the
        official app out for as long as the override is in use.

        The frame it rides on is the operation-data request, which the
        coordinator starts asking for while an override is armed - the
        override subscribes to a segment itself, exactly like an enabled
        diagnostic sensor does (see Device._sync_external_temperature_carrier).
        """
        self._external_temperature_override = temperature
        self._device.set_external_temperature_override(temperature)
        # Nothing else refreshes the entity here - no command goes out, so
        # there is no response to update from, and the next poll is up to a
        # minute away.
        self._update_state()
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn the entity off."""
        await self._device.async_queue_command({AirconCommands.Operation: False})

    def _require_home_leave_mode_capability(self) -> None:
        if not self._device.airco.Capabilities.home_leave_mode:
            raise ServiceValidationError(
                "This model does not report the HomeLeaveMode capability",
                translation_domain=DOMAIN,
                translation_key="home_leave_mode_not_supported",
            )

    async def async_request_home_leave_mode_status(self) -> None:
        """See Device.async_request_home_leave_mode_status - verified live
        against the official app's own display."""
        self._require_home_leave_mode_capability()
        await self._device.async_request_home_leave_mode_status()

    async def async_set_home_leave_mode(
        self,
        temp_rule_cooling: float,
        temp_setting_cooling: float,
        air_flow_cooling: int,
        temp_rule_heating: float,
        temp_setting_heating: float,
        air_flow_heating: int,
    ) -> None:
        """See Device.async_set_home_leave_mode - verified live."""
        self._require_home_leave_mode_capability()
        await self._device.async_set_home_leave_mode(
            HomeLeaveModeSetting(
                TempRule=temp_rule_cooling,
                TempSetting=temp_setting_cooling,
                AirFlow=air_flow_cooling,
            ),
            HomeLeaveModeSetting(
                TempRule=temp_rule_heating,
                TempSetting=temp_setting_heating,
                AirFlow=air_flow_heating,
            ),
        )

    def _update_state(self) -> None:
        """Private update attributes"""
        airco = self._device.airco

        # Apply indoor offset
        indoor_offset = self._device.options.get(CONF_INDOOR_OFFSET, 0.0)
        # Both the displayed hvac_mode and the target_offset resolution need
        # the underlying cool/heat mode, so it's computed once here and shared
        # between them.
        mode_from_operation = self._hvac_mode_from_operation
        # Mirror the subtraction in async_set_temperature() so the displayed
        # target_temperature agrees with what the user set - PresetTemp itself
        # holds the offset-lowered value that was actually sent to the device.
        target_offset = self._resolve_target_offset(mode_from_operation)

        self._attr_target_temperature = airco.PresetTemp + target_offset
        # Show the override whenever it is armed and the unit is in a mode that
        # actually regulates on a room temperature. Off and fan_only are handled
        # by the third condition. Deliberately diverges from the Indoor
        # Temperature sensor, which always shows the unit's own reading.
        if (
            self._external_temperature_override is not None
            and is_external_temperature_mode(airco.Operation, airco.OperationMode)
        ):
            self._attr_current_temperature = self._external_temperature_override
        else:
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

    def _determine_hvac_action(self, airco: Aircon) -> HVACAction:
        """Determine the current HVAC action from operation mode and state.

        CoolHotJudge (content[8] & 8) reflects what the unit's own AUTO logic
        is doing - set means COOLING, clear means HEATING. CompressorRunning
        (content[9] & 2) distinguishes "unit on" from "compressor actually
        running" (e.g. setpoint satisfied), same signal as the Compressor
        binary sensor - used here so COOL/HEAT/AUTO can report IDLE instead
        of claiming to cool/heat while the compressor is stopped.
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

        if not airco.CompressorRunning:
            return HVACAction.IDLE

        # AUTO mode - use CoolHotJudge directly (unit tells us what it's doing)
        if _mode == 0:
            return HVACAction.HEATING if airco.CoolHotJudge else HVACAction.COOLING

        # COOL mode
        if _mode == 1:
            return HVACAction.COOLING

        # HEAT mode
        if _mode == 2:
            return HVACAction.HEATING

        # Unknown mode with compressor running - nothing better to report
        return HVACAction.IDLE
