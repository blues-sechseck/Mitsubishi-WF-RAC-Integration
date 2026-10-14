"""Tests for __init__.py: config-entry migration.

The availability options were removed in v5. Older steps still reference them
because entries created under those versions carry the keys, but nothing reads
them at runtime any more - the migration's job is to leave no trace of them.
"""

import base64
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pywfrac import WfRacConnectionError, WfRacError

from custom_components.mitsubishi_wf_rac import (
    async_migrate_entry,
    async_remove_entry,
    create_device_from_entry,
)
from custom_components.mitsubishi_wf_rac.config_flow import WfRacConfigFlow
from custom_components.mitsubishi_wf_rac.const import (
    CONF_AVAILABILITY_CHECK,
    CONF_AVAILABILITY_RETRY_LIMIT,
    CONF_CONNECTION_METHOD,
    CONF_EXTERNAL_TEMPERATURE_SOURCE,
    CONF_OVERSHOOT_COOL,
    CONF_OVERSHOOT_DRY,
    CONF_OVERSHOOT_HEAT,
    CONF_STATUS_REQUEST_MODE,
    DOMAIN,
    STATUS_REQUEST_SILENT,
)
from custom_components.mitsubishi_wf_rac.coordinator import registration_full_issue_id
from custom_components.mitsubishi_wf_rac.foreign_writers import (
    status_request_unsupported_issue_id,
)
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import CONF_HOST, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from tests.unit.live_captures import LIVE_CAPTURES

_DATA = {
    "name": "Living Room AC",
    "device_id": "dev-1",
    "operator_id": "op-1",
    "airco_id": "airco-1",
    "port": 51443,
}

_CURRENT_VERSION = 8
SOURCE = CONF_EXTERNAL_TEMPERATURE_SOURCE


def _entry(
    hass: HomeAssistant, version: int, data: dict, options: dict
) -> MockConfigEntry:
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
        {
            CONF_HOST: "192.168.1.50",
            "indoor_offset": -1.5,
            CONF_AVAILABILITY_CHECK: True,
        },
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.options["indoor_offset"] == -1.5


async def test_migrate_is_idempotent_at_current_version(hass: HomeAssistant):
    options = {"indoor_offset": -1.5}
    entry = _entry(
        hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, options
    )

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


async def test_remove_entry_clears_the_registration_full_repair_issue(
    hass: HomeAssistant,
):
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
        ir.async_get(hass).async_get_issue(
            DOMAIN, registration_full_issue_id(entry.entry_id)
        )
        is None
    )


async def test_remove_entry_releases_the_account_of_a_lone_entry(
    hass: HomeAssistant,
):
    entry = _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, {})
    api = AsyncMock()
    # Unreachable, so the hand-back before the account goes has nothing to read
    # from; what it does is tested on its own below.
    api.get_aircon_stats.side_effect = WfRacConnectionError("unreachable")
    api.del_account_info.return_value = {"result": 0}

    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository", return_value=api
    ):
        await async_remove_entry(hass, entry)

    api.del_account_info.assert_awaited_once_with("airco-1")


async def test_remove_entry_releases_the_account_of_a_never_migrated_entry(
    hass: HomeAssistant,
):
    """A version 5 entry still keeps its host in options."""
    entry = _entry(hass, 5, dict(_DATA), {CONF_HOST: "192.168.1.50"})
    api = AsyncMock()
    # Unreachable, so the hand-back before the account goes has nothing to read
    # from; what it does is tested on its own below.
    api.get_aircon_stats.side_effect = WfRacConnectionError("unreachable")
    api.del_account_info.return_value = {"result": 0}

    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository", return_value=api
    ) as repository:
        await async_remove_entry(hass, entry)

    assert "192.168.1.50" in repository.call_args.args
    api.del_account_info.assert_awaited_once_with("airco-1")


async def test_remove_entry_keeps_the_account_another_entry_still_uses(
    hass: HomeAssistant,
):
    """Operator and device ids are shared, so releasing would cut off the survivor."""
    entry = _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, {})
    _entry(
        hass,
        _CURRENT_VERSION,
        {**_DATA, CONF_HOST: "192.168.1.51", "airco_id": "AIRCO-1"},
        {},
    )
    api = AsyncMock()

    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository", return_value=api
    ):
        await async_remove_entry(hass, entry)

    api.del_account_info.assert_not_awaited()


async def test_migration_renames_the_climate_unique_id_and_keeps_the_entity_id(
    hass: HomeAssistant, repository: AsyncMock
):
    """8.1 -> 8.2: the climate entity is identified by the lower-cased airco id."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=8,
        minor_version=1,
        data={**_DATA, CONF_HOST: "192.168.1.50", "airco_id": "AABBCCDDEEFF"},
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    climate = registry.async_get_or_create(
        "climate",
        DOMAIN,
        f"{DOMAIN}-AABBCCDDEEFF-climate",
        config_entry=entry,
        suggested_object_id="kept_name",
    )
    other = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{DOMAIN}-AABBCCDDEEFF-problem",
        config_entry=entry,
    )

    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=repository,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.minor_version == 2
    migrated = registry.async_get(climate.entity_id)
    assert migrated is not None
    assert migrated.unique_id == "aabbccddeeff"
    assert migrated.entity_id == "climate.kept_name"
    assert (
        registry.async_get(other.entity_id).unique_id
        == f"{DOMAIN}-aabbccddeeff-problem"
    )
    assert (
        sum(
            1
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
            if e.domain == "climate"
        )
        == 1
    )


async def _setup_81(hass, repository, entry):
    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=repository,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def _entry_81(airco_id: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=8,
        minor_version=1,
        data={**_DATA, CONF_HOST: "192.168.1.50", "airco_id": airco_id},
    )


async def test_migration_lower_cases_the_identities_but_not_the_stored_id(
    hass: HomeAssistant, repository: AsyncMock
):
    entry = _entry_81("AABBCCDDEEFF")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "AABBCCDDEEFF"), ("other", "keep")},
    )
    old_ids = {
        ("climate", f"{DOMAIN}-AABBCCDDEEFF-climate"): "aabbccddeeff",
        ("sensor", f"{DOMAIN}-AABBCCDDEEFF-energy-sensor"): (
            f"{DOMAIN}-aabbccddeeff-energy-sensor"
        ),
        ("switch", f"{DOMAIN}-AABBCCDDEEFF-silent-operation"): (
            f"{DOMAIN}-aabbccddeeff-silent-operation"
        ),
        ("select", f"{DOMAIN}-AABBCCDDEEFF-fan-speed"): (
            f"{DOMAIN}-aabbccddeeff-fan-speed"
        ),
        ("binary_sensor", f"{DOMAIN}-AABBCCDDEEFF-problem"): (
            f"{DOMAIN}-aabbccddeeff-problem"
        ),
    }
    created = {
        key: registry.async_get_or_create(
            key[0],
            DOMAIN,
            key[1],
            config_entry=entry,
            device_id=device.id,
            suggested_object_id=f"kept_{key[0]}",
        )
        for key in old_ids
    }

    await _setup_81(hass, repository, entry)

    assert entry.minor_version == 2
    # What the unit is sent stays as it reported itself.
    assert entry.data["airco_id"] == "AABBCCDDEEFF"
    assert repository.get_aircon_stats.await_args.args[0] == "AABBCCDDEEFF"
    for key, new in old_ids.items():
        migrated = registry.async_get(created[key].entity_id)
        assert migrated is not None
        assert migrated.entity_id == created[key].entity_id
        assert migrated.unique_id == new
    updated = device_registry.async_get(device.id)
    assert updated.identifiers == {(DOMAIN, "aabbccddeeff"), ("other", "keep")}


async def test_migration_of_a_lower_case_entry_leaves_ids_alone(
    hass: HomeAssistant, repository: AsyncMock
):
    entry = _entry_81("aabbccddeeff")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "aabbccddeeff")}
    )
    other = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{DOMAIN}-aabbccddeeff-problem",
        config_entry=entry,
        device_id=device.id,
    )

    await _setup_81(hass, repository, entry)

    assert entry.minor_version == 2
    assert entry.data["airco_id"] == "aabbccddeeff"
    assert registry.async_get(other.entity_id).unique_id == other.unique_id
    assert device_registry.async_get(device.id).identifiers == {
        (DOMAIN, "aabbccddeeff")
    }


async def test_migration_leaves_a_colliding_unique_id_alone(
    hass: HomeAssistant, repository: AsyncMock, caplog: pytest.LogCaptureFixture
):
    entry = _entry_81("AABBCCDDEEFF")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{DOMAIN}-AABBCCDDEEFF-problem",
        config_entry=entry,
        suggested_object_id="old_problem",
    )
    taken = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{DOMAIN}-aabbccddeeff-problem",
        config_entry=entry,
        suggested_object_id="taken_problem",
    )

    await _setup_81(hass, repository, entry)

    assert entry.minor_version == 2
    assert registry.async_get(old.entity_id).unique_id == old.unique_id
    assert registry.async_get(taken.entity_id).unique_id == taken.unique_id
    assert "is already in use" in caplog.text


async def test_a_current_entry_creates_the_climate_under_the_new_unique_id(
    hass: HomeAssistant, repository: AsyncMock
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=8,
        minor_version=2,
        data={**_DATA, CONF_HOST: "192.168.1.50", "airco_id": "AABBCCDDEEFF"},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=repository,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    climates = [
        e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.domain == "climate"
    ]
    assert [e.unique_id for e in climates] == ["aabbccddeeff"]


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

    # A real dict, not a bare mock: the poll reads named fields out of the
    # answer, and a mock answers every one of them with another mock.
    repository = AsyncMock()
    repository.get_aircon_stats.return_value = {}

    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=repository,
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
            device, "async_hand_back_room_sensor", AsyncMock()
        ) as release:
            await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            release.assert_not_awaited()

        device = entry.runtime_data.device
        with patch.object(
            device, "async_hand_back_room_sensor", AsyncMock()
        ) as release:
            hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
            await hass.async_block_till_done()
            release.assert_awaited_once()

        await device.async_shutdown()


async def test_disabling_the_entry_hands_the_override_back_but_a_reload_does_not(
    hass: HomeAssistant, repository: AsyncMock
):
    """Disabling is an unload nothing follows with a setup, so the value would
    stand at the unit - the reload that every saved option causes is followed
    by one, and re-arms within seconds.
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
            device, "async_hand_back_room_sensor", AsyncMock()
        ) as hand_back:
            await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            hand_back.assert_not_awaited()

        device = entry.runtime_data.device
        with patch.object(
            device, "async_hand_back_room_sensor", AsyncMock()
        ) as hand_back:
            await hass.config_entries.async_set_disabled_by(
                entry.entry_id, ConfigEntryDisabler.USER
            )
            await hass.async_block_till_done()
            hand_back.assert_awaited_once()


async def test_removal_hands_the_override_back_before_the_account_goes(
    hass: HomeAssistant, repository: AsyncMock
):
    """The frame is sent under the account: without it the module refuses, and
    a removed unit would keep the last value it was given."""
    entry = _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, {})
    calls: list[str] = []

    async def _send(_airco_id, command, **_kwargs):
        raw = base64.b64decode(command)
        calls.append(f"send:{raw[5]}")
        return LIVE_CAPTURES["on_cool"][0]

    async def _delete(_airco_id):
        calls.append("delete")
        return {"result": 0}

    repository.send_airco_command = AsyncMock(side_effect=_send)
    repository.del_account_info = AsyncMock(side_effect=_delete)
    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=repository,
    ):
        await async_remove_entry(hass, entry)

    assert calls == ["send:255", "delete"]


async def test_removal_leaves_the_unit_alone_while_another_entry_uses_the_slot(
    hass: HomeAssistant, repository: AsyncMock
):
    entry = _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, {})
    _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, {})
    repository.send_airco_command = AsyncMock()
    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=repository,
    ):
        await async_remove_entry(hass, entry)

    repository.send_airco_command.assert_not_awaited()


async def _set_up(
    hass: HomeAssistant, repository: AsyncMock, data: dict, options: dict
):
    entry = _entry(
        hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50", **data}, options
    )
    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=repository,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.mark.parametrize(
    ("data", "options", "expected"),
    [
        ({CONF_STATUS_REQUEST_MODE: STATUS_REQUEST_SILENT}, {SOURCE: "sensor.room"}, 1),
        ({CONF_STATUS_REQUEST_MODE: STATUS_REQUEST_SILENT}, {}, 0),
        ({}, {SOURCE: "sensor.room"}, 0),
    ],
)
async def test_a_silent_entry_with_a_source_hands_back_once_at_setup(
    hass: HomeAssistant, repository: AsyncMock, data, options, expected
):
    """The previous run may have left the source's last value standing, and a
    blocked feed never sends it again - so setup says so once, without clearing
    what the source armed."""
    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Device."
        "async_hand_back_room_sensor",
        AsyncMock(return_value=True),
    ) as hand_back:
        entry = await _set_up(hass, repository, data, options)
        assert hand_back.await_count == expected
        if expected:
            hand_back.assert_awaited_once_with(clear=False)

    await entry.runtime_data.device.async_shutdown()


async def test_a_silent_entry_gets_its_repair_back_at_setup(
    hass: HomeAssistant, repository: AsyncMock
):
    """The repair was not persistent when the mode was learned (#416): after a
    restart the entry was silent with nothing to say so."""
    entry = await _set_up(
        hass, repository, {CONF_STATUS_REQUEST_MODE: STATUS_REQUEST_SILENT}, {}
    )

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, status_request_unsupported_issue_id(entry.entry_id)
    )
    assert issue is not None
    assert issue.is_persistent is True
    assert issue.is_fixable is True
    assert issue.data == {"entry_id": entry.entry_id}

    await entry.runtime_data.device.async_shutdown()


async def test_a_strict_entry_gets_no_repair_at_setup(
    hass: HomeAssistant, repository: AsyncMock
):
    entry = await _set_up(hass, repository, {}, {})

    assert not ir.async_get(hass).issues

    await entry.runtime_data.device.async_shutdown()


async def test_removal_says_so_when_the_slot_is_not_released(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
):
    """The module keeps a small account table, and it can refuse to free ours.

    Nothing here can fix that - the slot has to be freed from the official
    app - so the removal goes through and says what was left behind.
    """
    entry = _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, {})

    repository = AsyncMock()
    repository.get_aircon_stats.side_effect = WfRacError("no answer")
    repository.del_account_info.side_effect = WfRacError("no answer")
    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.Repository",
        return_value=repository,
    ):
        await async_remove_entry(hass, entry)

    assert "Could not release the controller slot" in caplog.text


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


async def test_migrate_v7_shifts_the_overshoots_onto_the_manufacturer_scale(
    hass: HomeAssistant,
):
    """The room temperature used to reach the unit half a kelvin warm; pywfrac
    now encodes it on the manufacturer's scale. A stored figure moves by that
    constant so the byte the unit receives on upgrade is the one it received
    before - subtracted in cooling and dry, added in heating.
    """
    entry = _entry(
        hass,
        7,
        {**_DATA, CONF_HOST: "192.168.1.50"},
        {
            CONF_OVERSHOOT_COOL: 1.0,
            CONF_OVERSHOOT_DRY: 0.5,
            CONF_OVERSHOOT_HEAT: 0.75,
            "indoor_offset": -1.5,
        },
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.version == _CURRENT_VERSION
    assert entry.options[CONF_OVERSHOOT_COOL] == 0.5
    assert entry.options[CONF_OVERSHOOT_DRY] == 0.0
    assert entry.options[CONF_OVERSHOOT_HEAT] == 1.25
    assert entry.options["indoor_offset"] == -1.5


async def test_migrate_v7_leaves_a_zero_overshoot_alone(hass: HomeAssistant):
    """Zero meant "hand the unit the reading as it is" - and the reading it
    now gets is the one it was always meant to. Absent fields stay absent."""
    entry = _entry(
        hass,
        7,
        {**_DATA, CONF_HOST: "192.168.1.50"},
        {CONF_OVERSHOOT_COOL: 0.0, CONF_OVERSHOOT_DRY: 0},
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.options[CONF_OVERSHOOT_COOL] == 0.0
    assert entry.options[CONF_OVERSHOOT_DRY] == 0
    assert CONF_OVERSHOOT_HEAT not in entry.options


async def test_migrate_v7_keeps_the_shifted_overshoot_inside_the_range(
    hass: HomeAssistant,
):
    entry = _entry(
        hass,
        7,
        {**_DATA, CONF_HOST: "192.168.1.50"},
        {CONF_OVERSHOOT_COOL: -5.0, CONF_OVERSHOOT_HEAT: 5.0},
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.options[CONF_OVERSHOOT_COOL] == -5.0
    assert entry.options[CONF_OVERSHOOT_HEAT] == 5.0


async def test_the_device_name_follows_the_entry_title(hass: HomeAssistant):
    """Home Assistant's own rename changes the title, so that is what to read.

    A name kept in entry.data would leave the registry showing the new one
    while every entity kept announcing the old.
    """
    entry = _entry(hass, _CURRENT_VERSION, {**_DATA, CONF_HOST: "192.168.1.50"}, {})
    hass.config_entries.async_update_entry(entry, title="Bedroom AC")

    device = await create_device_from_entry(entry, hass)

    assert device.device_name == "Bedroom AC"
