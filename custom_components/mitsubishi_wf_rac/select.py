"""for select component used for horizontal swing."""
# pylint: disable = too-few-public-methods

from dataclasses import replace
import logging

from pywfrac import AIRFLOW_UNKNOWN, AirconCommands, HomeLeaveModeSetting

from homeassistant.components.climate.const import HVACMode
from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MitsubishiWfRacConfigEntry
from .const import (
    DOMAIN,
    FAN_MODE_TRANSLATION,
    HOME_LEAVE_TEMP_COOL,
    HOME_LEAVE_TEMP_HEAT,
    HVAC_TRANSLATION,
    NORMAL_TEMP,
    SUPPORT_SWING_HORIZONTAL_MODES,
    SUPPORT_SWING_MODES,
    SUPPORTED_FAN_MODES,
    SWING_3D_AUTO,
    SWING_HORIZONTAL_MODE_TRANSLATION,
    SWING_MODE_TRANSLATION,
)
from .coordinator import Device
from .entity import WfRacEntity

_LOGGER = logging.getLogger(__name__)
# Zero although this platform writes: the coordinator already serialises and
# spaces every request.
PARALLEL_UPDATES = 0

HOME_LEAVE_MODE_OFF = "off"
HOME_LEAVE_MODE_AWAY_COOL = "away_cool"
HOME_LEAVE_MODE_AWAY_HEAT = "away_heat"

# The app's own 0=auto/1-4=volume index for this feature specifically - see
# HomeLeaveModeSetting.AirFlow in models/aircon.py. rac_parser.py's
# _apply_home_leave_mode() already converts the raw wire byte to this same
# 0-4 index, so the option strings below map straight onto it.
HOME_LEAVE_AIRFLOW_OPTIONS = ["auto", "1", "2", "3", "4"]


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: MitsubishiWfRacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select entries."""

    device: Device = entry.runtime_data.device
    _LOGGER.debug("Setup selects for: %s, %s", device.device_name, device.airco_id)
    entities = [
        HorizontalSwingSelect(device),
        VerticalSwingSelect(device),
        FanSpeedSelect(device),
    ]

    # Same VacantProperty capability gate as OccupancyBinarySensor in
    # binary_sensor.py.
    if device.airco.Capabilities.vacant_property:
        entities.append(HomeLeaveModeSelect(device))

    # Same HomeLeaveMode capability gate as the diagnostic sensors removed by
    # _async_remove_home_leave_mode_sensors() in sensor.py.
    if device.airco.Capabilities.home_leave_mode:
        entities.append(HomeLeaveAirFlowSelect(device, "cooling"))
        entities.append(HomeLeaveAirFlowSelect(device, "heating"))

    async_add_entities(entities)


# HACS only: the climate entity already exposes horizontal swing, vertical
# swing and fan speed. These three are a second, flatter control surface for
# dashboards; core takes the climate entity alone.
class HorizontalSwingSelect(WfRacEntity, SelectEntity):
    """Select component to set the horizontal swing direction of the airco."""

    _attr_translation_key = "horizontal_swing"

    def __init__(self, device: Device) -> None:
        """Initialize the horizontal swing select."""
        super().__init__(device)
        self._attr_entity_registry_enabled_default = (
            device.swing_selects_enabled_default
        )
        self._attr_options = SUPPORT_SWING_HORIZONTAL_MODES
        self._attr_unique_id = (
            f"{DOMAIN}-{self.coordinator.airco_id}-horizontal-swing-direction"
        )
        self._apply_state()

    def _mark_state_unknown(self) -> None:
        self._attr_current_option = None

    def _update_state(self) -> None:
        self._attr_current_option = (
            SWING_3D_AUTO
            if self.coordinator.airco.Entrust
            else list(SWING_HORIZONTAL_MODE_TRANSLATION.keys())[
                self.coordinator.airco.WindDirectionLR
            ]
        )

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        _swing_auto = option == SWING_3D_AUTO
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
                        option
                    ],
                    AirconCommands.Entrust: False,
                }
            )
        self._attr_current_option = option


class VerticalSwingSelect(WfRacEntity, SelectEntity):
    """Select component to set the vertical swing direction of the airco."""

    _attr_translation_key = "vertical_swing"

    def __init__(self, device: Device) -> None:
        """Initialize the vertical swing select."""
        super().__init__(device)
        self._attr_entity_registry_enabled_default = (
            device.swing_selects_enabled_default
        )
        self._attr_options = SUPPORT_SWING_MODES
        self._attr_unique_id = (
            f"{DOMAIN}-{self.coordinator.airco_id}-vertical-swing-direction"
        )
        self._apply_state()

    def _mark_state_unknown(self) -> None:
        self._attr_current_option = None

    def _update_state(self) -> None:
        self._attr_current_option = (
            SWING_3D_AUTO
            if self.coordinator.airco.Entrust
            else list(SWING_MODE_TRANSLATION.keys())[
                self.coordinator.airco.WindDirectionUD
            ]
        )

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        _swing_auto = option == SWING_3D_AUTO
        if _swing_auto:
            await self.coordinator.async_queue_command(
                {
                    AirconCommands.Entrust: _swing_auto,
                }
            )
        else:
            await self.coordinator.async_queue_command(
                {
                    AirconCommands.WindDirectionUD: SWING_MODE_TRANSLATION[option],
                    AirconCommands.Entrust: False,
                }
            )
        self._attr_current_option = option


class FanSpeedSelect(WfRacEntity, SelectEntity):
    """Select component to set the fan speed of the airco."""

    _attr_translation_key = "fan_speed"

    def __init__(self, device: Device) -> None:
        """Initialize the fan speed select."""
        super().__init__(device)
        self._attr_entity_registry_enabled_default = (
            device.swing_selects_enabled_default
        )
        self._attr_options = SUPPORTED_FAN_MODES
        self._attr_unique_id = f"{DOMAIN}-{self.coordinator.airco_id}-fan-speed"
        self._apply_state()

    def _mark_state_unknown(self) -> None:
        self._attr_current_option = None

    def _update_state(self) -> None:
        # Same marker check as the climate entity's fan mode: the library
        # reports an unreadable fan step by name, and a sixth option here
        # would otherwise make it look like a real one.
        if self.coordinator.airco.AirFlow == AIRFLOW_UNKNOWN:
            raise IndexError("the unit reported a fan step pywfrac cannot read")
        self._attr_current_option = list(FAN_MODE_TRANSLATION.keys())[
            self.coordinator.airco.AirFlow
        ]

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        await self.coordinator.async_queue_command(
            {AirconCommands.AirFlow: FAN_MODE_TRANSLATION[option]}
        )
        self._attr_current_option = option


class HomeLeaveModeSelect(WfRacEntity, SelectEntity):
    """Select to enter or leave the unit's own Home Leave (vacant property) mode.

    The official app's away mode has two independent target points (Heat and
    Cool, each with its own Tag-248 threshold/setting - see the home_leave_*
    diagnostic sensors in sensor.py) and flips the same Vacant bit this
    entity reads/writes. See HOME_LEAVE_TEMP_HEAT/_COOL in const.py for the values
    used and why they're hardcoded rather than read from the live Tag-248
    TempSetting.
    """

    _attr_translation_key = "home_leave_mode"

    def __init__(self, device: Device) -> None:
        """Initialize the Home Leave mode select."""
        super().__init__(device)
        self._attr_options = [
            HOME_LEAVE_MODE_OFF,
            HOME_LEAVE_MODE_AWAY_COOL,
            HOME_LEAVE_MODE_AWAY_HEAT,
        ]
        self._attr_unique_id = f"{DOMAIN}-{self.coordinator.airco_id}-home-leave-mode"
        self._apply_state()

    def _mark_state_unknown(self) -> None:
        self._attr_current_option = None

    def _update_state(self) -> None:
        airco = self.coordinator.airco
        if not airco.Vacant:
            self._attr_current_option = HOME_LEAVE_MODE_OFF
            return
        mode_from_operation = list(HVAC_TRANSLATION.keys())[airco.OperationMode]
        if mode_from_operation == HVACMode.COOL:
            self._attr_current_option = HOME_LEAVE_MODE_AWAY_COOL
        elif mode_from_operation == HVACMode.HEAT:
            self._attr_current_option = HOME_LEAVE_MODE_AWAY_HEAT
        else:
            # Vacant set while running in some other mode than the two the
            # away feature itself uses - shouldn't happen, but "off" is a
            # safer fallback than silently claiming a direction that isn't
            # actually active.
            self._attr_current_option = HOME_LEAVE_MODE_OFF

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        if option == HOME_LEAVE_MODE_AWAY_HEAT:
            await self.coordinator.async_queue_command(
                {
                    AirconCommands.Operation: True,
                    AirconCommands.OperationMode: HVAC_TRANSLATION[HVACMode.HEAT],
                    AirconCommands.PresetTemp: HOME_LEAVE_TEMP_HEAT,
                }
            )
        elif option == HOME_LEAVE_MODE_AWAY_COOL:
            await self.coordinator.async_queue_command(
                {
                    AirconCommands.Operation: True,
                    AirconCommands.OperationMode: HVAC_TRANSLATION[HVACMode.COOL],
                    AirconCommands.PresetTemp: HOME_LEAVE_TEMP_COOL,
                }
            )
        else:
            await self.coordinator.async_queue_command(
                {
                    AirconCommands.PresetTemp: NORMAL_TEMP,
                }
            )
        self._attr_current_option = option


class HomeLeaveAirFlowSelect(WfRacEntity, SelectEntity):
    """Editable Home Leave Mode airflow level (Tag 248) for one direction.

    Stays unknown until Device.async_request_home_leave_mode_status() has been
    called at least once, same as the HomeLeaveModeNumber entities in
    number.py - see that class for why writing before that is refused rather
    than guessed at.
    """

    # A niche away-mode feature: not everyone with a HomeLeaveMode-capable
    # model wants extra entities on their device page.
    _attr_entity_registry_enabled_default = False

    def __init__(self, device: Device, mode: str) -> None:
        """Initialize the Home Leave air flow select for one mode."""
        super().__init__(device)
        self._mode = mode
        self._attr_translation_key = f"home_leave_{mode}_air_flow"
        self._attr_options = HOME_LEAVE_AIRFLOW_OPTIONS
        self._attr_unique_id = (
            f"{DOMAIN}-{self.coordinator.airco_id}-home-leave-{mode}-air-flow-select"
        )
        self._apply_state()

    def _current_setting(self) -> HomeLeaveModeSetting | None:
        return (
            self.coordinator.airco.HomeLeaveModeForCooling
            if self._mode == "cooling"
            else self.coordinator.airco.HomeLeaveModeForHeating
        )

    def _mark_state_unknown(self) -> None:
        self._attr_current_option = None

    def _update_state(self) -> None:
        setting = self._current_setting()
        if setting is None:
            self._attr_current_option = None
            return
        # Named rather than left to the list index, for the same reason as the
        # fan speed select: a sixth option would make the marker look like a
        # real fan step.
        if setting.AirFlow == AIRFLOW_UNKNOWN:
            raise IndexError("the unit reported a fan step pywfrac cannot read")
        self._attr_current_option = HOME_LEAVE_AIRFLOW_OPTIONS[setting.AirFlow]

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        cooling = self.coordinator.airco.HomeLeaveModeForCooling
        heating = self.coordinator.airco.HomeLeaveModeForHeating
        if cooling is None or heating is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="home_leave_mode_status_unknown",
            )
        air_flow = HOME_LEAVE_AIRFLOW_OPTIONS.index(option)
        if self._mode == "cooling":
            cooling = replace(cooling, AirFlow=air_flow)
        else:
            heating = replace(heating, AirFlow=air_flow)
        await self.coordinator.async_set_home_leave_mode(cooling, heating)
        self._attr_current_option = option
        self.async_write_ha_state()
