"""for binary sensor integration."""
# pylint: disable = too-few-public-methods

from __future__ import annotations

from pywfrac import describe_error_code

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MitsubishiWfRacConfigEntry
from .const import DOMAIN
from .coordinator import Device
from .entity import WfRacEntity

# Read-only as far as the device is concerned: the coordinator does the
# polling, and nothing on this platform sends a request of its own.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MitsubishiWfRacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor entries."""

    device: Device = entry.runtime_data.device

    entities = [
        ProblemBinarySensor(device),
        CompressorBinarySensor(device),
        ExternalControlBinarySensor(device),
        ExternalTemperatureActiveBinarySensor(device),
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
    _attr_translation_key = "problem"

    def __init__(self, device: Device) -> None:
        """Initialize the binary sensor."""
        super().__init__(device)
        self._attr_unique_id = f"{DOMAIN}-{device.airco_id}-problem"
        self._apply_state()

    def _mark_state_unknown(self) -> None:
        self._attr_is_on = None
        # The code goes with the state. Left behind it would keep answering
        # state_attr() with the last fault this entity read, while the entity
        # itself says it does not know.
        self._attr_extra_state_attributes = {}

    def _update_state(self) -> None:
        code = self.coordinator.airco.ErrorCode
        self._attr_is_on = code != "00"
        attrs: dict[str, str] = {"error_code": code}
        # No key at all, rather than a guessed or empty value, for codes
        # describe_error_code() does not recognize. An M<n> code reads as a
        # problem too, though it is a protective stop the unit recovered from:
        # narrowing that would change what existing automations see.
        description = describe_error_code(code)
        if description is not None:
            attrs["error_description"] = description
        self._attr_extra_state_attributes = attrs


class CompressorBinarySensor(WfRacEntity, BinarySensorEntity):
    """Reports whether *this* indoor unit is calling for the compressor.

    The flag is content[9] & 0x02, as opposed to just being powered on - see
    rac_parser.py. On a multi-split the shared compressor can keep running for
    a sibling unit while this reads off, so it is demand, not compressor state.
    """

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_translation_key = "compressor"

    def __init__(self, device: Device) -> None:
        """Initialize the binary sensor."""
        super().__init__(device)
        self._attr_unique_id = f"{DOMAIN}-{device.airco_id}-compressor"
        self._apply_state()

    def _mark_state_unknown(self) -> None:
        self._attr_is_on = None

    def _update_state(self) -> None:
        self._attr_is_on = self.coordinator.airco.CompressorRunning


class ExternalControlBinarySensor(WfRacEntity, BinarySensorEntity):
    """On while another client is using the unit and this integration holds back.

    The unit grants whoever wrote last 60 seconds of exclusive write access,
    so the operation-data request - itself a write - is paused while someone
    else is active (see Device.foreign_activity). Without this entity the
    only visible effect would be the operation-data sensors going unknown,
    with nothing to explain why.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "external_control"

    def __init__(self, device: Device) -> None:
        """Initialize the binary sensor."""
        super().__init__(device)
        self._attr_unique_id = f"{DOMAIN}-{device.airco_id}-external-control"
        self._apply_state()

    def _mark_state_unknown(self) -> None:
        self._attr_is_on = None

    def _update_state(self) -> None:
        self._attr_is_on = self.coordinator.foreign_activity


class ExternalTemperatureActiveBinarySensor(WfRacEntity, BinarySensorEntity):
    """Reports whether the unit is regulating on a temperature we supplied.

    Armed is not the same as in effect: nothing is written while the unit is
    off or in fan_only (see is_external_temperature_mode), and after a restart
    the value waits for the next outgoing frame.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "external_temperature_active"

    def __init__(self, device: Device) -> None:
        """Initialize the binary sensor."""
        super().__init__(device)
        self._attr_unique_id = f"{DOMAIN}-{device.airco_id}-external-temperature-active"
        self._apply_state()

    def _mark_state_unknown(self) -> None:
        self._attr_is_on = None

    def _update_state(self) -> None:
        self._attr_is_on = self.coordinator.external_temperature_applied


class OccupancyBinarySensor(WfRacEntity, BinarySensorEntity):
    """Reports the occupancy state of the unit (VacantProperty-capable models only)."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_translation_key = "occupancy"

    def __init__(self, device: Device) -> None:
        """Initialize the binary sensor."""
        super().__init__(device)
        self._attr_unique_id = f"{DOMAIN}-{device.airco_id}-occupancy"
        self._apply_state()

    def _mark_state_unknown(self) -> None:
        self._attr_is_on = None

    def _update_state(self) -> None:
        # Vacant == True means nobody is present.
        self._attr_is_on = not self.coordinator.airco.Vacant
