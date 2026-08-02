"""Proves the test harness itself works: hass fixture boots and our custom
component is discoverable via enable_custom_integrations. No device/network
involved - this only exercises manifest loading, not integration logic.
"""

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration


async def test_manifest_loads(hass: HomeAssistant) -> None:
    integration = await async_get_integration(hass, "mitsubishi_wf_rac")
    assert integration.domain == "mitsubishi_wf_rac"
    assert integration.name == "Mitsubishi WF-RAC"
