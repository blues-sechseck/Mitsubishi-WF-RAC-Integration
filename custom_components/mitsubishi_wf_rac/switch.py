"""for switch integration."""
# pylint: disable = too-few-public-methods

from __future__ import annotations
import logging

from homeassistant.components.climate.const import HVACMode
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers import entity_registry as er

from . import MitsubishiWfRacConfigEntry
from .entity import WfRacEntity
from .wfrac.device import Device
from .wfrac.models.aircon import AirconCommands
from .const import DOMAIN, HVAC_TRANSLATION

_LOGGER = logging.getLogger(__name__)

# Confirmed against real hardware (see #67, #187): "Vacant"/Home Leave mode is
# not a directly settable flag - the unit derives it itself from the heat
# target temperature, entering it below this threshold and leaving it above.
# Writing the raw Vacant bit in a command has no effect on its own.
HOME_LEAVE_TEMP = 10.0
# Temperature to restore when leaving Home Leave mode. There's no reliable way
# to recall whatever temperature was set before Home Leave was turned on (the
# unit itself doesn't report it), so this is a plain, reasonable default.
NORMAL_TEMP = 21.0


async def async_setup_entry(hass, entry: MitsubishiWfRacConfigEntry, async_add_entities):
    """Setup switch entries"""

    device: Device = entry.runtime_data.device

    entities: list[SwitchEntity] = []

    _async_remove_self_clean_switch(hass, device)

    # Home Leave mode only confirmed to work on ModelNr 1 units so far - see
    # rac_parser.py's own ModelNr-gated handling of this same Vacant bit.
    if device.airco.ModelNr == 1:
        entities.append(HomeLeaveModeSwitch(device))
    else:
        _LOGGER.debug(
            "Home Leave mode not confirmed for model %s (%s)",
            device.airco.ModelNr,
            device.device_name,
        )

    async_add_entities(entities)


def _async_remove_self_clean_switch(hass, device: Device) -> None:
    """Drop the former Self Clean switch from the entity registry.

    The unit's real self-clean cycle can only be started locally via the IR
    remote - the WiFi module offers no way to trigger it (see #209), so the
    switch never did anything. Removing it here keeps it from lingering as an
    unavailable leftover in dashboards and automations.
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "switch", DOMAIN, f"{DOMAIN}-{device.airco_id}-self-clean"
    )
    if entity_id:
        _LOGGER.debug("Removing obsolete self clean switch %s", entity_id)
        registry.async_remove(entity_id)


class HomeLeaveModeSwitch(WfRacEntity, SwitchEntity):
    """Switch to enter/leave the unit's own Home Leave (vacant property) mode.

    Enabling this lowers the heat target temperature below the unit's own
    Home Leave threshold (~16-18°C, observed as 10°C once active), which the
    unit then reports as "Vacant" - this is a frost-protection/low-power
    standby mode intended for when nobody's home, distinct from just turning
    the unit off. Disabling it raises the temperature back to a normal value.
    """

    _attr_translation_key = "home_leave_mode"
    _attr_has_entity_name: bool = True
    _attr_icon = "mdi:home-export-outline"

    def __init__(self, device: Device) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{DOMAIN}-{self._device.airco_id}-home-leave-mode"
        self._update_state()

    def _update_state(self) -> None:
        self._attr_is_on = self._device.airco.Vacant

    async def async_turn_on(self, **kwargs) -> None:
        """Enter Home Leave mode."""
        await self._device.async_queue_command(
            {
                AirconCommands.Operation: True,
                AirconCommands.OperationMode: HVAC_TRANSLATION[HVACMode.HEAT],
                AirconCommands.PresetTemp: HOME_LEAVE_TEMP,
            }
        )
        self._attr_is_on = True

    async def async_turn_off(self, **kwargs) -> None:
        """Leave Home Leave mode by restoring a normal target temperature."""
        await self._device.async_queue_command(
            {
                AirconCommands.PresetTemp: NORMAL_TEMP,
            }
        )
        self._attr_is_on = False
