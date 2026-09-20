"""for update integration (firmware-update-available indicator).

HACS only: it reports what firmware_check.py finds, so it goes wherever
that goes.
"""
# pylint: disable = too-few-public-methods

from __future__ import annotations

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MitsubishiWfRacConfigEntry
from .const import DOMAIN
from .coordinator import Device
from .entity import WfRacEntity

# Read-only as far as the device is concerned: the coordinator does the
# polling, and nothing on this platform sends a request of its own.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MitsubishiWfRacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up update entries."""

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
    firmware_check.py. Read-only: the module only downloads and flashes
    itself while switched off, so triggering an install isn't offered here.
    """

    _attr_translation_key = "firmware_update"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    # Reports only; installing is the module's own business (see the class
    # docstring), so this belongs with the diagnostics rather than among the
    # controls on the device page.
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: Device) -> None:
        """Initialize the firmware update entity."""
        super().__init__(device)
        self._attr_unique_id = f"{DOMAIN}-{device.airco_id}-firmware-update"
        self._apply_state()

    def _mark_state_unknown(self) -> None:
        self._attr_installed_version = None
        self._attr_latest_version = None

    def _update_state(self) -> None:
        self._attr_installed_version = self.coordinator.wireless_firmware_version
        latest = self.coordinator.latest_wireless_firmware_version
        # Only report a different latest_version once the cloud check has
        # actually confirmed one is newer - UpdateEntity treats any
        # installed_version != latest_version as "update available", and the
        # background check (see Device._maybe_check_firmware_update()) may
        # not have completed yet.
        self._attr_latest_version = (
            latest
            if self.coordinator.firmware_update_available and latest
            else self._attr_installed_version
        )
