"""for Climate integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from pywfrac import AIRFLOW_UNKNOWN, Aircon, AirconCommands, HomeLeaveModeSetting
from pywfrac.parser import EXTERNAL_TEMPERATURE_MAX, EXTERNAL_TEMPERATURE_MIN

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    FAN_AUTO,
    PRESET_AWAY,
    PRESET_NONE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.util.unit_conversion import TemperatureConverter

from . import MitsubishiWfRacConfigEntry
from .const import (
    CONF_EXTERNAL_TEMPERATURE_SOURCE,
    CONF_INDOOR_OFFSET,
    DOMAIN,
    FAN_MODE_TRANSLATION,
    HOME_LEAVE_TEMP_COOL,
    HOME_LEAVE_TEMP_HEAT,
    HVAC_TRANSLATION,
    NORMAL_TEMP,
    SUPPORT_FLAGS,
    SUPPORT_SWING_HORIZONTAL_MODES,
    SUPPORT_SWING_MODES,
    SUPPORTED_FAN_MODES,
    SUPPORTED_HVAC_MODES,
    SWING_3D_AUTO,
    SWING_HORIZONTAL_AUTO,
    SWING_HORIZONTAL_MODE_TRANSLATION,
    SWING_MODE_TRANSLATION,
    SWING_VERTICAL_AUTO,
)
from .coordinator import Device
from .entity import WfRacEntity

_LOGGER = logging.getLogger(__name__)
# Zero although this platform writes: the coordinator already serialises and
# spaces every request.
PARALLEL_UPDATES = 0

# The modes whose setpoint the unit actually regulates on. Off and fan-only
# have no setpoint of their own - see _setpoint_range_for_mode.
REGULATING_HVAC_MODES = (HVACMode.AUTO, HVACMode.COOL, HVACMode.HEAT, HVACMode.DRY)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MitsubishiWfRacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the climate entity."""
    device: Device = entry.runtime_data.device
    _LOGGER.debug("Setup climate for: %s, %s", device.device_name, device.airco_id)
    async_add_entities([AircoClimate(device)])


@dataclass
class ExternalTemperatureOverrideData(ExtraStoredData):
    """The armed external temperature override, stored next to the entity state.

    Deliberately not carried by a state attribute: attributes are only merged
    into a state while the entity is available, and this module goes
    unreachable for about a minute every hour, so an override armed before that
    gap would quietly disappear from the restored state.
    """

    external_temperature_override: float | None
    # Whether a configured source put it there, rather than the action. Only
    # the latter is a standing intent worth restoring: a value a source left
    # behind belongs to a source that may since have been removed, and
    # re-arming it would leave the unit regulating on a reading nobody updates.
    #
    # Recorded when the value is armed, not when it is saved. Saving happens
    # while the entity is being removed, and removing the source is what
    # reloads the entry (OptionsFlowWithReload) - so by then the options no
    # longer name a source, and asking them would write False for exactly the
    # value this flag exists to catch.
    from_source: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the restore-state store."""
        return {
            "external_temperature_override": self.external_temperature_override,
            "from_source": self.from_source,
        }


def _without_3d_auto(modes: list[str]) -> list[str]:
    """The mode list a unit gets when it cannot hand both vanes to the unit."""
    return [mode for mode in modes if mode != SWING_3D_AUTO]


def _stored_external_temperature_is_set(stored: ExtraStoredData | None) -> bool:
    """Whether anything was armed when this entity was last taken down.

    Only its presence is asked for: what the value was does not matter to the
    caller, which is deciding whether the unit may still be holding one.
    """
    if stored is None:
        return False
    return stored.as_dict().get("external_temperature_override") is not None


class AircoClimate(WfRacEntity, ClimateEntity, RestoreEntity):
    """Representation of a climate entity."""

    _external_temperature_override: float | None = None
    # Provenance of the value above, kept for extra_restore_state_data - see
    # ExternalTemperatureOverrideData.from_source.
    _external_temperature_from_source: bool = False

    _attr_supported_features: ClimateEntityFeature = SUPPORT_FLAGS
    _attr_temperature_unit: str = UnitOfTemperature.CELSIUS
    _attr_hvac_modes: list[HVACMode] = SUPPORTED_HVAC_MODES
    _attr_fan_modes: list[str] = SUPPORTED_FAN_MODES
    _attr_fan_mode: str | None = FAN_AUTO
    _attr_swing_mode: str | None = SWING_VERTICAL_AUTO
    _attr_swing_modes: list[str] | None = SUPPORT_SWING_MODES
    _attr_swing_horizontal_mode: str | None = SWING_HORIZONTAL_AUTO
    _attr_swing_horizontal_modes: list[str] | None = SUPPORT_SWING_HORIZONTAL_MODES
    # 0.5 K is what the wire format carries: the setpoint byte is
    # int(PresetTemp / 0.5), which truncates. Without declaring the step, HA
    # offers 0.1 K and the unit silently drops the remainder - 21.4 arrives as
    # 21.0. A target_offset that isn't a multiple of 0.5 still shifts the
    # displayed target off the grid; that is the offset's job, and it stays
    # visible rather than the UI promising a resolution the device lacks.
    _attr_target_temperature_step: float = 0.5
    _attr_preset_modes: list[str] | None = None
    _attr_preset_mode: str | None = None
    _attr_translation_key = "mitsubishi_wf_rac"
    # The unit itself: it carries the device's name and adds nothing of its own.
    _attr_name: str | None = None

    def __init__(self, device: Device) -> None:
        """Initialize the climate entity."""
        super().__init__(device)
        self._attr_unique_id = f"{DOMAIN}-{self.coordinator.airco_id}-climate"
        capabilities = device.airco.Capabilities
        features = SUPPORT_FLAGS
        # HomeLeaveModeSelect in select.py stays: it can name the direction
        # (away_cool/away_heat), which a single preset cannot, and it is what
        # existing automations target. Same capability gate as that select.
        if capabilities.vacant_property:
            features |= ClimateEntityFeature.PRESET_MODE
            self._attr_preset_modes = [PRESET_NONE, PRESET_AWAY]
        # The left/right vane and 3D auto belong to the model line: the
        # manufacturer's table has both off for the ceiling cassettes, which
        # have no horizontal vane to aim.
        if capabilities.wind_direction_lr:
            features |= ClimateEntityFeature.SWING_HORIZONTAL_MODE
        else:
            self._attr_swing_horizontal_mode = None
            self._attr_swing_horizontal_modes = None
        if not capabilities.entrust:
            self._attr_swing_modes = _without_3d_auto(SUPPORT_SWING_MODES)
            if self._attr_swing_horizontal_modes is not None:
                self._attr_swing_horizontal_modes = _without_3d_auto(
                    SUPPORT_SWING_HORIZONTAL_MODES
                )
        self._attr_supported_features = features
        self._apply_state()

    async def async_added_to_hass(self) -> None:
        """Register with the coordinator and publish the first state."""
        await super().async_added_to_hass()
        source = self._external_temperature_source
        stored = await self.async_get_last_extra_data()
        if source is None:
            self._restore_external_temperature_override(stored)
        else:
            # A source's current state is more authoritative than restore data:
            # the latter can be stale precisely when the source stopped reporting.
            self._set_external_temperature_from_source_state(
                self.hass.states.get(source)
            )
            if (
                self._external_temperature_override is None
                and _stored_external_temperature_is_set(stored)
            ):
                # Something was armed before this reload and the source has
                # nothing usable to put in its place. The frames that fed the
                # unit went out in the previous life of the coordinator, so it
                # cannot know the unit is still holding that value - only this
                # entity's restored state says so.
                self.coordinator.request_external_temperature_release()
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, source, self._handle_external_temperature_source_change
                )
            )
        # No async_write_ha_state() here: the platform writes the state itself
        # once this returns.
        self._apply_state()

    @property
    def _external_temperature_source(self) -> str | None:
        """Return the configured source, treating a legacy blank as unset."""
        source = self.coordinator.options.get(CONF_EXTERNAL_TEMPERATURE_SOURCE)
        return source if isinstance(source, str) and source else None

    def _external_temperature_from_source_state(
        self, state: State | None
    ) -> float | None:
        """Convert a usable source state to the quarter-degree protocol grid."""
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            value = TemperatureConverter.convert(
                float(state.state),
                state.attributes.get(
                    ATTR_UNIT_OF_MEASUREMENT, UnitOfTemperature.CELSIUS
                ),
                UnitOfTemperature.CELSIUS,
            )
        except (TypeError, ValueError):
            # Returning None clears the override rather than holding the last
            # good value: a source producing garbage is a source that is not
            # measuring the room, which is the same situation as unavailable.
            _LOGGER.warning(
                "Clearing the external temperature override: source state %s is unusable",
                state.state,
            )
            return None

        # Compare protocol values, not raw sensor precision: otherwise tiny
        # changes which encode identically would keep waking the write carrier.
        value = round(value * 4) / 4
        if not EXTERNAL_TEMPERATURE_MIN <= value <= EXTERNAL_TEMPERATURE_MAX:
            _LOGGER.warning(
                "Clearing the external temperature override: source value %s °C is "
                "outside the encodable range of %s..%s °C",
                value,
                EXTERNAL_TEMPERATURE_MIN,
                EXTERNAL_TEMPERATURE_MAX,
            )
            return None
        return value

    def _set_external_temperature_override(
        self, temperature: float | None, *, from_source: bool = False
    ) -> None:
        """Arm an override and immediately publish its integration-side state."""
        self._external_temperature_override = temperature
        self._external_temperature_from_source = from_source and temperature is not None
        self.coordinator.set_external_temperature_override(temperature)
        self._apply_state()
        self.async_write_ha_state()

    def _set_external_temperature_from_source_state(self, state: State | None) -> None:
        """Apply a source state only when its encoded value changes."""
        temperature = self._external_temperature_from_source_state(state)
        if temperature == self._external_temperature_override:
            return
        self._set_external_temperature_override(temperature, from_source=True)

    @callback
    def _handle_external_temperature_source_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Keep the override tied to the source's availability and value."""
        self._set_external_temperature_from_source_state(event.data["new_state"])

    def _restore_external_temperature_override(
        self, stored: ExtraStoredData | None
    ) -> None:
        """Re-arm the override recorded before the last restart or reload.

        Restoring only arms it integration-side - the unit is not told anything
        here. The next outgoing frame carries the value, and until one has, the
        override counts as unapplied (Device.external_temperature_applied).
        """
        if stored is None:
            return
        as_dict = stored.as_dict()
        restored = as_dict.get("external_temperature_override")
        if restored is None:
            return
        if as_dict.get("from_source"):
            # Reached only when no source is configured now, so the source that
            # armed this is gone - taking it out of the options is how a user
            # hands control back to the unit, and re-arming here would ignore
            # that (#218).
            _LOGGER.debug(
                "Dropping the restored external temperature override: it came "
                "from a source entity that is no longer configured"
            )
            # Dropping it here only stops us sending it again. The unit was
            # last told that value and holds it until a frame says otherwise,
            # so the release has to be asked for.
            self.coordinator.request_external_temperature_release()
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
        self.coordinator.set_external_temperature_override(value)

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """What async_added_to_hass() reads back after a restart or reload."""
        return ExternalTemperatureOverrideData(
            self._external_temperature_override,
            self._external_temperature_from_source,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the armed override, visible without calling the action.

        Display only - the value that survives a restart is the one in
        extra_restore_state_data above.
        """
        attrs = dict(super().extra_state_attributes or {})
        if self._external_temperature_override is not None:
            attrs["external_temperature_override"] = self._external_temperature_override
        return attrs

    def _min_temp_for_mode(self, hvac_mode: HVACMode) -> float:
        """Minimum setpoint depends on hvac_mode.

        The manufacturer's operable table ('21 SRK-T-324) gives 18-30C
        throughout, but cooling goes lower on every model. PresetTempRange2
        models go further per the app's own table: Auto/Cool/Dry to 16, Heat
        to 10 - that heating floor is unconfirmed on hardware.
        """
        if self.coordinator.airco.Capabilities.preset_temp_range_2:
            if hvac_mode == HVACMode.HEAT:
                return 10
            if hvac_mode in (HVACMode.COOL, HVACMode.DRY, HVACMode.AUTO):
                return 16
        return 16 if hvac_mode == HVACMode.COOL else 18

    def _max_temp_for_mode(self, hvac_mode: HVACMode) -> float:
        """Return the highest setpoint this hvac_mode allows.

        Depends on hvac_mode for PresetTempRange2 models - see
        _min_temp_for_mode.
        """
        if self.coordinator.airco.Capabilities.preset_temp_range_2 and hvac_mode in (
            HVACMode.COOL,
            HVACMode.DRY,
        ):
            return 33
        return 30

    def _setpoint_range_for_mode(
        self, hvac_mode: HVACMode | None
    ) -> tuple[float, float]:
        """The range a setpoint is held to before it is sent.

        A regulating mode is held to its own range; off, fan-only and an
        unreadable mode get the union, since the value applies to whichever
        mode comes next. The union is where the advertised range starts too,
        because climate validates against it before this entity sees hvac_mode.
        """
        if hvac_mode in REGULATING_HVAC_MODES:
            return (
                self._min_temp_for_mode(hvac_mode),
                self._max_temp_for_mode(hvac_mode),
            )
        return (
            min(self._min_temp_for_mode(mode) for mode in REGULATING_HVAC_MODES),
            max(self._max_temp_for_mode(mode) for mode in REGULATING_HVAC_MODES),
        )

    def _writing_mode(self, hvac_mode: HVACMode | None) -> HVACMode:
        """The mode a setpoint for this hvac_mode is actually written in.

        A call naming a regulating mode switches to it. Off leaves the unit on
        the mode it keeps underneath, and that is what the value lands in and
        what _update_state() reads it back with. That underlying mode can be
        fan-only itself, which has no setpoint range of its own.
        """
        if hvac_mode in REGULATING_HVAC_MODES:
            return hvac_mode
        return self._hvac_mode_from_operation

    def _offset_for_target(self, hvac_mode: HVACMode | None) -> float:
        """The target offset a setpoint for this mode is written with."""
        return self._resolve_target_offset(self._writing_mode(hvac_mode))

    def _displayed_setpoint_range(
        self, hvac_mode: HVACMode | None
    ) -> tuple[float, float]:
        """The setpoint range in the numbers the card shows.

        The device is held to _setpoint_range_for_mode(); what the user sets
        and reads back is that value plus the target offset, so the bounds have
        to move with it, or a setpoint at the edge passes validation and comes
        back one offset away from what was asked for.

        For a mode with no range of its own the shift happens per mode and the
        union comes after, because each mode carries its own offset. Home
        Assistant reads min_temp/max_temp and rejects on its own before the
        entity sees the call, so a union shifted by one mode's offset would
        reject a setpoint that is legal in the mode the call switches to, with
        no second chance to correct it.
        """
        if hvac_mode in REGULATING_HVAC_MODES:
            offset = self._resolve_target_offset(hvac_mode)
            return (
                self._min_temp_for_mode(hvac_mode) + offset,
                self._max_temp_for_mode(hvac_mode) + offset,
            )
        shifted = [
            (
                self._min_temp_for_mode(mode) + self._resolve_target_offset(mode),
                self._max_temp_for_mode(mode) + self._resolve_target_offset(mode),
            )
            for mode in REGULATING_HVAC_MODES
        ]
        return min(low for low, _ in shifted), max(high for _, high in shifted)

    def _advertised_range(self) -> tuple[float, float]:
        """The range HA validates against, and the one the slider offers.

        Wider than that union wherever the away preset is offered: Home Leave
        runs at 31C cooling and 10C heating, which the unit reports back as
        its target temperature. They stay reachable through the preset alone -
        a setpoint sent by hand is still held to its mode's range. Both carry
        that mode's target offset, since that is the number the card shows.
        """
        min_temp, max_temp = self._displayed_setpoint_range(None)
        if self._attr_preset_modes:
            return (
                min(
                    min_temp,
                    HOME_LEAVE_TEMP_HEAT + self._resolve_target_offset(HVACMode.HEAT),
                ),
                max(
                    max_temp,
                    HOME_LEAVE_TEMP_COOL + self._resolve_target_offset(HVACMode.COOL),
                ),
            )
        return (min_temp, max_temp)

    @property
    def min_temp(self) -> float:
        """Return the lowest setpoint any of this unit's modes allows."""
        return self._advertised_range()[0]

    @property
    def max_temp(self) -> float:
        """Return the highest setpoint any of this unit's modes allows."""
        return self._advertised_range()[1]

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        set_temp = kwargs[ATTR_TEMPERATURE]

        # The service schema coerces hvac_mode to the enum without checking
        # it against the modes this entity offers.
        requested_hvac_mode: HVACMode | None = kwargs.get(ATTR_HVAC_MODE)
        if requested_hvac_mode is not None:
            self._valid_mode_or_raise("hvac", requested_hvac_mode, self.hvac_modes)

        target_hvac_mode = (
            requested_hvac_mode
            if requested_hvac_mode is not None
            else self._attr_hvac_mode
        )
        target_hvac_mode = (
            HVACMode.OFF if target_hvac_mode is None else target_hvac_mode
        )
        min_temp, max_temp = self._displayed_setpoint_range(target_hvac_mode)

        # Naming the mode is the whole message: the range depends on it, and
        # an automation that sets a setpoint before switching mode gets
        # measured against the mode it is leaving. Saying so - and that
        # hvac_mode belongs in the same call - is the difference between a
        # rejection and a fix.
        if set_temp < min_temp:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="temperature_below_minimum",
                translation_placeholders={
                    "temperature": str(set_temp),
                    "min_temp": str(min_temp),
                    "hvac_mode": str(target_hvac_mode),
                },
            )

        if set_temp > max_temp:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="temperature_above_maximum",
                translation_placeholders={
                    "temperature": str(set_temp),
                    "max_temp": str(max_temp),
                    "hvac_mode": str(target_hvac_mode),
                },
            )

        # The unit regulates against its own return-air reading, which is
        # biased against the room. The target offset compensates that on the
        # wire while the displayed target_temperature stays what was asked
        # for - CONF_INDOOR_OFFSET is a separate, display-only correction and
        # is not what is subtracted here. Resolved against the mode the value
        # lands in, since cooling and heating have opposite-sign bias.
        target_temp = set_temp - self._offset_for_target(target_hvac_mode)
        # Cannot bite when the call names a regulating mode: the bounds checked
        # above are then that mode's own, shifted by the same offset just
        # subtracted. It is here for off and fan-only, where the range
        # deliberately spans every regulating mode and the value can
        # land outside the range of the one it is written in.
        device_low, device_high = self._setpoint_range_for_mode(
            self._writing_mode(target_hvac_mode)
        )
        target_temp = max(device_low, min(device_high, target_temp))

        # Home Assistant validates the advertised range but not the step, so a
        # value between two halves arrives here intact - and the frame would
        # truncate it, turning 21.4 into 21.0 rather than the 21.5 it is
        # nearer to. Rounded rather than refused: the unit cannot hold it
        # either way, and an automation that has always sent tenths should not
        # start failing over it.
        opts: dict[AirconCommands, Any] = {
            AirconCommands.PresetTemp: round(target_temp * 2) / 2
        }

        if requested_hvac_mode is not None:
            opts.update(
                {
                    AirconCommands.OperationMode: self.coordinator.airco.OperationMode
                    if target_hvac_mode == HVACMode.OFF
                    else HVAC_TRANSLATION[target_hvac_mode],
                    AirconCommands.Operation: target_hvac_mode != HVACMode.OFF,
                }
            )

        await self.coordinator.async_queue_command(opts)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode."""
        await self.coordinator.async_queue_command(
            {AirconCommands.AirFlow: FAN_MODE_TRANSLATION[fan_mode]}
        )

    async def async_turn_on(self) -> None:
        """Turn the entity on."""
        await self.coordinator.async_queue_command({AirconCommands.Operation: True})

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        await self.coordinator.async_queue_command(
            {
                AirconCommands.OperationMode: self.coordinator.airco.OperationMode
                if hvac_mode == HVACMode.OFF
                else HVAC_TRANSLATION[hvac_mode],
                AirconCommands.Operation: hvac_mode != HVACMode.OFF,
            }
        )

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set new target swing operation."""
        _swing_auto = swing_mode == SWING_3D_AUTO
        if _swing_auto:
            await self.coordinator.async_queue_command(
                {
                    AirconCommands.Entrust: _swing_auto,
                }
            )
        else:
            await self.coordinator.async_queue_command(
                {
                    AirconCommands.WindDirectionUD: SWING_MODE_TRANSLATION[swing_mode],
                    AirconCommands.Entrust: False,
                }
            )

    async def async_set_swing_horizontal_mode(self, swing_horizontal_mode: str) -> None:
        """Set new target horizontal swing operation."""
        swing_mode = swing_horizontal_mode
        _swing_auto = swing_mode == SWING_3D_AUTO
        if _swing_auto:
            await self.coordinator.async_queue_command(
                {
                    AirconCommands.Entrust: _swing_auto,
                }
            )
        else:
            await self.coordinator.async_queue_command(
                {
                    AirconCommands.WindDirectionLR: SWING_HORIZONTAL_MODE_TRANSLATION[
                        swing_mode
                    ],
                    AirconCommands.Entrust: False,
                }
            )

    async def async_set_external_temperature(
        self, temperature: float | None = None
    ) -> None:
        """Arm an external room temperature override, or revert to the sensor.

        Reverting means the unit's own internal sensor; the valid range of an
        armed value is enforced by the service schema.

        Arming only: nothing is sent from here. The value rides along on the
        next frame that goes out anyway - the operation-data request once a
        cycle, or any command in between - and the same is true of clearing
        it, since a frame without an override carries 0xFF and puts the unit
        back on its own sensor. Clearing keeps the carrier up until such a
        frame has gone out (Device.set_external_temperature_override): the
        unit holds the last value it was given, so dropping the carrier the
        moment the override goes would leave nothing to tell it otherwise.

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
        diagnostic sensor does (see ExternalTemperatureFeed._sync_carrier).
        """
        if temperature is not None and self._external_temperature_source is not None:
            # Two writers would make the service result immediately temporary
            # and leave users guessing which one controls the unit.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="external_temperature_source_configured",
            )
        self._set_external_temperature_override(temperature)

    async def async_turn_off(self) -> None:
        """Turn the entity off."""
        await self.coordinator.async_queue_command({AirconCommands.Operation: False})

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Enter or leave the unit's Home Leave mode.

        The unit has no single "away" command: it enters the mode on the away
        target of the direction it is running in, so the current hvac_mode
        decides. Auto, dry and fan-only have no such target - name the
        direction through HomeLeaveModeSelect instead.
        """
        if preset_mode == PRESET_NONE:
            # Offset-corrected like every other setpoint: NORMAL_TEMP is what
            # the card should read afterwards, not what goes on the wire - sent
            # raw, _update_state() would add the offset back and leave the card
            # showing 21 plus it. Held to the device's range too, which a
            # user's own request never is: this value is ours, and an offset
            # can push it under the mode's floor. It can never land back on a
            # Home Leave setpoint, but only because the options flow caps a
            # target offset at +/-5: reaching HOME_LEAVE_TEMP_HEAT would take
            # +11, HOME_LEAVE_TEMP_COOL -10. Widening that would need a guard
            # here.
            low, high = self._setpoint_range_for_mode(
                self._writing_mode(self._attr_hvac_mode)
            )
            normal_temp = NORMAL_TEMP - self._offset_for_target(self._attr_hvac_mode)
            await self.coordinator.async_queue_command(
                {AirconCommands.PresetTemp: max(low, min(high, normal_temp))}
            )
            return

        if self._attr_hvac_mode == HVACMode.COOL:
            away_temp = HOME_LEAVE_TEMP_COOL
        elif self._attr_hvac_mode == HVACMode.HEAT:
            away_temp = HOME_LEAVE_TEMP_HEAT
        else:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="preset_away_needs_cool_or_heat",
                translation_placeholders={"hvac_mode": str(self._attr_hvac_mode)},
            )

        await self.coordinator.async_queue_command(
            {
                AirconCommands.Operation: True,
                AirconCommands.OperationMode: HVAC_TRANSLATION[self._attr_hvac_mode],
                AirconCommands.PresetTemp: away_temp,
            }
        )

    def _require_home_leave_mode_capability(self) -> None:
        if not self.coordinator.airco.Capabilities.home_leave_mode:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="home_leave_mode_not_supported",
            )

    async def async_request_home_leave_mode_status(self) -> None:
        """See Device.async_request_home_leave_mode_status.

        Verified live against the official app's own display.
        """
        self._require_home_leave_mode_capability()
        await self.coordinator.async_request_home_leave_mode_status()

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
        await self.coordinator.async_set_home_leave_mode(
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

    def _mark_state_unknown(self) -> None:
        # All of it, not just hvac_mode: _update_state() writes the setpoint
        # and the room reading before it reaches the fields that can fail, so
        # leaving the rest in place would show a freshly updated temperature
        # and a stale action next to a state the entity itself calls unknown.
        self._attr_hvac_mode = None
        self._attr_hvac_action = None
        self._attr_target_temperature = None
        self._attr_current_temperature = None
        self._attr_fan_mode = None
        self._attr_swing_mode = None
        self._attr_swing_horizontal_mode = None
        self._attr_preset_mode = None

    def _update_state(self) -> None:
        """Private update attributes."""
        airco = self.coordinator.airco

        # Apply indoor offset
        indoor_offset = self.coordinator.options.get(CONF_INDOOR_OFFSET, 0.0)
        # Both the displayed hvac_mode and the target_offset resolution need
        # the underlying cool/heat mode, so it's computed once here and shared
        # between them.
        mode_from_operation = self._hvac_mode_from_operation
        # Mirror the subtraction in async_set_temperature() so the displayed
        # target_temperature agrees with what the user set - PresetTemp itself
        # holds the offset-lowered value that was actually sent to the device.
        target_offset = self._resolve_target_offset(mode_from_operation)

        self._attr_target_temperature = airco.PresetTemp + target_offset
        # While the unit is regulating on a temperature someone supplied, that
        # value is the room and the card shows it - see
        # Device.external_temperature_room_value. What the unit reports back
        # is what it was handed, overshoot correction included, which is why
        # the Indoor Temperature sensor (still verbatim) and the card disagree
        # exactly then and only then - by the correction.
        # Otherwise the reading is the unit's, plus the calibration offset -
        # which is suspended while an override is in effect, because it
        # corrects the unit's own return-air sensor and that sensor is out of
        # the loop just then.
        room = self.coordinator.external_temperature_room_value
        if room is not None:
            self._attr_current_temperature = room
        else:
            self._attr_current_temperature = airco.IndoorTemp + (
                0.0 if self.coordinator.external_temperature_applied else indoor_offset
            )
        # Named rather than left to index past the end of the list: the library
        # says so itself when it could not read the unit's fan step, and a sixth
        # fan mode here would otherwise turn that marker into a real one and
        # lose the unknown state without a sound.
        if airco.AirFlow == AIRFLOW_UNKNOWN:
            raise IndexError("the unit reported a fan step pywfrac cannot read")
        self._attr_fan_mode = list(FAN_MODE_TRANSLATION.keys())[airco.AirFlow]
        self._attr_swing_mode = (
            SWING_3D_AUTO
            if airco.Entrust
            else list(SWING_MODE_TRANSLATION.keys())[airco.WindDirectionUD]
        )
        self._attr_swing_horizontal_mode = (
            SWING_3D_AUTO
            if airco.Entrust
            else list(SWING_HORIZONTAL_MODE_TRANSLATION.keys())[airco.WindDirectionLR]
        )
        self._attr_hvac_mode = mode_from_operation

        if airco.Operation is False:
            self._attr_hvac_mode = HVACMode.OFF
            self._attr_hvac_action = HVACAction.OFF
        else:
            self._attr_hvac_action = self._determine_hvac_action(airco)

        # Read back from the same Vacant bit HomeLeaveModeSelect uses, so the
        # two never disagree - including when the mode was entered from the
        # official app or the IR remote.
        if self.supported_features & ClimateEntityFeature.PRESET_MODE:
            self._attr_preset_mode = PRESET_AWAY if airco.Vacant else PRESET_NONE

    def _determine_hvac_action(self, airco: Aircon) -> HVACAction:
        """Determine the current HVAC action from operation mode and state.

        CoolHotJudge reflects the unit's own AUTO logic and is inverted - the
        parser reads (content[8] & 8) == 0, so a true CoolHotJudge is HEATING.
        CompressorRunning separates "on" from "running", so a satisfied
        setpoint reports IDLE.
        """
        _mode = airco.OperationMode

        if _mode == 3:
            return HVACAction.FAN

        if _mode == 4:
            return HVACAction.DRYING

        if not airco.CompressorRunning:
            return HVACAction.IDLE

        # AUTO leaves the direction to the unit, so ask it what it picked.
        if _mode == 0:
            return HVACAction.HEATING if airco.CoolHotJudge else HVACAction.COOLING

        if _mode == 1:
            return HVACAction.COOLING

        return HVACAction.HEATING
