"""Shared base entity for all WF-RAC platform entities."""

from __future__ import annotations
import logging

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .wfrac.device import Device

_LOGGER = logging.getLogger(__name__)


class WfRacEntity(CoordinatorEntity[Device]):
    """Wires an entity to the shared Device coordinator.

    Subclasses keep their existing _update_state() (called once at the end of
    their own __init__ for the initial state, as before); this base class
    re-invokes it whenever the coordinator notifies listeners - either from
    its own poll or from Device.async_set_updated_data() right after a
    command completes.
    """

    def __init__(self, device: Device) -> None:
        super().__init__(device)
        self._device = device
        self._attr_device_info = device.device_info

    @property
    def available(self) -> bool:
        # Device tracks its own retry-tolerant availability (see
        # Device._set_availability()); update() never raises, so
        # CoordinatorEntity's default (coordinator.last_update_success) would
        # otherwise stay True even while the device is actually unreachable.
        return self._device.available

    @callback
    def _handle_coordinator_update(self) -> None:
        try:
            self._update_state()
        except (IndexError, KeyError, AttributeError, ValueError):
            _LOGGER.warning("Could not update %s", self.entity_id)
            self._device.set_available(False)
        self.async_write_ha_state()
