"""Tests for repairs.py: the way back from a unit that is no longer asked for
operation data."""

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mitsubishi_wf_rac.const import (
    CONF_CARRY_POWER_STATE,
    CONF_STATUS_REQUEST_MODE,
    DOMAIN,
    STATUS_REQUEST_SILENT,
)
from custom_components.mitsubishi_wf_rac.foreign_writers import (
    report_status_request_unsupported,
    status_request_unsupported_issue_id,
)
from homeassistant.components.repairs.issue_handler import RepairsFlowManager
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component


async def _start_fix(hass: HomeAssistant, entry: MockConfigEntry):
    assert await async_setup_component(hass, "repairs", {})
    # The repairs platform of an integration is only looked up once the
    # integration counts as loaded.
    hass.config.components.add(DOMAIN)
    report_status_request_unsupported(hass, entry.entry_id, "Living room")
    issue_id = status_request_unsupported_issue_id(entry.entry_id)
    manager: RepairsFlowManager = hass.data["repairs"]["flow_manager"]
    return manager, await manager.async_init(DOMAIN, data={"issue_id": issue_id})


async def test_the_fix_forgets_the_learned_mode_and_reloads(hass: HomeAssistant):
    """Both keys: the legacy one is read when the new one is missing, so
    leaving it behind would hand the old answer straight back."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "airco_id": "a",
            CONF_STATUS_REQUEST_MODE: STATUS_REQUEST_SILENT,
            CONF_CARRY_POWER_STATE: True,
        },
    )
    entry.add_to_hass(hass)

    manager, result = await _start_fix(hass, entry)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {"device_name": "Living room"}

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await manager.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.data) == {"airco_id": "a"}
    reload.assert_called_once_with(entry.entry_id)
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, status_request_unsupported_issue_id(entry.entry_id)
        )
        is None
    )


async def test_the_fix_of_a_removed_entry_just_closes_the_repair(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, data={"airco_id": "a"})
    entry.add_to_hass(hass)
    manager, result = await _start_fix(hass, entry)
    await hass.config_entries.async_remove(entry.entry_id)

    result = await manager.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
