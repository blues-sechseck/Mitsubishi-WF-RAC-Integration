"""Tests for wfrac/device.py: update(), set_airco()'s diff-merge and locking,
and async_queue_command()'s coalescing. Repository (the HTTP layer) is
replaced with an AsyncMock - no real network involved. Needs the `hass`
fixture (Device is a DataUpdateCoordinator), hence tests/integration/ rather
than tests/unit/.
"""

import asyncio
import base64
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.mitsubishi_wf_rac.wfrac import device as device_module
from custom_components.mitsubishi_wf_rac.wfrac.device import Device
from custom_components.mitsubishi_wf_rac.wfrac.models.aircon import AirconCommands
from custom_components.mitsubishi_wf_rac.wfrac.rac_parser import RacParser
from custom_components.mitsubishi_wf_rac.wfrac.repository import AirconApiError

from ..unit.live_captures import LIVE_CAPTURES

OFF_PAYLOAD, _ = LIVE_CAPTURES["off"]
ON_COOL_PAYLOAD, _ = LIVE_CAPTURES["on_cool"]


def _stats_response(payload: str) -> dict:
    return {
        "numOfAccount": 1,
        "airconStat": payload,
        "updatedBy": "local",
    }


def _build_stat_response(content: list[int]) -> str:
    """Wrap 18 receive-format content bytes into a translate_bytes()-parseable
    payload, with a start_length header of 21 (index 18 = 0) and an empty
    temperature segment - mirrors the envelope real device responses use.
    """
    assert len(content) == 18
    prefix = [0] * 21
    tail = [0, 0]
    raw = bytes((b & 0xFF) for b in (prefix + list(content) + tail))
    return base64.b64encode(raw).decode()


async def _echo_send_airco_command(_airco_id, command):
    """Fake device: decode the sent command's receive-segment (the same
    layout _parse_basic_settings expects) and echo it back as the new
    reported state - models how the real device echoes the state it just
    applied.
    """
    raw = base64.b64decode(command)
    signed = [(256 - a) * -1 if a > 127 else a for a in raw]
    receive_content = signed[25:43]
    return _build_stat_response(receive_content)


@pytest.fixture
async def device(hass):
    dev = Device(
        hass,
        "Test AC",
        "127.0.0.1",
        51443,
        "device-id",
        "operator-id",
        "airco-id",
        availability_retry=False,
        availability_retry_limit=3,
        create_swing_mode_select=True,
    )
    dev._api = AsyncMock()
    return dev


# --- update() -------------------------------------------------------------


async def test_update_success_marks_available_and_parses_state(device):
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    assert device.available is True
    assert device.airco.Operation is True


async def test_update_none_response_marks_unavailable(device):
    device._api.get_aircon_stats.return_value = None
    await device.update()
    assert device.available is False


async def test_update_api_error_marks_unavailable_and_reregisters(device):
    device._api.get_aircon_stats.side_effect = AirconApiError("boom")
    device._api.update_account_info = AsyncMock()
    await device.update()
    assert device.available is False
    device._api.update_account_info.assert_awaited_once()


async def test_update_malformed_stat_marks_unavailable(device):
    device._api.get_aircon_stats.return_value = {
        "numOfAccount": 1,
        "airconStat": "not valid base64!!!",
    }
    await device.update()
    assert device.available is False


# --- set_airco(): diff is merged with current state, not just the params -


async def test_set_airco_merges_params_with_current_state(device):
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    assert device.airco.Operation is False
    original_preset_temp = device.airco.PresetTemp

    captured = {}

    async def _capture_and_echo(airco_id, command):
        captured["command"] = command
        return await _echo_send_airco_command(airco_id, command)

    device._api.send_airco_command = AsyncMock(side_effect=_capture_and_echo)

    await device.set_airco({AirconCommands.Operation: True})

    raw = base64.b64decode(captured["command"])
    signed = [(256 - a) * -1 if a > 127 else a for a in raw]
    receive_content = signed[25:43]
    from custom_components.mitsubishi_wf_rac.wfrac.models.aircon import Aircon

    sent = Aircon()
    RacParser()._parse_basic_settings(sent, receive_content)

    # The changed field is in the sent command...
    assert sent.Operation is True
    # ...and the untouched field from the pre-existing state was carried
    # along, not reset to a default - this is the "full state block per
    # request" behavior the whole coalescing/locking design exists for.
    assert sent.PresetTemp == original_preset_temp
    assert device.airco.Operation is True


async def test_set_airco_raises_and_logs_on_send_failure(device):
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._api.send_airco_command = AsyncMock(side_effect=AirconApiError("boom"))

    with pytest.raises(AirconApiError):
        await device.set_airco({AirconCommands.Operation: True})


async def test_set_airco_fetches_state_first_if_unset(device):
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)
    # Device.airco defaults to an empty Aircon(), not None, so this path
    # relies on `self.airco is None` - simulate the "genuinely unset" case.
    device._airco = None

    await device.set_airco({AirconCommands.Operation: True})

    device._api.get_aircon_stats.assert_awaited_once()
    assert device.airco is not None


# --- set_airco()'s lock: a call must never snapshot stale state while ----
# another set_airco() call is still in flight (see conversation - this is
# the actual fix for the fan/temperature command collision bug).


async def test_set_airco_lock_prevents_stale_snapshot_race(device):
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()

    release_first_call = asyncio.Event()
    call_order = []

    async def _slow_first_then_fast(airco_id, command):
        if not call_order:
            call_order.append("first_started")
            await release_first_call.wait()
            call_order.append("first_finished")
        else:
            call_order.append("second_finished")
        return await _echo_send_airco_command(airco_id, command)

    device._api.send_airco_command = AsyncMock(side_effect=_slow_first_then_fast)

    first_task = asyncio.ensure_future(
        device.set_airco({AirconCommands.AirFlow: 3})
    )
    await asyncio.sleep(0)  # let the first call enter the lock and start "sending"
    assert call_order == ["first_started"]

    second_task = asyncio.ensure_future(
        device.set_airco({AirconCommands.PresetTemp: 24.0})
    )
    await asyncio.sleep(0)
    # Second call must be blocked on the lock, not sending yet.
    assert call_order == ["first_started"]

    release_first_call.set()
    await first_task
    await second_task

    assert call_order == ["first_started", "first_finished", "second_finished"]
    # Both changes must have landed - the second call's snapshot must have
    # been built from the first call's already-committed result.
    assert device.airco.AirFlow == 3
    assert device.airco.PresetTemp == 24.0


# --- async_queue_command(): coalesces calls within the consolidation window


async def test_async_queue_command_coalesces_into_one_send(device, monkeypatch):
    monkeypatch.setattr(device_module, "UPDATE_CONSOLIDATION_PERIOD", timedelta(milliseconds=5))
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)

    await device.async_queue_command({AirconCommands.AirFlow: 2})
    await device.async_queue_command({AirconCommands.PresetTemp: 25.0})

    await asyncio.sleep(0.05)

    device._api.send_airco_command.assert_awaited_once()
    assert device.airco.AirFlow == 2
    assert device.airco.PresetTemp == 25.0


async def test_async_queue_command_notifies_listeners(device, monkeypatch):
    monkeypatch.setattr(device_module, "UPDATE_CONSOLIDATION_PERIOD", timedelta(milliseconds=5))
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)

    # Coordinator listeners are plain sync callbacks (see
    # DataUpdateCoordinator.async_update_listeners(): `update_callback()`,
    # not awaited) - a MagicMock, not AsyncMock.
    listener = MagicMock()
    unsubscribe = device.async_add_listener(listener)
    try:
        await device.async_queue_command({AirconCommands.Operation: True})
        await asyncio.sleep(0.05)
        listener.assert_called()
    finally:
        # Registering the first listener schedules the coordinator's
        # periodic refresh interval; leaving it running trips the test
        # harness's lingering-timer check at teardown.
        unsubscribe()


async def test_async_queue_command_notifies_listeners_even_on_failure(device, monkeypatch):
    monkeypatch.setattr(device_module, "UPDATE_CONSOLIDATION_PERIOD", timedelta(milliseconds=5))
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._api.send_airco_command = AsyncMock(side_effect=AirconApiError("boom"))

    listener = MagicMock()
    unsubscribe = device.async_add_listener(listener)
    try:
        await device.async_queue_command({AirconCommands.Operation: True})
        await asyncio.sleep(0.05)
        listener.assert_called()
    finally:
        unsubscribe()


# --- misc: properties, delete_account(), availability retry, coordinator -


async def test_properties_reflect_constructor_args(device):
    assert device.device_name == "Test AC"
    assert device.host == "127.0.0.1"
    assert device.port == 51443
    assert device.device_id == "device-id"
    assert device.airco_id == "airco-id"
    assert device.create_swing_mode_select is True
    assert device.device_info["name"] == "Test AC"
    assert device.device_info["identifiers"] == {("mitsubishi_wf_rac", "airco-id")}


async def test_delete_account_success(device):
    device._api.del_account_info = AsyncMock(return_value={"result": 0})
    result = await device.delete_account()
    assert result == {"result": 0}
    device._api.del_account_info.assert_awaited_once_with("airco-id")


async def test_delete_account_failure_returns_none(device):
    device._api.del_account_info = AsyncMock(side_effect=AirconApiError("boom"))
    assert await device.delete_account() is None


async def test_availability_retry_tolerates_failures_below_limit(hass):
    dev = Device(
        hass, "Test AC", "127.0.0.1", 51443, "device-id", "operator-id", "airco-id",
        availability_retry=True, availability_retry_limit=3,
        create_swing_mode_select=True,
    )
    dev._api = AsyncMock()
    dev._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await dev.update()
    assert dev.available is True

    dev._api.get_aircon_stats.side_effect = AirconApiError("boom")
    dev._api.update_account_info = AsyncMock()

    await dev.update()
    assert dev.available is True  # 1st failure - within tolerance
    await dev.update()
    assert dev.available is True  # 2nd failure - still within tolerance
    await dev.update()
    assert dev.available is False  # 3rd failure - limit reached


async def test_availability_retry_disabled_fails_immediately(hass):
    dev = Device(
        hass, "Test AC", "127.0.0.1", 51443, "device-id", "operator-id", "airco-id",
        availability_retry=False, availability_retry_limit=3,
        create_swing_mode_select=True,
    )
    dev._api = AsyncMock()
    dev._api.get_aircon_stats.side_effect = AirconApiError("boom")
    dev._api.update_account_info = AsyncMock()

    await dev.update()
    assert dev.available is False


async def test_async_update_data_wraps_exception_in_update_failed(device):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    async def _boom():
        raise RuntimeError("unexpected")

    device.update = _boom
    with pytest.raises(UpdateFailed):
        await device._async_update_data()
