"""Tests for where the entity service actions and the options reload live.

Both used to sit somewhere else: the actions were registered by the climate
and sensor platforms, and an entry update listener did the reload after an
options change. HA deprecated the second of those in combination with the
config flow's own reloading methods (which this integration uses), so the
options flow now reloads itself - and the actions moved along, so they no
longer depend on a platform having come up.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

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


@pytest.mark.parametrize(
    "field",
    ["temp_rule_cooling", "temp_setting_cooling", "temp_rule_heating", "temp_setting_heating"],
)
async def test_set_home_leave_mode_rejects_a_temperature_the_unit_cannot_hold(
    hass: HomeAssistant, field: str
):
    """The thresholds go out as int(value * 2) in a single byte, masked rather
    than refused - anything outside the range reaches the unit as a different
    temperature.
    """
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    data = {
        "temp_rule_cooling": 35.0,
        "temp_setting_cooling": 33.0,
        "air_flow_cooling": 0,
        "temp_rule_heating": 5.0,
        "temp_setting_heating": 10.0,
        "air_flow_heating": 0,
    }
    data[field] = 200.0

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_HOME_LEAVE_MODE,
            {**data, "entity_id": "climate.not_here"},
            blocking=True,
        )


async def test_set_external_temperature_takes_a_numeric_string(hass: HomeAssistant):
    """A templated value renders as a string, and a string is not orderable
    against a float - so the schema has to coerce before it compares.
    """
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    # Passes validation; there is no entity to run it against, which the
    # service layer reports as a warning rather than raising.
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_EXTERNAL_TEMPERATURE,
        {"temperature": "21.5", "entity_id": "climate.not_here"},
        blocking=True,
    )

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_EXTERNAL_TEMPERATURE,
            {"temperature": "99", "entity_id": "climate.not_here"},
            blocking=True,
        )


async def test_options_flow_reloads_itself(hass: HomeAssistant):
    """OptionsFlowWithReload replaces the entry update listener. Keeping both
    would reload the entry twice and is deprecated as of HA 2026.6, an error
    from 2026.12 - so a set-up entry must carry no listener of ours.
    """
    assert issubclass(WfRacOptionsFlowHandler, config_entries.OptionsFlowWithReload)

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=6,
        data={
            "name": "AC",
            CONF_HOST: "127.0.0.1",
            "device_id": "d",
            "operator_id": "o",
            "airco_id": "a",
            "port": 51443,
        },
        options={},
    )
    entry.add_to_hass(hass)
    device = MagicMock(
        available=True,
        connection_method=None,
        update=AsyncMock(),
        # Awaited when hass stops at the end of the test.
        async_release_external_temperature=AsyncMock(),
    )
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
