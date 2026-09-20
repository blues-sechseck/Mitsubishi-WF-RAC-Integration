"""Fixtures shared by the integration tests."""

from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mitsubishi_wf_rac.const import DOMAIN
from custom_components.mitsubishi_wf_rac.coordinator import Device
from tests.unit.live_captures import LIVE_CAPTURES


@pytest.fixture
async def platform_device(hass):
    """A Device holding one parsed live capture, with the API mocked out."""
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    device = Device(
        hass,
        entry,
        "Test AC",
        "127.0.0.1",
        51443,
        "device-id",
        "operator-id",
        "airco-id",
        swing_selects_enabled_default=True,
    )
    device._api = AsyncMock()
    device._api.get_aircon_stats.return_value = {
        "numOfAccount": 1,
        "airconStat": LIVE_CAPTURES["on_cool"][0],
    }
    await device.update()
    yield device
    # Same teardown the other Device fixtures have. A test that leaves a
    # listener on the coordinator - an armed override's carrier frame, or the
    # release one owed after it is cleared - leaves its refresh timer
    # scheduled with it, which the harness flags as a lingering timer.
    await device.async_shutdown()


@pytest.fixture
def repository() -> AsyncMock:
    """A module that answers every poll with one parsed live capture."""
    api = AsyncMock()
    api.get_aircon_stats.return_value = {
        "numOfAccount": 1,
        "airconStat": LIVE_CAPTURES["on_cool"][0],
    }
    # Persisted into entry.data, so it has to be storable.
    api.method = "https"
    return api
