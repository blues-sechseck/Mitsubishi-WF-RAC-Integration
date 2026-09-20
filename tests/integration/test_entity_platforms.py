"""Current entity-platform behaviour pinned to parsed live device state."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockEntityPlatform
from pywfrac import AIRFLOW_UNKNOWN, AirconCommands, HomeLeaveModeSetting

from custom_components.mitsubishi_wf_rac import (
    binary_sensor,
    button,
    climate,
    number,
    select,
    sensor,
    switch,
    update,
)
from custom_components.mitsubishi_wf_rac.const import (
    ATTR_COMPRESSOR_FREQUENCY_RAW,
    ATTR_DISCHARGE_SUPERHEAT_RAW,
    ATTR_HOT_GAS_TEMP_RAW,
    ATTR_INDOOR_COIL_OUTLET_RAW,
    ATTR_INDOOR_COIL_RAW,
    ATTR_OPERATING_CURRENT_RAW,
    ATTR_OUTDOOR_COIL_RAW,
    ATTR_PROTECTION_RAW,
    DOMAIN,
    FAN_MODE_TRANSLATION,
    HVAC_TRANSLATION,
    SWING_3D_AUTO,
    SWING_HORIZONTAL_MODE_TRANSLATION,
    SWING_MODE_TRANSLATION,
)
from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from tests.unit.live_captures import LIVE_CAPTURES


def _entry(device, options=None):
    return MagicMock(
        runtime_data=MagicMock(device=device),
        options=device.config_entry.options if options is None else options,
    )


async def _entities(setup, hass, entry):
    added = []
    await setup(hass, entry, added.extend)
    return added


def _attached(hass, entity, entity_id):
    """Give a directly constructed entity the context async_write_ha_state() needs.

    Entities built in a test rather than through a platform have no platform
    data, and writing state resolves the translated name through it.
    """
    entity.hass = hass
    entity.platform = MockEntityPlatform(hass)
    entity.entity_id = entity_id
    return entity


@pytest.mark.parametrize(
    "module", [binary_sensor, button, climate, number, select, sensor, switch, update]
)
def test_no_platform_serializes_on_top_of_the_coordinator(module):
    """Zero everywhere, including the platforms that write.

    The serialisation the module needs (one connection, a second between
    requests) lives in the coordinator's send lock, not in a platform
    semaphore. A semaphore on top only keeps actions issued together out of
    the same consolidation window - and those are the ones worth merging into
    a single frame.
    """
    assert module.PARALLEL_UPDATES == 0


async def test_platform_entity_composition_and_metadata(hass, platform_device):
    """All optional entities retain their current default-enabled/category contract."""
    platform_device.airco.Capabilities = replace(
        platform_device.airco.Capabilities, vacant_property=True, home_leave_mode=True
    )
    platform_device.airco.Electric = 1.2
    entry = _entry(platform_device)

    entities = []
    for setup in (
        binary_sensor.async_setup_entry,
        button.async_setup_entry,
        climate.async_setup_entry,
        number.async_setup_entry,
        select.async_setup_entry,
        sensor.async_setup_entry,
        update.async_setup_entry,
    ):
        entities.extend(await _entities(setup, hass, entry))

    details = {
        entity.unique_id: (
            entity.entity_registry_enabled_default,
            entity.entity_category,
        )
        for entity in entities
    }
    assert details[f"{DOMAIN}-airco-id-energy-sensor"] == (True, None)
    assert details[f"{DOMAIN}-airco-id-energy-total-sensor"] == (True, None)
    assert details[f"{DOMAIN}-airco-id-reset-energy-total"] == (
        True,
        EntityCategory.CONFIG,
    )
    assert details[f"{DOMAIN}-airco-id-problem"] == (True, EntityCategory.DIAGNOSTIC)
    assert details[f"{DOMAIN}-airco-id-compressor"] == (True, None)
    assert details[f"{DOMAIN}-airco-id-occupancy"] == (True, None)
    assert details[f"{DOMAIN}-airco-id-external-control"] == (
        True,
        EntityCategory.DIAGNOSTIC,
    )
    assert details[f"{DOMAIN}-airco-id-home-leave-cooling-temp_rule-number"] == (
        False,
        None,
    )
    assert details[f"{DOMAIN}-airco-id-home-leave-heating-air-flow-select"] == (
        False,
        None,
    )
    operation_data_sensors = [
        entity for entity in entities if isinstance(entity, sensor.ServiceDataSensor)
    ]
    assert len(operation_data_sensors) == 15
    assert all(
        entity.entity_registry_enabled_default is False
        for entity in operation_data_sensors
    )
    assert all(
        entity.entity_category is EntityCategory.DIAGNOSTIC
        for entity in operation_data_sensors
    )
    for raw_type in (
        ATTR_COMPRESSOR_FREQUENCY_RAW,
        ATTR_OPERATING_CURRENT_RAW,
        ATTR_HOT_GAS_TEMP_RAW,
        ATTR_INDOOR_COIL_RAW,
        ATTR_INDOOR_COIL_OUTLET_RAW,
        ATTR_OUTDOOR_COIL_RAW,
        ATTR_DISCHARGE_SUPERHEAT_RAW,
        ATTR_PROTECTION_RAW,
    ):
        raw_sensor = next(
            entity
            for entity in operation_data_sensors
            if entity.unique_id == f"{DOMAIN}-airco-id-{raw_type}-sensor"
        )
        assert raw_sensor.device_class is None
        assert raw_sensor.native_unit_of_measurement is None
    assert details[f"{DOMAIN}-airco-id-airco_id-sensor"] == (
        False,
        EntityCategory.DIAGNOSTIC,
    )
    assert details[f"{DOMAIN}-airco-id-operator_id-sensor"] == (
        False,
        EntityCategory.DIAGNOSTIC,
    )
    assert details[f"{DOMAIN}-airco-id-account_expires-sensor"] == (
        False,
        EntityCategory.DIAGNOSTIC,
    )
    assert details[f"{DOMAIN}-airco-id-error-sensor"] == (
        True,
        EntityCategory.DIAGNOSTIC,
    )
    assert details[f"{DOMAIN}-airco-id-updated_by-sensor"] == (
        True,
        EntityCategory.DIAGNOSTIC,
    )
    assert details[f"{DOMAIN}-airco-id-auto_heating-sensor"] == (
        True,
        EntityCategory.DIAGNOSTIC,
    )
    assert details[f"{DOMAIN}-airco-id-target_temperature-sensor"] == (False, None)


async def test_platform_option_and_capability_gates(hass, platform_device):
    entry = _entry(platform_device)
    platform_device.airco.Electric = None
    platform_device.airco.Capabilities = replace(
        platform_device.airco.Capabilities, vacant_property=False, home_leave_mode=False
    )
    platform_device._swing_selects_enabled_default = False
    assert await _entities(binary_sensor.async_setup_entry, hass, entry)
    assert await _entities(button.async_setup_entry, hass, entry) == []
    assert await _entities(number.async_setup_entry, hass, entry) == []
    swing_entities = await _entities(select.async_setup_entry, hass, entry)
    assert [entity.entity_registry_enabled_default for entity in swing_entities] == [
        False,
        False,
        False,
    ]
    assert await _entities(switch.async_setup_entry, hass, entry) == []

    platform_device._firmware_update_check_enabled = False
    assert await _entities(update.async_setup_entry, hass, entry) == []
    platform_device._firmware_update_check_enabled = True
    firmware_entities = await _entities(update.async_setup_entry, hass, entry)
    assert [entity.unique_id for entity in firmware_entities] == [
        f"{DOMAIN}-airco-id-firmware-update"
    ]


@pytest.mark.parametrize(
    ("helper", "unique_ids"),
    [
        (
            sensor._async_remove_home_leave_mode_sensors,
            [
                f"{DOMAIN}-airco-id-home-leave-{mode}-{slug}-sensor"
                for mode in ("cooling", "heating")
                for slug in ("temp_rule", "temp_setting", "air_flow")
            ],
        ),
        (switch._async_remove_self_clean_switch, [f"{DOMAIN}-airco-id-self-clean"]),
        (
            switch._async_remove_home_leave_mode_switch,
            [f"{DOMAIN}-airco-id-home-leave-mode"],
        ),
    ],
)
async def test_registry_cleanup_removes_only_named_entities(
    hass, platform_device, helper, unique_ids
):
    registry = er.async_get(hass)
    expected = [
        registry.async_get_or_create(
            "sensor" if "sensor" in uid else "switch", DOMAIN, uid
        ).entity_id
        for uid in unique_ids
    ]
    survivor = registry.async_get_or_create("sensor", DOMAIN, "unrelated").entity_id
    helper(hass, platform_device)
    assert all(registry.async_get(entity_id) is None for entity_id in expected)
    assert registry.async_get(survivor) is not None


async def test_external_control_sensor_follows_the_backoff(platform_device):
    """The only thing a user would otherwise see while we hold back is the
    operation-data sensors going unknown, with no stated reason (#294).
    """
    entity = binary_sensor.ExternalControlBinarySensor(platform_device)
    assert entity.is_on is False

    platform_device.foreign_writers._until = dt_util.utcnow() + timedelta(minutes=3)
    entity._update_state()
    assert entity.is_on is True

    platform_device.foreign_writers._until = dt_util.utcnow() - timedelta(seconds=1)
    entity._update_state()
    assert entity.is_on is False


async def test_external_temperature_active_sensor_shows_when_it_took_effect(
    platform_device,
):
    """Arming is not the same as in effect: nothing is written while the unit
    is off, and after a restart the value waits for the next frame. Both used
    to be invisible, which is what sent people to the README."""
    entity = binary_sensor.ExternalTemperatureActiveBinarySensor(platform_device)
    assert entity.is_on is False

    platform_device.set_external_temperature_override(21.0)
    entity._update_state()
    # Armed, but no frame has carried it yet.
    assert entity.is_on is False

    raw = round(21.0 * 4) + 59
    platform_device.external_temperature._written.append(raw)
    platform_device.airco.ControllerRoomTempRaw = raw
    entity._update_state()
    assert entity.is_on is True

    platform_device.set_external_temperature_override(None)
    entity._update_state()
    assert entity.is_on is False


@pytest.mark.parametrize(
    ("capture", "mode", "action"),
    [
        ("off", HVACMode.OFF, HVACAction.OFF),
        ("on_cool", HVACMode.COOL, HVACAction.IDLE),
        ("on_heat", HVACMode.HEAT, HVACAction.IDLE),
        ("on_fan_only", HVACMode.FAN_ONLY, HVACAction.FAN),
        ("on_dry", HVACMode.DRY, HVACAction.DRYING),
    ],
)
async def test_climate_maps_live_states(platform_device, capture, mode, action):
    platform_device._api.get_aircon_stats.return_value = {
        "numOfAccount": 1,
        "airconStat": LIVE_CAPTURES[capture][0],
    }
    await platform_device.update()
    entity = climate.AircoClimate(platform_device)
    assert entity.hvac_mode == mode
    assert entity.hvac_action == action


async def test_climate_maps_commands_both_directions(platform_device):
    platform_device.async_queue_command = AsyncMock()
    entity = climate.AircoClimate(platform_device)
    await entity.async_set_hvac_mode(HVACMode.OFF)
    assert (
        platform_device.async_queue_command.await_args.args[0][AirconCommands.Operation]
        is False
    )
    await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert platform_device.async_queue_command.await_args.args[0] == {
        AirconCommands.OperationMode: HVAC_TRANSLATION[HVACMode.HEAT],
        AirconCommands.Operation: True,
    }
    await entity.async_set_swing_mode(SWING_3D_AUTO)
    assert platform_device.async_queue_command.await_args.args[0] == {
        AirconCommands.Entrust: True
    }


async def test_climate_commands_and_state_branches(platform_device):
    platform_device.async_queue_command = AsyncMock()
    entity = climate.AircoClimate(platform_device)
    vertical = next(mode for mode in SWING_MODE_TRANSLATION if mode != SWING_3D_AUTO)
    horizontal = next(
        mode for mode in SWING_HORIZONTAL_MODE_TRANSLATION if mode != SWING_3D_AUTO
    )
    fan_mode = next(iter(FAN_MODE_TRANSLATION))

    await entity.async_set_temperature(temperature=22, hvac_mode=HVACMode.COOL)
    assert (
        platform_device.async_queue_command.await_args.args[0][AirconCommands.Operation]
        is True
    )
    await entity.async_set_fan_mode(fan_mode)
    assert platform_device.async_queue_command.await_args.args[0] == {
        AirconCommands.AirFlow: FAN_MODE_TRANSLATION[fan_mode]
    }
    await entity.async_turn_on()
    assert platform_device.async_queue_command.await_args.args[0] == {
        AirconCommands.Operation: True
    }
    await entity.async_turn_off()
    assert platform_device.async_queue_command.await_args.args[0] == {
        AirconCommands.Operation: False
    }
    await entity.async_set_swing_mode(vertical)
    assert (
        platform_device.async_queue_command.await_args.args[0][AirconCommands.Entrust]
        is False
    )
    await entity.async_set_swing_horizontal_mode(SWING_3D_AUTO)
    assert platform_device.async_queue_command.await_args.args[0] == {
        AirconCommands.Entrust: True
    }
    await entity.async_set_swing_horizontal_mode(horizontal)
    assert (
        platform_device.async_queue_command.await_args.args[0][AirconCommands.Entrust]
        is False
    )

    with pytest.raises(ServiceValidationError) as below:
        await entity.async_set_temperature(temperature=9, hvac_mode=HVACMode.HEAT)
    with pytest.raises(ServiceValidationError) as above:
        await entity.async_set_temperature(temperature=34, hvac_mode=HVACMode.COOL)

    # Asserted on the key, not on the rendered text: the message the user
    # reads comes from strings.json, and matching the English wording here
    # would pass just as well if the key were wrong.
    assert below.value.translation_key == "temperature_below_minimum"
    assert above.value.translation_key == "temperature_above_maximum"
    # generate_message, not just the key: HomeAssistantError only renders
    # the translation when it is constructed without a message of its own,
    # and the key alone is set either way.
    assert below.value.generate_message is True
    assert above.value.generate_message is True
    # The message names the mode the range came from - without the
    # placeholder it renders as a literal {hvac_mode} and the one useful part
    # of the message is gone.
    assert below.value.translation_placeholders["hvac_mode"] == HVACMode.HEAT
    assert above.value.translation_placeholders["hvac_mode"] == HVACMode.COOL

    # A setpoint set while the unit is off belongs to the mode that comes
    # next, so it is held to what the unit accepts anywhere rather than to the
    # 18C floor of a mode nobody asked for (#317).
    entity._attr_hvac_mode = HVACMode.OFF
    await entity.async_set_temperature(temperature=16)
    assert (
        platform_device.async_queue_command.await_args.args[0][
            AirconCommands.PresetTemp
        ]
        == 16
    )
    # What is advertised does not follow the running mode: climate measures a
    # call against min_temp/max_temp before this entity sees hvac_mode, so a
    # range that moved with the mode would reject a setpoint the same call
    # switches into a mode that allows it. It is the union over every mode,
    # widened to the away setpoints this unit offers.
    assert (entity.min_temp, entity.max_temp) == (10, 31)
    entity._attr_hvac_mode = HVACMode.FAN_ONLY
    assert (entity.min_temp, entity.max_temp) == (10, 31)
    entity._attr_hvac_mode = HVACMode.HEAT
    assert (entity.min_temp, entity.max_temp) == (10, 31)
    # The mode's own range still bites inside the call.
    with pytest.raises(ServiceValidationError) as too_low:
        await entity.async_set_temperature(temperature=16)
    assert too_low.value.translation_key == "temperature_below_minimum"
    assert too_low.value.generate_message is True
    entity._attr_hvac_mode = HVACMode.COOL

    platform_device.airco.Operation = True
    platform_device.airco.CompressorRunning = True
    for mode, expected in (
        (0, HVACAction.COOLING),
        (1, HVACAction.COOLING),
        (2, HVACAction.HEATING),
        (3, HVACAction.FAN),
        (4, HVACAction.DRYING),
    ):
        platform_device.airco.OperationMode = mode
        platform_device.airco.CoolHotJudge = False
        entity._update_state()
        assert entity.hvac_action == expected
    platform_device.airco.OperationMode = 0
    platform_device.airco.CoolHotJudge = True
    entity._update_state()
    assert entity.hvac_action == HVACAction.HEATING

    platform_device.airco.Capabilities = replace(
        platform_device.airco.Capabilities, home_leave_mode=False
    )
    with pytest.raises(ServiceValidationError) as unsupported:
        await entity.async_request_home_leave_mode_status()
    assert unsupported.value.translation_key == "home_leave_mode_not_supported"
    assert unsupported.value.generate_message is True
    platform_device.airco.Capabilities = replace(
        platform_device.airco.Capabilities, home_leave_mode=True
    )
    platform_device.async_request_home_leave_mode_status = AsyncMock()
    platform_device.async_set_home_leave_mode = AsyncMock()
    await entity.async_request_home_leave_mode_status()
    await entity.async_set_home_leave_mode(12, 31, 0, 13, 10, 1)
    assert platform_device.async_request_home_leave_mode_status.await_count == 1
    assert platform_device.async_set_home_leave_mode.await_args.args[0].TempRule == 12
    assert entity._min_temp_for_mode(HVACMode.HEAT) == 18
    platform_device.airco.Capabilities = replace(
        platform_device.airco.Capabilities, preset_temp_range_2=True
    )
    assert (
        entity._min_temp_for_mode(HVACMode.HEAT),
        entity._max_temp_for_mode(HVACMode.COOL),
    ) == (10, 33)


async def test_select_maps_current_state_and_commands(platform_device):
    platform_device.async_queue_command = AsyncMock()
    platform_device.airco.OperationMode = HVAC_TRANSLATION[HVACMode.COOL]
    platform_device.airco.Vacant = True
    away = select.HomeLeaveModeSelect(platform_device)
    assert away.current_option == select.HOME_LEAVE_MODE_AWAY_COOL
    await away.async_select_option(select.HOME_LEAVE_MODE_OFF)
    assert platform_device.async_queue_command.await_args.args[0] == {
        AirconCommands.PresetTemp: select.NORMAL_TEMP
    }


async def test_select_command_and_state_branches(hass, platform_device):
    platform_device.async_queue_command = AsyncMock()
    vertical = select.VerticalSwingSelect(platform_device)
    horizontal = select.HorizontalSwingSelect(platform_device)
    fan = select.FanSpeedSelect(platform_device)
    vertical_option = next(
        mode for mode in SWING_MODE_TRANSLATION if mode != SWING_3D_AUTO
    )
    horizontal_option = next(
        mode for mode in SWING_HORIZONTAL_MODE_TRANSLATION if mode != SWING_3D_AUTO
    )
    fan_option = next(iter(FAN_MODE_TRANSLATION))
    await vertical.async_select_option(vertical_option)
    assert vertical.current_option == vertical_option
    await horizontal.async_select_option(horizontal_option)
    assert horizontal.current_option == horizontal_option
    await fan.async_select_option(fan_option)
    assert platform_device.async_queue_command.await_args.args[0] == {
        AirconCommands.AirFlow: FAN_MODE_TRANSLATION[fan_option]
    }

    away = select.HomeLeaveModeSelect(platform_device)
    await away.async_select_option(select.HOME_LEAVE_MODE_AWAY_HEAT)
    assert (
        platform_device.async_queue_command.await_args.args[0][
            AirconCommands.PresetTemp
        ]
        == select.HOME_LEAVE_TEMP_HEAT
    )
    await away.async_select_option(select.HOME_LEAVE_MODE_AWAY_COOL)
    assert (
        platform_device.async_queue_command.await_args.args[0][
            AirconCommands.PresetTemp
        ]
        == select.HOME_LEAVE_TEMP_COOL
    )
    platform_device.airco.Vacant = True
    platform_device.airco.OperationMode = HVAC_TRANSLATION[HVACMode.HEAT]
    away._update_state()
    assert away.current_option == select.HOME_LEAVE_MODE_AWAY_HEAT
    platform_device.airco.OperationMode = HVAC_TRANSLATION[HVACMode.FAN_ONLY]
    away._update_state()
    assert away.current_option == select.HOME_LEAVE_MODE_OFF

    airflow = _attached(
        hass,
        select.HomeLeaveAirFlowSelect(platform_device, "heating"),
        "select.home_leave_heating_air_flow",
    )
    with pytest.raises(HomeAssistantError) as unknown:
        await airflow.async_select_option("2")
    assert unknown.value.translation_key == "home_leave_mode_status_unknown"
    assert unknown.value.generate_message is True
    platform_device.airco.HomeLeaveModeForCooling = HomeLeaveModeSetting(12, 31, 0)
    platform_device.airco.HomeLeaveModeForHeating = HomeLeaveModeSetting(13, 10, 1)
    platform_device.async_set_home_leave_mode = AsyncMock()
    await airflow.async_select_option("3")
    cooling, heating = platform_device.async_set_home_leave_mode.await_args.args
    assert (cooling.AirFlow, heating.AirFlow) == (0, 3)
    cooling_airflow = _attached(
        hass,
        select.HomeLeaveAirFlowSelect(platform_device, "cooling"),
        "select.home_leave_cooling_air_flow",
    )
    await cooling_airflow.async_select_option("4")
    cooling, heating = platform_device.async_set_home_leave_mode.await_args.args
    # The untouched side is carried over from what the unit currently reports,
    # not from the previous call: async_set_home_leave_mode is mocked here, so
    # airco still holds the heating AirFlow of 1 set above.
    assert (cooling.AirFlow, heating.AirFlow) == (4, 1)


@pytest.mark.parametrize(
    ("mode", "attribute", "expected"),
    [
        pytest.param("cooling", "TempRule", (15, 13), id="cooling_temp_rule"),
        pytest.param("heating", "TempRule", (12, 15), id="heating_temp_rule"),
        pytest.param("cooling", "TempSetting", (15, 10), id="cooling_temp_setting"),
        pytest.param("heating", "TempSetting", (31, 15), id="heating_temp_setting"),
    ],
)
async def test_home_leave_controls_require_known_settings_and_preserve_other_side(
    hass, platform_device, mode, attribute, expected
):
    """Each control writes its own field and carries the other three over.

    The unit takes both directions in one frame, so the three fields this
    control does not own have to be sent back unchanged - the value it writes
    is the only difference between what came in and what goes out.
    """
    platform_device.async_set_home_leave_mode = AsyncMock()
    control = _attached(
        hass,
        number.HomeLeaveModeNumber(platform_device, mode, attribute),
        f"number.home_leave_{mode}_{attribute.lower()}",
    )
    with pytest.raises(HomeAssistantError) as unknown:
        await control.async_set_native_value(15)
    assert unknown.value.translation_key == "home_leave_mode_status_unknown"
    assert unknown.value.generate_message is True
    platform_device.airco.HomeLeaveModeForCooling = HomeLeaveModeSetting(12, 31, 0)
    platform_device.airco.HomeLeaveModeForHeating = HomeLeaveModeSetting(13, 10, 1)
    await control.async_set_native_value(15)
    cooling, heating = platform_device.async_set_home_leave_mode.await_args.args
    assert (getattr(cooling, attribute), getattr(heating, attribute)) == expected


@pytest.mark.parametrize(
    ("available", "latest"), [(True, "2.0"), (False, "1.0"), (False, None)]
)
async def test_update_version_states(platform_device, available, latest):
    platform_device._wireless_firmware_ver = "1.0"
    platform_device._latest_wireless_firmware_ver = latest
    platform_device._firmware_update_available = available
    entity = update.FirmwareUpdateEntity(platform_device)
    assert entity.installed_version == "1.0"
    assert entity.latest_version == (latest if available else "1.0")


async def test_fan_speed_select_recognises_an_unreadable_fan_step(platform_device):
    """Same marker as the climate entity's fan mode, same first-read guard.

    FanSpeedSelect indexes FAN_MODE_TRANSLATION from its own __init__, so an
    AIRFLOW_UNKNOWN nibble used to take the whole select platform with it.
    """
    platform_device.airco.AirFlow = AIRFLOW_UNKNOWN
    platform_device._record_reachable()

    fan = select.FanSpeedSelect(platform_device)

    assert fan.current_option is None
    assert platform_device.last_update_success is True


async def test_home_leave_air_flow_select_recognises_an_unreadable_step(
    platform_device,
):
    """Same marker as the fan speed select, one decode path further in."""
    platform_device.airco.HomeLeaveModeForCooling = HomeLeaveModeSetting(
        TempRule=35.0, TempSetting=33.0, AirFlow=AIRFLOW_UNKNOWN
    )
    platform_device._record_reachable()

    entity = select.HomeLeaveAirFlowSelect(platform_device, "cooling")

    assert entity.current_option is None
    assert platform_device.last_update_success is True


async def test_marking_the_climate_state_unknown_clears_all_of_it(platform_device):
    """state: unknown next to a fresh temperature and a stale action is worse
    than either. _update_state() writes the setpoint and the room reading
    before it reaches a field that can fail, so the hook has to undo the lot.
    """
    entity = climate.AircoClimate(platform_device)
    assert entity.hvac_action is not None
    assert entity.target_temperature is not None

    entity._mark_state_unknown()

    assert entity.hvac_mode is None
    assert entity.hvac_action is None
    assert entity.target_temperature is None
    assert entity.current_temperature is None
    assert entity.fan_mode is None
    assert entity.swing_mode is None
    assert entity.swing_horizontal_mode is None
    assert entity.preset_mode is None


async def test_marking_the_problem_state_unknown_drops_the_error_code(platform_device):
    """The code is part of the state. Left behind, state_attr() keeps
    answering with the last fault while the entity says it knows nothing."""
    platform_device.airco.ErrorCode = "E7"
    entity = binary_sensor.ProblemBinarySensor(platform_device)
    assert entity.extra_state_attributes["error_code"] == "E7"

    entity._mark_state_unknown()

    assert entity.is_on is None
    assert not entity.extra_state_attributes


@pytest.mark.parametrize(
    ("cls", "field"),
    [
        (select.HorizontalSwingSelect, "WindDirectionLR"),
        (select.VerticalSwingSelect, "WindDirectionUD"),
    ],
)
async def test_a_swing_select_survives_a_vane_value_it_cannot_read(
    platform_device, cls, field
):
    """Both used to decode in __init__ instead of going through
    _apply_state(), so one unreadable vane byte took the whole platform down
    and the entry came up with no selects at all."""
    setattr(platform_device.airco, field, 99)
    platform_device.airco.Entrust = False

    entity = cls(platform_device)

    assert entity.current_option is None
    assert platform_device.last_update_success is True


async def test_the_climate_entity_is_the_device_itself(platform_device):
    """It carries the device's name and adds nothing of its own.

    Every other platform here is already built this way. Climate used to copy
    device_name into _attr_name instead, which displayed the same string but
    only picked up a rename at the next reload.
    """
    entity = climate.AircoClimate(platform_device)

    assert entity.has_entity_name is True
    assert entity.name is None
