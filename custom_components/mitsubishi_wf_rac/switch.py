"""for switch integration."""
# pylint: disable = too-few-public-methods

from __future__ import annotations
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.event import async_call_later

from . import MitsubishiWfRacConfigEntry
from .wfrac.device import Device
from .wfrac.models.aircon import AirconCommands
from .wfrac.repository import AirconApiError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# The set-command response for self clean often still reflects the previous
# (cached) state rather than the freshly toggled one - re-read the real state
# a bit later instead of trusting it immediately.
REFRESH_DELAY_SECONDS = 15


async def async_setup_entry(hass, entry: MitsubishiWfRacConfigEntry, async_add_entities):
    """Setup switch entries"""

    device: Device = entry.runtime_data.device

    # Self clean is only supported by ModelNr 1 and 2 units.
    if device.airco.ModelNr in (1, 2):
        async_add_entities([SelfCleanSwitch(device)])
    else:
        _LOGGER.debug(
            "Self clean not supported by model %s (%s)",
            device.airco.ModelNr,
            device.device_name,
        )


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
