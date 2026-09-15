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

    Subclasses implement _update_state() and call _apply_state() once at the
    end of their own __init__ for the initial state; this base class
    re-invokes it whenever the coordinator notifies listeners - either from
    its own poll or from Device.async_set_updated_data() right after a
    command completes.
    """

    _attr_has_entity_name = True

    def __init__(self, device: Device, context: Any | None = None) -> None:
        super().__init__(device, context=context)
        self._device = device
        self._attr_device_info = device.device_info
        self._state_unreadable = False

    @property
    def _hvac_mode_from_operation(self) -> HVACMode:
        """The unit's underlying cool/heat mode.

        airco.OperationMode keeps reporting it while the unit is off, which is
        what the offset resolution below needs - the climate entity's own
        hvac_mode is forced to OFF in that case.
        """
        return list(HVAC_TRANSLATION.keys())[self._device.airco.OperationMode]

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
        options = self._device.options
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

        Overridden per platform. Called when a frame arrives that the entity
        cannot read: the unit answered and still takes commands, so it is not
        unavailable - its state is merely unknown until a frame it can read
        comes along.
        """
        raise NotImplementedError

    def _update_state(self) -> None:
        """Refresh entity state from the coordinator. Every concrete
        subclass overrides this; never invoked through this base
        implementation."""
        raise NotImplementedError

    def _apply_state(self) -> None:
        """Read the current frame into this entity, or mark it unknown.

        Every read goes through here, the very first one included. A frame can
        decode cleanly and still carry a value this entity cannot translate,
        and letting that escape a constructor is not the same failure as
        letting it escape a poll: the platform never finishes setting up, so
        the config entry loads with no entity at all and only a traceback to
        say why. The same value arriving one frame later merely makes this
        entity's state unknown.
        """
        try:
            self._update_state()
        except (IndexError, KeyError, AttributeError, ValueError):
            # Once, with the traceback: which field was missing is the whole
            # diagnosis, and the condition holds until the unit sends
            # something else - a line per poll would say nothing more.
            if not self._state_unreadable:
                # entity_id is only assigned once the entity is added, so on
                # the first read the unique id is all there is to name it by.
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
