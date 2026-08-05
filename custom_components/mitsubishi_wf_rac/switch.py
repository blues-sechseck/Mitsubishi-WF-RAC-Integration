"""for switch integration."""
# pylint: disable = too-few-public-methods

from __future__ import annotations
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers import entity_registry as er

from . import MitsubishiWfRacConfigEntry
from .wfrac.device import Device
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry: MitsubishiWfRacConfigEntry, async_add_entities):
    """Setup switch entries"""

    device: Device = entry.runtime_data.device

    entities: list[SwitchEntity] = []

    _async_remove_self_clean_switch(hass, device)
    _async_remove_home_leave_mode_switch(hass, device)

    async_add_entities(entities)


def _async_remove_home_leave_mode_switch(hass, device: Device) -> None:
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
