"""Tests for __init__.py: config-entry migration.

The availability options were removed in v5. Older steps still reference them
because entries created under those versions carry the keys, but nothing reads
them at runtime any more - the migration's job is to leave no trace of them.
"""

from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mitsubishi_wf_rac import (
    async_migrate_entry,
    async_remove_entry,
    create_device_from_entry,
)
from custom_components.mitsubishi_wf_rac.const import (
    CONF_AVAILABILITY_CHECK,
    CONF_AVAILABILITY_RETRY_LIMIT,
    DOMAIN,
)
from custom_components.mitsubishi_wf_rac.wfrac.device import registration_full_issue_id

_DATA = {
    "name": "Living Room AC",
    "device_id": "dev-1",
    "operator_id": "op-1",
    "airco_id": "airco-1",
    "port": 51443,
}

_CURRENT_VERSION = 5


def _entry(hass: HomeAssistant, version: int, data: dict, options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, version=version, data=data, options=options)
    entry.add_to_hass(hass)
    return entry


async def test_migrate_v1_moves_host_into_options(hass: HomeAssistant):
    """A v1 entry runs through every step in one go."""
    entry = _entry(hass, 1, {**_DATA, CONF_HOST: "192.168.1.50"}, {})

    assert await async_migrate_entry(hass, entry)

    assert entry.version == _CURRENT_VERSION
    assert CONF_HOST not in entry.data
    assert entry.options[CONF_HOST] == "192.168.1.50"


async def test_migrate_drops_the_check_and_floors_the_retry_limit(hass: HomeAssistant):
    """From any version that could carry them. A limit above the floor is the
    user's choice and survives; anything below it - including the check being
    off, which was the same thing - comes out at the floor.
    """
    for version, options, expected_limit in (
        (2, {CONF_AVAILABILITY_CHECK: True, CONF_AVAILABILITY_RETRY_LIMIT: 8}, 8),
        (3, {CONF_AVAILABILITY_CHECK: False, CONF_AVAILABILITY_RETRY_LIMIT: 0}, 3),
        (4, {CONF_AVAILABILITY_CHECK: False, CONF_AVAILABILITY_RETRY_LIMIT: 1}, 3),
        (4, {CONF_AVAILABILITY_CHECK: True}, 3),
    ):
        entry = _entry(hass, version, _DATA, {CONF_HOST: "192.168.1.50", **options})

        assert await async_migrate_entry(hass, entry)

        assert entry.version == _CURRENT_VERSION
        assert CONF_AVAILABILITY_CHECK not in entry.options
        assert entry.options[CONF_AVAILABILITY_RETRY_LIMIT] == expected_limit
        assert entry.options[CONF_HOST] == "192.168.1.50"


async def test_migrate_v3_drops_dead_availability_retry_key(hass: HomeAssistant):
    """The v1 -> v2 step used to write a key nothing ever read."""
    entry = _entry(
        hass,
        3,
        _DATA,
        {CONF_HOST: "192.168.1.50", "availability_retry": False},
    )

    assert await async_migrate_entry(hass, entry)

    assert "availability_retry" not in entry.options


async def test_migrate_keeps_unrelated_options(hass: HomeAssistant):
    entry = _entry(
        hass,
        4,
        _DATA,
        {CONF_HOST: "192.168.1.50", "indoor_offset": -1.5, CONF_AVAILABILITY_CHECK: True},
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.options["indoor_offset"] == -1.5


async def test_migrate_is_idempotent_at_current_version(hass: HomeAssistant):
    options = {CONF_HOST: "192.168.1.50"}
    entry = _entry(hass, _CURRENT_VERSION, _DATA, options)

    assert await async_migrate_entry(hass, entry)

    assert entry.version == _CURRENT_VERSION
    assert entry.options == options


async def test_device_is_built_without_availability_options(hass: HomeAssistant):
    """Tolerance is a property of the module's behaviour, not a setting - an
    entry carrying nothing but the host must still get it.
    """
    entry = _entry(hass, _CURRENT_VERSION, _DATA, {CONF_HOST: "192.168.1.50"})

    device = await create_device_from_entry(entry, hass)

    assert device._consecutive_failures == 0  # pylint: disable=protected-access


async def test_remove_entry_clears_the_registration_full_repair_issue(hass: HomeAssistant):
    """A repair issue is entry-scoped (see wfrac/device.py's add_account) - it
    must not survive the entry it was raised against, or it stays in the
    Repairs list forever pointing at nothing.
    """
    entry = _entry(hass, _CURRENT_VERSION, _DATA, {CONF_HOST: "192.168.1.50"})
    ir.async_create_issue(
        hass,
        DOMAIN,
        registration_full_issue_id(entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="too_many_devices",
        translation_placeholders={"device_name": "Living Room AC"},
    )

    with patch(
        "custom_components.mitsubishi_wf_rac.wfrac.device.Repository",
        return_value=AsyncMock(),
    ):
        await async_remove_entry(hass, entry)

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, registration_full_issue_id(entry.entry_id))
        is None
    )
