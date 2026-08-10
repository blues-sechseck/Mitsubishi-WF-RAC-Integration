"""Tests for wfrac/device.py: update(), set_airco()'s diff-merge and locking,
and async_queue_command()'s coalescing. Repository (the HTTP layer) is
replaced with an AsyncMock - no real network involved. Needs the `hass`
fixture (Device is a DataUpdateCoordinator), hence tests/integration/ rather
than tests/unit/.
"""

import asyncio
import base64
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.mitsubishi_wf_rac.wfrac import device as device_module
from custom_components.mitsubishi_wf_rac.wfrac.device import (
    AVAILABILITY_FAILURE_LIMIT_MIN,
    Device,
)
from custom_components.mitsubishi_wf_rac.wfrac.models.aircon import Aircon, AirconCommands
from custom_components.mitsubishi_wf_rac.wfrac.rac_parser import RacParser
from custom_components.mitsubishi_wf_rac.wfrac.repository import (
    AirconApiError,
    AirconCommandError,
    AirconConnectionError,
)

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


def _shorten_service_data_timing(monkeypatch, offset_ms: int = 5) -> None:
    """Collapse the real cadence (30s offset, 5s retry delay) to something a
    test can wait out.
    """
    monkeypatch.setattr(
        device_module, "UPDATE_CONSOLIDATION_PERIOD", timedelta(milliseconds=5)
    )
    monkeypatch.setattr(
        device_module, "SERVICE_DATA_REQUEST_OFFSET", timedelta(milliseconds=offset_ms)
    )
    monkeypatch.setattr(
        device_module, "SERVICE_DATA_RETRY_DELAY", timedelta(milliseconds=5)
    )


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


async def test_update_unreachable_does_not_reregister(device, caplog):
    """An account can only have been evicted by a unit that answered - after a
    bare connection failure there is nothing to re-register against, and the
    hourly WiFi restart these modules do would make it a recurring no-op.
    """
    device._api.get_aircon_stats.side_effect = AirconConnectionError("no route")
    device._api.update_account_info = AsyncMock()
    await device.update()
    assert device.available is False
    device._api.update_account_info.assert_not_awaited()
    # One line for the user; the trace stays available on debug.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0].exc_info is None
    assert any(r.exc_info for r in caplog.records if r.levelname == "DEBUG")


async def test_update_refused_command_reregisters(device):
    """An evicted account answers (HTTP 400 / result:2) rather than timing
    out, so this path keeps the re-registration attempt.
    """
    device._api.get_aircon_stats.side_effect = AirconCommandError("refused")
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


async def test_availability_tolerates_failures_below_limit(hass):
    """The module reassociates to WiFi about once an hour and misses a poll
    while it does; only a sustained run of failures is a real outage."""
    dev = Device(
        hass, "Test AC", "127.0.0.1", 51443, "device-id", "operator-id", "airco-id",
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


async def test_availability_limit_can_be_raised_but_not_lowered(hass):
    """The option exists for weak links, where three minutes of grace isn't
    enough. Below the floor it only ever produced phantom outages, so a lower
    value is clamped rather than honoured."""
    raised = Device(
        hass, "Test AC", "127.0.0.1", 51443, "device-id", "operator-id", "airco-id",
        create_swing_mode_select=True, availability_failure_limit=5,
    )
    assert raised._availability_failure_limit == 5

    lowered = Device(
        hass, "Test AC", "127.0.0.1", 51443, "device-id", "operator-id", "airco-id",
        create_swing_mode_select=True, availability_failure_limit=1,
    )
    assert lowered._availability_failure_limit == AVAILABILITY_FAILURE_LIMIT_MIN

    lowered._api = AsyncMock()
    lowered._api.update_account_info = AsyncMock()
    lowered._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await lowered.update()

    lowered._api.get_aircon_stats.side_effect = AirconApiError("boom")
    await lowered.update()
    assert lowered.available is True  # would already be unavailable at limit 1


async def test_availability_recovers_and_resets_the_failure_count(hass):
    """A success in between must clear the run, not leave it part-way to the
    limit."""
    dev = Device(
        hass, "Test AC", "127.0.0.1", 51443, "device-id", "operator-id", "airco-id",
        create_swing_mode_select=True,
    )
    dev._api = AsyncMock()
    dev._api.update_account_info = AsyncMock()
    dev._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await dev.update()

    dev._api.get_aircon_stats.side_effect = AirconApiError("boom")
    await dev.update()
    await dev.update()
    assert dev.available is True

    dev._api.get_aircon_stats.side_effect = None
    await dev.update()
    assert dev.available is True

    dev._api.get_aircon_stats.side_effect = AirconApiError("boom")
    await dev.update()
    await dev.update()
    assert dev.available is True  # count restarted, not resumed at 2
    await dev.update()
    assert dev.available is False



# --- firmware update check (wfrac/firmware_check.py) ----------------------


def _stats_response_with_firmware(payload: str, firm_type: str, wireless_ver: str) -> dict:
    return {
        **_stats_response(payload),
        "firmType": firm_type,
        "wireless": {"firmVer": wireless_ver},
    }


async def test_update_does_not_check_firmware_when_disabled_by_default(device, monkeypatch):
    # The firmware check is the only outbound internet call in the
    # integration - it must stay off unless explicitly enabled via the
    # firmware_update_check option (see const.py's CONF_FIRMWARE_UPDATE_CHECK).
    assert device.firmware_update_check_enabled is False
    fetch = AsyncMock(return_value={"wireless": "026", "mcu": "200"})
    monkeypatch.setattr(device_module, "fetch_latest_firmware", fetch)
    device._api.get_aircon_stats.return_value = _stats_response_with_firmware(
        ON_COOL_PAYLOAD, "WF-RAC-HTTPS", "025"
    )

    await device.update()
    await device.hass.async_block_till_done()

    fetch.assert_not_awaited()
    assert device.firmware_update_available is None
    assert device.latest_wireless_firmware_version is None


async def test_update_detects_available_firmware_update(device, monkeypatch):
    device._firmware_update_check_enabled = True
    fetch = AsyncMock(return_value={"wireless": "026", "mcu": "200"})
    monkeypatch.setattr(device_module, "fetch_latest_firmware", fetch)
    device._api.get_aircon_stats.return_value = _stats_response_with_firmware(
        ON_COOL_PAYLOAD, "WF-RAC-HTTPS", "025"
    )

    await device.update()
    await device.hass.async_block_till_done()

    fetch.assert_awaited_once_with(device._hass, "WF-RAC-HTTPS")
    assert device.wireless_firmware_version == "025"
    assert device.latest_wireless_firmware_version == "026"
    assert device.firmware_update_available is True


async def test_update_does_not_flag_downgrade_or_equal_version_as_update(device, monkeypatch):
    # Strictly-greater-than only (see FUNDE.md's updateFirmware section): the
    # module silently no-ops a requested version <= its current one, so
    # neither "equal" nor "older" may be reported as an available update.
    device._firmware_update_check_enabled = True
    fetch = AsyncMock(return_value={"wireless": "025", "mcu": "200"})
    monkeypatch.setattr(device_module, "fetch_latest_firmware", fetch)
    device._api.get_aircon_stats.return_value = _stats_response_with_firmware(
        ON_COOL_PAYLOAD, "WF-RAC-HTTPS", "025"
    )

    await device.update()
    await device.hass.async_block_till_done()

    assert device.firmware_update_available is False


async def test_update_firmware_check_is_rate_limited(device, monkeypatch):
    device._firmware_update_check_enabled = True
    fetch = AsyncMock(return_value={"wireless": "026", "mcu": "200"})
    monkeypatch.setattr(device_module, "fetch_latest_firmware", fetch)
    device._api.get_aircon_stats.return_value = _stats_response_with_firmware(
        ON_COOL_PAYLOAD, "WF-RAC-HTTPS", "025"
    )

    await device.update()
    await device.hass.async_block_till_done()
    await device.update()
    await device.hass.async_block_till_done()

    fetch.assert_awaited_once()


async def test_update_firmware_check_failure_leaves_state_unknown(device, monkeypatch):
    device._firmware_update_check_enabled = True
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(device_module, "fetch_latest_firmware", fetch)
    device._api.get_aircon_stats.return_value = _stats_response_with_firmware(
        ON_COOL_PAYLOAD, "WF-RAC-HTTPS", "025"
    )

    await device.update()
    await device.hass.async_block_till_done()

    assert device.firmware_update_available is None
    assert device.latest_wireless_firmware_version is None


# --- service data request (opt-in, rac_parser.SERVICE_DATA_CODES) ---------


async def test_update_does_not_request_service_data_when_disabled_by_default(device, monkeypatch):
    # Unlike the firmware check, this stays on the local network - but it's
    # still an extra setAirconStat write on top of the regular read-only
    # poll, so it must stay off unless explicitly enabled via the
    # service_data option (see const.py's CONF_SERVICE_DATA).
    assert device.service_data_enabled is False
    monkeypatch.setattr(device_module, "UPDATE_CONSOLIDATION_PERIOD", timedelta(milliseconds=5))
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)

    await device.update()
    await asyncio.sleep(0.05)

    device._api.send_airco_command.assert_not_awaited()


async def test_update_requests_service_data_when_enabled(device, monkeypatch):
    device._service_data_enabled = True
    _shorten_service_data_timing(monkeypatch)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)

    await device.update()
    await asyncio.sleep(0.05)

    device._api.send_airco_command.assert_awaited_once()


async def test_service_data_request_is_offset_from_the_poll(device, monkeypatch):
    """It must not ride straight off the back of the status poll - landing a
    second write that close is what the unit refuses with HTTP 501 (#230).
    """
    device._service_data_enabled = True
    _shorten_service_data_timing(monkeypatch, offset_ms=40)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)

    await device.update()
    await asyncio.sleep(0.01)
    device._api.send_airco_command.assert_not_awaited()

    await asyncio.sleep(0.06)
    device._api.send_airco_command.assert_awaited_once()


async def test_service_data_request_is_retried_once_when_refused(device, monkeypatch):
    device._service_data_enabled = True
    _shorten_service_data_timing(monkeypatch)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    calls = []

    async def _refuse_then_answer(airco_id, command):
        calls.append(command)
        if len(calls) == 1:
            raise AirconCommandError("HTTP 501: Not supported this command")
        return await _echo_send_airco_command(airco_id, command)

    device._api.send_airco_command = AsyncMock(side_effect=_refuse_then_answer)

    await device.update()
    await asyncio.sleep(0.1)

    assert len(calls) == 2


async def test_service_data_request_gives_up_after_the_retry(device, monkeypatch, caplog):
    device._service_data_enabled = True
    _shorten_service_data_timing(monkeypatch)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(
        side_effect=AirconCommandError("HTTP 501: Not supported this command")
    )

    await device.update()
    await asyncio.sleep(0.1)

    assert device._api.send_airco_command.await_count == 2
    # One warning for the cycle, not one per attempt.
    refusals = [r for r in caplog.records if "refused twice" in r.message]
    assert len(refusals) == 1


async def test_update_service_data_request_is_rate_limited(device, monkeypatch):
    device._service_data_enabled = True
    _shorten_service_data_timing(monkeypatch)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)

    await device.update()
    await asyncio.sleep(0.05)
    await device.update()
    await asyncio.sleep(0.05)

    device._api.send_airco_command.assert_awaited_once()


async def test_service_data_survives_a_poll_that_answered_early(device, monkeypatch):
    """Polls arrive one interval apart, but the stamp is taken when each one
    finishes: a poll answering marginally faster than the previous one leaves
    slightly less than the interval between the two stamps. Measuring the rate
    limit against the full interval dropped those cycles (#230, 6 of 36 on the
    reporting unit).
    """
    device._service_data_enabled = True
    _shorten_service_data_timing(monkeypatch)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)
    device._last_service_data_request = datetime.now() - (
        device_module.SERVICE_DATA_REQUEST_INTERVAL - timedelta(milliseconds=100)
    )

    await device.update()
    await asyncio.sleep(0.05)

    device._api.send_airco_command.assert_awaited_once()


async def test_async_update_data_wraps_exception_in_update_failed(device):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    async def _boom():
        raise RuntimeError("unexpected")

    device.update = _boom
    with pytest.raises(UpdateFailed):
        await device._async_update_data()


async def test_async_update_data_names_the_timeout(device, monkeypatch):
    """str(TimeoutError()) is empty, so without a message of our own the
    coordinator logs "Error fetching <name> data:" and nothing else.
    """
    from homeassistant.helpers.update_coordinator import UpdateFailed

    monkeypatch.setattr(device_module, "POLL_TIMEOUT", timedelta(milliseconds=10))

    async def _hang():
        await asyncio.sleep(5)

    device.update = _hang
    with pytest.raises(UpdateFailed) as excinfo:
        await device._async_update_data()

    assert str(excinfo.value)
    assert "did not answer" in str(excinfo.value)


# --- service data is carried between polls, but not forever ---------------


async def test_service_data_is_carried_forward_between_polls(device):
    device._airco.CompressorFrequency = 40.0
    device._last_service_data_response = datetime.now()
    new_airco = Aircon()

    device._carry_forward_service_data(new_airco)

    assert new_airco.CompressorFrequency == 40.0


async def test_service_data_expires_when_nothing_fresh_arrives(device):
    """A unit that keeps refusing the request (#230) must not leave entities
    reporting a frozen value that looks live.
    """
    device._airco.CompressorFrequency = 40.0
    device._last_service_data_response = datetime.now() - (
        device_module.SERVICE_DATA_MAX_AGE + timedelta(seconds=1)
    )
    new_airco = Aircon()

    device._carry_forward_service_data(new_airco)

    assert new_airco.CompressorFrequency is None


async def test_fresh_service_data_restarts_the_clock(device):
    device._airco.CompressorFrequency = 40.0
    device._airco.HotGasTemp = 50.0
    device._last_service_data_response = datetime.now() - (
        device_module.SERVICE_DATA_MAX_AGE + timedelta(seconds=1)
    )
    new_airco = Aircon()
    new_airco.CompressorFrequency = 45.0  # this poll carried the segments

    device._carry_forward_service_data(new_airco)

    assert new_airco.CompressorFrequency == 45.0
    # The rest of the block comes with it, so they are carried again.
    assert new_airco.HotGasTemp == 50.0
