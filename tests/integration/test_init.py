"""Tests for __init__.py: config-entry migration and the availability-check
default that create_device_from_entry() falls back to.
"""

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mitsubishi_wf_rac import async_migrate_entry, create_device_from_entry
from custom_components.mitsubishi_wf_rac.const import (
    CONF_AVAILABILITY_CHECK,
    CONF_AVAILABILITY_RETRY_LIMIT,
    DOMAIN,
)

_DATA = {
    "name": "Living Room AC",
    "device_id": "dev-1",
    "operator_id": "op-1",
    "airco_id": "airco-1",
    "port": 51443,
}


def _entry(hass: HomeAssistant, version: int, data: dict, options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, version=version, data=data, options=options)
    entry.add_to_hass(hass)
    return entry


async def test_migrate_v1_moves_host_into_options_and_ends_at_v4(hass: HomeAssistant):
    """A v1 entry runs through every step in one go. The v1 -> v2 step still
    writes CONF_AVAILABILITY_CHECK False, but v3 -> v4 must turn it back on.
    """
    entry = _entry(hass, 1, {**_DATA, CONF_HOST: "192.168.1.50"}, {})

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 4
    assert CONF_HOST not in entry.data
    assert entry.options[CONF_HOST] == "192.168.1.50"
    assert entry.options[CONF_AVAILABILITY_CHECK] is True
    assert entry.options[CONF_AVAILABILITY_RETRY_LIMIT] == 3


async def test_migrate_v2_keeps_user_retry_limit(hass: HomeAssistant):
    """The v2 -> v3 step used to reset the limit to 3 over whatever the user
    had configured.
    """
    entry = _entry(
        hass,
        2,
        _DATA,
        {CONF_HOST: "192.168.1.50", CONF_AVAILABILITY_CHECK: True, CONF_AVAILABILITY_RETRY_LIMIT: 8},
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 4
    assert entry.options[CONF_AVAILABILITY_RETRY_LIMIT] == 8


async def test_migrate_v3_enables_availability_check(hass: HomeAssistant):
    entry = _entry(
        hass,
        3,
        _DATA,
        {CONF_HOST: "192.168.1.50", CONF_AVAILABILITY_CHECK: False, CONF_AVAILABILITY_RETRY_LIMIT: 3},
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 4
    assert entry.options[CONF_AVAILABILITY_CHECK] is True


async def test_migrate_v3_drops_dead_availability_retry_key(hass: HomeAssistant):
    entry = _entry(
        hass,
        3,
        _DATA,
        {
            CONF_HOST: "192.168.1.50",
            CONF_AVAILABILITY_CHECK: True,
            CONF_AVAILABILITY_RETRY_LIMIT: 3,
            "availability_retry": False,
        },
    )

    assert await async_migrate_entry(hass, entry)

    assert "availability_retry" not in entry.options


async def test_migrate_v3_lifts_retry_limits_below_two(hass: HomeAssistant):
    """0 and 1 both mean "give up on the first failed poll" in
    Device._set_availability(), i.e. they cancel out the check being on.
    """
    for limit in (0, 1):
        entry = _entry(
            hass,
            3,
            _DATA,
            {
                CONF_HOST: "192.168.1.50",
                CONF_AVAILABILITY_CHECK: False,
                CONF_AVAILABILITY_RETRY_LIMIT: limit,
            },
        )

        assert await async_migrate_entry(hass, entry)

        assert entry.options[CONF_AVAILABILITY_RETRY_LIMIT] == 3


async def test_migrate_v3_keeps_deliberate_higher_retry_limit(hass: HomeAssistant):
    entry = _entry(
        hass,
        3,
        _DATA,
        {CONF_HOST: "192.168.1.50", CONF_AVAILABILITY_CHECK: False, CONF_AVAILABILITY_RETRY_LIMIT: 10},
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.options[CONF_AVAILABILITY_RETRY_LIMIT] == 10


async def test_availability_check_defaults_to_on_when_key_missing(hass: HomeAssistant):
    """The options form shows this as on for a missing key, so the runtime has
    to agree - otherwise the form and the actual behaviour disagree.
    """
    entry = _entry(hass, 4, _DATA, {CONF_HOST: "192.168.1.50"})

    device = await create_device_from_entry(entry, hass)

    assert device._availability_retry is True  # pylint: disable=protected-access
    assert device._availability_retry_limit == 3  # pylint: disable=protected-access


async def test_migrate_is_idempotent_at_current_version(hass: HomeAssistant):
    options = {
        CONF_HOST: "192.168.1.50",
        CONF_AVAILABILITY_CHECK: False,
        CONF_AVAILABILITY_RETRY_LIMIT: 1,
    }
    entry = _entry(hass, 4, _DATA, options)

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 4
    assert entry.options == options
