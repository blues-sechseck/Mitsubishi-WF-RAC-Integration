"""Tests for __init__.py: config-entry migration.

The availability options were removed in v5. Older steps still reference them
because entries created under those versions carry the keys, but nothing reads
them at runtime any more - the migration's job is to leave no trace of them.
"""

from unittest.mock import AsyncMock, patch

import pytest
from pywfrac import WfRacConnectionError, WfRacError

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mitsubishi_wf_rac import (
    async_migrate_entry,
    async_remove_entry,
    create_device_from_entry,
)
from custom_components.mitsubishi_wf_rac.config_flow import WfRacConfigFlow
from custom_components.mitsubishi_wf_rac.const import (
    CONF_CONNECTION_METHOD,
    CONF_AVAILABILITY_CHECK,
    CONF_AVAILABILITY_RETRY_LIMIT,
    DOMAIN,
)
from custom_components.mitsubishi_wf_rac.coordinator import registration_full_issue_id

_DATA = {
    "name": "Living Room AC",
    "device_id": "dev-1",
    "operator_id": "op-1",
    "airco_id": "airco-1",
    "port": 51443,
}

_CURRENT_VERSION = 7


def _entry(hass: HomeAssistant, version: int, data: dict, options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, version=version, data=data, options=options)
    entry.add_to_hass(hass)
    return entry


async def test_migrate_v1_leaves_the_host_in_data(hass: HomeAssistant):
    """A v1 entry runs through every step in one go.

    v2 moved the host into options and v6 moved it back, so an entry that
    starts before both ends up where it began - which is the point: setup and
    the discovery address refresh both read entry.data.
    """
    entry = _entry(hass, 1, {**_DATA, CONF_HOST: "192.168.1.50"}, {})

    assert await async_migrate_entry(hass, entry)

    assert entry.version == _CURRENT_VERSION
    assert CONF_HOST not in entry.options
    assert entry.data[CONF_HOST] == "192.168.1.50"


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
        assert entry.data[CONF_HOST] == "192.168.1.50"


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
    options = {"indoor_offset": -1.5}
    entry = _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, options)

    assert await async_migrate_entry(hass, entry)

    assert entry.version == _CURRENT_VERSION
    assert entry.options == options
    assert entry.data[CONF_HOST] == "192.168.1.50"


async def test_device_is_built_without_availability_options(hass: HomeAssistant):
    """Tolerance is a property of the module's behaviour, not a setting - an
    entry carrying nothing but the host must still get it.
    """
    entry = _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, {})

    device = await create_device_from_entry(entry, hass)

    assert device._consecutive_failures == 0  # pylint: disable=protected-access


async def test_remove_entry_clears_the_registration_full_repair_issue(hass: HomeAssistant):
    """A repair issue is entry-scoped (see wfrac/device.py's add_account) - it
    must not survive the entry it was raised against, or it stays in the
    Repairs list forever pointing at nothing.
    """
    entry = _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, {})
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
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=AsyncMock(),
    ):
        await async_remove_entry(hass, entry)

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, registration_full_issue_id(entry.entry_id))
        is None
    )


def test_the_config_flow_declares_the_version_the_migration_ends_at():
    """Home Assistant stops migrating as soon as entry.version reaches this.

    ConfigEntry.async_migrate returns early when the entry is already at the
    handler's VERSION, so a migration step added without raising it here is
    never called - and every entry keeps whatever layout that step was meant
    to fix.
    """
    assert WfRacConfigFlow.VERSION == _CURRENT_VERSION


async def test_an_entry_from_before_the_host_moved_still_sets_up(hass: HomeAssistant):
    """What a manually added installation actually has on disk.

    Discovery writes the host into entry.data as a side effect of its address
    refresh, so a discovered entry has one either way. An entry added by hand
    never was: the host lived in options alone, and setup reads it from data.
    """
    entry = _entry(hass, 5, _DATA, {CONF_HOST: "192.168.1.50"})

    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=AsyncMock(),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is not ConfigEntryState.SETUP_ERROR
    assert entry.version == _CURRENT_VERSION
    assert entry.data[CONF_HOST] == "192.168.1.50"
    assert CONF_HOST not in entry.options


async def test_a_unit_that_is_unreachable_at_startup_gets_retried(hass: HomeAssistant):
    """update() reports failure through .available rather than raising.

    ConfigEntryNotReady is what buys HA's retry-with-backoff; without it the
    entry would sit there "loaded" with entities that never get a reading.
    """
    entry = _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, {})

    repository = AsyncMock()
    repository.get_aircon_stats.side_effect = WfRacConnectionError("no route")
    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=repository,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_the_connection_method_is_remembered(
    hass: HomeAssistant, repository: AsyncMock
):
    """Protocol discovery costs a round-trip on every start otherwise."""
    entry = _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, {})

    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=repository,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.data[CONF_CONNECTION_METHOD] is not None

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_a_failed_platform_unload_keeps_the_coordinator(
    hass: HomeAssistant, repository: AsyncMock
):
    """Entities that stayed loaded must keep the coordinator that feeds them.

    Shutting it down anyway would leave a loaded entry that never updates
    again.
    """
    entry = _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, {})

    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=repository,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        device = entry.runtime_data.device

        with patch.object(
            hass.config_entries, "async_unload_platforms", return_value=False
        ):
            assert not await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()

        assert device.last_update_success

        await device.async_shutdown()


async def test_stopping_home_assistant_hands_the_override_back(
    hass: HomeAssistant, repository: AsyncMock
):
    """A stop is the one case where nothing of ours writes again, so the value
    the unit was handed would stand there. A reload must not do this: the
    override is re-armed seconds later, and the unit would flick back to its
    own sensor on every saved option.
    """
    entry = _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, {})

    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=repository,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        device = entry.runtime_data.device

        with patch.object(
            device, "async_release_external_temperature", AsyncMock()
        ) as release:
            await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            release.assert_not_awaited()

        device = entry.runtime_data.device
        with patch.object(
            device, "async_release_external_temperature", AsyncMock()
        ) as release:
            hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
            await hass.async_block_till_done()
            release.assert_awaited_once()

        await device.async_shutdown()


async def test_removal_says_so_when_the_slot_is_not_released(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
):
    """The module keeps a small account table, and it can refuse to free ours.

    Nothing here can fix that - the slot has to be freed from the official
    app - so the removal goes through and says what was left behind.
    """
    entry = _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, {})

    repository = AsyncMock()
    repository.del_account_info.side_effect = WfRacError("no answer")
    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=repository,
    ):
        await async_remove_entry(hass, entry)

    assert "Could not delete operator ID" in caplog.text


async def test_migrate_v6_registers_the_airco_id_as_unique_id(hass: HomeAssistant):
    """Entries added by hand never got one.

    Without a unique id zeroconf cannot recognise the entry, so a unit that
    moved was offered as a new discovery and its address was never refreshed.
    """
    entry = _entry(hass, 6, {**_DATA, CONF_HOST: "192.168.1.50"}, {})

    assert await async_migrate_entry(hass, entry)

    assert entry.version == _CURRENT_VERSION
    assert entry.unique_id == "airco-1"


async def test_migrate_v6_lowers_the_case_of_the_unique_id(hass: HomeAssistant):
    """Discovery reads the id from the announced hostname, this from the unit.

    Compared as they arrive, a difference in case would leave a configured
    unit unrecognised and its address never refreshed.
    """
    entry = _entry(
        hass, 6, {**_DATA, "airco_id": "348E89C5A137", CONF_HOST: "192.168.1.50"}, {}
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.unique_id == "348e89c5a137"


async def test_the_device_name_follows_the_entry_title(hass: HomeAssistant):
    """Home Assistant's own rename changes the title, so that is what to read.

    A name kept in entry.data would leave the registry showing the new one
    while every entity kept announcing the old.
    """
    entry = _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, {})
    hass.config_entries.async_update_entry(entry, title="Bedroom AC")

    device = await create_device_from_entry(entry, hass)

    assert device.device_name == "Bedroom AC"
