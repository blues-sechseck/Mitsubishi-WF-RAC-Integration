"""for binary sensor integration."""
# pylint: disable = too-few-public-methods

from __future__ import annotations
import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory

from . import MitsubishiWfRacConfigEntry
from .entity import WfRacEntity
from .wfrac.device import Device
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry: MitsubishiWfRacConfigEntry, async_add_entities):
    """Setup binary sensor entries"""

    device: Device = entry.runtime_data.device

    entities = [
        ProblemBinarySensor(device),
    ]
    # Occupancy ("vacant") detection is only reported by ModelNr 1 units.
    if device.airco.ModelNr == 1:
        entities.append(OccupancyBinarySensor(device))

    async_add_entities(entities)


class ProblemBinarySensor(WfRacEntity, BinarySensorEntity):
    """Reports whether the unit currently has an error code."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_translation_key = "problem"

    def __init__(self, device: Device) -> None:
        """Initialize the binary sensor."""
        super().__init__(device)
        self._attr_unique_id = f"{DOMAIN}-{device.airco_id}-problem"
        self._update_state()

    def _update_state(self) -> None:
        self._attr_is_on = self._device.airco.ErrorCode != "00"
        self._attr_extra_state_attributes = {
            "error_code": self._device.airco.ErrorCode
        }


class OccupancyBinarySensor(WfRacEntity, BinarySensorEntity):
    """Reports the occupancy state of the unit (ModelNr 1 only)."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_has_entity_name = True
    _attr_translation_key = "occupancy"

    def __init__(self, device: Device) -> None:
        """Initialize the binary sensor."""
        super().__init__(device)
        self._attr_unique_id = f"{DOMAIN}-{device.airco_id}-occupancy"
        self._update_state()

    def _update_state(self) -> None:
        # Vacant == True means nobody is present.
        self._attr_is_on = not self._device.airco.Vacant
