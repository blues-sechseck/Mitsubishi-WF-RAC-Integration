"""Regression test for #219: WfRacEntity._handle_coordinator_update() must
not report a failure for an entity whose _update_state() runs cleanly.
EnergyTotalResetButton had no _update_state() at all, so this exact path
raised AttributeError on every coordinator update and called
Device.set_available(False) - needs the `hass` fixture (Device is a
DataUpdateCoordinator), hence tests/integration/ rather than tests/unit/.
"""

from unittest.mock import AsyncMock

import pytest

from custom_components.mitsubishi_wf_rac.button import EnergyTotalResetButton
from custom_components.mitsubishi_wf_rac.wfrac.device import Device


@pytest.fixture
async def device(hass):
    dev = Device(
        hass, "Test AC", "127.0.0.1", 51443, "device-id", "operator-id", "airco-id",
        create_swing_mode_select=True,
    )
    dev._api = AsyncMock()
    return dev


async def test_coordinator_update_does_not_report_failure(device, monkeypatch):
    entity = EnergyTotalResetButton(device)
    entity.async_write_ha_state = lambda: None
    reported = []
    monkeypatch.setattr(device, "set_available", lambda available: reported.append(available))

    entity._handle_coordinator_update()

    assert reported == []
