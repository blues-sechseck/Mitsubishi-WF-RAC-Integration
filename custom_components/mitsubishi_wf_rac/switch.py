"""for switch integration."""
# pylint: disable = too-few-public-methods

from __future__ import annotations
import logging
from typing import Any

from homeassistant.components.climate.const import HVACMode
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.event import async_call_later

from . import MitsubishiWfRacConfigEntry
from .wfrac.device import Device
from .wfrac.models.aircon import AirconCommands
from .wfrac.repository import AirconApiError
from .const import DOMAIN, HVAC_TRANSLATION

_LOGGER = logging.getLogger(__name__)

# The set-command response for self clean often still reflects the previous
# (cached) state rather than the freshly toggled one - re-read the real state
# a bit later instead of trusting it immediately.
REFRESH_DELAY_SECONDS = 15

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

    # Self clean is only supported by ModelNr 1 and 2 units.
    if device.airco.ModelNr in (1, 2):
        entities.append(SelfCleanSwitch(device))
    else:
        _LOGGER.debug(
            "Self clean not supported by model %s (%s)",
            device.airco.ModelNr,
            device.device_name,
        )

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


class SelfCleanSwitch(SwitchEntity):
    """Switch to start/stop the self clean operation (experimental)."""

    _attr_icon = "mdi:shimmer"
    _attr_has_entity_name = True
    _attr_translation_key = "self_clean"

    def __init__(self, device: Device) -> None:
        """Initialize the switch."""
        self._device = device
        self._attr_device_info = device.device_info
        self._attr_unique_id = f"{DOMAIN}-{device.airco_id}-self-clean"
        self._update_state()

    def _update_state(self) -> None:
        self._attr_is_on = self._device.airco.IsSelfCleanOperation
        self._attr_available = self._device.available

    async def async_update(self):
        """Retrieve latest state."""
        self._update_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start self clean."""
        await self._set_self_clean({AirconCommands.IsSelfCleanOperation: True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop/reset self clean."""
        await self._set_self_clean(
            {
                AirconCommands.IsSelfCleanOperation: False,
                AirconCommands.IsSelfCleanReset: True,
            }
        )

    async def _set_self_clean(self, params: dict[str, Any]) -> None:
        try:
            await self._device.set_airco(params)
        except (AirconApiError, KeyError, TypeError, ValueError):
            # Already logged in Device.set_airco(); still refresh below so the
            # entity picks up self._device.available if that's what changed.
            pass
        self._update_state()
        self.async_write_ha_state()
        async_call_later(self.hass, REFRESH_DELAY_SECONDS, self._async_delayed_refresh)

    async def _async_delayed_refresh(self, _now) -> None:
        """Re-read the real (non-cached) self clean state."""
        await self._device.update()
        self._update_state()
        self.async_write_ha_state()


class HomeLeaveModeSwitch(SwitchEntity):
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
        super().__init__()
        self._device = device
        self._attr_device_info = device.device_info
        self._attr_unique_id = f"{DOMAIN}-{self._device.airco_id}-home-leave-mode"
        self._update_state()

    def _update_state(self) -> None:
        self._attr_is_on = self._device.airco.Vacant
        self._attr_available = self._device.available

    async def async_update(self):
        """Retrieve latest state."""
        self._update_state()

    async def async_turn_on(self, **kwargs) -> None:
        """Enter Home Leave mode."""
        await self._device.set_airco(
            {
                AirconCommands.Operation: True,
                AirconCommands.OperationMode: HVAC_TRANSLATION[HVACMode.HEAT],
                AirconCommands.PresetTemp: HOME_LEAVE_TEMP,
            }
        )
        self._attr_is_on = True

    async def async_turn_off(self, **kwargs) -> None:
        """Leave Home Leave mode by restoring a normal target temperature."""
        await self._device.set_airco(
            {
                AirconCommands.PresetTemp: NORMAL_TEMP,
            }
        )
        self._attr_is_on = False
