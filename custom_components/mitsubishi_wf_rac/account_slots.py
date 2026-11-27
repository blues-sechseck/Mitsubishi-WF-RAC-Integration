"""Account (operator id) slots on the wireless module."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pywfrac import WfRacError

from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import Device

_LOGGER = logging.getLogger(__name__)


def registration_full_issue_id(entry_id: str) -> str:
    """Repair-issue id for a full account table on this entry's airco.

    Shared with async_remove_entry, which clears it when the entry is deleted.
    An unload leaves it standing: the condition outlives a reload.
    """
    return f"too_many_devices_{entry_id}"


def result_code(answer: Any) -> int | None:
    """The result code of a module answer, or None if it carries none.

    The parsed body arrives as it came, so neither its shape nor the field's
    type is guaranteed.
    """
    if not isinstance(answer, dict):
        return None
    try:
        return int(answer["result"])
    except (KeyError, TypeError, ValueError):
        return None


class AccountSlots:
    """Registering and releasing this operator id on one unit."""

    def __init__(self, device: Device) -> None:
        """Bind the slots to their coordinator."""
        self._device = device

    async def delete(self) -> dict[str, Any] | None:
        """Delete account (operator id) from the airco.

        None means the slot was not released - the request failed, or the
        answer did not confirm it. Nothing but a result code of 0 does: the
        refusal, the rate limit and the module's internal error all leave the
        slot where it was.
        """
        try:
            result = await self._device.api.del_account_info(self._device.airco_id)
        except (WfRacError, KeyError, TypeError):
            _LOGGER.warning(
                "Could not delete account from airco %s", self._device.airco_id
            )
            return None
        if result_code(result) != 0:
            return None
        return result

    async def add(self) -> dict[str, Any] | None:
        """Add account (operator id) from the airco."""
        try:
            result = await self._device.api.update_account_info(
                self._device.airco_id, self._device.hass.config.time_zone
            )
        except (WfRacError, KeyError, TypeError):
            _LOGGER.debug("Could not add account from airco %s", self._device.airco_id)
            return None

        # Here result:2 means the account table is full, and nothing frees a
        # slot but the official app - a standing condition for Repairs, ended
        # by a registration that went through and by nothing else.
        code = result_code(result)
        if code == 2:
            self.report_registration_full()
        elif code == 0:
            self.clear_registration_full_issue()
        return result

    def report_registration_full(self) -> None:
        """Raise the standing Repairs issue for a full account table."""
        ir.async_create_issue(
            self._device.hass,
            DOMAIN,
            registration_full_issue_id(self._device.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="too_many_devices",
            translation_placeholders={"device_name": self._device.device_name},
        )

    def clear_registration_full_issue(self) -> None:
        """End the full-table issue."""
        ir.async_delete_issue(
            self._device.hass,
            DOMAIN,
            registration_full_issue_id(self._device.entry_id),
        )
