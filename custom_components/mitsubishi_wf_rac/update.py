"""for update integration (firmware-update-available indicator)."""
# pylint: disable = too-few-public-methods

from __future__ import annotations
import logging

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity

from . import MitsubishiWfRacConfigEntry
from .entity import WfRacEntity
from .wfrac.device import Device
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry: MitsubishiWfRacConfigEntry, async_add_entities):
    """Setup update entries"""

    device: Device = entry.runtime_data.device
    # Off by default and opt-in only (see const.py's CONF_FIRMWARE_UPDATE_CHECK) -
    # this is the only entity in the integration that needs an internet
    # connection, so it's not created at all unless the user asked for it.
    if not device.firmware_update_check_enabled:
        return
    async_add_entities([FirmwareUpdateEntity(device)])


class FirmwareUpdateEntity(WfRacEntity, UpdateEntity):
    """Reports whether newer wireless-module firmware is available.

    Compares the version reported locally (device.py, from getAirconStat)
    against the manufacturer's unauthenticated getFirmware endpoint - see
    wfrac/firmware_check.py. Read-only: the module only downloads and flashes
    itself while switched off (see FUNDE.md's OTA section), so triggering an
    install isn't offered here.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "firmware_update"
    _attr_device_class = UpdateDeviceClass.FIRMWARE

    def __init__(self, device: Device) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{DOMAIN}-{device.airco_id}-firmware-update"
        self._update_state()

    def _update_state(self) -> None:
        self._attr_installed_version = self._device.wireless_firmware_version
        latest = self._device.latest_wireless_firmware_version
        # Only report a different latest_version once the cloud check has
        # actually confirmed one is newer - UpdateEntity treats any
        # installed_version != latest_version as "update available", and the
        # background check (see Device._maybe_check_firmware_update()) may
        # not have completed yet.
        self._attr_latest_version = (
            latest if self._device.firmware_update_available and latest
            else self._attr_installed_version
        )
