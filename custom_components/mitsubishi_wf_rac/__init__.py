"""The WF-RAC sensor integration."""  # pylint: disable=invalid-name

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_DEVICE_ID,
    Platform,
)

from .const import (
    CONF_AIRCO_ID,
    CONF_AVAILABILITY_CHECK,
    CONF_AVAILABILITY_RETRY_LIMIT,
    CONF_CARRY_POWER_STATE,
    CONF_STATUS_REQUEST_MODE,
    CONF_CONNECTION_METHOD,
    CONF_FIRMWARE_UPDATE_CHECK,
    CONF_OPERATOR_ID, CONF_CREATE_SWING_MODE_SELECT,
    DOMAIN,
    STATUS_REQUEST_ECHO,
    STATUS_REQUEST_STRICT,
)
from .coordinator import (
    AVAILABILITY_FAILURE_LIMIT_MIN,
    Device,
    registration_full_issue_id,
    request_stops_unit_issue_id,
    service_data_unanswered_issue_id,
    status_request_unsupported_issue_id,
)
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]


@dataclass
class MitsubishiWfRacData:
    """Class for storing runtime data."""
    device: Device


type MitsubishiWfRacConfigEntry = ConfigEntry[MitsubishiWfRacData]


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration's entity service actions.

    They live here rather than in the platforms so a config entry that fails
    to set up - an unreachable device at startup, say - doesn't take the
    actions down with it.
    """
    async_setup_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entry."""

    if entry.version == 1:
        new_data = entry.data.copy()
        new_options = {
            CONF_HOST: new_data.pop(CONF_HOST),
            CONF_AVAILABILITY_CHECK: False,
            CONF_AVAILABILITY_RETRY_LIMIT: 3,
        }

        hass.config_entries.async_update_entry(
            entry, data=new_data, options=new_options, version=2
        )
    if entry.version == 2:
        # Nothing to change here any more; the v3 -> v4 step below clears
        # what this step once wrote.
        hass.config_entries.async_update_entry(entry, version=3)
    if entry.version == 3:
        new_options = dict(entry.options)
        new_options.pop("availability_retry", None)
        # The v1 -> v2 step above hard-set CONF_AVAILABILITY_CHECK to False at a
        # time when the flag was dead code (see create_device_from_entry), so
        # every entry predating v2 has been running with no retry tolerance at
        # all: one failed poll marks the device unavailable. The WF-RAC module
        # reassociates on its own roughly once an hour, which a 60s poll
        # interval turns into a visible outage. Turn the check on, and lift
        # limits below 2, which are equivalent to it being off (Device.
        # _set_availability() needs limit-1 consecutive failures to tolerate).
        new_options[CONF_AVAILABILITY_CHECK] = True
        if new_options.get(CONF_AVAILABILITY_RETRY_LIMIT, 3) < 2:
            new_options[CONF_AVAILABILITY_RETRY_LIMIT] = 3

        hass.config_entries.async_update_entry(entry, options=new_options, version=4)
    if entry.version == 4:
        # Drop the on/off toggle and put a floor under the retry limit. The
        # toggle was never a defensible choice - the module's hourly
        # reassociation makes some tolerance always right, and switching it off
        # was arithmetically identical to a limit of 1. Raising the limit is a
        # real choice on a weak link, so the number stays; only values below
        # AVAILABILITY_FAILURE_LIMIT_MIN are lifted, which is what the v3 -> v4
        # step above was already having to do by hand.
        new_options = dict(entry.options)
        new_options.pop(CONF_AVAILABILITY_CHECK, None)
        new_options[CONF_AVAILABILITY_RETRY_LIMIT] = max(
            AVAILABILITY_FAILURE_LIMIT_MIN,
            new_options.get(CONF_AVAILABILITY_RETRY_LIMIT, AVAILABILITY_FAILURE_LIMIT_MIN),
        )

        hass.config_entries.async_update_entry(entry, options=new_options, version=5)
    if entry.version == 5:
        # Move the host back into entry.data, where connection-critical data
        # belongs. It has lived in options since v2 so the options dialog
        # could edit it, which the reconfigure flow does now - and options
        # was the wrong home for a second reason: the discovery helper that
        # refreshes a changed address (_abort_if_unique_id_configured with
        # updates=) only ever merges into entry.data, so a unit that moved to
        # a new IP had the refresh written to a key setup never reads, and
        # kept being polled at the old address.
        new_data = dict(entry.data)
        new_options = dict(entry.options)
        if CONF_HOST in new_options:
            new_data[CONF_HOST] = new_options.pop(CONF_HOST)

        hass.config_entries.async_update_entry(
            entry, data=new_data, options=new_options, version=6
        )
    if entry.version == 6:
        # Entries added by hand never got a unique id: the manual step checked
        # for a duplicate airco itself instead of registering one. Without it
        # zeroconf cannot recognise the entry, so a unit that moved was offered
        # as a new discovery and its address was never refreshed. The module
        # announces itself as <mac>.local and the airco id is that same MAC, so
        # this is the identity discovery already matches on - lower case,
        # because the two sides supply it in whatever case they read it.
        hass.config_entries.async_update_entry(
            entry, unique_id=entry.data[CONF_AIRCO_ID].lower(), version=7
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: MitsubishiWfRacConfigEntry) -> bool:
    """Establish connection with mitsubishi-wf-rac."""
    device: str = entry.data[CONF_HOST]
    _device = await create_device_from_entry(entry, hass)

    await _device.update()  # initial update to get fresh values
    # update() catches its own errors and reflects them via .available instead
    # of raising (see coordinator.py) - check that instead of try/except so a
    # device that's unreachable at startup gets HA's automatic retry-with-backoff
    # rather than a silently "loaded" entry with no working entities.
    if not _device.available:
        # No positional message: HomeAssistantError only renders the
        # translation when it is constructed without one.
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"device": device},
        )

    # Persist the discovered connection method (http/https) so we can skip
    # protocol discovery (and its potential extra round-trip) after the next
    # restart. Writing entry.data here is safe on its own: nothing listens for
    # entry updates any more, the options flow reloads itself instead.
    method = _device.connection_method
    if method and entry.data.get(CONF_CONNECTION_METHOD) != method:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_CONNECTION_METHOD: method}
        )
        _LOGGER.debug(
            "Persisted connection method [%s] for device [%s]", method, device
        )

    entry.runtime_data = MitsubishiWfRacData(_device)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def create_device_from_entry(entry: ConfigEntry, hass: HomeAssistant) -> Device:
    device: str = entry.data[CONF_HOST]
    # The entry title, not a stored name: that is what Home Assistant's own
    # rename changes, and a name kept in entry.data would quietly ignore it.
    name: str = entry.title
    device_id: str = entry.data[CONF_DEVICE_ID]
    operator_id: str = entry.data[CONF_OPERATOR_ID]
    port: int = entry.data[CONF_PORT]
    airco_id: str = entry.data[CONF_AIRCO_ID]
    swing_selects_enabled_default: bool = entry.data.get(CONF_CREATE_SWING_MODE_SELECT, True)
    # Off unless the user explicitly opted in via the options flow - this is
    # the only outbound internet call in the integration (see
    # coordinator.py's _maybe_check_firmware_update()).
    firmware_update_check_enabled: bool = entry.options.get(CONF_FIRMWARE_UPDATE_CHECK, False)
    # Floored in Device itself, so an entry that predates the v4 -> v5
    # migration can't run with less tolerance than the module needs.
    availability_failure_limit: int = entry.options.get(
        CONF_AVAILABILITY_RETRY_LIMIT, AVAILABILITY_FAILURE_LIMIT_MIN
    )
    connection_method: str | None = entry.data.get(CONF_CONNECTION_METHOD)
    # Not migrated in async_migrate_entry: this is learned state, not
    # configuration, and an entry that has never met the fault simply has
    # neither key. The old one is honoured where it exists so a beta tester
    # who already paid for the discovery does not pay again.
    status_request_mode: str = entry.data.get(
        CONF_STATUS_REQUEST_MODE,
        STATUS_REQUEST_ECHO
        if entry.data.get(CONF_CARRY_POWER_STATE, False)
        else STATUS_REQUEST_STRICT,
    )
    _device = Device(hass, entry, name, device, port, device_id, operator_id, airco_id,
                     swing_selects_enabled_default,
                     availability_failure_limit=availability_failure_limit,
                     firmware_update_check_enabled=firmware_update_check_enabled,
                     connection_method=connection_method,
                     status_request_mode=status_request_mode)
    return _device


async def async_unload_entry(hass: HomeAssistant, entry: MitsubishiWfRacConfigEntry) -> bool:
    """Handle unload of entry."""

    # Unload entities for this entry/device.
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # The coordinator can hold a listener of its own (the carrier for an armed
    # external temperature override), which would outlive the entry and keep
    # its refresh timer running. Only tear it down once the entities are
    # really gone: if unloading the platforms failed they stay loaded, and a
    # stopped coordinator would leave them on an entry that never updates
    # again. An entry whose setup never stored its runtime data has no
    # coordinator to shut down at all.
    if unload_ok and (data := getattr(entry, "runtime_data", None)) is not None:
        await data.device.async_shutdown()

    if unload_ok:
        _LOGGER.info("Unloaded entry for device [%s]", entry.title)
    else:
        _LOGGER.warning("Failed to unload entry for device [%s]", entry.title)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: MitsubishiWfRacConfigEntry) -> None:
    """Handle removal of an entry."""

    temp_device = await create_device_from_entry(entry, hass)
    # delete_account() returns None on failure rather than raising, so the
    # result is what says whether the slot was actually released.
    result = await temp_device.delete_account()
    if result is not None:
        _LOGGER.info(
            "Deleted operator ID [%s] from airco [%s]",
            temp_device.operator_id,
            temp_device.airco_id,
        )
    else:
        _LOGGER.warning(
            "Could not delete operator ID [%s] from airco [%s]",
            temp_device.operator_id,
            temp_device.airco_id,
        )

    # Entry-scoped, so it would otherwise dangle in the repair list forever
    # pointing at an entry_id that no longer resolves to anything.
    ir.async_delete_issue(hass, DOMAIN, registration_full_issue_id(entry.entry_id))
    ir.async_delete_issue(hass, DOMAIN, request_stops_unit_issue_id(entry.entry_id))
    ir.async_delete_issue(
        hass, DOMAIN, status_request_unsupported_issue_id(entry.entry_id)
    )
    ir.async_delete_issue(
        hass, DOMAIN, service_data_unanswered_issue_id(entry.entry_id)
    )
