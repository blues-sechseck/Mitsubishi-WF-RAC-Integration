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

from homeassistant.components.climate.const import HVACMode

from custom_components.mitsubishi_wf_rac.climate import AircoClimate
from custom_components.mitsubishi_wf_rac.const import (
    CONF_TARGET_OFFSET,
    CONF_TARGET_OFFSET_COOL,
    CONF_TARGET_OFFSET_HEAT,
    HVAC_TRANSLATION,
)
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


# --- per-mode target_offset resolver ------------------------------------
#
# CONF_TARGET_OFFSET_COOL/_HEAT are optional per-mode overrides that must
# fall back to the single CONF_TARGET_OFFSET when unset (None, not 0.0),
# so that existing installs configuring only target_offset keep behaving
# identically across all hvac_modes.


@pytest.mark.parametrize(
    "hvac_mode,override_key",
    [
        (HVACMode.COOL, CONF_TARGET_OFFSET_COOL),
        (HVACMode.DRY, CONF_TARGET_OFFSET_COOL),
        (HVACMode.HEAT, CONF_TARGET_OFFSET_HEAT),
    ],
)
async def test_resolve_target_offset_uses_override_when_set(device, hvac_mode, override_key):
    device.config_entry.options[CONF_TARGET_OFFSET] = 1.0
    device.config_entry.options[override_key] = 2.5
    entity = AircoClimate(device)

    assert entity._resolve_target_offset(hvac_mode) == 2.5


@pytest.mark.parametrize(
    "hvac_mode",
    [HVACMode.COOL, HVACMode.DRY, HVACMode.HEAT],
)
async def test_resolve_target_offset_falls_back_when_override_unset(device, hvac_mode):
    device.config_entry.options[CONF_TARGET_OFFSET] = 1.0
    entity = AircoClimate(device)

    assert entity._resolve_target_offset(hvac_mode) == 1.0


@pytest.mark.parametrize(
    "hvac_mode",
    [HVACMode.AUTO, HVACMode.FAN_ONLY, HVACMode.OFF],
)
async def test_resolve_target_offset_ignores_overrides_for_other_modes(device, hvac_mode):
    # AUTO/FAN_ONLY/OFF never had per-mode behaviour asked for them - they
    # must always use the global value even when both overrides are set.
    device.config_entry.options[CONF_TARGET_OFFSET] = 1.0
    device.config_entry.options[CONF_TARGET_OFFSET_COOL] = 2.5
    device.config_entry.options[CONF_TARGET_OFFSET_HEAT] = -2.5
    entity = AircoClimate(device)

    assert entity._resolve_target_offset(hvac_mode) == 1.0


# --- round-trip symmetry across modes ------------------------------------
#
# Regression guard for the 2026.9.1-beta2 fix: the write path (subtract) and
# the read-back path (add) must resolve the *same* offset for the same mode,
# or target_temperature permanently disagrees with what was requested and
# automations re-send the command in a loop. This must hold per-mode now
# that the offset resolution depends on hvac_mode.


@pytest.mark.parametrize(
    "hvac_mode,override_key,offset",
    [
        (HVACMode.COOL, CONF_TARGET_OFFSET_COOL, 1.5),
        (HVACMode.DRY, CONF_TARGET_OFFSET_COOL, 1.5),
        (HVACMode.HEAT, CONF_TARGET_OFFSET_HEAT, -1.5),
        (HVACMode.AUTO, None, 0.5),
    ],
)
async def test_round_trip_symmetry_per_mode(device, hvac_mode, override_key, offset):
    if override_key is not None:
        device.config_entry.options[override_key] = offset
    else:
        device.config_entry.options[CONF_TARGET_OFFSET] = offset
    device.async_queue_command = AsyncMock()
    entity = AircoClimate(device)

    await entity.async_set_temperature(temperature=23, hvac_mode=hvac_mode)

    sent = device.async_queue_command.call_args.args[0]
    device.airco.PresetTemp = sent[AirconCommands.PresetTemp]
    device.airco.OperationMode = HVAC_TRANSLATION[hvac_mode]
    entity._update_state()

    assert entity._attr_target_temperature == 23


async def test_round_trip_symmetry_survives_unit_being_off(device):
    # airco.OperationMode still reports the underlying cool/heat mode while
    # airco.Operation is False (unit off) - the offset resolution must use
    # that underlying mode, not the OFF hvac_mode the entity reports.
    device.config_entry.options[CONF_TARGET_OFFSET] = 0.0
    device.config_entry.options[CONF_TARGET_OFFSET_HEAT] = -2.0
    device.async_queue_command = AsyncMock()
    entity = AircoClimate(device)

    await entity.async_set_temperature(temperature=21, hvac_mode=HVACMode.HEAT)
    sent = device.async_queue_command.call_args.args[0]

    device.airco.PresetTemp = sent[AirconCommands.PresetTemp]
    device.airco.OperationMode = HVAC_TRANSLATION[HVACMode.HEAT]
    device.airco.Operation = False
    entity._update_state()

    assert entity._attr_target_temperature == 21
