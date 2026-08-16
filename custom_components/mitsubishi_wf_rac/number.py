"""for number component used for the Home Leave Mode temperature thresholds."""
# pylint: disable = too-few-public-methods

from __future__ import annotations
from dataclasses import replace
import logging

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MitsubishiWfRacConfigEntry
from .entity import WfRacEntity
from .wfrac.device import Device
from .wfrac.models.aircon import HomeLeaveModeSetting
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1

# Same bounds as the temp_rule_*/temp_setting_* fields in services.yaml's
# set_home_leave_mode action.
HOME_LEAVE_TEMP_MIN = 10.0
HOME_LEAVE_TEMP_MAX = 50.0
HOME_LEAVE_TEMP_STEP = 0.5


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: MitsubishiWfRacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Setup number entries"""

    device: Device = entry.runtime_data.device

    entities: list[NumberEntity] = []
    if device.airco.Capabilities.home_leave_mode:
        for mode in ("cooling", "heating"):
            entities.append(HomeLeaveModeNumber(device, mode, "TempRule"))
            entities.append(HomeLeaveModeNumber(device, mode, "TempSetting"))

    async_add_entities(entities)


class HomeLeaveModeNumber(WfRacEntity, NumberEntity):
    """Editable Home Leave Mode temperature threshold/setting (Tag 248).

    Replaces the former read-only home_leave_* diagnostic sensors in
    sensor.py - same values, but directly writable from the device's
    Controls section instead of only via the set_home_leave_mode action.

    Stays unavailable until Device.async_request_home_leave_mode_status()
    has been called at least once (see the climate entity's "Request Home
    Leave Mode status" action) - the unit omits the Tag-248 extension segment
    from a plain poll otherwise, see rac_parser.py. Writing before that has
    happened would mean guessing at the other five values instead of
    preserving them, so it's refused rather than risking silently
    overwriting real settings with defaults.
    """

    # None (not DIAGNOSTIC) so these land in the device page's main Controls
    # section, directly editable - the whole point of replacing the former
    # diagnostic sensors. Disabled by default though, same as those sensors
    # were: a niche away-mode feature, not everyone with a HomeLeaveMode-
    # capable model wants six extra entities on their device page.
    _attr_entity_category = None
    _attr_entity_registry_enabled_default = False
    _attr_has_entity_name: bool = True
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = HOME_LEAVE_TEMP_MIN
    _attr_native_max_value = HOME_LEAVE_TEMP_MAX
    _attr_native_step = HOME_LEAVE_TEMP_STEP
    _attr_mode = NumberMode.BOX

    def __init__(self, device: Device, mode: str, attribute: str) -> None:
        """Initialize the number. mode is 'cooling'/'heating', attribute is
        'TempRule' or 'TempSetting'."""
        super().__init__(device)
        self._mode = mode
        self._attribute = attribute
        slug = "temp_rule" if attribute == "TempRule" else "temp_setting"
        self._attr_translation_key = f"home_leave_{mode}_{slug}"
        self._attr_unique_id = (
            f"{DOMAIN}-{self._device.airco_id}-home-leave-{mode}-{slug}-number"
        )
        self._update_state()

    def _current_setting(self) -> HomeLeaveModeSetting | None:
        return (
            self._device.airco.HomeLeaveModeForCooling
            if self._mode == "cooling"
            else self._device.airco.HomeLeaveModeForHeating
        )

    def _update_state(self) -> None:
        # WfRacEntity.available reflects device connectivity, not per-value
        # readiness - a not-yet-requested Home Leave value just reads as
        # "unknown" (native_value None), same as the sensor it replaced.
        setting = self._current_setting()
        self._attr_native_value = (
            getattr(setting, self._attribute) if setting is not None else None
        )

    async def async_set_native_value(self, value: float) -> None:
        """Change the value."""
        cooling = self._device.airco.HomeLeaveModeForCooling
        heating = self._device.airco.HomeLeaveModeForHeating
        if cooling is None or heating is None:
            raise HomeAssistantError(
                "Home Leave Mode values are unknown yet - call the climate "
                "entity's 'Request Home Leave Mode status' action once "
                "first, the unit doesn't include them in a plain poll.",
                translation_domain=DOMAIN,
                translation_key="home_leave_mode_status_unknown",
            )
        # Named kwargs rather than replace(setting, **{self._attribute: value}):
        # HomeLeaveModeSetting also has an AirFlow: int field, so a dynamic
        # **{str: float} unpacking can't statically prove it only ever
        # touches the two float fields this class is instantiated for.
        if self._attribute == "TempRule":
            if self._mode == "cooling":
                cooling = replace(cooling, TempRule=value)
            else:
                heating = replace(heating, TempRule=value)
        else:
            if self._mode == "cooling":
                cooling = replace(cooling, TempSetting=value)
            else:
                heating = replace(heating, TempSetting=value)
        await self._device.async_set_home_leave_mode(cooling, heating)
        self._attr_native_value = value
        self.async_write_ha_state()
