"""Silent operation, and registry cleanup for switches no longer created.

HACS only: core publishes no switch. Besides the silent operation switch the
platform drops two switches that earlier HACS releases created.
"""
# pylint: disable = too-few-public-methods

from __future__ import annotations

import logging

from pywfrac.parser import SERVICE_DATA_CODE_BY_FIELD

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MitsubishiWfRacConfigEntry
from .const import DOMAIN
from .coordinator import Device
from .entity import WfRacEntity

_LOGGER = logging.getLogger(__name__)
# Zero although this platform writes: the coordinator already serialises and
# spaces every request.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MitsubishiWfRacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entries."""

    device: Device = entry.runtime_data.device

    entities: list[SwitchEntity] = [SilentOperationSwitch(device)]

    _async_remove_self_clean_switch(hass, device)
    _async_remove_home_leave_mode_switch(hass, device)

    async_add_entities(entities)


class SilentOperationSwitch(WfRacEntity, SwitchEntity):
    """Silent operation, an outdoor-unit setting the unit keeps across power.

    The state is only ever what the unit reports in 0xDD: the IR remote can
    change it, and a state set there cannot always be cleared over the bus.
    Unavailable until the unit has answered, so a model without the code shows
    no switch rather than a guess.
    """

    _attr_translation_key = "silent_operation"
    _attr_entity_registry_enabled_default = False

    def __init__(self, device: Device) -> None:
        """Set up the switch and subscribe only while it is enabled."""
        super().__init__(device, context=SERVICE_DATA_CODE_BY_FIELD["SilentOperation"])
        self._attr_unique_id = f"{DOMAIN}-{device.airco_id}-silent-operation"
        self._apply_state()

    @property
    def available(self) -> bool:
        """Wait for a real answer rather than guessing capability."""
        return super().available and self.coordinator.airco.SilentOperation is not None

    def _mark_state_unknown(self) -> None:
        self._attr_is_on = None

    def _update_state(self) -> None:
        self._attr_is_on = self.coordinator.airco.SilentOperation

    async def async_turn_on(self, **kwargs: object) -> None:
        """Switch silent operation on."""
        await self.coordinator.async_set_silent_operation(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Switch silent operation off."""
        await self.coordinator.async_set_silent_operation(False)


def _async_remove_home_leave_mode_switch(hass: HomeAssistant, device: Device) -> None:
    """Drop the former Home Leave Mode switch from the entity registry.

    That switch only ever faked Home Leave mode by pushing the heat target
    below the unit's own threshold (Heat+10°C) - a one-directional guess with
    no way to express the unit's real Cool-side away target. Replaced by
    HomeLeaveModeSelect in select.py (off / away_cool / away_heat), backed by
    the same Vacant bit plus the now-live-verified Tag-248 HomeLeaveMode data.
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "switch", DOMAIN, f"{DOMAIN}-{device.airco_id}-home-leave-mode"
    )
    if entity_id:
        _LOGGER.debug("Removing obsolete home leave mode switch %s", entity_id)
        registry.async_remove(entity_id)


def _async_remove_self_clean_switch(hass: HomeAssistant, device: Device) -> None:
    """Drop the former Self Clean switch from the entity registry.

    The unit's real self-clean cycle can only be started locally via the IR
    remote - the WiFi module offers no way to trigger it, so the switch never
    did anything. Removing it here keeps it from lingering as an unavailable
    leftover in dashboards and automations.
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "switch", DOMAIN, f"{DOMAIN}-{device.airco_id}-self-clean"
    )
    if entity_id:
        _LOGGER.debug("Removing obsolete self clean switch %s", entity_id)
        registry.async_remove(entity_id)
