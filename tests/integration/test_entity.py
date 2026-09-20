"""Regression test for #219: WfRacEntity._handle_coordinator_update() must
not report a failure for an entity whose _update_state() runs cleanly.
EnergyTotalResetButton had no _update_state() at all, so this exact path
raised AttributeError on every coordinator update and took the whole device
offline with it - needs the `hass` fixture (Device is a
DataUpdateCoordinator), hence tests/integration/ rather than tests/unit/.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mitsubishi_wf_rac.button import EnergyTotalResetButton
from custom_components.mitsubishi_wf_rac.const import DOMAIN
from custom_components.mitsubishi_wf_rac.coordinator import (
    AVAILABILITY_FAILURE_LIMIT_MIN,
    Device,
)
from custom_components.mitsubishi_wf_rac.entity import WfRacEntity


@pytest.fixture
async def device(hass):
    dev = Device(
        hass,
        MockConfigEntry(domain=DOMAIN),
        "Test AC",
        "127.0.0.1",
        51443,
        "device-id",
        "operator-id",
        "airco-id",
        swing_selects_enabled_default=True,
    )
    dev._api = AsyncMock()
    return dev


async def test_coordinator_update_does_not_report_failure(device, caplog):
    """EnergyTotalResetButton had no _update_state() at all, so every
    coordinator update raised AttributeError inside it."""
    entity = EnergyTotalResetButton(device)
    entity.async_write_ha_state = lambda: None

    with caplog.at_level(logging.WARNING):
        entity._handle_coordinator_update()

    assert "Could not update" not in caplog.text
    assert entity._state_unreadable is False


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


async def test_an_unreadable_frame_marks_the_entity_unknown_not_the_device(device):
    """The unit answered and still takes commands, so it is not unavailable."""
    entity = WfRacEntity(device)
    entity._update_state = MagicMock(side_effect=ValueError)
    entity._mark_state_unknown = MagicMock()
    entity.async_write_ha_state = lambda: None
    device._record_reachable()

    # More failures in a row than it takes to declare the device unavailable:
    # one unreadable field must not do it, and neither must a run of them.
    for _ in range(AVAILABILITY_FAILURE_LIMIT_MIN + 1):
        entity._handle_coordinator_update()

    entity._mark_state_unknown.assert_called_with()
    assert device.last_update_success is True
    assert entity.available is True


async def test_apply_state_swallows_a_first_read_that_fails(device, monkeypatch):
    """A constructor read must fail like a poll, not like a setup error.

    Before this, every platform called _update_state() straight from its
    __init__. A frame that decodes cleanly can still carry a value an entity
    cannot translate, and there the exception took the whole platform down:
    the config entry loaded without a single entity of that kind.
    """
    entity = WfRacEntity(device)
    entity._attr_unique_id = "airco-id-something"
    entity._update_state = MagicMock(side_effect=IndexError)
    entity._mark_state_unknown = MagicMock()

    entity._apply_state()

    entity._mark_state_unknown.assert_called_once_with()


async def test_apply_state_names_the_entity_by_unique_id_before_it_is_added(
    device, monkeypatch, caplog
):
    """entity_id is only assigned on add, so the first read has nothing else."""
    entity = WfRacEntity(device)
    entity._attr_unique_id = "airco-id-fan-speed"
    entity._update_state = MagicMock(side_effect=IndexError)
    entity._mark_state_unknown = MagicMock()

    entity._apply_state()

    assert "airco-id-fan-speed" in caplog.text


async def test_an_unreadable_frame_is_logged_once_and_the_entity_recovers(
    device, caplog
):
    """The condition holds until the unit sends something else."""
    entity = WfRacEntity(device)
    entity._attr_unique_id = "airco-id-fan-speed"
    entity._mark_state_unknown = MagicMock()
    entity._update_state = MagicMock(side_effect=IndexError)

    entity._apply_state()
    entity._apply_state()

    assert caplog.text.count("Could not update") == 1

    entity._update_state = MagicMock()
    entity._apply_state()
    entity._update_state = MagicMock(side_effect=IndexError)
    entity._apply_state()

    assert caplog.text.count("Could not update") == 2
