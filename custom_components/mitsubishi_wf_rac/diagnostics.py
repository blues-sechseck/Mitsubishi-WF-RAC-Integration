"""Diagnostics support for Mitsubishi WF-RAC."""

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import MitsubishiWfRacConfigEntry

# These values let a third party identify or control a unit, so diagnostic
# downloads must remain safe to attach to public issue reports.
TO_REDACT = {"operator_id", "operatorId", "device_id", "airco_id", "host"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MitsubishiWfRacConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    del hass
    device = entry.runtime_data.device

    diagnostics = {
        "config_entry": {
            "data": dict(entry.data),
            "options": dict(entry.options),
            "version": entry.version,
        },
        "device": {
            "available": device.available,
            "connection_method": device.connection_method,
            "wireless_firmware_version": device.wireless_firmware_version,
            "latest_wireless_firmware_version": (
                device.latest_wireless_firmware_version
            ),
            "firmware_update_available": device.firmware_update_available,
            "firmware_update_check_enabled": device.firmware_update_check_enabled,
            "num_accounts": device.num_accounts,
            "updated_by": device.updated_by,
            "account_expires": device.account_expires,
            "led_status": device.led_status,
            "auto_heating": device.auto_heating,
            "port": device.port,
        },
        "aircon": asdict(device.airco),
    }

    return async_redact_data(diagnostics, TO_REDACT)
