"""Units that fill only part of the state block (#431).

Their model, power/mode, fan and setpoint bytes are there; the regulating
temperature (content[5]), CoolHotJudge, the compressor flag and the rest stay
zero. They also apply the request that only asks, never answer operation data
and ignore an injected room temperature - so none of the machinery built on
those is started for them.
"""

import asyncio
import base64
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pywfrac import SERVICE_DATA_COMPRESSOR_FREQ

from custom_components.mitsubishi_wf_rac import (
    binary_sensor,
    climate,
    coordinator as coordinator_module,
    number,
    select,
    sensor,
    service_data as service_data_module,
    switch,
)
from custom_components.mitsubishi_wf_rac.const import (
    CONF_EXTERNAL_TEMPERATURE_SOURCE,
    CONF_OVERSHOOT_COOL,
    CONF_STATUS_REQUEST_MODE,
    DOMAIN,
    SERVICE_DATA_SENSOR_TYPES,
    STATUS_REQUEST_ECHO,
    STATUS_REQUEST_STOPPED,
    STATUS_REQUEST_STRICT,
)
from custom_components.mitsubishi_wf_rac.coordinator import Device
from custom_components.mitsubishi_wf_rac.reduced_status import (
    external_temperature_unsupported_issue_id,
    unsupported_entity_unique_ids,
)
from custom_components.mitsubishi_wf_rac.sensor import ServiceDataSensor
from homeassistant.components.climate.const import HVACAction
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from tests.unit.live_captures import LIVE_CAPTURES

ON_HEAT_PAYLOAD, _ = LIVE_CAPTURES["on_heat"]
ON_COOL_PAYLOAD, _ = LIVE_CAPTURES["on_cool"]
SOURCE = "sensor.living_room_temperature"
AIRCO_ID = "a"


def _content_of(payload: str) -> list[int]:
    raw = [(256 - b) * (-1) if b > 127 else b for b in base64.b64decode(payload)]
    start = raw[18] * 4 + 21
    return raw[start : start + 18]


def _build(content: list[int]) -> str:
    """Wrap 18 state bytes into a parseable airconStat (start_length 21)."""
    assert len(content) == 18
    raw = bytes((b & 0xFF) for b in [*([0] * 21), *content, 0, 0])
    return base64.b64encode(raw).decode()


def _reduced_payload(*, heating: bool = True) -> str:
    """A running ZT unit that reports only what such a unit reports."""
    content = _content_of(ON_HEAT_PAYLOAD if heating else ON_COOL_PAYLOAD)
    content[0] = 3
    for index in (5, 8, 9, 10, 11, 12, 15):
        content[index] = 0
    return _build(content)


REDUCED_PAYLOAD = _reduced_payload()


def _stats(payload: str) -> dict:
    return {"numOfAccount": 1, "airconStat": payload, "updatedBy": "local"}


async def _device(
    hass: HomeAssistant,
    payload: str,
    *,
    options: dict | None = None,
    mode: str = STATUS_REQUEST_STRICT,
) -> Device:
    entry = MockConfigEntry(domain=DOMAIN, options=options or {})
    entry.add_to_hass(hass)
    device = Device(
        hass,
        entry,
        "Test AC",
        "127.0.0.1",
        51443,
        "device-id",
        "operator-id",
        "airco-id",
        swing_selects_enabled_default=True,
        status_request_mode=mode,
    )
    device._api = AsyncMock()
    device._api.get_aircon_stats.return_value = _stats(payload)
    assert await device.update()
    return device


@pytest.fixture
async def reduced(hass):
    device = await _device(hass, REDUCED_PAYLOAD)
    yield device
    await device.async_shutdown()


# --- detection --------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(LIVE_CAPTURES))
async def test_a_normal_unit_is_not_reduced(hass, name):
    """content[5] is the regulating temperature on every other unit, off too."""
    device = await _device(hass, LIVE_CAPTURES[name][0])
    assert device.reduced_status_block is False
    await device.async_shutdown()


async def test_a_reduced_block_is_detected(reduced):
    assert reduced.airco.ControllerRoomTempRaw == 0
    assert reduced.reduced_status_block is True


async def test_an_all_zero_block_is_a_hiccup_not_a_unit(hass):
    device = await _device(hass, _build([0] * 18))
    assert device.reduced_status_block is False
    await device.async_shutdown()


async def test_a_block_without_a_setpoint_is_not_reduced(hass):
    content = _content_of(REDUCED_PAYLOAD)
    content[4] = 0
    device = await _device(hass, _build(content))
    assert device.reduced_status_block is False
    await device.async_shutdown()


async def test_the_decision_is_not_revisited_during_the_run(reduced):
    reduced._api.get_aircon_stats.return_value = _stats(ON_HEAT_PAYLOAD)
    await reduced.update()
    assert reduced.airco.ControllerRoomTempRaw != 0
    assert reduced.reduced_status_block is True


async def test_a_reduced_unit_is_put_on_the_carrying_request_in_memory_only(reduced):
    assert reduced.status_request_mode == STATUS_REQUEST_ECHO
    assert reduced._parser.status_request_carries_state is True
    assert CONF_STATUS_REQUEST_MODE not in reduced.config_entry.data


async def test_a_stopped_reduced_unit_stays_stopped(hass):
    device = await _device(hass, REDUCED_PAYLOAD, mode=STATUS_REQUEST_STOPPED)
    assert device.reduced_status_block is True
    assert device.status_request_mode == STATUS_REQUEST_STOPPED
    assert device._parser.status_request_carries_state is False
    await device.async_shutdown()


# --- no operation data --------------------------------------------------------


async def test_no_operation_data_request_is_ever_sent(reduced, monkeypatch):
    monkeypatch.setattr(
        reduced, "async_contexts", lambda: {SERVICE_DATA_COMPRESSOR_FREQ}
    )
    monkeypatch.setattr(
        service_data_module, "SERVICE_DATA_REQUEST_OFFSET", timedelta(milliseconds=1)
    )
    monkeypatch.setattr(service_data_module, "SERVICE_DATA_MIN_SPACING", timedelta(0))
    reduced._api.send_airco_command = AsyncMock()

    reduced.maybe_request_service_data()
    await asyncio.sleep(0.05)

    assert reduced.service_data.task is None
    reduced._api.send_airco_command.assert_not_awaited()


async def test_home_leave_status_is_not_read_at_setup(reduced):
    reduced.airco.Capabilities = replace(
        reduced.airco.Capabilities, home_leave_mode=True
    )
    reduced._api.send_airco_command = AsyncMock()

    await reduced.async_read_home_leave_mode_once()

    reduced._api.send_airco_command.assert_not_awaited()


async def test_the_actions_that_need_operation_data_say_so(reduced):
    reduced._api.send_airco_command = AsyncMock()

    for call in (
        reduced.async_request_home_leave_mode_status(),
        reduced.async_set_silent_operation(True),
    ):
        with pytest.raises(HomeAssistantError) as err:
            await call
        assert err.value.translation_key == "reduced_status_not_supported"

    reduced._api.send_airco_command.assert_not_awaited()


# --- no room temperature ------------------------------------------------------


async def test_the_external_temperature_is_never_armed_or_sent(hass, monkeypatch):
    device = await _device(
        hass, REDUCED_PAYLOAD, options={CONF_EXTERNAL_TEMPERATURE_SOURCE: SOURCE}
    )
    device._api.send_airco_command = AsyncMock()
    feed = device.external_temperature

    device.set_external_temperature_override(21.5)
    device.request_external_temperature_release()
    await asyncio.sleep(0.05)

    assert feed.source_configured is True
    assert device.external_temperature_override is None
    assert feed.needs_frame is False
    assert feed.may_hold_unit is False
    assert feed.wire_value is None
    assert device.external_temperature_room_value is None
    device._api.send_airco_command.assert_not_awaited()
    await device.async_shutdown()


async def test_handing_back_on_a_reduced_unit_sends_nothing_even_when_forced(reduced):
    reduced._api.send_airco_command = AsyncMock()

    assert await reduced.async_hand_back_room_sensor(force=True) is False

    reduced._api.send_airco_command.assert_not_awaited()


# --- the climate card -----------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (1, HVACAction.COOLING),
        (2, HVACAction.HEATING),
        (0, None),
        (3, HVACAction.FAN),
        (4, HVACAction.DRYING),
    ],
)
async def test_hvac_action_follows_the_mode_on_a_reduced_unit(
    hass, reduced, mode, expected
):
    """The compressor flag is always off there, which read as idle while heating."""
    added: list = []
    await climate.async_setup_entry(
        hass, MagicMock(runtime_data=MagicMock(device=reduced)), added.extend
    )
    (entity,) = added
    assert reduced.airco.CompressorRunning is False

    reduced.airco.OperationMode = mode

    assert entity._determine_hvac_action(reduced.airco) == expected


async def test_a_normal_unit_still_reads_the_compressor_flag(hass):
    device = await _device(hass, ON_HEAT_PAYLOAD)
    added: list = []
    await climate.async_setup_entry(
        hass, MagicMock(runtime_data=MagicMock(device=device)), added.extend
    )
    (entity,) = added

    assert entity._determine_hvac_action(device.airco) == HVACAction.IDLE
    await device.async_shutdown()


# --- the central applied-request check ----------------------------------------


def _cleared_payload() -> str:
    """What an applied all-zero command block leaves behind (#329)."""
    content = _content_of(ON_COOL_PAYLOAD)
    content[2] = 0
    content[3] = 0
    content[4] = 20
    content[11] = 0
    content[12] = 0
    return _build(content)


CLEARED_PAYLOAD = _cleared_payload()


async def test_a_silent_operation_write_that_clears_the_unit_is_detected(
    hass, monkeypatch
):
    monkeypatch.setattr(coordinator_module, "SILENT_OPERATION_READBACK_DELAY", 0)
    device = await _device(hass, ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(return_value=CLEARED_PAYLOAD)

    with pytest.raises(HomeAssistantError):
        await device.async_set_silent_operation(True)
    await hass.async_block_till_done()

    assert device.status_request_mode == STATUS_REQUEST_ECHO
    assert device.config_entry.data[CONF_STATUS_REQUEST_MODE] == STATUS_REQUEST_ECHO
    await device.async_shutdown()


async def test_a_hand_back_frame_that_clears_the_unit_is_detected(hass):
    device = await _device(hass, ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(return_value=CLEARED_PAYLOAD)

    assert await device.async_hand_back_room_sensor(force=True) is True
    await hass.async_block_till_done()

    assert device.status_request_mode == STATUS_REQUEST_ECHO
    assert device.config_entry.data[CONF_STATUS_REQUEST_MODE] == STATUS_REQUEST_ECHO
    await device.async_shutdown()


async def test_an_ordinary_command_is_not_watched_as_a_status_request(hass):
    device = await _device(hass, ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(return_value=CLEARED_PAYLOAD)

    await device.set_airco({})
    await hass.async_block_till_done()

    assert device.status_request_mode == STATUS_REQUEST_STRICT
    await device.async_shutdown()


# --- setup, entities and repairs --------------------------------------------


def _entry(options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=8,
        minor_version=2,
        unique_id=AIRCO_ID,
        data={
            "name": "AC",
            CONF_HOST: "127.0.0.1",
            "device_id": "d",
            "operator_id": "o",
            "airco_id": AIRCO_ID,
            "port": 51443,
        },
        options=options or {},
    )


def _module(payload: str) -> AsyncMock:
    api = AsyncMock()
    api.get_aircon_stats.return_value = _stats(payload)
    api.send_airco_command.return_value = payload
    api.method = "https"
    return api


async def _set_up(hass: HomeAssistant, entry: MockConfigEntry, api: AsyncMock) -> None:
    if entry.entry_id not in hass.config_entries._entries:
        entry.add_to_hass(hass)
    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=api,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def _issue(hass: HomeAssistant, entry: MockConfigEntry):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, external_temperature_unsupported_issue_id(entry.entry_id)
    )


def _stale_entries(
    hass: HomeAssistant, entry: MockConfigEntry, *, leave: str = ""
) -> list[str]:
    """Register what an earlier run of the integration left behind."""
    registry = er.async_get(hass)
    entity_ids = []
    for domain, unique_ids in unsupported_entity_unique_ids(AIRCO_ID).items():
        for unique_id in unique_ids:
            if unique_id == leave:
                continue
            created = registry.async_get_or_create(
                domain, DOMAIN, unique_id, config_entry=entry
            )
            entity_ids.append(created.entity_id)
    return entity_ids


async def test_setup_on_a_reduced_unit(hass: HomeAssistant):
    entry = _entry({CONF_EXTERNAL_TEMPERATURE_SOURCE: SOURCE})
    entry.add_to_hass(hass)
    # An entity of another entry is not this entry's to remove.
    other = _entry()
    other.disabled_by = ConfigEntryDisabler.USER
    other.add_to_hass(hass)
    foreign_id = f"{DOMAIN}-{AIRCO_ID}-silent-operation"
    foreign = er.async_get(hass).async_get_or_create(
        "switch", DOMAIN, foreign_id, config_entry=other
    )
    stale = _stale_entries(hass, entry, leave=foreign_id)
    api = _module(REDUCED_PAYLOAD)

    await _set_up(hass, entry, api)

    registry = er.async_get(hass)
    assert all(registry.async_get(entity_id) is None for entity_id in stale)
    assert registry.async_get(foreign.entity_id) is not None
    # The home leave status is not asked for at setup, and nothing else is sent.
    api.send_airco_command.assert_not_awaited()

    remaining = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert AIRCO_ID in remaining  # the climate entity
    assert f"{DOMAIN}-{AIRCO_ID}-problem" in remaining
    assert f"{DOMAIN}-{AIRCO_ID}-external-control" in remaining
    assert f"{DOMAIN}-{AIRCO_ID}-home-leave-mode" in remaining
    assert f"{DOMAIN}-{AIRCO_ID}-occupancy" in remaining
    assert not remaining & {
        uid for uids in unsupported_entity_unique_ids(AIRCO_ID).values() for uid in uids
    }

    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.is_persistent
    assert not issue.is_fixable
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders == {"device_name": entry.title}


async def test_the_repair_goes_when_the_source_does(hass: HomeAssistant):
    entry = _entry({CONF_EXTERNAL_TEMPERATURE_SOURCE: SOURCE})
    api = _module(REDUCED_PAYLOAD)
    await _set_up(hass, entry, api)
    assert _issue(hass, entry) is not None

    hass.config_entries.async_update_entry(entry, options={})
    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=api,
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert _issue(hass, entry) is None


async def test_no_repair_without_a_source(hass: HomeAssistant):
    entry = _entry()
    await _set_up(hass, entry, _module(REDUCED_PAYLOAD))
    assert _issue(hass, entry) is None


async def test_removing_the_entry_removes_the_repair(hass: HomeAssistant):
    entry = _entry({CONF_EXTERNAL_TEMPERATURE_SOURCE: SOURCE})
    api = _module(REDUCED_PAYLOAD)
    await _set_up(hass, entry, api)
    assert _issue(hass, entry) is not None

    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=api,
    ):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, external_temperature_unsupported_issue_id(entry.entry_id)
        )
        is None
    )


async def test_a_normal_unit_keeps_its_entities_and_gets_no_repair(hass: HomeAssistant):
    entry = _entry({CONF_EXTERNAL_TEMPERATURE_SOURCE: SOURCE})
    registry = er.async_get(hass)
    entry.add_to_hass(hass)
    stale = _stale_entries(hass, entry)

    await _set_up(hass, entry, _module(ON_HEAT_PAYLOAD))

    assert all(registry.async_get(entity_id) is not None for entity_id in stale)
    assert _issue(hass, entry) is None


# --- the options form ----------------------------------------------------------


def _form_sections(result) -> dict[str, set[str]]:
    schema = result["data_schema"].schema
    return {
        key.schema: {inner.schema for inner in value.schema.schema}
        for key, value in schema.items()
        if hasattr(value, "schema") and hasattr(value.schema, "schema")
    }


async def test_the_options_form_offers_no_source_to_a_reduced_unit(hass: HomeAssistant):
    entry = _entry({CONF_OVERSHOOT_COOL: 0.5})
    await _set_up(hass, entry, _module(REDUCED_PAYLOAD))
    assert entry.state is ConfigEntryState.LOADED

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert "indoor_temperature_source" not in _form_sections(result)
    # Saving carries what the form could not show.
    saved = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "availability_retry_limit": 3,
            "firmware_update_check": False,
            "setpoint_offsets": {},
            "sensor_offsets": {},
        },
    )
    assert saved["data"][CONF_OVERSHOOT_COOL] == 0.5


async def test_a_source_that_is_already_set_can_still_be_removed(hass: HomeAssistant):
    entry = _entry({CONF_EXTERNAL_TEMPERATURE_SOURCE: SOURCE, CONF_OVERSHOOT_COOL: 0.5})
    await _set_up(hass, entry, _module(REDUCED_PAYLOAD))

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert _form_sections(result)["indoor_temperature_source"] == {
        CONF_EXTERNAL_TEMPERATURE_SOURCE
    }
    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=_module(REDUCED_PAYLOAD),
    ):
        saved = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "availability_retry_limit": 3,
                "firmware_update_check": False,
                "indoor_temperature_source": {},
                "setpoint_offsets": {},
                "sensor_offsets": {},
            },
        )
    assert CONF_EXTERNAL_TEMPERATURE_SOURCE not in saved["data"]
    assert saved["data"][CONF_OVERSHOOT_COOL] == 0.5
    await hass.async_block_till_done()


async def test_the_options_form_of_a_normal_unit_is_unchanged(hass: HomeAssistant):
    entry = _entry()
    await _set_up(hass, entry, _module(ON_HEAT_PAYLOAD))

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert _form_sections(result)["indoor_temperature_source"] == {
        CONF_EXTERNAL_TEMPERATURE_SOURCE
    }


# --- the ids the cleanup names are the ones the entities build -----------------


async def test_the_cleanup_names_exactly_the_ids_the_entities_build(
    hass: HomeAssistant, platform_device
):
    platform_device.airco.Capabilities = replace(
        platform_device.airco.Capabilities, vacant_property=True, home_leave_mode=True
    )
    hass.data[f"{DOMAIN}_prerelease_build"] = True
    entry = MagicMock(runtime_data=MagicMock(device=platform_device))

    built: dict[str, set[str]] = {}
    for domain, module in (
        ("sensor", sensor),
        ("switch", switch),
        ("binary_sensor", binary_sensor),
        ("number", number),
        ("select", select),
    ):
        added: list = []
        await module.async_setup_entry(hass, entry, added.extend)
        built[domain] = {entity.unique_id for entity in added}

    airco_id = platform_device.airco_id_lower
    for domain, unique_ids in unsupported_entity_unique_ids(airco_id).items():
        assert set(unique_ids) <= built[domain], domain
    assert len(SERVICE_DATA_SENSOR_TYPES) == len(ServiceDataSensor._FIELD_BY_TYPE)
    assert set(SERVICE_DATA_SENSOR_TYPES) == set(ServiceDataSensor._FIELD_BY_TYPE)
