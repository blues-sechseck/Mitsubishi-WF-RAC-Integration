"""The WF-RAC sensor integration."""  # pylint: disable=invalid-name

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_NAME,
    CONF_DEVICE_ID,
    Platform,
)

from .const import (
    CONF_AIRCO_ID,
    CONF_AVAILABILITY_CHECK,
    CONF_AVAILABILITY_RETRY_LIMIT,
    CONF_CONNECTION_METHOD,
    CONF_OPERATOR_ID, CONF_CREATE_SWING_MODE_SELECT,
)
from .wfrac.device import Device

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


@dataclass
class MitsubishiWfRacData:
    """Class for storing runtime data."""
    device: Device


type MitsubishiWfRacConfigEntry = ConfigEntry[MitsubishiWfRacData]


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
        # This step used to write an "availability_retry" key that nothing ever
        # reads, and to reset CONF_AVAILABILITY_RETRY_LIMIT back to 3 over any
        # value the user had picked. Both are gone; the version bump is all that
        # is left. Entries that already ran the old step get the stale key
        # cleaned up by the v3 -> v4 step below.
        hass.config_entries.async_update_entry(entry, version=3)
    if entry.version == 3:
        new_options = dict(entry.options)
        new_options.pop("availability_retry", None)
        # The v1 -> v2 step above hard-set CONF_AVAILABILITY_CHECK to False at a
        # time when the flag was dead code (see create_device_from_entry), so
        # every entry predating v2 has been running with no retry tolerance at
        # all: one failed poll marks the device unavailable. The WF-RAC module
        # reassociates on its own roughly once an hour (#173), which a 60s poll
        # interval turns into a visible outage. Turn the check on, and lift
        # limits below 2, which are equivalent to it being off (Device.
        # _set_availability() needs limit-1 consecutive failures to tolerate).
        new_options[CONF_AVAILABILITY_CHECK] = True
        if new_options.get(CONF_AVAILABILITY_RETRY_LIMIT, 3) < 2:
            new_options[CONF_AVAILABILITY_RETRY_LIMIT] = 3

        hass.config_entries.async_update_entry(entry, options=new_options, version=4)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: MitsubishiWfRacConfigEntry) -> bool:
    """Establish connection with mitsubishi-wf-rac."""
    device: str = entry.options[CONF_HOST]
    _device = await create_device_from_entry(entry, hass)

    await _device.update()  # initial update to get fresh values
    # update() catches its own errors and reflects them via .available instead
    # of raising (see wfrac/device.py) - check that instead of try/except so a
    # device that's unreachable at startup gets HA's automatic retry-with-backoff
    # rather than a silently "loaded" entry with no working entities.
    if not _device.available:
        raise ConfigEntryNotReady(f"Could not reach device [{device}]")

    # Persist the discovered connection method (http/https) so we can skip
    # protocol discovery (and its potential extra round-trip) after the next
    # restart. Done before the update listener is registered below, so this
    # does not itself trigger a reload.
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
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def create_device_from_entry(entry: ConfigEntry, hass: HomeAssistant) -> Device:
    device: str = entry.options[CONF_HOST]
    name: str = entry.data[CONF_NAME]
    device_id: str = entry.data[CONF_DEVICE_ID]
    operator_id: str = entry.data[CONF_OPERATOR_ID]
    port: int = entry.data[CONF_PORT]
    airco_id: str = entry.data[CONF_AIRCO_ID]
    # Bugfix: config_flow.py stores this toggle under CONF_AVAILABILITY_CHECK
    # ("availability_check") via the Options Flow; the literal "availability_retry"
    # key is never written anywhere, so this always defaulted to False and the
    # retry-tolerance logic in wfrac/device.py (_availability_retry_limit) was
    # dead code even when the user enabled "Availability Check" in the UI.
    # The default here is True to match both the value new entries are created
    # with (config_flow._async_create_common) and the one the options form shows
    # for a missing key - it used to be False, so an entry without the key ran
    # with the tolerance off while the form displayed it as on.
    availability_retry: bool = entry.options.get(CONF_AVAILABILITY_CHECK, True)
    availability_retry_limit: int = entry.options.get(CONF_AVAILABILITY_RETRY_LIMIT, 3)
    create_swing_mode_select: bool = entry.data.get(CONF_CREATE_SWING_MODE_SELECT, True)
    connection_method: str | None = entry.data.get(CONF_CONNECTION_METHOD)
    _device = Device(hass, name, device, port, device_id, operator_id, airco_id, availability_retry,
                     availability_retry_limit, create_swing_mode_select,
                     connection_method=connection_method)
    return _device


async def async_unload_entry(hass: HomeAssistant, entry: MitsubishiWfRacConfigEntry) -> bool:
    """Handle unload of entry."""

    # Unload entities for this entry/device.
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        _LOGGER.info("Unloaded entry for device [%s]", entry.data[CONF_NAME])
    else:
        _LOGGER.warning("Failed to unload entry for device [%s]", entry.data[CONF_NAME])

    return unload_ok


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    reloaded = await hass.config_entries.async_reload(entry.entry_id)

    if reloaded:
        _LOGGER.info("Options updated to [%s]", entry.options)
    else:
        _LOGGER.warning("Failed to update options to [%s]", entry.options)


async def async_remove_entry(hass: HomeAssistant, entry: MitsubishiWfRacConfigEntry) -> None:
    """Handle removal of an entry."""

    temp_device = await create_device_from_entry(entry, hass)
    # delete_account() catches its own errors and returns None on failure (see
    # wfrac/device.py) rather than raising, so check the result instead of
    # try/except - the previous try/except here could never actually trigger,
    # and the "Deleted" log below used to fire unconditionally even on failure.
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
