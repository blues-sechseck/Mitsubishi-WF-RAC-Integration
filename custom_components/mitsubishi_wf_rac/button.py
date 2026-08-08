"""for button integration."""
# pylint: disable = too-few-public-methods

from __future__ import annotations
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import MitsubishiWfRacConfigEntry
from .entity import WfRacEntity
from .wfrac.device import Device
from .const import DOMAIN, SIGNAL_SET_ENERGY_TOTAL

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry: MitsubishiWfRacConfigEntry, async_add_entities):
    """Setup button entries"""

    device: Device = entry.runtime_data.device

    entities: list[ButtonEntity] = []
    if device.airco.Electric is not None:
        entities.append(EnergyTotalResetButton(device))

    async_add_entities(entities)


class EnergyTotalResetButton(WfRacEntity, ButtonEntity):
    """Resets the accumulated Energy Usage Total back to zero.

    The unit's own counter cannot be written, so this only clears what the
    integration has added up - see EnergyTotalSensor in sensor.py. Reaches the
    sensor over SIGNAL_SET_ENERGY_TOTAL rather than holding a reference to it,
    since the two live on separately set up platforms.
    """

    _attr_translation_key = "reset_energy_total"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True

    def __init__(self, device: Device) -> None:
        """Initialize the button."""
        super().__init__(device)
        self._attr_unique_id = f"{DOMAIN}-{self._device.airco_id}-reset-energy-total"

    async def async_press(self) -> None:
        """Handle the button press."""
        async_dispatcher_send(
            self.hass,
            f"{SIGNAL_SET_ENERGY_TOTAL}_{self._device.airco_id}",
            0.0,
        )

    def _update_state(self) -> None:
        """No state to reflect - the button has none."""
