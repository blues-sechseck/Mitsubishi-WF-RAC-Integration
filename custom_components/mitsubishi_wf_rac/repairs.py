"""Fix flows for the repairs this integration raises."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import CONF_CARRY_POWER_STATE, CONF_STATUS_REQUEST_MODE, DOMAIN
from .foreign_writers import status_request_unsupported_issue_id

if TYPE_CHECKING:
    # Not exported from the repairs package by older cores; on the oldest
    # supported core a runtime import would keep this platform from loading.
    from homeassistant.components.repairs import RepairsFlowResult


class StatusRequestRetryFlow(ConfirmRepairFlow):
    """Ask the unit for operation data again.

    The detection that stopped the request reads a pattern in the unit's
    answer and can be set off by somebody using the remote at the same moment,
    so there has to be a way back that does not mean deleting the entry. It
    forgets the mode that was learned and reloads; the detection then runs
    again from the start and, if the unit really does apply the request, ends
    up here once more.
    """

    def __init__(self, entry_id: str) -> None:
        """Remember which entry to reset."""
        super().__init__()
        self._entry_id = entry_id

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Reset the learned mode once the user confirms."""
        if user_input is None:
            return await super().async_step_confirm()
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is not None:
            # The legacy key is read as a fallback when the mode is missing
            # (see create_device_from_entry), so leaving it behind would bring
            # the old answer straight back.
            data = {
                key: value
                for key, value in entry.data.items()
                if key not in (CONF_STATUS_REQUEST_MODE, CONF_CARRY_POWER_STATE)
            }
            self.hass.config_entries.async_update_entry(entry, data=data)
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        ir.async_delete_issue(
            self.hass, DOMAIN, status_request_unsupported_issue_id(self._entry_id)
        )
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the fix flow for an issue raised by this integration."""
    entry_id = data.get("entry_id") if data else None
    if not isinstance(entry_id, str):
        # Raised by an earlier version, which carried no data. Setup raises it
        # again with the entry id, so this is only a stale row.
        return ConfirmRepairFlow()
    return StatusRequestRetryFlow(entry_id)
