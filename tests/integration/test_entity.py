"""Regression test for #219: WfRacEntity._handle_coordinator_update() must
not report a failure for an entity whose _update_state() runs cleanly.
EnergyTotalResetButton had no _update_state() at all, so this exact path
raised AttributeError on every coordinator update and called
Device.set_available(False) - needs the `hass` fixture (Device is a
DataUpdateCoordinator), hence tests/integration/ rather than tests/unit/.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mitsubishi_wf_rac.button import EnergyTotalResetButton
from custom_components.mitsubishi_wf_rac.const import DOMAIN
from custom_components.mitsubishi_wf_rac.entity import WfRacEntity
from custom_components.mitsubishi_wf_rac.wfrac.device import Device


@pytest.fixture
async def device(hass):
    dev = Device(
        hass, MockConfigEntry(domain=DOMAIN), "Test AC", "127.0.0.1", 51443,
        "device-id", "operator-id", "airco-id",
        swing_selects_enabled_default=True,
    )
    dev._api = AsyncMock()
    return dev


async def test_coordinator_update_does_not_report_failure(device, monkeypatch):
    entity = EnergyTotalResetButton(device)
    entity.async_write_ha_state = lambda: None
    reported = []
    monkeypatch.setattr(device, "set_available", lambda available: reported.append(available))

    entity._handle_coordinator_update()

    assert reported == []


async def test_coordinator_contexts_include_only_contextual_entities(device):
    contextual_entity = WfRacEntity(device, context="operation-data-code")
    contextless_entity = WfRacEntity(device)
    contextual_entity.hass = device.hass
    contextless_entity.hass = device.hass

    await contextual_entity.async_added_to_hass()
    await contextless_entity.async_added_to_hass()

    assert set(device.async_contexts()) == {"operation-data-code"}

    await contextual_entity.async_will_remove_from_hass()
    await contextless_entity.async_will_remove_from_hass()
    contextual_entity._call_on_remove_callbacks()
    contextless_entity._call_on_remove_callbacks()


async def test_base_entity_marks_device_unavailable_when_state_update_fails(device, monkeypatch):
    entity = WfRacEntity(device)
    entity._update_state = MagicMock(side_effect=ValueError)
    entity.async_write_ha_state = lambda: None
    set_available = MagicMock()
    monkeypatch.setattr(device, "set_available", set_available)

    assert entity.available is device.available
    entity._handle_coordinator_update()

    set_available.assert_called_once_with(False)
