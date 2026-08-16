"""Tests for the diagnostics download."""

from dataclasses import fields
import json

from homeassistant.const import CONF_HOST
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mitsubishi_wf_rac import MitsubishiWfRacData
from custom_components.mitsubishi_wf_rac.const import DOMAIN
from custom_components.mitsubishi_wf_rac.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)
from custom_components.mitsubishi_wf_rac.wfrac.capabilities import ModelCapabilities
from custom_components.mitsubishi_wf_rac.wfrac.device import Device
from custom_components.mitsubishi_wf_rac.wfrac.models.aircon import (
    Aircon,
    HomeLeaveModeSetting,
)


async def test_diagnostics_redacts_sensitive_data_and_is_json_serializable(hass):
    """Downloads preserve useful state without exposing unit credentials."""
    sensitive_values = {
        "operator_id": "operator-secret-123",
        "operatorId": "operator-camel-secret-456",
        "device_id": "device-secret-789",
        "airco_id": "airco-secret-012",
        "host": "192.0.2.123",
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=5,
        data={
            "operator_id": sensitive_values["operator_id"],
            "operatorId": sensitive_values["operatorId"],
            "device_id": sensitive_values["device_id"],
            "airco_id": sensitive_values["airco_id"],
        },
        options={CONF_HOST: sensitive_values["host"]},
    )
    device = Device(
        hass,
        entry,
        "Test AC",
        sensitive_values["host"],
        51443,
        sensitive_values["device_id"],
        sensitive_values["operator_id"],
        sensitive_values["airco_id"],
        swing_selects_enabled_default=True,
        firmware_update_check_enabled=True,
        connection_method="https",
    )
    device._available = True  # pylint: disable=protected-access
    device._connected_accounts = 2  # pylint: disable=protected-access
    device._updated_by = "local"  # pylint: disable=protected-access
    device._account_expires = 1_725_000_000  # pylint: disable=protected-access
    device._led_status = 1  # pylint: disable=protected-access
    device._auto_heating = 0  # pylint: disable=protected-access
    device._wireless_firmware_ver = "105"  # pylint: disable=protected-access
    device._latest_wireless_firmware_ver = "106"  # pylint: disable=protected-access
    device._firmware_update_available = True  # pylint: disable=protected-access
    device._airco = Aircon(  # pylint: disable=protected-access
        ModelNrRaw=3,
        HomeLeaveModeForCooling=HomeLeaveModeSetting(18.0, 20.0, 2),
        HomeLeaveModeForHeating=None,
        CompressorFrequency=42.5,
        OperatingCurrent=None,
        HotGasTemp=55.0,
        EevPulses=123,
        IndoorCoilTemp=None,
        ProtectionRaw=7,
    )
    entry.runtime_data = MitsubishiWfRacData(device)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    serialized = json.dumps(diagnostics)

    assert all(value not in serialized for value in sensitive_values.values())
    assert set(TO_REDACT).issuperset(sensitive_values)
    assert diagnostics["config_entry"]["data"]["operator_id"] == "**REDACTED**"
    assert diagnostics["config_entry"]["options"][CONF_HOST] == "**REDACTED**"
    assert diagnostics["device"] == {
        "available": True,
        "connection_method": "https",
        "wireless_firmware_version": "105",
        "latest_wireless_firmware_version": "106",
        "firmware_update_available": True,
        "firmware_update_check_enabled": True,
        "num_accounts": 2,
        "updated_by": "local",
        "account_expires": 1_725_000_000,
        "led_status": 1,
        "auto_heating": 0,
        "port": 51443,
    }
    assert diagnostics["config_entry"]["version"] == 5
    assert set(diagnostics["aircon"]) == {field.name for field in fields(Aircon)}
    assert diagnostics["aircon"]["HomeLeaveModeForHeating"] is None
    assert diagnostics["aircon"]["OperatingCurrent"] is None
    assert diagnostics["aircon"]["Capabilities"] == {
        field.name: getattr(device.airco.Capabilities, field.name)
        for field in fields(ModelCapabilities)
    }
