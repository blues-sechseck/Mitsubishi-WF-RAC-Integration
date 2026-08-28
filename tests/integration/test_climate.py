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
from homeassistant.helpers.restore_state import RestoredExtraData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mitsubishi_wf_rac.climate import AircoClimate
from custom_components.mitsubishi_wf_rac.sensor import TemperatureSensor
from custom_components.mitsubishi_wf_rac.const import (
    ATTR_TARGET_TEMPERATURE,
    CONF_INDOOR_OFFSET,
    CONF_TARGET_OFFSET,
    CONF_TARGET_OFFSET_COOL,
    CONF_TARGET_OFFSET_HEAT,
    DOMAIN,
    HVAC_TRANSLATION,
)
from custom_components.mitsubishi_wf_rac.coordinator import Device
from custom_components.mitsubishi_wf_rac.wfrac.models.aircon import AirconCommands
from custom_components.mitsubishi_wf_rac.wfrac.rac_parser import (
    SERVICE_DATA_INDOOR_COIL_RAW,
)


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
    yield dev
    await dev.async_shutdown()


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


def _service_entity(device) -> AircoClimate:
    """A climate entity wired up enough to write its own state.

    The override actions refresh the entity right away rather than waiting for
    the next poll, so unlike the read-path tests these need hass and an
    entity_id on the entity.
    """
    entity = AircoClimate(device)
    entity.hass = device.hass
    entity.entity_id = "climate.test_ac"
    return entity


def _mark_reached_the_unit(device, temperature: float) -> None:
    """Put the device in the state that follows a frame carrying the override:
    the frame recorded what it wrote, byte 5 echoes it back, and the 0.1 K
    segment carries the same reading."""
    raw = round(temperature * 4) + 61
    device._external_temperature_written.append(raw)
    device.airco.ControllerRoomTempRaw = raw
    device.airco.IndoorTemp = temperature + 0.5


async def test_set_external_temperature_arms_without_sending_anything(device):
    # The action never sends a frame of its own, not even in a mode that could
    # use the value right away: byte 5 cannot be written on its own, so the
    # frame would re-assert every other setting and take the write lock for a
    # minute. It rides along on the next frame instead.
    device.airco.Operation = True
    device.airco.OperationMode = HVAC_TRANSLATION[HVACMode.COOL]
    device.async_queue_command = AsyncMock()
    entity = _service_entity(device)

    await entity.async_set_external_temperature(temperature=18.7)

    assert entity._external_temperature_override == 18.7
    assert device.external_temperature_override == 18.7
    device.async_queue_command.assert_not_awaited()


async def test_arming_an_override_switches_the_operation_data_request_on(device):
    # No diagnostic sensor is enabled here: the override subscribes on its own
    # behalf, so the request that carries it starts going out.
    entity = _service_entity(device)

    await entity.async_set_external_temperature(temperature=18.7)

    assert SERVICE_DATA_INDOOR_COIL_RAW in set(device.async_contexts())

    await entity.async_set_external_temperature(temperature=None)

    assert SERVICE_DATA_INDOOR_COIL_RAW not in set(device.async_contexts())


async def test_update_state_uses_indoor_temp_without_override(device):
    _set_options(device, {CONF_INDOOR_OFFSET: 1.5})
    device.airco.IndoorTemp = 22.0
    entity = AircoClimate(device)

    entity._update_state()

    assert entity._attr_current_temperature == 23.5


async def test_update_state_shows_what_the_unit_reports_under_an_override(device):
    # The unit echoes the injected value back, so the reading follows it on its
    # own. The calibration offset drops out: it corrects the unit's own sensor,
    # which is not what the unit is regulating on any more.
    _set_options(device, {CONF_INDOOR_OFFSET: 1.5})
    device.airco.IndoorTemp = 22.0
    device.airco.Operation = True
    device.airco.OperationMode = HVAC_TRANSLATION[HVACMode.COOL]
    entity = _service_entity(device)

    await entity.async_set_external_temperature(temperature=20.0)
    _mark_reached_the_unit(device, 20.0)
    entity._update_state()

    assert entity._attr_current_temperature == 20.5


async def test_update_state_keeps_indoor_temp_until_the_override_is_sent(device):
    # Armed but not yet carried by any frame - the case after a restart. The
    # unit is still regulating on its own sensor, so that is what is shown.
    _set_options(device, {CONF_INDOOR_OFFSET: 1.5})
    device.airco.IndoorTemp = 22.0
    device.airco.Operation = True
    device.airco.OperationMode = HVAC_TRANSLATION[HVACMode.COOL]
    entity = _service_entity(device)

    await entity.async_set_external_temperature(temperature=20.0)
    entity._update_state()

    assert entity._attr_current_temperature == 23.5


async def test_update_state_reapplies_the_offset_while_a_new_value_is_in_flight(device):
    # A source sensor feeding the action reports a new value every cycle. Until
    # the unit confirms the new one, it is still regulating on the old - and
    # the display follows the unit rather than what has merely been armed.
    _set_options(device, {CONF_INDOOR_OFFSET: 1.5})
    device.airco.Operation = True
    device.airco.OperationMode = HVAC_TRANSLATION[HVACMode.COOL]
    entity = _service_entity(device)

    await entity.async_set_external_temperature(temperature=20.0)
    _mark_reached_the_unit(device, 20.0)
    await entity.async_set_external_temperature(temperature=20.25)
    entity._update_state()

    assert entity._attr_current_temperature == 20.5

    _mark_reached_the_unit(device, 20.25)
    entity._update_state()

    assert entity._attr_current_temperature == 20.75


async def test_update_state_keeps_the_offset_while_the_unit_is_off_or_fan_only(device):
    # Nothing writes byte 5 in those modes, so the unit is on its own sensor
    # however the override is armed - and its own sensor is what the offset
    # calibrates.
    _set_options(device, {CONF_INDOOR_OFFSET: 1.5})
    device.airco.IndoorTemp = 22.0
    device.airco.Operation = True
    device.airco.OperationMode = HVAC_TRANSLATION[HVACMode.COOL]
    entity = _service_entity(device)

    await entity.async_set_external_temperature(temperature=20.0)

    for operation, mode in (
        (False, HVAC_TRANSLATION[HVACMode.COOL]),
        (True, HVAC_TRANSLATION[HVACMode.FAN_ONLY]),
    ):
        device.airco.Operation = operation
        device.airco.OperationMode = mode
        entity._update_state()

        assert entity._attr_current_temperature == 23.5


async def test_set_external_temperature_none_clears_override(device):
    # Clearing needs no frame of its own either: the next one to go out
    # carries 0xFF, which is what puts the unit back on its own sensor.
    device.airco.Operation = True
    device.airco.OperationMode = HVAC_TRANSLATION[HVACMode.COOL]
    device.async_queue_command = AsyncMock()
    entity = _service_entity(device)

    await entity.async_set_external_temperature(temperature=20.0)
    await entity.async_set_external_temperature(temperature=None)

    assert entity._external_temperature_override is None
    assert device.external_temperature_override is None
    device.async_queue_command.assert_not_awaited()


async def test_set_external_temperature_arms_while_the_unit_is_off(device):
    # Nothing to defer: the value sits armed and goes out with whatever frame
    # comes next, whether or not the unit can use it yet.
    device.airco.Operation = False
    device.async_queue_command = AsyncMock()
    entity = _service_entity(device)

    await entity.async_set_external_temperature(temperature=20.0)

    assert entity._external_temperature_override == 20.0
    device.async_queue_command.assert_not_awaited()


def _restoring_entity(device, restored: dict[str, float | str | None]) -> AircoClimate:
    entity = _service_entity(device)
    entity.async_get_last_extra_data = AsyncMock(return_value=RestoredExtraData(restored))
    return entity


async def _add_and_remove(entity: AircoClimate) -> None:
    await entity.async_added_to_hass()
    # Added by hand rather than through a platform, so the coordinator
    # listener has to be released the same way - see tests/integration/
    # test_entity.py.
    entity._call_on_remove_callbacks()


async def test_restore_state_restores_external_temperature_override(device):
    entity = _restoring_entity(device, {"external_temperature_override": 19.25})

    await _add_and_remove(entity)

    assert entity._external_temperature_override == 19.25
    assert device._external_temperature_override == 19.25
    # Restored, not sent: nothing has told the unit about it yet.
    assert device.external_temperature_applied is False


@pytest.mark.parametrize("restored", [60.0, -30.0, "unavailable"])
async def test_restore_state_ignores_an_unusable_override(device, restored):
    # Out of range is not merely useless: encoding it raises, and it would do
    # so on every frame, taking the write path down with it after every restart.
    entity = _restoring_entity(device, {"external_temperature_override": restored})

    await _add_and_remove(entity)

    assert entity._external_temperature_override is None
    assert device._external_temperature_override is None
