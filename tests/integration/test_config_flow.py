"""Tests for config_flow.py: user/zeroconf/discovery_confirm flows and the
options flow. Repository (the HTTP layer) is patched out - no real network.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pywfrac.repository import WfRacError

from custom_components import mitsubishi_wf_rac
from custom_components.mitsubishi_wf_rac.config_flow import (
    SECTION_INDOOR_TEMPERATURE_SOURCE,
    SECTION_SENSOR_OFFSETS,
    SECTION_SETPOINT_OFFSETS,
)
from custom_components.mitsubishi_wf_rac.const import (
    CONF_AIRCO_ID,
    CONF_AVAILABILITY_RETRY_LIMIT,
    CONF_EXTERNAL_TEMPERATURE_SOURCE,
    CONF_FIRMWARE_UPDATE_CHECK,
    CONF_INDOOR_OFFSET,
    CONF_OPERATOR_ID,
    CONF_OUTDOOR_OFFSET,
    CONF_OVERSHOOT_COOL,
    CONF_OVERSHOOT_DRY,
    CONF_OVERSHOOT_HEAT,
    CONF_TARGET_OFFSET,
    CONF_TARGET_OFFSET_COOL,
    CONF_TARGET_OFFSET_HEAT,
    DOMAIN,
)
from homeassistant import config_entries
from homeassistant.const import CONF_DEVICE_ID, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData, section
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo


def _mock_repository(airco_id="airco-1", update_result=0):
    repo = AsyncMock()
    repo.get_airco_id.return_value = airco_id
    repo.update_account_info.return_value = {"result": update_result}
    return repo


def _patch_repository(repo):
    return patch(
        "custom_components.mitsubishi_wf_rac.config_flow.Repository", return_value=repo
    )


@pytest.fixture(autouse=True)
def bypass_entry_setup():
    """Config-flow tests only exercise the flow itself, not the full device
    connection - CREATE_ENTRY normally triggers a real async_setup_entry(),
    which would open a real network connection via Device.update().
    """
    with patch(
        "custom_components.mitsubishi_wf_rac.async_setup_entry", return_value=True
    ):
        yield


async def test_user_flow_shows_form_with_no_input(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_user_flow_success_creates_entry(hass: HomeAssistant):
    repo = _mock_repository(airco_id="airco-1", update_result=0)
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "192.168.1.50", "port": 51443},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Titled after the unit, not asked for: the last four characters of the
    # airco id tell two units apart without putting the whole id into the
    # device name and every entity id built from it.
    assert result["title"] == "WF-RAC co-1"
    # CONF_HOST moves from data to options (see _async_create_common) - not
    # duplicated across both.
    assert result["data"]["host"] == "192.168.1.50"
    assert "host" not in result["options"]
    assert result["data"][CONF_AIRCO_ID] == "airco-1"
    # Off by default for new entries too - see CONF_FIRMWARE_UPDATE_CHECK.
    assert result["options"][CONF_FIRMWARE_UPDATE_CHECK] is False
    assert CONF_OPERATOR_ID in result["data"]
    assert CONF_DEVICE_ID in result["data"]


async def test_user_flow_invalid_host_shows_error(hass: HomeAssistant):
    repo = _mock_repository()
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "ab"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"host": "invalid_host"}
    repo.get_airco_id.assert_not_awaited()


async def test_user_flow_host_already_configured_shows_error(hass: HomeAssistant):
    MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Existing AC", "host": "192.168.1.50"},
        options={},
    ).add_to_hass(hass)

    repo = _mock_repository()
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.50", "port": 51443}
        )

    assert result["type"] is FlowResultType.FORM
    # HostAlreadyConfigured.applies_to_field is CONF_HOST, and "host" is
    # present in this schema, so the error attaches to that field rather
    # than falling back to CONF_BASE.
    assert result["errors"] == {"host": "host_already_configured"}


async def test_user_flow_force_update_bypasses_duplicate_host_check(
    hass: HomeAssistant,
):
    MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Existing AC", "host": "192.168.1.50"},
        options={},
    ).add_to_hass(hass)

    repo = _mock_repository()
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "192.168.1.50", "port": 51443, "force_update": True},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_cannot_connect_shows_error(hass: HomeAssistant):
    repo = _mock_repository()
    repo.get_airco_id.side_effect = WfRacError("timeout")
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.50", "port": 51443}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_empty_airco_id_is_cannot_connect(hass: HomeAssistant):
    repo = _mock_repository(airco_id="")
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.50", "port": 51443}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_update_account_info_falsy_is_cannot_connect(
    hass: HomeAssistant,
):
    repo = _mock_repository()
    repo.update_account_info.return_value = None
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.50", "port": 51443}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_registration_failure_is_cannot_connect(hass: HomeAssistant):
    """The registration request is the second of the two, and the module
    answers one caller at a time - a timeout there is an ordinary outcome,
    not an unexpected error."""
    repo = _mock_repository()
    repo.update_account_info.side_effect = WfRacError("timeout")
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.50", "port": 51443}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_unreadable_registration_answer_is_cannot_connect(
    hass: HomeAssistant,
):
    """Whatever answered is not a WF-RAC module."""
    repo = _mock_repository()
    repo.update_account_info.return_value = {"result": "not a number"}
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.50", "port": 51443}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize(
    "answer",
    [
        ValueError("Expecting value: line 1 column 1"),
        AttributeError("'list' object has no attribute 'get'"),
        OSError("[SSL] PEM lib"),
    ],
    ids=["not json", "json but not an object", "no tls handshake"],
)
async def test_user_flow_non_json_answer_is_cannot_connect(
    hass: HomeAssistant, answer: Exception
):
    """Typing the address of some other HTTP service in the house is a
    connection problem from where the user stands. None of these leave the
    library as a WfRacError, so the flow has to name them itself."""
    repo = _mock_repository()
    repo.get_airco_id.side_effect = answer
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.50", "port": 51443}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_too_many_devices_shows_error(hass: HomeAssistant):
    repo = _mock_repository(update_result=2)
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.50", "port": 51443}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "too_many_devices_registered"}


async def test_user_flow_unexpected_exception_shows_generic_error(hass: HomeAssistant):
    repo = _mock_repository()
    repo.get_airco_id.side_effect = RuntimeError("boom")
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.50", "port": 51443}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unexpected_error"}


async def test_user_flow_reuses_operator_and_device_id_from_existing_entry(
    hass: HomeAssistant,
):
    MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "Existing AC",
            "host": "192.168.1.60",
            CONF_OPERATOR_ID: "shared-operator-id",
            CONF_DEVICE_ID: "shared-device-id",
        },
        options={},
    ).add_to_hass(hass)

    repo = _mock_repository()
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.50", "port": 51443}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_OPERATOR_ID] == "shared-operator-id"
    assert result["data"][CONF_DEVICE_ID] == "shared-device-id"


def _existing_entry(
    hass: HomeAssistant, name="Living Room AC", host="192.168.1.50", port=51443
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=7,
        # Every entry carries the unit's own id as its unique id since the v7
        # migration - a reconfigure compares against it.
        unique_id="airco-1",
        data={
            "name": name,
            "host": host,
            "port": port,
            CONF_AIRCO_ID: "airco-1",
            CONF_OPERATOR_ID: "operator-1",
            CONF_DEVICE_ID: "device-1",
        },
        options={CONF_FIRMWARE_UPDATE_CHECK: False},
    )
    entry.add_to_hass(hass)
    return entry


async def test_reconfigure_flow_shows_form_with_current_values(hass: HomeAssistant):
    entry = _existing_entry(
        hass, name="Living Room AC", host="192.168.1.50", port=51443
    )

    result = await entry.start_reconfigure_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    suggested = {
        key.schema: key.description["suggested_value"]
        for key in result["data_schema"].schema
    }
    assert suggested == {"host": "192.168.1.50", "port": 51443}


async def test_reconfigure_flow_updates_host_and_reloads(hass: HomeAssistant):
    entry = _existing_entry(hass, host="192.168.1.50")

    repo = _mock_repository(airco_id="airco-1")
    with _patch_repository(repo):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "192.168.1.60", "port": 51443},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["host"] == "192.168.1.60"
    assert "host" not in entry.options
    # Everything not touched by the form survives the update.
    assert entry.data[CONF_OPERATOR_ID] == "operator-1"
    assert entry.data[CONF_DEVICE_ID] == "device-1"
    assert entry.options[CONF_FIRMWARE_UPDATE_CHECK] is False


async def test_reconfigure_flow_allows_resubmitting_the_same_host(hass: HomeAssistant):
    # The entry being reconfigured already "owns" this host among its own
    # options - the duplicate-host check must exclude it, not just every
    # *other* entry, or every reconfigure with an unchanged host would fail.
    entry = _existing_entry(hass, host="192.168.1.50")

    repo = _mock_repository(airco_id="airco-1")
    with _patch_repository(repo):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "192.168.1.50", "port": 51443},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


async def test_reconfigure_flow_rejects_a_different_unit(hass: HomeAssistant):
    """A typo can point the entry at a second air conditioner. Every entity's
    unique id is built from the airco id, so following it would rename them
    all, orphan the originals, and leave discovery unable to place either
    unit."""
    entry = _existing_entry(hass, host="192.168.1.50")

    repo = _mock_repository(airco_id="airco-2")
    with _patch_repository(repo):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "192.168.1.99", "port": 51443},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert entry.data["host"] == "192.168.1.50"
    assert entry.data[CONF_AIRCO_ID] == "airco-1"
    # Registering claims one of the few account slots the module has. The
    # stranger must not lose one to our typo, so the abort comes before the
    # write, not after it.
    repo.update_account_info.assert_not_awaited()


async def test_reconfigure_flow_rejects_another_entrys_host(hass: HomeAssistant):
    MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Bedroom AC", "host": "192.168.1.99"},
        options={},
    ).add_to_hass(hass)
    entry = _existing_entry(hass, host="192.168.1.50")

    repo = _mock_repository()
    with _patch_repository(repo):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "192.168.1.99", "port": 51443},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"host": "host_already_configured"}
    assert entry.data["host"] == "192.168.1.50"


async def test_reconfigure_flow_cannot_connect_shows_error(hass: HomeAssistant):
    entry = _existing_entry(hass, host="192.168.1.50")

    repo = _mock_repository()
    repo.get_airco_id.side_effect = WfRacError("timeout")
    with _patch_repository(repo):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "192.168.1.60", "port": 51443},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data["host"] == "192.168.1.50"


def _zeroconf_info(host="192.168.1.50", port=51443, hostname="ac-living-room.local."):
    return ZeroconfServiceInfo(
        ip_address=__import__("ipaddress").ip_address(host),
        ip_addresses=[__import__("ipaddress").ip_address(host)],
        hostname=hostname,
        name="ac-living-room._beaver._tcp.local.",
        port=port,
        properties={},
        type="_beaver._tcp.local.",
    )


async def test_zeroconf_discovery_shows_confirm_form(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zeroconf_info(),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"


async def test_zeroconf_discovery_aborts_if_host_already_configured(
    hass: HomeAssistant,
):
    MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Existing AC", "host": "192.168.1.50"},
        options={},
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zeroconf_info(host="192.168.1.50"),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_zeroconf_discovery_confirm_creates_entry(hass: HomeAssistant):
    repo = _mock_repository()
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=_zeroconf_info(),
        )
        # The form pre-fills the announced port and the user confirms it.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"port": 51443}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["host"] == "192.168.1.50"
    assert result["data"]["port"] == 51443


async def test_zeroconf_announced_port_falls_back_to_the_fixed_one(hass: HomeAssistant):
    """The module serves a port fixed in firmware, so an announcement carrying
    something else (#290) is the announcement being wrong, not the device. Try
    the real port rather than failing setup on a value it cannot have meant.
    """
    repo = _mock_repository()
    repo.get_airco_id.side_effect = [WfRacError("no answer"), "airco-1"]
    with _patch_repository(repo) as repository_cls:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=_zeroconf_info(port=5353),
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"port": 5353}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The entry keeps the port that answered, not the one that was announced.
    assert result["data"]["port"] == 51443
    assert [call.args[2] for call in repository_cls.call_args_list] == [5353, 51443]


async def test_manual_port_is_not_second_guessed(hass: HomeAssistant):
    """Only an announced port is retried on the default. A port the user typed
    is taken at face value, so a genuinely unusual setup still fails visibly
    instead of being silently redirected.
    """
    repo = _mock_repository()
    repo.get_airco_id.side_effect = WfRacError("no answer")
    with _patch_repository(repo) as repository_cls:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "192.168.1.50", "port": 8443},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"]
    assert [call.args[2] for call in repository_cls.call_args_list] == [8443]


async def test_zeroconf_discovery_confirm_port_can_be_overridden(hass: HomeAssistant):
    """A bad mDNS advertisement (see #290: port 5353, the mDNS port itself,
    instead of the fixed 51443) must be correctable in the confirm step
    rather than silently trusted.
    """
    repo = _mock_repository()
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=_zeroconf_info(port=5353),
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"port": 51443}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["port"] == 51443


_OPTION_SECTIONS = {
    CONF_EXTERNAL_TEMPERATURE_SOURCE: SECTION_INDOOR_TEMPERATURE_SOURCE,
    CONF_OVERSHOOT_COOL: SECTION_INDOOR_TEMPERATURE_SOURCE,
    CONF_OVERSHOOT_DRY: SECTION_INDOOR_TEMPERATURE_SOURCE,
    CONF_OVERSHOOT_HEAT: SECTION_INDOOR_TEMPERATURE_SOURCE,
    CONF_TARGET_OFFSET: SECTION_SETPOINT_OFFSETS,
    CONF_TARGET_OFFSET_COOL: SECTION_SETPOINT_OFFSETS,
    CONF_TARGET_OFFSET_HEAT: SECTION_SETPOINT_OFFSETS,
    CONF_INDOOR_OFFSET: SECTION_SENSOR_OFFSETS,
    CONF_OUTDOOR_OFFSET: SECTION_SENSOR_OFFSETS,
}


def _form_input(values: dict | None = None) -> dict:
    """Shape flat option values the way the sectioned form hands them back.

    Every section is present even when empty: they are vol.Required, so a
    submission that leaves one out is rejected before any field is looked at.
    """
    nested: dict = {
        SECTION_INDOOR_TEMPERATURE_SOURCE: {},
        SECTION_SETPOINT_OFFSETS: {},
        SECTION_SENSOR_OFFSETS: {},
    }
    for key, value in (values or {}).items():
        section = _OPTION_SECTIONS.get(key)
        if section is None:
            nested[key] = value
        else:
            nested[section][key] = value
    return nested


async def test_options_flow_shows_form_with_current_defaults(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={"host": "192.168.1.50", CONF_TARGET_OFFSET: 1.5},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"


async def test_options_flow_saves_submitted_values(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _form_input(
            {
                CONF_INDOOR_OFFSET: 1.0,
                CONF_OUTDOOR_OFFSET: -1.0,
                CONF_TARGET_OFFSET: 0.5,
                CONF_EXTERNAL_TEMPERATURE_SOURCE: "sensor.living_room_temperature",
            }
        ),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TARGET_OFFSET] == 0.5
    assert (
        result["data"][CONF_EXTERNAL_TEMPERATURE_SOURCE]
        == "sensor.living_room_temperature"
    )
    # The host is connection data now, so it is not in the options at all and
    # an options save cannot touch it.
    assert "host" not in result["data"]


async def test_options_flow_refuses_its_own_entity_as_source(hass: HomeAssistant):
    # An armed override makes the unit report the injected value back, so this
    # integration's own temperature sensors follow it. Feeding one back in
    # would feed the override into itself - and with an overshoot set, walk it
    # away from the room by that much per poll - so the selector excludes them,
    # and rejects one on submit, not just in the picker.
    import voluptuous as vol

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={},
    )
    entry.add_to_hass(hass)
    own = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        "own-indoor-temperature",
        suggested_object_id="living_room_ac_indoor_temperature",
        config_entry=entry,
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises((vol.Invalid, InvalidData)):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            _form_input({CONF_EXTERNAL_TEMPERATURE_SOURCE: own.entity_id}),
        )

    assert entry.options.get(CONF_EXTERNAL_TEMPERATURE_SOURCE) is None


async def test_options_flow_accepts_a_foreign_temperature_sensor(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={},
    )
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, "own-indoor-temperature", config_entry=entry
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _form_input({CONF_EXTERNAL_TEMPERATURE_SOURCE: "sensor.hallway_temperature"}),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (
        result["data"][CONF_EXTERNAL_TEMPERATURE_SOURCE] == "sensor.hallway_temperature"
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (CONF_INDOOR_OFFSET, 100.0),  # outside -15..15
        (CONF_OUTDOOR_OFFSET, -100.0),  # outside -15..15
        (CONF_TARGET_OFFSET, 50.0),  # outside -5..5
        (CONF_TARGET_OFFSET_COOL, 50.0),  # outside -5..5
        (CONF_TARGET_OFFSET_HEAT, -50.0),  # outside -5..5
    ],
)
async def test_options_flow_enforces_offset_range(hass: HomeAssistant, key, value):
    # Regression test for the PR #182 fix: vol.Coerce(float, vol.Range(...))
    # silently passed Range as Coerce's error-message arg rather than
    # chaining it, so out-of-range values were never rejected. Now
    # vol.All(vol.Coerce(float), vol.Range(...)) - this must raise.
    import voluptuous as vol

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]

    with pytest.raises(vol.MultipleInvalid):
        schema(_form_input({key: value}))


async def test_options_flow_rejects_a_retry_limit_below_the_floor(hass: HomeAssistant):
    # Values below the floor are what the v3 -> v4 and v4 -> v5 migrations kept
    # having to correct; the form must not let a new one in.
    import voluptuous as vol

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]

    with pytest.raises(vol.MultipleInvalid):
        schema(_form_input({CONF_AVAILABILITY_RETRY_LIMIT: 1}))

    assert (
        schema(_form_input({CONF_AVAILABILITY_RETRY_LIMIT: 5}))[
            CONF_AVAILABILITY_RETRY_LIMIT
        ]
        == 5
    )


async def test_options_flow_defaults_firmware_update_check_to_off(hass: HomeAssistant):
    # The firmware check is the only outbound internet call in the
    # integration and must default to off (see const.py's
    # CONF_FIRMWARE_UPDATE_CHECK) - confirm the options form defaults it that
    # way for both brand-new entries and pre-existing ones that predate the
    # option entirely.
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]

    validated = schema(_form_input())
    assert validated[CONF_FIRMWARE_UPDATE_CHECK] is False


async def test_options_flow_saves_submitted_firmware_update_check(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _form_input({CONF_FIRMWARE_UPDATE_CHECK: True}),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_FIRMWARE_UPDATE_CHECK] is True


async def test_options_flow_saves_submitted_per_mode_offsets(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _form_input(
            {
                CONF_TARGET_OFFSET: 0.5,
                CONF_TARGET_OFFSET_COOL: 1.5,
                CONF_TARGET_OFFSET_HEAT: -1.5,
            }
        ),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TARGET_OFFSET_COOL] == 1.5
    assert result["data"][CONF_TARGET_OFFSET_HEAT] == -1.5


@pytest.mark.parametrize("value", [1.25, -1.25, 0.0, 3.0, -3.0])
async def test_options_flow_accepts_a_signed_overshoot(hass: HomeAssistant, value):
    # Overshooting is what everyone has measured, but a unit that stops short
    # of the setting needs the correction the other way. The fields only exist
    # while a source is configured - there is no room temperature to bend
    # without one.
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={
            "host": "192.168.1.50",
            CONF_EXTERNAL_TEMPERATURE_SOURCE: "sensor.hallway_temperature",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _form_input({CONF_OVERSHOOT_COOL: value})
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_OVERSHOOT_COOL] == value


async def test_options_flow_leaves_per_mode_offsets_unset_when_omitted(
    hass: HomeAssistant,
):
    # CONF_TARGET_OFFSET_COOL/_HEAT must persist as genuinely absent (None
    # via .get()) when left blank, not coerced to 0.0 - that's what makes
    # the climate.py resolver's fallback to CONF_TARGET_OFFSET work. A
    # default= on these fields (instead of description={"suggested_value"})
    # would silently turn a blank field into 0.0 and break that fallback.
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _form_input({CONF_TARGET_OFFSET: 0.5}),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_TARGET_OFFSET_COOL not in result["data"]
    assert CONF_TARGET_OFFSET_HEAT not in result["data"]
    assert result["data"].get(CONF_TARGET_OFFSET_COOL) is None
    assert result["data"].get(CONF_TARGET_OFFSET_HEAT) is None


async def test_options_flow_only_offers_the_overshoots_with_a_source(
    hass: HomeAssistant,
):
    """They bend the room temperature handed to the unit, so without a source
    there is nothing for them to act on and they would sit there doing
    nothing.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    source_section = next(
        value
        for key, value in result["data_schema"].schema.items()
        if str(key.schema) == SECTION_INDOOR_TEMPERATURE_SOURCE
    )
    assert {str(key.schema) for key in source_section.schema.schema} == {
        CONF_EXTERNAL_TEMPERATURE_SOURCE
    }

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_EXTERNAL_TEMPERATURE_SOURCE: "sensor.hallway_temperature",
        },
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    source_section = next(
        value
        for key, value in result["data_schema"].schema.items()
        if str(key.schema) == SECTION_INDOOR_TEMPERATURE_SOURCE
    )
    assert {str(key.schema) for key in source_section.schema.schema} == {
        CONF_EXTERNAL_TEMPERATURE_SOURCE,
        CONF_OVERSHOOT_COOL,
        CONF_OVERSHOOT_DRY,
        CONF_OVERSHOOT_HEAT,
    }


async def test_options_flow_opens_the_cooling_overshoot_on_the_measured_figure(
    hass: HomeAssistant,
):
    """Four units stop about half a kelvin past the setting, so a fresh field
    opening on 0 opens on a number that is certainly wrong. It is a pre-fill:
    a stored value wins, and nothing is corrected until the form is saved.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={
            "host": "192.168.1.50",
            CONF_EXTERNAL_TEMPERATURE_SOURCE: "sensor.hallway_temperature",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    validated = result["data_schema"](_form_input())[SECTION_INDOOR_TEMPERATURE_SOURCE]
    assert validated[CONF_OVERSHOOT_COOL] == 0.5
    # Heating has looked symmetric wherever it was measured - no figure to offer.
    assert validated[CONF_OVERSHOOT_HEAT] == 0.0
    # Dry opens on 0 as a measured figure: the one unit measured there needed
    # nothing beyond the scale correction pywfrac now applies (#218).
    assert validated[CONF_OVERSHOOT_DRY] == 0.0
    # Nothing is applied by opening the form: the resolver still reads 0.
    assert entry.options.get(CONF_OVERSHOOT_COOL) is None

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_OVERSHOOT_COOL: 0.0}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    validated = result["data_schema"](_form_input())[SECTION_INDOOR_TEMPERATURE_SOURCE]
    assert validated[CONF_OVERSHOOT_COOL] == 0.0


async def test_options_flow_saves_a_dry_overshoot(hass: HomeAssistant):
    """Dry cools as well, so its correction takes the cooling sign - but it is
    a field of its own rather than a share of the cooling one, because the band
    being corrected belongs to how the unit runs and dry runs a different
    airflow.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={
            "host": "192.168.1.50",
            CONF_EXTERNAL_TEMPERATURE_SOURCE: "sensor.hallway_temperature",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _form_input({CONF_OVERSHOOT_DRY: 0.75})
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_OVERSHOOT_DRY] == 0.75
    # The cooling field is untouched by it: separate figures, separate modes.
    assert result["data"][CONF_OVERSHOOT_COOL] == 0.5


async def test_options_flow_keeps_values_it_never_showed(hass: HomeAssistant):
    """async_create_entry replaces the options wholesale, so a field the form
    did not render is dropped unless it is carried over by hand. Without that,
    saving anything at all after removing a source would also throw away the
    overshoot figures the user measured.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={
            "host": "192.168.1.50",
            CONF_OVERSHOOT_COOL: 1.0,
            CONF_OVERSHOOT_HEAT: -0.5,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _form_input({CONF_TARGET_OFFSET: 0.5})
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_OVERSHOOT_COOL] == 1.0
    assert result["data"][CONF_OVERSHOOT_HEAT] == -0.5


async def test_options_flow_clears_a_source_the_form_did_show(hass: HomeAssistant):
    """The carry-over must not resurrect a field that was shown and left
    empty - clearing the source is how a user turns the override off.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={
            "host": "192.168.1.50",
            CONF_EXTERNAL_TEMPERATURE_SOURCE: "sensor.hallway_temperature",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _form_input()
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_EXTERNAL_TEMPERATURE_SOURCE not in result["data"]


async def test_options_form_fields_all_have_a_label(hass: HomeAssistant):
    """A field without an entry in strings.json renders as its raw key.

    That is not a crash and no test catches it downstream, so the form can
    grow a field and show the user "availability_retry_limit" indefinitely.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"].schema
    fields = set()
    for key, value in schema.items():
        if isinstance(value, section):
            fields |= {str(inner.schema) for inner in value.schema.schema}
        else:
            fields.add(str(key.schema))

    strings_file = Path(mitsubishi_wf_rac.__file__).parent / "strings.json"
    strings = json.loads(strings_file.read_text(encoding="utf-8"))
    init = strings["options"]["step"]["init"]
    labelled = set(init["data"])
    for group in init["sections"].values():
        labelled |= set(group["data"])
    # This entry has no source, so the overshoot fields are not rendered -
    # they are still labelled, and must be.
    assert fields <= labelled
    assert labelled - fields == {
        CONF_OVERSHOOT_COOL,
        CONF_OVERSHOOT_DRY,
        CONF_OVERSHOOT_HEAT,
    }


def test_is_matching_compares_unique_ids():
    from custom_components.mitsubishi_wf_rac.config_flow import WfRacConfigFlow

    flow_a = WfRacConfigFlow()
    flow_a.context = {"unique_id": "device-1"}
    flow_b = WfRacConfigFlow()
    flow_b.context = {"unique_id": "device-1"}
    flow_c = WfRacConfigFlow()
    flow_c.context = {"unique_id": "device-2"}

    assert flow_a.is_matching(flow_b) is True
    assert flow_a.is_matching(flow_c) is False


def test_is_matching_without_unique_id_never_matches():
    from custom_components.mitsubishi_wf_rac.config_flow import WfRacConfigFlow

    flow_a = WfRacConfigFlow()
    flow_a.context = {}
    flow_b = WfRacConfigFlow()
    flow_b.context = {}

    assert flow_a.is_matching(flow_b) is False


async def test_a_rediscovery_refreshes_the_address_but_not_the_port(
    hass: HomeAssistant,
):
    """A configured entry's port is not the announcement's to change.

    Modules have been seen announcing 5353 - the mDNS port itself - in the
    SRV record where the API port belongs (#290). At first discovery the
    confirm step and the registration fallback catch that. An entry that is
    already running has neither: the refresh would write the bad port
    straight into it and take the unit offline until it is reconfigured
    by hand (#329).
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "Living Room AC",
            "device_id": "dev-1",
            "operator_id": "op-1",
            "airco_id": "airco-1",
            CONF_HOST: "192.168.1.50",
            CONF_PORT: 51443,
        },
        options={},
        unique_id="ac-living-room",
        version=6,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zeroconf_info(host="192.168.1.60", port=5353),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.168.1.60"
    assert entry.data[CONF_PORT] == 51443


async def test_a_shouted_hostname_still_matches_the_entry(hass: HomeAssistant):
    """The unique id is one case, whoever supplied it.

    Discovery takes it from the announced hostname and every other path from
    the airconId the unit reports. Compared as they arrive, a difference in
    case would offer a configured unit as a new discovery and never refresh
    its address.
    """
    entry = _existing_entry(hass, host="192.168.1.50")
    hass.config_entries.async_update_entry(entry, unique_id="ac-living-room")

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zeroconf_info(host="192.168.1.99", hostname="AC-Living-Room.local."),
    )

    assert result["type"] is FlowResultType.ABORT
    assert entry.data["host"] == "192.168.1.99"


async def test_the_manual_flow_registers_the_unit_as_the_unique_id(hass: HomeAssistant):
    """What lets a later discovery recognise a hand-added entry."""
    repo = _mock_repository(airco_id="348E89C5A137", update_result=0)
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.50", "port": 51443}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == "348e89c5a137"
    assert result["title"] == "WF-RAC A137"
