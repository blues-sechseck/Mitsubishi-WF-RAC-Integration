"""Tests for where the entity service actions and the options reload live.

Both used to sit somewhere else: the actions were registered by the climate
and sensor platforms, and an entry update listener did the reload after an
options change. HA deprecated the second of those in combination with the
config flow's own reloading methods (which this integration uses), so the
options flow now reloads itself - and the actions moved along, so they no
longer depend on a platform having come up.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mitsubishi_wf_rac import async_setup_entry
from custom_components.mitsubishi_wf_rac.config_flow import WfRacOptionsFlowHandler
from custom_components.mitsubishi_wf_rac.const import (
    DOMAIN,
    SERVICE_REQUEST_HOME_LEAVE_MODE_STATUS,
    SERVICE_SET_ENERGY_TOTAL,
    SERVICE_SET_EXTERNAL_TEMPERATURE,
    SERVICE_SET_HOME_LEAVE_MODE,
    SERVICE_SET_HORIZONTAL_SWING_MODE,
    SERVICE_SET_VERTICAL_SWING_MODE,
)

_ACTIONS = (
    SERVICE_SET_HORIZONTAL_SWING_MODE,
    SERVICE_SET_VERTICAL_SWING_MODE,
    SERVICE_REQUEST_HOME_LEAVE_MODE_STATUS,
    SERVICE_SET_HOME_LEAVE_MODE,
    SERVICE_SET_EXTERNAL_TEMPERATURE,
    SERVICE_SET_ENERGY_TOTAL,
)


async def test_actions_exist_without_a_working_device(hass: HomeAssistant):
    """The whole point of registering them from async_setup: an unreachable
    device at startup must not take the actions out of the UI and out of every
    automation that references them.
    """
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    for action in _ACTIONS:
        assert hass.services.has_service(DOMAIN, action), action


async def test_options_flow_reloads_itself(hass: HomeAssistant):
    """OptionsFlowWithReload replaces the entry update listener. Keeping both
    would reload the entry twice and is deprecated as of HA 2026.6, an error
    from 2026.12 - so a set-up entry must carry no listener of ours.
    """
    assert issubclass(WfRacOptionsFlowHandler, config_entries.OptionsFlowWithReload)

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=5,
        data={"name": "AC", "device_id": "d", "operator_id": "o", "airco_id": "a", "port": 51443},
        options={CONF_HOST: "127.0.0.1"},
    )
    entry.add_to_hass(hass)
    device = MagicMock(available=True, connection_method=None, update=AsyncMock())
    with (
        patch(
            "custom_components.mitsubishi_wf_rac.create_device_from_entry",
            AsyncMock(return_value=device),
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            AsyncMock(),
        ),
    ):
        assert await async_setup_entry(hass, entry)

    assert entry.update_listeners == []
