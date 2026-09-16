"""Shared base entity for all WF-RAC platform entities."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate.const import HVACMode
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_TARGET_OFFSET,
    CONF_TARGET_OFFSET_COOL,
    CONF_TARGET_OFFSET_HEAT,
    HVAC_TRANSLATION,
)
from .coordinator import Device

_LOGGER = logging.getLogger(__name__)


class WfRacEntity(CoordinatorEntity[Device]):
    """Wires an entity to the shared Device coordinator.

    Subclasses implement _update_state() and call _apply_state() at the end of
    their own __init__.
    """

    _attr_has_entity_name = True

    def __init__(self, device: Device, context: Any | None = None) -> None:
        """Wire the entity to the shared coordinator."""
        super().__init__(device, context=context)
        self._attr_device_info = device.device_info
        self._state_unreadable = False

    @property
    def _hvac_mode_from_operation(self) -> HVACMode:
        """The unit's underlying cool/heat mode.

        Reported while the unit is off too, which is why the climate entity
        forces its own hvac_mode to OFF instead, and why the offset resolution
        below can still tell cooling from heating.
        """
        return list(HVAC_TRANSLATION.keys())[self.coordinator.airco.OperationMode]

    def _resolve_target_offset(self, hvac_mode: HVACMode) -> float:
        """Resolve the effective target_offset for a given hvac_mode.

        COOL/DRY fall back to CONF_TARGET_OFFSET_COOL, HEAT to
        CONF_TARGET_OFFSET_HEAT, everything else always uses the global
        CONF_TARGET_OFFSET - and so does COOL/HEAT when its per-mode option
        is unset (None), which is what keeps single-target_offset installs
        unchanged. Lives on the base entity so the climate write path, the
        climate read-back path and the target temperature sensor can never
        resolve a different offset for the same mode.
        """
        options = self.coordinator.options
        base_offset = options.get(CONF_TARGET_OFFSET, 0.0)
        if hvac_mode in (HVACMode.COOL, HVACMode.DRY):
            override = options.get(CONF_TARGET_OFFSET_COOL)
        elif hvac_mode == HVACMode.HEAT:
            override = options.get(CONF_TARGET_OFFSET_HEAT)
        else:
            override = None
        return float(base_offset if override is None else override)

    def _mark_state_unknown(self) -> None:
        """Drop the attributes that carry this entity's state.

        For a frame the entity cannot read: the unit answered and still takes
        commands, so its state is unknown rather than unavailable.
        """
        raise NotImplementedError

    def _update_state(self) -> None:
        """Refresh entity state from the coordinator, overridden per platform."""
        raise NotImplementedError

    def _apply_state(self) -> None:
        """Read the current frame into this entity, or mark it unknown.

        Every read goes through here, the constructor's included: a value it
        cannot translate must not escape there and abort the platform setup.
        """
        try:
            self._update_state()
        except (IndexError, KeyError, AttributeError, ValueError):
            if not self._state_unreadable:
                _LOGGER.warning(
                    "Could not update %s",
                    self.entity_id or self._attr_unique_id,
                    exc_info=True,
                )
            self._state_unreadable = True
            self._mark_state_unknown()
        else:
            self._state_unreadable = False

    @callback
    def _handle_coordinator_update(self) -> None:
        self._apply_state()
        self.async_write_ha_state()
