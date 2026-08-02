"""Tests for config_flow.py: user/zeroconf/discovery_confirm flows and the
options flow. Repository (the HTTP layer) is patched out - no real network.
"""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_DEVICE_ID
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mitsubishi_wf_rac.const import (
    CONF_AIRCO_ID,
    CONF_AVAILABILITY_CHECK,
    CONF_INDOOR_OFFSET,
    CONF_OPERATOR_ID,
    CONF_OUTDOOR_OFFSET,
    CONF_TARGET_OFFSET,
    CONF_TARGET_OFFSET_COOL,
    CONF_TARGET_OFFSET_HEAT,
    DOMAIN,
)
from custom_components.mitsubishi_wf_rac.wfrac.repository import AirconApiError


def _mock_repository(airco_id="airco-1", update_result=0):
    repo = AsyncMock()
    repo.get_airco_id.return_value = airco_id
    repo.update_account_info.return_value = {"result": update_result}
    return repo


def _patch_repository(repo):
    return patch("custom_components.mitsubishi_wf_rac.config_flow.Repository", return_value=repo)


@pytest.fixture(autouse=True)
def bypass_entry_setup():
    """Config-flow tests only exercise the flow itself, not the full device
    connection - CREATE_ENTRY normally triggers a real async_setup_entry(),
    which would open a real network connection via Device.update().
    """
    with patch("custom_components.mitsubishi_wf_rac.async_setup_entry", return_value=True):
        yield


# --- user flow --------------------------------------------------------


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
            {"name": "Living Room AC", "host": "192.168.1.50", "port": 51443},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living Room AC"
    # CONF_HOST moves from data to options (see _async_create_common) - not
    # duplicated across both.
    assert "host" not in result["data"]
    assert result["options"]["host"] == "192.168.1.50"
    assert result["data"][CONF_AIRCO_ID] == "airco-1"
    assert CONF_OPERATOR_ID in result["data"]
    assert CONF_DEVICE_ID in result["data"]


async def test_user_flow_invalid_host_shows_error(hass: HomeAssistant):
    repo = _mock_repository()
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Living Room AC", "host": "ab"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"host": "invalid_host"}
    repo.get_airco_id.assert_not_awaited()


async def test_user_flow_invalid_name_shows_error(hass: HomeAssistant):
    repo = _mock_repository()
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "ab", "host": "192.168.1.50"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"name": "name_invalid"}


async def test_user_flow_host_already_configured_shows_error(hass: HomeAssistant):
    MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Existing AC"},
        options={"host": "192.168.1.50"},
    ).add_to_hass(hass)

    repo = _mock_repository()
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "New AC", "host": "192.168.1.50", "port": 51443}
        )

    assert result["type"] is FlowResultType.FORM
    # HostAlreadyConfigured.applies_to_field is CONF_HOST, and "host" is
    # present in this schema, so the error attaches to that field rather
    # than falling back to CONF_BASE.
    assert result["errors"] == {"host": "host_already_configured"}


async def test_user_flow_force_update_bypasses_duplicate_host_check(hass: HomeAssistant):
    MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Existing AC"},
        options={"host": "192.168.1.50"},
    ).add_to_hass(hass)

    repo = _mock_repository()
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"name": "New AC", "host": "192.168.1.50", "port": 51443, "force_update": True},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_cannot_connect_shows_error(hass: HomeAssistant):
    repo = _mock_repository()
    repo.get_airco_id.side_effect = AirconApiError("timeout")
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Living Room AC", "host": "192.168.1.50", "port": 51443}
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
            result["flow_id"], {"name": "Living Room AC", "host": "192.168.1.50", "port": 51443}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_update_account_info_falsy_is_cannot_connect(hass: HomeAssistant):
    repo = _mock_repository()
    repo.update_account_info.return_value = None
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Living Room AC", "host": "192.168.1.50", "port": 51443}
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
            result["flow_id"], {"name": "Living Room AC", "host": "192.168.1.50", "port": 51443}
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
            result["flow_id"], {"name": "Living Room AC", "host": "192.168.1.50", "port": 51443}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unexpected_error"}


async def test_user_flow_reuses_operator_and_device_id_from_existing_entry(hass: HomeAssistant):
    MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "Existing AC",
            CONF_OPERATOR_ID: "shared-operator-id",
            CONF_DEVICE_ID: "shared-device-id",
        },
        options={"host": "192.168.1.60"},
    ).add_to_hass(hass)

    repo = _mock_repository()
    with _patch_repository(repo):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Second AC", "host": "192.168.1.50", "port": 51443}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_OPERATOR_ID] == "shared-operator-id"
    assert result["data"][CONF_DEVICE_ID] == "shared-device-id"


# --- zeroconf discovery -------------------------------------------------


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


async def test_zeroconf_discovery_aborts_if_host_already_configured(hass: HomeAssistant):
    MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Existing AC"},
        options={"host": "192.168.1.50"},
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
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Living Room AC"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["host"] == "192.168.1.50"


# --- options flow --------------------------------------------------------


async def test_options_flow_shows_form_with_current_defaults(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={"host": "192.168.1.50", CONF_AVAILABILITY_CHECK: True, CONF_TARGET_OFFSET: 1.5},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"


async def test_options_flow_saves_submitted_values(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={"host": "192.168.1.50"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "host": "192.168.1.50",
            CONF_AVAILABILITY_CHECK: True,
            "availability_retry_limit": 5,
            CONF_INDOOR_OFFSET: 1.0,
            CONF_OUTDOOR_OFFSET: -1.0,
            CONF_TARGET_OFFSET: 0.5,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TARGET_OFFSET] == 0.5


@pytest.mark.parametrize(
    "key,value",
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
        options={"host": "192.168.1.50"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]

    with pytest.raises(vol.MultipleInvalid):
        schema({"host": "192.168.1.50", CONF_AVAILABILITY_CHECK: True, key: value})


async def test_options_flow_saves_submitted_per_mode_offsets(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={"host": "192.168.1.50"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "host": "192.168.1.50",
            CONF_AVAILABILITY_CHECK: True,
            CONF_TARGET_OFFSET: 0.5,
            CONF_TARGET_OFFSET_COOL: 1.5,
            CONF_TARGET_OFFSET_HEAT: -1.5,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TARGET_OFFSET_COOL] == 1.5
    assert result["data"][CONF_TARGET_OFFSET_HEAT] == -1.5


async def test_options_flow_leaves_per_mode_offsets_unset_when_omitted(hass: HomeAssistant):
    # CONF_TARGET_OFFSET_COOL/_HEAT must persist as genuinely absent (None
    # via .get()) when left blank, not coerced to 0.0 - that's what makes
    # the climate.py resolver's fallback to CONF_TARGET_OFFSET work. A
    # default= on these fields (instead of description={"suggested_value"})
    # would silently turn a blank field into 0.0 and break that fallback.
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Living Room AC"},
        options={"host": "192.168.1.50"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "host": "192.168.1.50",
            CONF_AVAILABILITY_CHECK: True,
            CONF_TARGET_OFFSET: 0.5,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_TARGET_OFFSET_COOL not in result["data"]
    assert CONF_TARGET_OFFSET_HEAT not in result["data"]
    assert result["data"].get(CONF_TARGET_OFFSET_COOL) is None
    assert result["data"].get(CONF_TARGET_OFFSET_HEAT) is None


# --- WfRacConfigFlow.is_matching() / _name ------------------------------


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


def test_name_property_reads_from_context():
    from custom_components.mitsubishi_wf_rac.config_flow import WfRacConfigFlow

    flow = WfRacConfigFlow()
    flow.context = {"name": "Living Room AC"}
    assert flow._name == "Living Room AC"
