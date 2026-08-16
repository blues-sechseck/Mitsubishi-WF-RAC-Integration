"""Tests for target_offset symmetry between the write path
(async_set_temperature) and the read-back path (_update_state). Without this,
a non-zero CONF_TARGET_OFFSET makes target_temperature permanently disagree
with what the user set, which trips automations' `state_attr(...) != desired`
guards into a set_temperature re-send loop. The "Target" temperature sensor
displays the same setpoint and is covered here too, since it has to resolve
the offset exactly like the climate entity. Needs the `hass` fixture (Device
is a DataUpdateCoordinator), hence tests/integration/ rather than tests/unit/.
"""

from unittest.mock import AsyncMock

import pytest

from homeassistant.components.climate.const import HVACMode
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mitsubishi_wf_rac.climate import AircoClimate
from custom_components.mitsubishi_wf_rac.sensor import TemperatureSensor
from custom_components.mitsubishi_wf_rac.const import (
    ATTR_TARGET_TEMPERATURE,
    CONF_TARGET_OFFSET,
    CONF_TARGET_OFFSET_COOL,
    CONF_TARGET_OFFSET_HEAT,
    DOMAIN,
    HVAC_TRANSLATION,
)
from custom_components.mitsubishi_wf_rac.wfrac.device import Device
from custom_components.mitsubishi_wf_rac.wfrac.models.aircon import AirconCommands


def _set_options(device: Device, options: dict[str, float]) -> None:
    # ConfigEntry.options is a read-only mappingproxy, so tests set the offsets
    # the same way the options flow does - through async_update_entry(), merged
    # onto whatever is already there.
    device.hass.config_entries.async_update_entry(
        device.config_entry,
        options={**device.config_entry.options, **options},
    )


@pytest.fixture
async def device(hass):
    # The climate paths use per-entry target offsets, so each test needs
    # options it can tailor without a full integration setup.
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    dev = Device(
        hass,
        entry,
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


async def test_set_temperature_subtracts_target_offset(device):
    _set_options(device, {CONF_TARGET_OFFSET: 1.0})
    device.async_queue_command = AsyncMock()
    entity = AircoClimate(device)

    await entity.async_set_temperature(temperature=23)

    sent = device.async_queue_command.call_args.args[0]
    assert sent[AirconCommands.PresetTemp] == 22


async def test_update_state_re_adds_target_offset(device):
    _set_options(device, {CONF_TARGET_OFFSET: 1.0})
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
    _set_options(device, {CONF_TARGET_OFFSET: 1.0, override_key: 2.5})
    entity = AircoClimate(device)

    assert entity._resolve_target_offset(hvac_mode) == 2.5


@pytest.mark.parametrize(
    "hvac_mode",
    [HVACMode.COOL, HVACMode.DRY, HVACMode.HEAT],
)
async def test_resolve_target_offset_falls_back_when_override_unset(device, hvac_mode):
    _set_options(device, {CONF_TARGET_OFFSET: 1.0})
    entity = AircoClimate(device)

    assert entity._resolve_target_offset(hvac_mode) == 1.0


@pytest.mark.parametrize(
    "hvac_mode",
    [HVACMode.AUTO, HVACMode.FAN_ONLY, HVACMode.OFF],
)
async def test_resolve_target_offset_ignores_overrides_for_other_modes(device, hvac_mode):
    # AUTO/FAN_ONLY/OFF never had per-mode behaviour asked for them - they
    # must always use the global value even when both overrides are set.
    _set_options(
        device,
        {
            CONF_TARGET_OFFSET: 1.0,
            CONF_TARGET_OFFSET_COOL: 2.5,
            CONF_TARGET_OFFSET_HEAT: -2.5,
        },
    )
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
        _set_options(device, {override_key: offset})
    else:
        _set_options(device, {CONF_TARGET_OFFSET: offset})
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
    _set_options(
        device,
        {CONF_TARGET_OFFSET: 0.0, CONF_TARGET_OFFSET_HEAT: -2.0},
    )
    device.async_queue_command = AsyncMock()
    entity = AircoClimate(device)

    await entity.async_set_temperature(temperature=21, hvac_mode=HVACMode.HEAT)
    sent = device.async_queue_command.call_args.args[0]

    device.airco.PresetTemp = sent[AirconCommands.PresetTemp]
    device.airco.OperationMode = HVAC_TRANSLATION[HVACMode.HEAT]
    device.airco.Operation = False
    entity._update_state()

    assert entity._attr_target_temperature == 21


# --- the Target sensor agrees with the climate entity ---------------------
#
# TemperatureSensor("Target") shows the same setpoint as the climate entity,
# derived from the same PresetTemp, so it has to resolve the offset the same
# way. Adding only the global CONF_TARGET_OFFSET there made the two disagree
# by the difference as soon as a per-mode override was configured.


@pytest.mark.parametrize(
    "hvac_mode,override_key,offset",
    [
        (HVACMode.COOL, CONF_TARGET_OFFSET_COOL, 1.5),
        (HVACMode.DRY, CONF_TARGET_OFFSET_COOL, 1.5),
        (HVACMode.HEAT, CONF_TARGET_OFFSET_HEAT, -1.5),
        (HVACMode.AUTO, None, 0.5),
    ],
)
async def test_target_sensor_matches_climate_entity(device, hvac_mode, override_key, offset):
    _set_options(device, {CONF_TARGET_OFFSET: 1.0})
    if override_key is not None:
        _set_options(device, {override_key: offset})
    else:
        _set_options(device, {CONF_TARGET_OFFSET: offset})
    device.airco.PresetTemp = 22
    device.airco.OperationMode = HVAC_TRANSLATION[hvac_mode]

    climate = AircoClimate(device)
    sensor = TemperatureSensor(device, "Target", ATTR_TARGET_TEMPERATURE, False)
    climate._update_state()
    sensor._update_state()

    assert sensor._attr_native_value == 22 + offset
    assert sensor._attr_native_value == climate._attr_target_temperature
