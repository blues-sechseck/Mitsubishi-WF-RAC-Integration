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
from custom_components.mitsubishi_wf_rac.repairs import (
    StatusRequestRetryFlow,
    async_create_fix_flow,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir


async def _fix_flow(hass: HomeAssistant, entry: MockConfigEntry):
    """The flow Home Assistant's repairs manager would build for the issue.

    Built directly rather than through the manager: how the manager finds a
    custom integration's repairs platform differs between test harness
    releases, and what is under test is the flow, not that lookup.
    """
    report_status_request_unsupported(hass, entry.entry_id, "Living room")
    issue_id = status_request_unsupported_issue_id(entry.entry_id)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.is_fixable
    assert issue.is_persistent
    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    flow.handler = DOMAIN
    flow.issue_id = issue_id
    flow.data = issue.data
    return flow


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
    flow = await _fix_flow(hass, entry)
    assert isinstance(flow, StatusRequestRetryFlow)

    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {"device_name": "Living room"}

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await flow.async_step_confirm({})

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
    flow = await _fix_flow(hass, entry)
    await hass.config_entries.async_remove(entry.entry_id)

    result = await flow.async_step_confirm({})

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_repair_without_data_gets_the_plain_confirm_flow(hass: HomeAssistant):
    """Rows raised by an earlier version carry no entry id."""
    flow = await async_create_fix_flow(hass, "status_request_unsupported_x", None)

    assert not isinstance(flow, StatusRequestRetryFlow)
