"""for binary sensor integration."""
# pylint: disable = too-few-public-methods

from __future__ import annotations
import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MitsubishiWfRacConfigEntry
from .entity import WfRacEntity
from .wfrac.device import Device
from .wfrac.error_codes import describe_error_code
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MitsubishiWfRacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Setup binary sensor entries"""

    device: Device = entry.runtime_data.device

    entities = [
        ProblemBinarySensor(device),
        CompressorBinarySensor(device),
    ]
    # Occupancy ("vacant") detection is only reported by units whose capability
    # table has VacantProperty=true - includes ZT-2025 (raw=3), which the
    # wire-protocol ModelNr grouping alone would miss (see capabilities.py).
    if device.airco.Capabilities.vacant_property:
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
        code = self._device.airco.ErrorCode
        self._attr_is_on = code != "00"
        attrs: dict[str, str] = {"error_code": code}
        # Deliberately no key at all (rather than a guessed/empty value) for
        # codes describe_error_code() doesn't recognize.
        # NB this still reports is_on for an M<n> code, which is a protective
        # stop the unit recovered from rather than a fault it is displaying -
        # arguably not a "problem". Left as it was for now; changing it would
        # alter what existing automations see.
        description = describe_error_code(code)
        if description is not None:
            attrs["error_description"] = description
        self._attr_extra_state_attributes = attrs


class CompressorBinarySensor(WfRacEntity, BinarySensorEntity):
    """Reports whether *this* indoor unit is calling for the compressor
    (content[9] & 0x02), as opposed to just being powered on - see
    rac_parser.py. On a multi-split the shared compressor can keep running for
    a sibling unit while this reads off, so it is demand, not compressor state."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_has_entity_name = True
    _attr_translation_key = "compressor"

    def __init__(self, device: Device) -> None:
        """Initialize the binary sensor."""
        super().__init__(device)
        self._attr_unique_id = f"{DOMAIN}-{device.airco_id}-compressor"
        self._update_state()

    def _update_state(self) -> None:
        self._attr_is_on = self._device.airco.CompressorRunning


class OccupancyBinarySensor(WfRacEntity, BinarySensorEntity):
    """Reports the occupancy state of the unit (VacantProperty-capable models only)."""

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
