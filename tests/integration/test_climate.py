"""Tests for climate.py's target_offset symmetry between the write path
(async_set_temperature) and the read-back path (_update_state). Without this,
a non-zero CONF_TARGET_OFFSET makes target_temperature permanently disagree
with what the user set, which trips automations' `state_attr(...) != desired`
guards into a set_temperature re-send loop. Needs the `hass` fixture (Device
is a DataUpdateCoordinator), hence tests/integration/ rather than tests/unit/.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.mitsubishi_wf_rac.climate import AircoClimate
from custom_components.mitsubishi_wf_rac.const import CONF_TARGET_OFFSET
from custom_components.mitsubishi_wf_rac.wfrac.device import Device
from custom_components.mitsubishi_wf_rac.wfrac.models.aircon import AirconCommands


@pytest.fixture
async def device(hass):
    dev = Device(
        hass,
        "Test AC",
        "127.0.0.1",
        51443,
        "device-id",
        "operator-id",
        "airco-id",
        availability_retry=False,
        availability_retry_limit=3,
        create_swing_mode_select=True,
    )
    dev._api = AsyncMock()
    # async_set_temperature()/_update_state() read the option straight off
    # config_entry.options - Device doesn't get one from the coordinator base
    # outside of a real config entry setup, so tests provide their own.
    dev.config_entry = SimpleNamespace(options={})
    return dev


async def test_set_temperature_subtracts_target_offset(device):
    device.config_entry.options[CONF_TARGET_OFFSET] = 1.0
    device.async_queue_command = AsyncMock()
    entity = AircoClimate(device)

    await entity.async_set_temperature(temperature=23)

    sent = device.async_queue_command.call_args.args[0]
    assert sent[AirconCommands.PresetTemp] == 22


async def test_update_state_re_adds_target_offset(device):
    device.config_entry.options[CONF_TARGET_OFFSET] = 1.0
    device.airco.PresetTemp = 22
    entity = AircoClimate(device)

    entity._update_state()

    assert entity._attr_target_temperature == 23


async def test_target_offset_zero_is_identity(device):
    device.async_queue_command = AsyncMock()
    entity = AircoClimate(device)

    await entity.async_set_temperature(temperature=23)
    sent = device.async_queue_command.call_args.args[0]
    assert sent[AirconCommands.PresetTemp] == 23

    device.airco.PresetTemp = 23
    entity._update_state()
    assert entity._attr_target_temperature == 23
