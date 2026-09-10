"""Tests for wfrac/device.py: update(), set_airco()'s diff-merge and locking,
and async_queue_command()'s coalescing. Repository (the HTTP layer) is
replaced with an AsyncMock - no real network involved. Needs the `hass`
fixture (Device is a DataUpdateCoordinator), hence tests/integration/ rather
than tests/unit/.
"""

import asyncio
import base64
import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mitsubishi_wf_rac.const import (
    CONF_STATUS_REQUEST_MODE,
    STATUS_REQUEST_ECHO,
    STATUS_REQUEST_SILENT,
    CONF_OVERSHOOT_COOL,
    CONF_OVERSHOOT_DRY,
    CONF_OVERSHOOT_HEAT,
    DOMAIN,
)


def _set_options(device, options: dict) -> None:
    """ConfigEntry.options is a read-only mappingproxy - update it the way the
    options flow does. The fixture's entry is not registered with hass, and
    async_update_entry only works on one that is."""
    entry = device.config_entry
    if entry.entry_id not in device.hass.config_entries._entries:
        entry.add_to_hass(device.hass)
    device.hass.config_entries.async_update_entry(
        entry, options={**entry.options, **options}
    )
from custom_components.mitsubishi_wf_rac import coordinator as coordinator_module
from custom_components.mitsubishi_wf_rac.coordinator import (
    AVAILABILITY_FAILURE_LIMIT_MIN,
    SERVICE_DATA_MAX_AGE,
    Device,
)
from pywfrac import (
    Aircon,
    AirconCommands,
    AirconStat,
)
from pywfrac.parser import (
    RacParser,
    SERVICE_DATA_CODE_BY_FIELD,
    SERVICE_DATA_CODES,
    SERVICE_DATA_COMPRESSOR_FREQ,
    SERVICE_DATA_DISCHARGE_SUPERHEAT_RAW,
    SERVICE_DATA_EEV_PULSES,
    SERVICE_DATA_PROTECTION_RAW,
    SERVICE_DATA_HOT_GAS_TEMP,
    SERVICE_DATA_INDOOR_COIL_OUTLET_RAW,
    SERVICE_DATA_INDOOR_COIL_RAW,
    SERVICE_DATA_OPERATING_CURRENT,
    SERVICE_DATA_OUTDOOR_COIL_RAW,
    SERVICE_DATA_PROTECTION_RAW,
)
from pywfrac.repository import (
    WfRacError,
    WfRacCommandError,
    WfRacConnectionError,
    WfRacRegistrationError,
    WfRacWriteRefusedError,
)

from ..unit.live_captures import LIVE_CAPTURES
from homeassistant.util import dt as dt_util

OFF_PAYLOAD, _ = LIVE_CAPTURES["off"]
ON_COOL_PAYLOAD, _ = LIVE_CAPTURES["on_cool"]
FAN_SPEED_4_PAYLOAD, _ = LIVE_CAPTURES["on_cool_fanspeed4"]
ON_HEAT_PAYLOAD, _ = LIVE_CAPTURES["on_heat"]


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


def _build_stat_response_with_segments(
    content: list[int], segments: list[tuple[int, int, int, int]]
) -> str:
    """Like _build_stat_response, plus operation-data segments in the trailer.

    translate_bytes() reads the trailer from start_length + 19 to the last two
    bytes, so the one byte between the state block and the segments is skipped
    on the way in and only has to be there.
    """
    assert len(content) == 18
    body = [0] * 21 + list(content) + [0]
    for segment in segments:
        body += list(segment)
    raw = bytes((b & 0xFF) for b in (body + [0, 0]))
    return base64.b64encode(raw).decode()


async def _echo_send_airco_command(_airco_id, command, **_kwargs):
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
        coordinator_module, "UPDATE_CONSOLIDATION_PERIOD", timedelta(milliseconds=5)
    )
    monkeypatch.setattr(
        coordinator_module, "SERVICE_DATA_RETRY_DELAY", timedelta(milliseconds=5)
    )
    # The ceiling doubles as the starting value, so shrinking it is what makes
    # the request fire inside a test.
    monkeypatch.setattr(
        coordinator_module, "SERVICE_DATA_REQUEST_OFFSET", timedelta(milliseconds=offset_ms)
    )
    monkeypatch.setattr(
        coordinator_module, "SERVICE_DATA_OFFSET_MIN", timedelta(milliseconds=1)
    )


def _activate_service_data_contexts(device, monkeypatch) -> None:
    monkeypatch.setattr(device, "async_contexts", lambda: set(SERVICE_DATA_CODES))


@pytest.fixture
async def device(hass):
    dev = Device(
        hass,
        MockConfigEntry(domain=DOMAIN),
        "Test AC",
        "127.0.0.1",
        51443,
        "device-id",
        "operator-id",
        "airco-id",
        swing_selects_enabled_default=True,
    )
    dev._api = AsyncMock()
    yield dev
    await dev.async_shutdown()


async def test_update_success_marks_available_and_parses_state(device):
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    assert await device.update() is True
    assert device.available is True
    assert device.airco.Operation is True


async def test_update_none_response_marks_unavailable(device):
    device._api.get_aircon_stats.return_value = None
    assert await device.update() is False
    assert device.available is False


async def test_update_api_error_marks_unavailable_and_reregisters(device):
    device._api.get_aircon_stats.side_effect = WfRacError("boom")
    device._api.update_account_info = AsyncMock(return_value={"result": 0})
    assert await device.update() is False
    assert device.available is False
    device._api.update_account_info.assert_awaited_once()


async def test_update_transient_unreachable_is_debug_only(device, caplog):
    """An account can only have been evicted by a unit that answered - after a
    bare connection failure there is nothing to re-register against, and the
    hourly WiFi restart these modules do would make it a recurring no-op.
    """
    caplog.set_level("DEBUG", logger=coordinator_module.__name__)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    caplog.clear()

    device._api.get_aircon_stats.side_effect = WfRacConnectionError("no route")
    device._api.update_account_info = AsyncMock(return_value={"result": 0})
    await device.update()

    assert device.available is True
    device._api.update_account_info.assert_not_awaited()
    records = [r for r in caplog.records if r.name == coordinator_module.__name__]
    assert not [r for r in records if r.levelname == "WARNING"]
    assert len(records) == 1
    assert records[0].levelname == "DEBUG"
    assert records[0].exc_info is None

    caplog.clear()
    device._api.get_aircon_stats.side_effect = None
    await device.update()
    assert not [r for r in caplog.records if "is available again" in r.message]


async def test_update_sustained_unreachable_logs_one_transition(device, caplog):
    caplog.set_level("DEBUG", logger=coordinator_module.__name__)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    caplog.clear()

    device._api.get_aircon_stats.side_effect = WfRacConnectionError("no route")
    device._api.update_account_info = AsyncMock(return_value={"result": 0})
    for _ in range(10):
        await device.update()

    assert device.available is False
    assert device._consecutive_failures == device._availability_failure_limit
    device._api.update_account_info.assert_not_awaited()
    warnings = [
        r
        for r in caplog.records
        if r.name == coordinator_module.__name__ and r.levelname == "WARNING"
    ]
    assert len(warnings) == 1
    assert "is unavailable after 3 failed polls" in warnings[0].message
    assert warnings[0].exc_info is None
    assert sum(
        r.exc_info is not None
        for r in caplog.records
        if r.name == coordinator_module.__name__ and r.levelname == "DEBUG"
    ) == 1


async def test_update_initially_unreachable_logs_threshold_once(device, caplog):
    """An unavailable device at startup still has a distinct threshold event,
    even though its public availability flag starts out false.
    """
    device._api.get_aircon_stats.side_effect = WfRacConnectionError("no route")
    device._api.update_account_info = AsyncMock(return_value={"result": 0})
    for _ in range(5):
        await device.update()

    warnings = [
        r
        for r in caplog.records
        if r.name == coordinator_module.__name__ and r.levelname == "WARNING"
    ]
    assert len(warnings) == 1
    assert "is unavailable after 3 failed polls" in warnings[0].message


async def test_update_recovery_is_logged_once(device, caplog):
    caplog.set_level("INFO", logger=coordinator_module.__name__)
    device._api.get_aircon_stats.side_effect = WfRacConnectionError("no route")
    for _ in range(3):
        await device.update()

    device._api.get_aircon_stats.side_effect = None
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    await device.update()

    assert device.available is True
    recoveries = [
        r
        for r in caplog.records
        if r.name == coordinator_module.__name__
        and r.levelname == "INFO"
        and "is available again" in r.message
    ]
    assert len(recoveries) == 1


async def test_update_refused_command_reregisters(device):
    """An evicted account answers (HTTP 400 / result:2) rather than timing
    out, so this path keeps the re-registration attempt.
    """
    device._api.get_aircon_stats.side_effect = WfRacCommandError("refused")
    device._api.update_account_info = AsyncMock(return_value={"result": 0})
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


async def test_set_airco_merges_params_with_current_state(device):
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    assert device.airco.Operation is False
    original_preset_temp = device.airco.PresetTemp

    captured = {}

    async def _capture_and_echo(airco_id, command, **_kwargs):
        captured["command"] = command
        return await _echo_send_airco_command(airco_id, command)

    device._api.send_airco_command = AsyncMock(side_effect=_capture_and_echo)

    await device.set_airco({AirconCommands.Operation: True})

    raw = base64.b64decode(captured["command"])
    signed = [(256 - a) * -1 if a > 127 else a for a in raw]
    receive_content = signed[25:43]
    from pywfrac import Aircon

    sent = Aircon()
    RacParser()._parse_basic_settings(sent, receive_content)

    # The changed field is in the sent command...
    assert sent.Operation is True
    # ...and the untouched field from the pre-existing state was carried
    # along, not reset to a default - this is the "full state block per
    # request" behavior the whole coalescing/locking design exists for.
    assert sent.PresetTemp == original_preset_temp
    assert device.airco.Operation is True


async def test_set_airco_includes_stored_external_temperature_override(device):
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._external_temperature_override = 18.7

    captured = {}

    async def _capture_and_echo(airco_id, command, **_kwargs):
        captured["command"] = command
        return await _echo_send_airco_command(airco_id, command)

    device._api.send_airco_command = AsyncMock(side_effect=_capture_and_echo)

    await device.set_airco({AirconCommands.Operation: True})

    raw = base64.b64decode(captured["command"])

    assert raw[5] == int(round(18.7 * 4)) + 61


@pytest.mark.parametrize(
    "operation_mode,overshoot_key,expected",
    [
        # Cooling stops below the setting, so the unit is told the room is
        # that much colder than it is - it then reaches its stop point where
        # the room is actually on target. Heating is the mirror image, and dry
        # cools, so it takes the cooling sign with a figure of its own.
        (1, CONF_OVERSHOOT_COOL, 18.7 - 1.25),
        (2, CONF_OVERSHOOT_HEAT, 18.7 + 1.25),
        (4, CONF_OVERSHOOT_DRY, 18.7 - 1.25),
    ],
)
async def test_set_airco_bends_the_override_by_the_configured_overshoot(
    device, operation_mode, overshoot_key, expected
):
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    _set_options(device, {overshoot_key: 1.25})
    device._external_temperature_override = 18.7

    captured = {}

    async def _capture_and_echo(airco_id, command, **_kwargs):
        captured["command"] = command
        return await _echo_send_airco_command(airco_id, command)

    device._api.send_airco_command = AsyncMock(side_effect=_capture_and_echo)

    await device.set_airco(
        {AirconCommands.Operation: True, AirconCommands.OperationMode: operation_mode}
    )

    raw = base64.b64decode(captured["command"])
    assert raw[5] == int(round(expected * 4)) + 61


async def test_set_airco_bends_the_override_the_other_way_when_negative(device):
    # A unit that stops short of the setting rather than past it needs the
    # correction reversed: it is told the room is warmer, so it keeps cooling.
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    _set_options(device, {CONF_OVERSHOOT_COOL: -0.75})
    device._external_temperature_override = 18.7

    captured = {}

    async def _capture_and_echo(airco_id, command, **_kwargs):
        captured["command"] = command
        return await _echo_send_airco_command(airco_id, command)

    device._api.send_airco_command = AsyncMock(side_effect=_capture_and_echo)

    await device.set_airco(
        {AirconCommands.Operation: True, AirconCommands.OperationMode: 1}
    )

    raw = base64.b64decode(captured["command"])
    assert raw[5] == int(round((18.7 + 0.75) * 4)) + 61


async def test_set_airco_leaves_the_override_alone_in_auto(device):
    # Auto is not corrected at all: the direction it runs in is the unit's own
    # cool/heat decision, which some units never report, so there is nothing to
    # hang the sign of a correction on. Every other overshoot is set here as
    # well - none of them may reach a mode by inheritance. (fan_only carries no
    # room temperature at all, see the 0xFF test above.)
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    _set_options(
        device,
        {
            CONF_OVERSHOOT_COOL: 1.25,
            CONF_OVERSHOOT_HEAT: 1.25,
            CONF_OVERSHOOT_DRY: 1.25,
        },
    )
    device._external_temperature_override = 18.7

    captured = {}

    async def _capture_and_echo(airco_id, command, **_kwargs):
        captured["command"] = command
        return await _echo_send_airco_command(airco_id, command)

    device._api.send_airco_command = AsyncMock(side_effect=_capture_and_echo)

    await device.set_airco(
        {AirconCommands.Operation: True, AirconCommands.OperationMode: 0}
    )

    raw = base64.b64decode(captured["command"])
    assert raw[5] == int(round(18.7 * 4)) + 61


async def test_set_airco_explicitly_clears_external_temperature_override(device):
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._external_temperature_override = 18.7

    captured = {}

    async def _capture_and_echo(airco_id, command, **_kwargs):
        captured["command"] = command
        return await _echo_send_airco_command(airco_id, command)

    device._api.send_airco_command = AsyncMock(side_effect=_capture_and_echo)

    device.set_external_temperature_override(None)
    await device.set_airco({AirconCommands.PresetTemp: 22})

    raw = base64.b64decode(captured["command"])

    assert raw[5] == 0xFF


async def test_arming_an_override_asks_for_the_frame_that_carries_it(device, monkeypatch):
    """The override has no frame of its own, and only a poll schedules the one
    it rides on - while the poll during setup runs before any entity exists to
    arm it. Saving the options reloads the entry, so without this a changed
    overshoot would wait a whole poll interval to reach the unit.
    """
    _shorten_service_data_timing(monkeypatch)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    device.set_airco = set_airco = AsyncMock()

    device.set_external_temperature_override(18.7)
    await asyncio.sleep(0.05)

    set_airco.assert_awaited_once()
    assert set_airco.await_args.kwargs["is_status_request"] is True

    # A source reporting again is not a new arming: the carrier is already
    # subscribed, so the new value rides the cadence like every other one.
    set_airco.reset_mock()
    device.set_external_temperature_override(19.0)
    await asyncio.sleep(0.05)

    set_airco.assert_not_awaited()


async def test_external_temperature_applied_reads_the_echoed_byte(device):
    # The unit echoes an injected value back in byte 5 unchanged, so the byte
    # it reports matching one a recent frame carried is the whole question.
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()

    assert device.external_temperature_applied is False

    device.set_external_temperature_override(18.7)
    raw = round(18.7 * 4) + 61

    # Armed, but nothing has carried it yet - the unit is still on its own
    # sensor even if that happens to read the same.
    device.airco.ControllerRoomTempRaw = raw
    assert device.external_temperature_applied is False

    device._external_temperature_written.append(raw)
    assert device.external_temperature_applied is True

    # 0xFF: the unit is back on its own sensor, whatever is still armed here.
    device.airco.ControllerRoomTempRaw = 0xFF
    assert device.external_temperature_applied is False

    device.airco.ControllerRoomTempRaw = raw
    device.set_external_temperature_override(None)
    assert device.external_temperature_applied is False


async def test_external_temperature_applied_survives_a_value_change(device):
    # A source sensor feeding new values must not make the flag - and with it
    # the indoor offset - flicker: the frame carrying a new value goes out a
    # cycle before the unit reports it back, and until then the previous value
    # is what the unit is regulating on.
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    device.set_external_temperature_override(18.7)
    old_raw = round(18.7 * 4) + 61
    device._external_temperature_written.append(old_raw)
    device.airco.ControllerRoomTempRaw = old_raw

    device.set_external_temperature_override(19.0)
    new_raw = round(19.0 * 4) + 61
    device._external_temperature_written.append(new_raw)

    # The frame is out, the unit still reports the previous value.
    assert device.external_temperature_applied is True

    device.airco.ControllerRoomTempRaw = new_raw
    assert device.external_temperature_applied is True


async def test_set_airco_raises_and_logs_on_send_failure(device):
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._api.send_airco_command = AsyncMock(side_effect=WfRacError("boom"))

    with pytest.raises(WfRacError):
        await device.set_airco({AirconCommands.Operation: True})


async def test_set_airco_reregisters_and_retries_once_on_registration_error(device):
    """A write refused with result 2 - our operator id is not in the airco's
    account table - should self-heal like the read path already does, instead
    of losing the command outright.
    """
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._api.update_account_info = AsyncMock(return_value={"result": 0})

    calls = {"n": 0}

    async def _fail_once_then_echo(airco_id, command, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise WfRacRegistrationError("result 2")
        return await _echo_send_airco_command(airco_id, command)

    device._api.send_airco_command = AsyncMock(side_effect=_fail_once_then_echo)

    await device.set_airco({AirconCommands.Operation: True})

    device._api.update_account_info.assert_awaited_once()
    assert device._api.send_airco_command.await_count == 2
    assert device.airco.Operation is True


async def test_set_airco_gives_up_after_one_retry_if_still_refused(device):
    """The table can be genuinely full rather than just evicted - retrying
    forever would just hammer the unit. add_account() already raises the
    repair issue for that case (#287); this should still fail (and log) like
    any other refused write, not loop.
    """
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._api.update_account_info = AsyncMock(return_value={"result": 2})
    device._api.send_airco_command = AsyncMock(
        side_effect=WfRacRegistrationError("result 2")
    )

    with pytest.raises(WfRacRegistrationError):
        await device.set_airco({AirconCommands.Operation: True})

    device._api.update_account_info.assert_awaited_once()
    assert device._api.send_airco_command.await_count == 2


async def test_set_airco_fetches_state_first_if_unset(device):
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)
    # Device.airco defaults to an empty Aircon(), not None, so this path
    # relies on `self.airco is None` - simulate the "genuinely unset" case.
    device._airco = None

    await device.set_airco({AirconCommands.Operation: True})

    device._api.get_aircon_stats.assert_awaited_once()
    assert device.airco is not None


# another set_airco() call is still in flight (see conversation - this is
# the actual fix for the fan/temperature command collision bug).


async def test_set_airco_lock_prevents_stale_snapshot_race(device):
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()

    release_first_call = asyncio.Event()
    call_order = []

    async def _slow_first_then_fast(airco_id, command, **_kwargs):
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


async def test_async_queue_command_coalesces_into_one_send(device, monkeypatch):
    """Commands issued together still leave as a single frame.

    Together, not one after the other: each caller now awaits the flush its
    parameters landed in, so a second command awaited after the first has
    missed the window by definition. What consolidation is for is the case
    that arrives concurrently - a scene, an automation step that fans out -
    which is why the platforms run with PARALLEL_UPDATES = 0.
    """
    monkeypatch.setattr(coordinator_module, "UPDATE_CONSOLIDATION_PERIOD", timedelta(milliseconds=5))
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)

    await asyncio.gather(
        device.async_queue_command({AirconCommands.AirFlow: 2}),
        device.async_queue_command({AirconCommands.PresetTemp: 25.0}),
    )

    device._api.send_airco_command.assert_awaited_once()
    assert device.airco.AirFlow == 2
    assert device.airco.PresetTemp == 25.0


async def test_async_queue_command_reports_a_refusal_to_its_caller(device, monkeypatch):
    """A command the unit refused has to reach the action that issued it.

    It used to be sent by a detached task that logged the failure and dropped
    it, so a service call reported success for a command that never arrived.
    """
    monkeypatch.setattr(coordinator_module, "UPDATE_CONSOLIDATION_PERIOD", timedelta(milliseconds=5))
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._api.send_airco_command = AsyncMock(
        side_effect=WfRacConnectionError("offline")
    )

    with pytest.raises(HomeAssistantError) as raised:
        await device.async_queue_command({AirconCommands.Operation: True})

    assert raised.value.translation_key == "command_failed"


async def test_a_caller_giving_up_does_not_cancel_the_shared_command(
    device, monkeypatch
):
    """The flush is one task shared by everyone in the window.

    A caller that goes away - a cancelled service call - must not take the
    other callers' command down with it, which is what the shield is for.
    """
    monkeypatch.setattr(coordinator_module, "UPDATE_CONSOLIDATION_PERIOD", timedelta(milliseconds=20))
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)

    leaving = asyncio.ensure_future(
        device.async_queue_command({AirconCommands.AirFlow: 2})
    )
    staying = asyncio.ensure_future(
        device.async_queue_command({AirconCommands.PresetTemp: 25.0})
    )
    await asyncio.sleep(0)
    leaving.cancel()
    await staying

    device._api.send_airco_command.assert_awaited_once()
    assert device.airco.PresetTemp == 25.0


async def test_async_queue_command_notifies_listeners(device, monkeypatch):
    monkeypatch.setattr(coordinator_module, "UPDATE_CONSOLIDATION_PERIOD", timedelta(milliseconds=5))
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
    monkeypatch.setattr(coordinator_module, "UPDATE_CONSOLIDATION_PERIOD", timedelta(milliseconds=5))
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._api.send_airco_command = AsyncMock(
        side_effect=WfRacConnectionError("offline")
    )

    listener = MagicMock()
    unsubscribe = device.async_add_listener(listener)
    try:
        with pytest.raises(HomeAssistantError):
            await device.async_queue_command({AirconCommands.Operation: True})
        listener.assert_called()
    finally:
        unsubscribe()


async def test_home_leave_mode_status_request_does_not_swallow_a_queued_command(
    device, monkeypatch
):
    """Regression: async_request_home_leave_mode_status() used to go through
    async_queue_command(), so if it landed in the same consolidation window as
    a real command, both were merged into one AirconStat. to_base64() then
    saw HomeLeaveModeStatusRequest set and picked status_request_to_byte(),
    which on this module carries no set-bits at all - so the real command
    (here: a setpoint change) went out unset and was silently ignored. Sending the
    status request directly through set_airco() keeps it out of that merge.
    """
    monkeypatch.setattr(coordinator_module, "UPDATE_CONSOLIDATION_PERIOD", timedelta(milliseconds=5))
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    sent = []

    async def _capture_and_echo(airco_id, command, **_kwargs):
        sent.append(command)
        return await _echo_send_airco_command(airco_id, command)

    device._api.send_airco_command = AsyncMock(side_effect=_capture_and_echo)

    await device.async_queue_command({AirconCommands.PresetTemp: 25.0})
    await device.async_request_home_leave_mode_status()
    await asyncio.sleep(0.05)

    assert len(sent) == 2
    # The setpoint change must have been sent in its own, separate command
    # block with its set-bit (DB2[7]) intact.
    blocks = [base64.b64decode(command)[:18] for command in sent]
    assert any(block[4] & 0x80 for block in blocks)


async def test_properties_reflect_constructor_args(device):
    assert device.device_name == "Test AC"
    assert device.host == "127.0.0.1"
    assert device.port == 51443
    assert device.device_id == "device-id"
    assert device.airco_id == "airco-id"
    assert device.swing_selects_enabled_default is True
    assert device.device_info["name"] == "Test AC"
    assert device.device_info["identifiers"] == {("mitsubishi_wf_rac", "airco-id")}


async def test_device_info_claims_the_mac_only_when_the_id_is_one(hass):
    """airconId is MAC-derived and in practice the bare MAC, but the shape is
    the only evidence we have. Registering a connection for an id that isn't
    one would merge this device with whatever really holds that address.
    """
    args = (hass, MockConfigEntry(domain=DOMAIN), "Test AC", "127.0.0.1", 51443, "device-id", "operator-id")

    not_a_mac = Device(*args, "airco-id", swing_selects_enabled_default=True)
    assert "connections" not in not_a_mac.device_info
    await not_a_mac.async_shutdown()

    a_mac = Device(*args, "348E89C5A137", swing_selects_enabled_default=True)
    assert a_mac.device_info["connections"] == {
        (dr.CONNECTION_NETWORK_MAC, "34:8e:89:c5:a1:37")
    }
    await a_mac.async_shutdown()


async def test_device_info_carries_the_model_number_as_model_id(device):
    """ModelNr is a capability grouping, not a type name - it belongs in
    model_id, and "model" stays empty rather than showing a bare digit.
    """
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()

    info = device.device_info
    assert info["model_id"] == str(device.airco.ModelNrRaw)
    assert "model" not in info


async def test_delete_account_success(device):
    device._api.del_account_info = AsyncMock(return_value={"result": 0})
    result = await device.delete_account()
    assert result == {"result": 0}
    device._api.del_account_info.assert_awaited_once_with("airco-id")


async def test_delete_account_failure_returns_none(device):
    device._api.del_account_info = AsyncMock(side_effect=WfRacError("boom"))
    assert await device.delete_account() is None


async def test_availability_tolerates_failures_below_limit(hass):
    """The module reassociates to WiFi about once an hour and misses a poll
    while it does; only a sustained run of failures is a real outage."""
    dev = Device(
        hass, MockConfigEntry(domain=DOMAIN), "Test AC", "127.0.0.1", 51443,
        "device-id", "operator-id", "airco-id",
        swing_selects_enabled_default=True,
    )
    dev._api = AsyncMock()
    dev._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await dev.update()
    assert dev.available is True

    dev._api.get_aircon_stats.side_effect = WfRacError("boom")
    dev._api.update_account_info = AsyncMock(return_value={"result": 0})

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
        hass, MockConfigEntry(domain=DOMAIN), "Test AC", "127.0.0.1", 51443,
        "device-id", "operator-id", "airco-id",
        swing_selects_enabled_default=True, availability_failure_limit=5,
    )
    assert raised._availability_failure_limit == 5

    lowered = Device(
        hass, MockConfigEntry(domain=DOMAIN), "Test AC", "127.0.0.1", 51443,
        "device-id", "operator-id", "airco-id",
        swing_selects_enabled_default=True, availability_failure_limit=1,
    )
    assert lowered._availability_failure_limit == AVAILABILITY_FAILURE_LIMIT_MIN

    lowered._api = AsyncMock()
    lowered._api.update_account_info = AsyncMock(return_value={"result": 0})
    lowered._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await lowered.update()

    lowered._api.get_aircon_stats.side_effect = WfRacError("boom")
    await lowered.update()
    assert lowered.available is True  # would already be unavailable at limit 1


async def test_availability_recovers_and_resets_the_failure_count(hass):
    """A success in between must clear the run, not leave it part-way to the
    limit."""
    dev = Device(
        hass, MockConfigEntry(domain=DOMAIN), "Test AC", "127.0.0.1", 51443,
        "device-id", "operator-id", "airco-id",
        swing_selects_enabled_default=True,
    )
    dev._api = AsyncMock()
    dev._api.update_account_info = AsyncMock(return_value={"result": 0})
    dev._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await dev.update()

    dev._api.get_aircon_stats.side_effect = WfRacError("boom")
    await dev.update()
    await dev.update()
    assert dev.available is True

    dev._api.get_aircon_stats.side_effect = None
    await dev.update()
    assert dev.available is True

    dev._api.get_aircon_stats.side_effect = WfRacError("boom")
    await dev.update()
    await dev.update()
    assert dev.available is True  # count restarted, not resumed at 2
    await dev.update()
    assert dev.available is False


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
    monkeypatch.setattr(coordinator_module, "fetch_latest_firmware", fetch)
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
    monkeypatch.setattr(coordinator_module, "fetch_latest_firmware", fetch)
    device._api.get_aircon_stats.return_value = _stats_response_with_firmware(
        ON_COOL_PAYLOAD, "WF-RAC-HTTPS", "025"
    )

    await device.update()
    await device.hass.async_block_till_done()

    fetch.assert_awaited_once_with(device.hass, "WF-RAC-HTTPS")
    assert device.wireless_firmware_version == "025"
    assert device.latest_wireless_firmware_version == "026"
    assert device.firmware_update_available is True


async def test_update_does_not_flag_downgrade_or_equal_version_as_update(device, monkeypatch):
    # Strictly-greater-than only: the module silently no-ops a requested
    # version <= its current one, so neither "equal" nor "older" may be
    # reported as an available update.
    device._firmware_update_check_enabled = True
    fetch = AsyncMock(return_value={"wireless": "025", "mcu": "200"})
    monkeypatch.setattr(coordinator_module, "fetch_latest_firmware", fetch)
    device._api.get_aircon_stats.return_value = _stats_response_with_firmware(
        ON_COOL_PAYLOAD, "WF-RAC-HTTPS", "025"
    )

    await device.update()
    await device.hass.async_block_till_done()

    assert device.firmware_update_available is False


async def test_update_firmware_check_is_rate_limited(device, monkeypatch):
    device._firmware_update_check_enabled = True
    fetch = AsyncMock(return_value={"wireless": "026", "mcu": "200"})
    monkeypatch.setattr(coordinator_module, "fetch_latest_firmware", fetch)
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
    monkeypatch.setattr(coordinator_module, "fetch_latest_firmware", fetch)
    device._api.get_aircon_stats.return_value = _stats_response_with_firmware(
        ON_COOL_PAYLOAD, "WF-RAC-HTTPS", "025"
    )

    await device.update()
    await device.hass.async_block_till_done()

    assert device.firmware_update_available is None
    assert device.latest_wireless_firmware_version is None


async def test_update_does_not_request_service_data_without_active_entities(device, monkeypatch):
    monkeypatch.setattr(coordinator_module, "UPDATE_CONSOLIDATION_PERIOD", timedelta(milliseconds=5))
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)

    await device.update()
    await asyncio.sleep(0.05)

    device._api.send_airco_command.assert_not_awaited()


async def test_update_requests_service_data_for_active_entities(device, monkeypatch):
    _activate_service_data_contexts(device, monkeypatch)
    _shorten_service_data_timing(monkeypatch)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)

    await device.update()
    await asyncio.sleep(0.05)

    device._api.send_airco_command.assert_awaited_once()


async def test_service_data_request_uses_active_segment_codes(device, monkeypatch):
    _shorten_service_data_timing(monkeypatch)
    monkeypatch.setattr(
        device,
        "async_contexts",
        lambda: {
            SERVICE_DATA_HOT_GAS_TEMP,
            SERVICE_DATA_EEV_PULSES,
            SERVICE_DATA_EEV_PULSES,
        },
    )
    set_airco = AsyncMock()
    device.set_airco = set_airco

    device._maybe_request_service_data()
    await asyncio.sleep(0.05)

    set_airco.assert_awaited_once_with(
        {
            AirconCommands.ServiceDataStatusRequest: (
                SERVICE_DATA_EEV_PULSES,
                SERVICE_DATA_HOT_GAS_TEMP,
            )
        },
        log_failure=False,
        timestamp_offset=-round(
            coordinator_module.SERVICE_DATA_STAMP_BACKDATE.total_seconds()
        ),
        is_status_request=True,
        retry_when_locked=False,
    )


@pytest.mark.parametrize(
    ("field", "code"),
    (
        ("CompressorFrequencyRaw", SERVICE_DATA_COMPRESSOR_FREQ),
        ("OperatingCurrentRaw", SERVICE_DATA_OPERATING_CURRENT),
        ("HotGasTempRaw", SERVICE_DATA_HOT_GAS_TEMP),
        ("IndoorCoilRaw", SERVICE_DATA_INDOOR_COIL_RAW),
        ("IndoorCoilOutletRaw", SERVICE_DATA_INDOOR_COIL_OUTLET_RAW),
        ("OutdoorCoilRaw", SERVICE_DATA_OUTDOOR_COIL_RAW),
        ("DischargeSuperheatRaw", SERVICE_DATA_DISCHARGE_SUPERHEAT_RAW),
        ("ProtectionRaw", SERVICE_DATA_PROTECTION_RAW),
    ),
)
async def test_raw_service_data_sensor_requests_its_segment_code(device, monkeypatch, field, code):
    _shorten_service_data_timing(monkeypatch)
    assert SERVICE_DATA_CODE_BY_FIELD[field] == code
    monkeypatch.setattr(device, "async_contexts", lambda: {code})
    device.set_airco = set_airco = AsyncMock()

    device._maybe_request_service_data()
    await asyncio.sleep(0.05)

    set_airco.assert_awaited_once_with(
        {AirconCommands.ServiceDataStatusRequest: (code,)},
        log_failure=False,
        timestamp_offset=-round(
            coordinator_module.SERVICE_DATA_STAMP_BACKDATE.total_seconds()
        ),
        is_status_request=True,
        retry_when_locked=False,
    )


def _stats_response_with_expires(payload: str, expires: int) -> dict:
    return _stats_response(payload) | {"expires": expires}


async def test_rising_expires_without_our_own_write_is_foreign_activity(device):
    """The module's `expires` only moves when a setAirconStat succeeds, so a
    rise we did not cause means another client wrote (#294).
    """
    device._api.get_aircon_stats.return_value = _stats_response_with_expires(
        OFF_PAYLOAD, 1_000
    )
    await device.update()
    assert device.foreign_activity is False

    device._api.get_aircon_stats.return_value = _stats_response_with_expires(
        OFF_PAYLOAD, 1_060
    )
    await device.update()

    assert device.foreign_activity is True


async def test_rising_expires_after_our_own_write_is_not_foreign_activity(device):
    """Our own writes move `expires` too - attributing those to a stranger
    would pause operation data permanently.
    """
    device._api.get_aircon_stats.return_value = _stats_response_with_expires(
        OFF_PAYLOAD, 1_000
    )
    await device.update()
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)

    await device.set_airco({AirconCommands.Operation: True})

    device._api.get_aircon_stats.return_value = _stats_response_with_expires(
        OFF_PAYLOAD, 1_060
    )
    await device.update()

    assert device.foreign_activity is False


async def test_unchanged_expires_is_not_foreign_activity(device):
    device._api.get_aircon_stats.return_value = _stats_response_with_expires(
        OFF_PAYLOAD, 1_000
    )
    await device.update()
    await device.update()

    assert device.foreign_activity is False


async def test_operation_data_backdates_its_timestamp_to_shorten_the_lock(
    device, monkeypatch
):
    """The request renews the module's 60s write lock, so one per 60s poll
    never lets go of it and locks every other client out for good (#294).
    Backdating the request's timestamp by SERVICE_DATA_STAMP_BACKDATE makes the
    lock it takes expire that much sooner, leaving a free window in every poll -
    so the request goes out on every poll now, carrying the negative offset.
    """
    _activate_service_data_contexts(device, monkeypatch)
    _shorten_service_data_timing(monkeypatch)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)
    one_poll = coordinator_module.MIN_TIME_BETWEEN_UPDATES
    expected_offset = -round(
        coordinator_module.SERVICE_DATA_STAMP_BACKDATE.total_seconds()
    )

    device._last_service_data_request = dt_util.utcnow() - one_poll
    await device.update()
    await asyncio.sleep(0.05)

    device._api.send_airco_command.assert_awaited_once()
    assert (
        device._api.send_airco_command.await_args.kwargs["timestamp_offset"]
        == expected_offset
    )

    device._last_service_data_request = dt_util.utcnow() - one_poll
    await device.update()
    await asyncio.sleep(0.05)

    assert device._api.send_airco_command.await_count == 2


async def test_service_data_stamps_honestly_right_after_our_own_command(device):
    """Backdating an operation-data request within one lock's span of a real
    command would overwrite that command's own 60s lock with a short one (same
    deviceId bypasses the lock check). Within the window it must stamp honestly
    (offset 0) instead; before and after, it backdates."""
    device._last_command_at = None
    assert (
        device._service_data_stamp_backdate()
        == coordinator_module.SERVICE_DATA_STAMP_BACKDATE
    )

    device._last_command_at = dt_util.utcnow()
    assert device._service_data_stamp_backdate() == timedelta(0)

    device._last_command_at = dt_util.utcnow() - 2 * coordinator_module.MIN_TIME_BETWEEN_UPDATES
    assert (
        device._service_data_stamp_backdate()
        == coordinator_module.SERVICE_DATA_STAMP_BACKDATE
    )


async def test_a_real_command_records_its_time_for_the_backdate_guard(device, monkeypatch):
    """A flushed user command marks _last_command_at so the guard above can
    see it - a bare status request (no set-bits) must not."""
    monkeypatch.setattr(
        coordinator_module, "UPDATE_CONSOLIDATION_PERIOD", timedelta(milliseconds=5)
    )
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)
    assert device._last_command_at is None

    await device.async_queue_command({AirconCommands.PresetTemp: 22.0})
    await asyncio.sleep(0.05)

    assert device._last_command_at is not None


async def test_a_refused_write_waits_out_the_lock_that_refused_it(device):
    """A retry at a fixed delay lands inside the same lock as often as not,
    and a request spent on a refusal that was certain is a request wasted.
    The unit reports when the lock lapses, against a clock our own request
    just set (the module has no RTC), so the retry can be placed right after
    it.
    """
    # Someone else took the lock 40 seconds ago: 20 of its 60 are left.
    device._api.get_aircon_stats.return_value = _stats_response_with_expires(
        OFF_PAYLOAD, int(dt_util.utcnow().timestamp()) - 40 + 60
    )

    delay = await device._async_write_lock_delay()

    assert 20 <= delay <= 22


async def test_a_refused_write_is_never_held_longer_than_a_lock_can_run(device):
    """The lock runs 60 seconds, so a deadline further out than that came
    from a client whose clock is off, not from a lock that is really still
    running. No reason to leave a service call hanging on it.
    """
    device._api.get_aircon_stats.return_value = _stats_response_with_expires(
        OFF_PAYLOAD, int(dt_util.utcnow().timestamp()) + 3_600
    )

    delay = await device._async_write_lock_delay()

    assert delay == coordinator_module.WRITE_LOCK_MAX_WAIT.total_seconds()


async def test_a_refused_write_retries_at_once_when_the_lock_has_lapsed(device):
    """A lock that has already run out means the refusal came from something
    else - the MCU, most likely - and nothing is gained by waiting.
    """
    device._api.get_aircon_stats.return_value = _stats_response_with_expires(
        OFF_PAYLOAD, int(dt_util.utcnow().timestamp()) - 30
    )

    assert await device._async_write_lock_delay() == 0.0


async def test_a_refused_write_falls_back_when_the_unit_reports_no_deadline(device):
    """Older firmware may not report `expires` at all. A short retry is still
    worth more than none.
    """
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)

    assert (
        await device._async_write_lock_delay()
        == coordinator_module.WRITE_LOCK_RETRY_DELAY.total_seconds()
    )


async def test_a_refused_write_falls_back_when_the_unit_stops_answering(device):
    """The probe is an extra request and can fail on its own."""
    device._api.get_aircon_stats.side_effect = WfRacConnectionError("no route")

    assert (
        await device._async_write_lock_delay()
        == coordinator_module.WRITE_LOCK_RETRY_DELAY.total_seconds()
    )


async def test_missing_expires_field_is_not_foreign_activity(device):
    """Older firmware may not report it at all; absence must not be read as
    a stranger writing every single poll.
    """
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    await device.update()

    assert device.foreign_activity is False


async def test_no_service_data_request_while_another_client_is_active(device, monkeypatch):
    """The operation-data request is a setAirconStat and renews the 60s write
    lock for another minute. While someone else is using the unit, that lock
    is exactly what must not be taken (#294).
    """
    _shorten_service_data_timing(monkeypatch)
    _activate_service_data_contexts(device, monkeypatch)
    device.set_airco = set_airco = AsyncMock()
    device._foreign_activity_until = dt_util.utcnow() + timedelta(minutes=3)

    device._maybe_request_service_data()
    await asyncio.sleep(0.05)

    set_airco.assert_not_awaited()
    assert device._last_service_data_request is None


async def test_operation_data_survives_the_pause_instead_of_expiring(device):
    """A pause we chose is not the same as a unit that stopped answering:
    SERVICE_DATA_MAX_AGE must not run while we are deliberately not asking,
    and the readings must not expire the moment it ends either.
    """
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._airco.CompressorFrequency = 42
    device._last_service_data_response = dt_util.utcnow()

    # Well past MAX_AGE, but the whole time was spent standing down - the
    # state _detect_foreign_activity() leaves behind when it trips.
    device._foreign_activity_since = dt_util.utcnow() - 2 * SERVICE_DATA_MAX_AGE
    device._foreign_activity_until = dt_util.utcnow() + timedelta(minutes=3)
    device._last_service_data_response = dt_util.utcnow() - 2 * SERVICE_DATA_MAX_AGE
    await device.update()
    assert device.airco.CompressorFrequency == 42
    assert device._service_data_expired is False

    # ...and once it lapses, the age restarts from the resume rather than
    # expiring the readings on the very next poll.
    device._foreign_activity_until = dt_util.utcnow() - timedelta(seconds=1)
    await device.update()
    assert device.airco.CompressorFrequency == 42
    assert device._service_data_expired is False


async def test_operation_data_still_expires_when_the_unit_goes_quiet(device):
    """The pause exemption must not disarm the guard it is carved out of."""
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._airco.CompressorFrequency = 42
    device._last_service_data_response = dt_util.utcnow() - 2 * SERVICE_DATA_MAX_AGE

    await device.update()

    assert device.airco.CompressorFrequency is None
    assert device._service_data_expired is True


async def test_service_data_resumes_once_the_backoff_lapses(device, monkeypatch):
    _shorten_service_data_timing(monkeypatch)
    _activate_service_data_contexts(device, monkeypatch)
    device.set_airco = set_airco = AsyncMock()
    device._foreign_activity_until = dt_util.utcnow() - timedelta(seconds=1)

    device._maybe_request_service_data()
    await asyncio.sleep(0.05)

    set_airco.assert_awaited_once()


async def test_service_data_request_gives_up_immediately_when_refused_as_a_write(
    device, monkeypatch
):
    """A declined write is not worth contesting for an optional sensor read -
    winning the retry would only mean holding the lock ourselves.
    """
    _shorten_service_data_timing(monkeypatch)
    _activate_service_data_contexts(device, monkeypatch)
    device.set_airco = set_airco = AsyncMock(
        side_effect=WfRacWriteRefusedError("result 1")
    )

    device._maybe_request_service_data()
    await asyncio.sleep(0.05)

    set_airco.assert_awaited_once()


async def test_a_refused_service_data_request_is_not_retried_inside_set_airco(
    device, monkeypatch
):
    """The test above mocks set_airco() away, so it cannot see that set_airco()
    answers a refusal with a wait-and-retry of its own - which for this request
    is the very contest it is trying to avoid, and blocks _send_lock (and with
    it any user command) for as long as the foreign lock runs.
    """
    _shorten_service_data_timing(monkeypatch)
    _activate_service_data_contexts(device, monkeypatch)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    device._api.send_airco_command = AsyncMock(
        side_effect=WfRacWriteRefusedError("result 1")
    )

    device._maybe_request_service_data()
    await asyncio.sleep(0)
    task = device._service_data_task
    assert task is not None
    # The wait matters as much as the second send: it happens inside
    # _send_lock, so a request that sits it out holds every user command up
    # behind it. A task still running here is a task in that wait.
    await asyncio.wait_for(task, 1)

    assert device._api.send_airco_command.await_count == 1


async def test_set_airco_waits_and_retries_once_when_the_write_lock_is_held(
    device, monkeypatch
):
    """A user command refused because someone else holds the lock should wait
    it out and retry - re-registering cannot help, the registration is fine.
    """
    monkeypatch.setattr(
        coordinator_module, "WRITE_LOCK_RETRY_DELAY", timedelta(milliseconds=5)
    )
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._api.update_account_info = AsyncMock(return_value={"result": 0})

    calls = {"n": 0}

    async def _fail_once_then_echo(airco_id, command, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise WfRacWriteRefusedError("result 1")
        return await _echo_send_airco_command(airco_id, command)

    device._api.send_airco_command = AsyncMock(side_effect=_fail_once_then_echo)

    await device.set_airco({AirconCommands.Operation: True})

    device._api.update_account_info.assert_not_awaited()
    assert device._api.send_airco_command.await_count == 2
    assert device.airco.Operation is True


async def test_service_data_request_does_not_overlap_an_active_request(device, monkeypatch):
    _activate_service_data_contexts(device, monkeypatch)

    async def _still_asleep() -> None:
        await asyncio.sleep(10)

    # A real task rather than a stand-in: the fixture's teardown shuts the
    # coordinator down, and async_shutdown() now cancels and awaits this
    # attribute, which a MagicMock cannot answer.
    device._service_data_task = asyncio.create_task(_still_asleep())

    device._maybe_request_service_data()

    assert device._last_service_data_request is None


async def test_shutdown_cancels_a_request_still_waiting_out_its_offset(
    device, monkeypatch
):
    """The request sleeps out its offset for most of its life, so an unload
    lands in that sleep more often than not. hass only cancels background
    tasks when hass itself stops - on a config-entry reload the request would
    go out afterwards, from a Repository whose spacing knows nothing about the
    one the new entry is already polling through.
    """
    _activate_service_data_contexts(device, monkeypatch)
    monkeypatch.setattr(
        coordinator_module, "SERVICE_DATA_REQUEST_OFFSET", timedelta(seconds=30)
    )
    device.set_airco = set_airco = AsyncMock()

    device._maybe_request_service_data()
    await asyncio.sleep(0)
    task = device._service_data_task
    assert task is not None and not task.done()

    await device.async_shutdown()

    assert task.cancelled()
    set_airco.assert_not_awaited()
    assert device._service_data_task is None


async def test_shutdown_does_not_fail_on_a_task_that_had_already_raised(device):
    """An unload that raises leaves the entities loaded on an entry that will
    never update again, so a leftover failure is logged here, not propagated.
    """

    async def _boom() -> None:
        raise RuntimeError("nothing retrieved this")

    task = asyncio.create_task(_boom())
    await asyncio.sleep(0)
    device._service_data_task = task

    await device.async_shutdown()

    assert device._service_data_task is None


async def test_add_account_returns_none_on_api_error(device):
    device._api.update_account_info.side_effect = WfRacError("failed")

    assert await device.add_account() is None


def _issue(device):
    return ir.async_get(device.hass).async_get_issue(
        DOMAIN, coordinator_module.registration_full_issue_id(device.config_entry.entry_id)
    )


async def test_add_account_reports_repair_issue_when_table_is_full(device):
    device._api.update_account_info.return_value = {"result": 2}

    await device.add_account()

    assert _issue(device) is not None


async def test_add_account_clears_repair_issue_once_registration_succeeds(device):
    device._api.update_account_info.return_value = {"result": 2}
    await device.add_account()
    assert _issue(device) is not None

    device._api.update_account_info.return_value = {"result": 0}
    await device.add_account()

    assert _issue(device) is None


async def test_add_account_does_not_report_an_issue_on_ordinary_success(device):
    device._api.update_account_info.return_value = {"result": 0}

    await device.add_account()

    assert _issue(device) is None


async def test_update_reregister_reports_repair_issue_when_table_stays_full(device):
    device._api.get_aircon_stats.side_effect = WfRacError("evicted")
    device._api.update_account_info.return_value = {"result": 2}

    await device.update()

    assert _issue(device) is not None


async def test_set_airco_raises_when_refresh_does_not_provide_state(device):
    device._airco = None
    device._api.get_aircon_stats.return_value = None

    with pytest.raises(ValueError, match="Airco object is empty"):
        await device.set_airco({})


async def test_service_data_request_is_offset_from_the_poll(device, monkeypatch):
    """It must not ride straight off the back of the status poll - landing a
    second write that close is what the unit refuses with HTTP 501 (#230).
    """
    _activate_service_data_contexts(device, monkeypatch)
    _shorten_service_data_timing(monkeypatch, offset_ms=40)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)

    await device.update()
    await asyncio.sleep(0.01)
    device._api.send_airco_command.assert_not_awaited()

    await asyncio.sleep(0.06)
    device._api.send_airco_command.assert_awaited_once()


async def test_service_data_request_carries_no_set_bits(device, monkeypatch):
    """The request only reads, so its command block leaves every set-bit clear
    and the unit applies none of it - that is what keeps a change made at the
    unit itself from being undone a minute later (#241/#250).
    """
    _activate_service_data_contexts(device, monkeypatch)
    _shorten_service_data_timing(monkeypatch, offset_ms=40)
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    sent = []

    async def _capture(airco_id, command, **_kwargs):
        sent.append(command)
        return _stats_response(OFF_PAYLOAD)

    device._api.send_airco_command = AsyncMock(side_effect=_capture)

    await device.update()
    await asyncio.sleep(0.08)

    assert len(sent) == 1
    block = base64.b64decode(sent[0])[:18]
    # Power DB0[1], mode DB0[5], vane DB0[7]/DB1[7], fan DB1[3], setpoint
    # DB2[7]: without these the values in the frame mean nothing to the unit.
    assert block[2] == 0
    assert block[3] == 0
    assert block[4] == 0
    assert block[10] == 0
    assert block[11] == 0
    assert block[12] == 0
    # ...but byte 5 still says "keep using your own room sensor", and byte 8 is
    # carried as usual because it has no set-bit of its own.
    assert block[5] == 0xFF


async def test_service_data_request_does_not_re_read_before_sending(
    device, monkeypatch
):
    """One read per cycle. The refresh that used to sit in front of the write
    (#247) is unnecessary now that the write applies nothing.
    """
    _activate_service_data_contexts(device, monkeypatch)
    _shorten_service_data_timing(monkeypatch, offset_ms=40)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)

    await device.update()
    await asyncio.sleep(0.08)

    assert device._api.get_aircon_stats.await_count == 1
    device._api.send_airco_command.assert_awaited_once()


async def test_service_data_request_runs_after_a_change_at_the_unit(
    device, monkeypatch
):
    """No cycle is skipped any more: there is nothing left to protect against,
    and skipping cost a cycle of every operation-data sensor.
    """
    _activate_service_data_contexts(device, monkeypatch)
    _shorten_service_data_timing(monkeypatch)
    changed_at_the_unit = _stats_response(ON_COOL_PAYLOAD) | {"updatedBy": "aircon"}
    device._api.get_aircon_stats.return_value = changed_at_the_unit
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)

    await device.update()
    await asyncio.sleep(0.05)

    device._api.send_airco_command.assert_awaited_once()


async def test_service_data_request_is_retried_once_when_refused(device, monkeypatch):
    _activate_service_data_contexts(device, monkeypatch)
    _shorten_service_data_timing(monkeypatch)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    calls = []

    async def _refuse_then_answer(airco_id, command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise WfRacCommandError("HTTP 501: Not supported this command")
        return await _echo_send_airco_command(airco_id, command)

    device._api.send_airco_command = AsyncMock(side_effect=_refuse_then_answer)

    await device.update()
    await asyncio.sleep(0.1)

    assert len(calls) == 2


async def test_service_data_request_gives_up_after_the_retry(device, monkeypatch, caplog):
    _activate_service_data_contexts(device, monkeypatch)
    _shorten_service_data_timing(monkeypatch)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(
        side_effect=WfRacCommandError("HTTP 501: Not supported this command")
    )

    await device.update()
    await asyncio.sleep(0.1)

    assert device._api.send_airco_command.await_count == 2
    # One line for the cycle, not one per attempt - and on debug, because a
    # skipped cycle costs the user nothing (see _note_service_data_expired).
    refusals = [r for r in caplog.records if "refused twice" in r.message]
    assert len(refusals) == 1
    assert refusals[0].levelname == "DEBUG"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_update_service_data_request_is_rate_limited(device, monkeypatch):
    _activate_service_data_contexts(device, monkeypatch)
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
    _activate_service_data_contexts(device, monkeypatch)
    _shorten_service_data_timing(monkeypatch)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    device._api.send_airco_command = AsyncMock(side_effect=_echo_send_airco_command)
    device._last_service_data_request = dt_util.utcnow() - (
        coordinator_module.SERVICE_DATA_REQUEST_INTERVAL - timedelta(milliseconds=100)
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


async def test_coordinator_tracks_transient_failure_without_regular_log_noise(
    device, caplog
):
    caplog.set_level("DEBUG", logger=coordinator_module.__name__)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.async_refresh()
    caplog.clear()

    device._api.get_aircon_stats.side_effect = WfRacConnectionError("no route")
    await device.async_refresh()

    # An expected miss deliberately leaves the coordinator successful: it is
    # what keeps HA's own "Error fetching ... data" out of the log, and
    # entity availability comes from Device.available instead.
    assert device.last_update_success is True
    assert device.available is True
    assert not [
        record for record in caplog.records if record.levelno >= logging.INFO
    ]

    caplog.clear()
    device._api.get_aircon_stats.side_effect = None
    await device.async_refresh()

    assert device.last_update_success is True
    assert device.available is True
    assert not [
        record for record in caplog.records if record.levelno >= logging.INFO
    ]


async def test_coordinator_notifies_when_device_reaches_unavailable_threshold(device):
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.async_refresh()
    listener = MagicMock()
    unsubscribe = device.async_add_listener(listener)

    try:
        device._api.get_aircon_stats.side_effect = WfRacConnectionError("no route")
        await device.async_refresh()
        await device.async_refresh()
        listener.reset_mock()

        await device.async_refresh()

        assert device.available is False
        # The poll that crosses the threshold has to reach entities, or they
        # keep showing their last state while the device is marked unavailable.
        listener.assert_called_once()
    finally:
        unsubscribe()


async def test_coordinator_still_logs_unexpected_failures(device, caplog):
    caplog.set_level("DEBUG", logger=coordinator_module.__name__)

    async def _boom():
        raise RuntimeError("unexpected")

    device.update = _boom
    await device.async_refresh()

    assert device.last_update_success is False
    assert len([record for record in caplog.records if record.levelname == "ERROR"]) == 1


async def test_async_update_data_counts_timeouts_as_connection_failures(
    device, monkeypatch, caplog
):
    monkeypatch.setattr(coordinator_module, "POLL_TIMEOUT", timedelta(milliseconds=10))
    caplog.set_level("DEBUG", logger=coordinator_module.__name__)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.async_refresh()
    original_update = device.update
    caplog.clear()

    async def _hang():
        await asyncio.sleep(5)

    device.update = _hang
    for _ in range(5):
        await device.async_refresh()

    assert device.available is False
    # A timeout is an expected miss like any other, so the coordinator stays
    # successful and only the availability threshold speaks up.
    assert device.last_update_success is True
    assert device._consecutive_failures == device._availability_failure_limit
    warnings = [
        record
        for record in caplog.records
        if record.name == coordinator_module.__name__ and record.levelname == "WARNING"
    ]
    assert len(warnings) == 1
    assert "is unavailable after 3 failed polls" in warnings[0].message
    assert not [record for record in caplog.records if record.levelname == "ERROR"]
    assert sum(
        record.message.startswith("Could not reach") for record in caplog.records
    ) == 4

    caplog.clear()
    device.update = original_update
    await device.async_refresh()
    await device.async_refresh()

    assert device.available is True
    assert device.last_update_success is True
    recovery_records = [
        record
        for record in caplog.records
        if record.levelname == "INFO" and "available again" in record.message
    ]
    assert len(recovery_records) == 1
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.INFO and "data recovered" in record.message
    ]


async def test_service_data_is_carried_forward_between_polls(device):
    device._airco.CompressorFrequency = 40.0
    device._last_service_data_response = dt_util.utcnow()
    new_airco = Aircon()

    device._carry_forward_service_data(new_airco)

    assert new_airco.CompressorFrequency == 40.0


async def test_raw_service_data_is_carried_forward_between_polls(device):
    device._airco.CompressorFrequencyRaw = 0x10C8
    device._airco.OperatingCurrentRaw = 0x04
    device._airco.HotGasTempRaw = 0x15
    device._last_service_data_response = dt_util.utcnow()
    new_airco = Aircon()

    device._carry_forward_service_data(new_airco)

    assert new_airco.CompressorFrequencyRaw == 0x10C8
    assert new_airco.OperatingCurrentRaw == 0x04
    assert new_airco.HotGasTempRaw == 0x15


async def test_unconvertible_coil_reading_is_not_carried_forward(device):
    """"Segment absent" and "segment arrived, value unusable" must not look
    the same. The coil conversion stops above its calibrated band, which is
    where a heating unit sits for a whole season - carrying the last
    convertible reading forward would freeze a summer temperature on screen
    until spring.
    """
    device._airco.IndoorCoilTemp = 37.5
    device._airco.IndoorCoilRaw = 119
    device._last_service_data_response = dt_util.utcnow()
    new_airco = Aircon()
    new_airco.IndoorCoilRaw = 252  # arrived, but off the end of the table

    device._carry_forward_service_data(new_airco)

    assert new_airco.IndoorCoilRaw == 252
    assert new_airco.IndoorCoilTemp is None


async def test_missing_coil_segment_is_still_carried_forward(device):
    """The other half of the pair: nothing arrived, so the last reading holds
    exactly like every other operation-data field.
    """
    device._airco.IndoorCoilTemp = 21.5
    device._airco.IndoorCoilRaw = 88
    device._last_service_data_response = dt_util.utcnow()
    new_airco = Aircon()
    new_airco.CompressorFrequency = 40.0  # some other segment did arrive

    device._carry_forward_service_data(new_airco)

    assert new_airco.IndoorCoilTemp == 21.5
    assert new_airco.IndoorCoilRaw == 88


async def test_service_data_expires_when_nothing_fresh_arrives(device):
    """A unit that keeps refusing the request (#230) must not leave entities
    reporting a frozen value that looks live.
    """
    device._airco.CompressorFrequency = 40.0
    device._last_service_data_response = dt_util.utcnow() - (
        coordinator_module.SERVICE_DATA_MAX_AGE + timedelta(seconds=1)
    )
    new_airco = Aircon()

    device._carry_forward_service_data(new_airco)

    assert new_airco.CompressorFrequency is None


async def test_fresh_service_data_restarts_the_clock(device):
    device._airco.CompressorFrequency = 40.0
    device._airco.HotGasTemp = 50.0
    device._last_service_data_response = dt_util.utcnow() - (
        coordinator_module.SERVICE_DATA_MAX_AGE + timedelta(seconds=1)
    )
    new_airco = Aircon()
    new_airco.CompressorFrequency = 45.0  # this poll carried the segments

    device._carry_forward_service_data(new_airco)

    assert new_airco.CompressorFrequency == 45.0
    # The rest of the block comes with it, so they are carried again.
    assert new_airco.HotGasTemp == 50.0


async def test_expiring_service_data_warns_once_and_reports_the_recovery(
    device, caplog
):
    """The refusals themselves are routine; running out of values is not."""
    caplog.set_level("DEBUG", logger=coordinator_module.__name__)
    device._airco.CompressorFrequency = 40.0
    device._last_service_data_response = dt_util.utcnow() - (
        coordinator_module.SERVICE_DATA_MAX_AGE + timedelta(seconds=1)
    )

    for _ in range(3):
        device._carry_forward_service_data(Aircon())

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "now report unknown" in warnings[0].message

    caplog.clear()
    recovered = Aircon()
    recovered.CompressorFrequency = 45.0
    device._carry_forward_service_data(recovered)

    assert [r.levelname for r in caplog.records if r.levelno >= logging.INFO] == [
        "INFO"
    ]
    assert "being reported again" in caplog.records[-1].message


async def test_service_data_that_never_arrived_stays_quiet_until_it_is_due(
    device, caplog
):
    """A unit asked for the first time has nothing to lose yet."""
    caplog.set_level("DEBUG", logger=coordinator_module.__name__)
    device._last_service_data_request = dt_util.utcnow()

    device._carry_forward_service_data(Aircon())

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def _run_service_data_request(device, monkeypatch, ceiling_ms: int = 1):
    # The ceiling doubles as the starting value, so it has to stay small
    # enough for the request to fire inside a test - and, where the
    # adaptation itself is under test, large enough to leave room above.
    monkeypatch.setattr(
        coordinator_module, "SERVICE_DATA_REQUEST_OFFSET", timedelta(milliseconds=ceiling_ms)
    )
    # Cycles run back to back here; the real spacing would swallow every
    # request after the first.
    monkeypatch.setattr(
        coordinator_module, "SERVICE_DATA_MIN_SPACING", timedelta(0)
    )
    monkeypatch.setattr(
        device, "async_contexts", lambda: {SERVICE_DATA_EEV_PULSES}
    )
    device._maybe_request_service_data()
    await asyncio.sleep(0.05)


def _content_of(payload: str) -> list[int]:
    """The 18 state bytes inside a captured payload."""
    raw = [
        (256 - b) * (-1) if b > 127 else b for b in base64.b64decode(payload)
    ]
    start = raw[18] * 4 + 21
    return raw[start:start + 18]


def _cleared_payload() -> str:
    """A running unit's capture rewritten to what an applied all-zero command
    block leaves behind: off, mode auto, fan and both vanes at position 1, and
    a setpoint of zero clamped up to the 10 C heating floor - the four fields
    Wasbiertje's log showed moving together (#329).
    """
    raw = [
        (256 - b) * (-1) if b > 127 else b
        for b in base64.b64decode(ON_COOL_PAYLOAD)
    ]
    start = raw[18] * 4 + 21
    content = raw[start:start + 18]
    content[2] = 0
    content[3] = 0
    content[4] = 20  # the unit's own floor, not the zero it was sent
    content[11] = 0
    content[12] = 0
    return _build_stat_response(content)


CLEARED_PAYLOAD = _cleared_payload()


async def test_a_unit_that_applies_our_request_is_switched_to_carrying_its_state(
    device, monkeypatch
):
    """The request carries no set-bits, so nothing should change - but one
    module applies every field anyway (#329), and the zeros land as power off,
    mode auto, fan and vanes at 1 and the setpoint on the unit's floor.

    Detected from that whole pattern rather than from firmType: the bridge MCU
    handles this frame identically across firmware branches, so the branch
    would be the wrong thing to gate on. One occurrence is enough - the test is
    not that something moved but that everything that moved landed on its
    minimum, and nothing else does that.
    """
    # Registered, so the entry can actually be written to - see _set_options.
    device.config_entry.add_to_hass(device.hass)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    assert device._parser.status_request_carries_state is False

    device._api.send_airco_command = AsyncMock(return_value=CLEARED_PAYLOAD)
    await _run_service_data_request(device, monkeypatch)

    assert device._parser.status_request_carries_state is True
    # Written down, so the next start does not put the unit through the same
    # disturbance to learn the same thing (#329, reported again after an update
    # had reset the counter this replaced).
    assert device.config_entry.data[CONF_STATUS_REQUEST_MODE] == STATUS_REQUEST_ECHO


async def test_the_settings_the_request_cleared_are_written_back(
    device, monkeypatch
):
    """Detection alone would leave the unit on values nobody chose.

    The values put back are one round trip old, which is the same freshness
    the echo itself runs on - and the alternative is a unit sitting at 10 C
    until somebody notices.
    """
    device.config_entry.add_to_hass(device.hass)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    before = device._settings_snapshot()

    sent: list[dict] = []
    real_set_airco = device.set_airco

    async def _record(params, **kwargs):
        sent.append(params)
        await real_set_airco(params, **kwargs)

    monkeypatch.setattr(device, "set_airco", _record)
    device._api.send_airco_command = AsyncMock(return_value=CLEARED_PAYLOAD)
    await _run_service_data_request(device, monkeypatch)
    await asyncio.sleep(0.05)

    assert len(sent) == 2, "expected the request, then a frame putting it back"
    restore = sent[-1]
    assert restore[AirconCommands.PresetTemp] == before["PresetTemp"]
    assert restore[AirconCommands.Operation] == before["Operation"]
    assert restore[AirconCommands.OperationMode] == before["OperationMode"]
    assert restore[AirconCommands.AirFlow] == before["AirFlow"]


async def test_a_unit_that_is_already_off_is_still_detected(device, monkeypatch):
    """The case the earlier detector could not see.

    It watched for the unit switching off, which a unit that is already off can
    no longer do - so on the one unit it was written for it stopped firing
    after the first shutdown, while every following request went on clearing
    the setpoint, the mode, the fan and both vanes.
    """
    device.config_entry.add_to_hass(device.hass)
    running_but_off = _stats_response(ON_COOL_PAYLOAD)
    device._api.get_aircon_stats.return_value = running_but_off
    await device.update()
    # Off, but with its settings intact - the state his unit sat in for five
    # betas.
    device._airco.Operation = False

    device._api.send_airco_command = AsyncMock(return_value=CLEARED_PAYLOAD)
    await _run_service_data_request(device, monkeypatch)

    assert device._parser.status_request_carries_state is True


async def test_a_change_that_only_shows_up_at_the_next_poll_is_still_detected(
    device, monkeypatch
):
    """The timing the affected unit actually has (#329, log of 10.09.2026).

    The module answers our request out of its own cache, and the frame's trip
    down the CNS bus to the indoor unit need not have finished by then: his
    request went out at 19:25:37 and the cleared setpoint, fan and both vanes
    appeared in the poll at 19:26:08, not in the answer. A detector reading
    only the answer would have the same blind spot as the one it replaced.
    """
    device.config_entry.add_to_hass(device.hass)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()

    # The answer still shows the unit as it was.
    device._api.send_airco_command = AsyncMock(return_value=ON_COOL_PAYLOAD)
    await _run_service_data_request(device, monkeypatch)
    assert device._parser.status_request_carries_state is False

    device._api.get_aircon_stats.return_value = _stats_response(CLEARED_PAYLOAD)
    await device.update()

    assert device._parser.status_request_carries_state is True
    assert device.config_entry.data[CONF_STATUS_REQUEST_MODE] == STATUS_REQUEST_ECHO


async def test_a_change_after_a_real_command_is_not_blamed_on_the_request(
    device, monkeypatch
):
    """A command of our own explains what moves after it, so the status
    request before it stops having to answer for the next poll."""
    device.config_entry.add_to_hass(device.hass)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()

    device._api.send_airco_command = AsyncMock(return_value=ON_COOL_PAYLOAD)
    await _run_service_data_request(device, monkeypatch)
    await device.set_airco({AirconCommands.Operation: False})

    device._api.get_aircon_stats.return_value = _stats_response(CLEARED_PAYLOAD)
    await device.update()

    assert device._parser.status_request_carries_state is False


async def test_a_reading_of_zero_counts_as_an_answer(device, monkeypatch):
    """Giving up must never rest on the value.

    Several of these fields sit at zero for hours quite legitimately - the
    overload-protection byte reads 0 on every unit measured so far, including
    while the protection is demonstrably engaged. What is counted is whether a
    segment arrived at all, which is why the check is `is not None` and not a
    truth test.
    """
    device.config_entry.add_to_hass(device.hass)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()

    answered_with_zero = _build_stat_response_with_segments(
        _content_of(ON_COOL_PAYLOAD),
        [(SERVICE_DATA_PROTECTION_RAW, 0xFF, 0, 0)],
    )
    device._api.send_airco_command = AsyncMock(return_value=answered_with_zero)
    for _ in range(coordinator_module.SERVICE_DATA_UNANSWERED_LIMIT * 2):
        await _run_service_data_request(device, monkeypatch)

    assert device.airco.ProtectionRaw == 0
    assert device.service_data_supported is True


async def test_a_unit_that_answers_nothing_at_all_is_given_up_on(
    device, monkeypatch
):
    """Wasbiertje's unit (#329): it takes the request and replies, but has
    never once put a segment in the reply - so the sensors were never going to
    have a value, and asking every minute is a write spent on nothing.
    """
    device.config_entry.add_to_hass(device.hass)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()

    empty_answer = _build_stat_response_with_segments(
        _content_of(ON_COOL_PAYLOAD), []
    )
    device._api.send_airco_command = AsyncMock(return_value=empty_answer)
    for _ in range(coordinator_module.SERVICE_DATA_UNANSWERED_LIMIT):
        await _run_service_data_request(device, monkeypatch)

    assert device.service_data_supported is False

    # And it stops asking.
    device._api.send_airco_command = AsyncMock(return_value=empty_answer)
    await _run_service_data_request(device, monkeypatch)
    device._api.send_airco_command.assert_not_awaited()


async def test_giving_up_on_the_channel_is_not_written_down(device, monkeypatch):
    """Unlike the frame shape. Relearning that costs the user a unit that
    clears its settings; relearning this costs ten requests - so a module that
    starts answering is picked up again after a reload rather than written off
    for good.
    """
    device.config_entry.add_to_hass(device.hass)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()

    empty_answer = _build_stat_response_with_segments(
        _content_of(ON_COOL_PAYLOAD), []
    )
    device._api.send_airco_command = AsyncMock(return_value=empty_answer)
    for _ in range(coordinator_module.SERVICE_DATA_UNANSWERED_LIMIT):
        await _run_service_data_request(device, monkeypatch)

    assert device.service_data_supported is False
    assert CONF_STATUS_REQUEST_MODE not in device.config_entry.data
    assert "service_data_unsupported" not in device.config_entry.data


async def test_an_echo_that_still_moves_the_unit_stops_the_request(
    device, monkeypatch
):
    """The escalation. If the frame confirms the unit's own settings and the
    unit changes them anyway, the frame is not what it objects to - and then
    the only honest answer is to stop sending it, rather than to keep the unit
    unusable for the sake of five diagnostic sensors.
    """
    device.config_entry.add_to_hass(device.hass)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    device._status_request_mode = STATUS_REQUEST_ECHO
    device._parser.status_request_carries_state = True

    device._api.send_airco_command = AsyncMock(return_value=CLEARED_PAYLOAD)
    await _run_service_data_request(device, monkeypatch)

    assert device._status_request_mode == STATUS_REQUEST_SILENT
    assert device.config_entry.data[CONF_STATUS_REQUEST_MODE] == STATUS_REQUEST_SILENT

    # And nothing is asked of it again.
    device._api.send_airco_command = AsyncMock(return_value=ON_COOL_PAYLOAD)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    await _run_service_data_request(device, monkeypatch)
    device._api.send_airco_command.assert_not_awaited()


async def test_an_ordinary_change_at_the_unit_is_not_read_as_the_fault(
    device, monkeypatch
):
    """Somebody using the remote while our request is in flight moves fields to
    the values they chose. Only the fault sweeps the whole block to the bottom.
    """
    device.config_entry.add_to_hass(device.hass)
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()

    device._api.send_airco_command = AsyncMock(return_value=FAN_SPEED_4_PAYLOAD)
    await _run_service_data_request(device, monkeypatch)

    assert device._parser.status_request_carries_state is False
    assert CONF_STATUS_REQUEST_MODE not in device.config_entry.data


async def test_a_learned_status_request_mode_survives_a_restart(hass):
    """A module that applies the request has always applied it.

    The decision used to live only in memory as a counter, so every restart
    and every reload started it from zero - and the unit paid for it again
    each time. An update restarts Home Assistant, which is how the one
    affected tester never reached the threshold at all.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_STATUS_REQUEST_MODE: STATUS_REQUEST_ECHO}
    )
    entry.add_to_hass(hass)
    device = Device(
        hass, entry, "Test AC", "127.0.0.1", 51443, "device-id", "operator-id",
        "airco-id", swing_selects_enabled_default=True,
        status_request_mode=entry.data[CONF_STATUS_REQUEST_MODE],
    )
    device._api = AsyncMock()

    assert device._parser.status_request_carries_state is True

    await device.async_shutdown()


async def test_a_unit_switched_off_at_the_unit_is_not_blamed_on_us(
    device, monkeypatch
):
    """updatedBy "aircon" means somebody reached for the remote in the same
    second. Reacting to that would switch the request over on a device that
    was never affected - and carrying the power state has a cost of its own.
    """
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()

    response = _stats_response(OFF_PAYLOAD)
    response["updatedBy"] = "aircon"
    device._api.get_aircon_stats.return_value = response
    device._api.send_airco_command = AsyncMock(return_value=OFF_PAYLOAD)
    await device.update()
    await _run_service_data_request(device, monkeypatch)

    assert device._parser.status_request_carries_state is False


async def test_no_request_goes_out_while_a_carrying_unit_is_believed_off(
    device, monkeypatch
):
    """Once the power state is carried, the request is a power write in all but
    name - and what it would write is as old as the last poll. So it is not
    sent at all while the unit is believed off: a unit switched on with the
    remote inside that window would be switched straight back off by the frame
    meant to confirm its state (#329).
    """
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await device.update()
    device._parser.status_request_carries_state = True
    device.set_airco = set_airco = AsyncMock()

    await _run_service_data_request(device, monkeypatch)

    set_airco.assert_not_awaited()

    # Running again, the same request goes out as usual.
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    await _run_service_data_request(device, monkeypatch)

    set_airco.assert_awaited()


async def test_a_carrying_request_echoes_what_it_just_read(device, monkeypatch):
    """The state is written back, so it has to be the unit's current one.

    Anything changed at the unit since the last poll would otherwise be undone
    by the very frame meant to read from it.
    """
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    device._parser.status_request_carries_state = True
    stale_fan = device.airco.AirFlow
    device.set_airco = AsyncMock()

    monkeypatch.setattr(
        coordinator_module, "SERVICE_DATA_REQUEST_OFFSET", timedelta(milliseconds=30)
    )
    monkeypatch.setattr(coordinator_module, "SERVICE_DATA_MIN_SPACING", timedelta(0))
    monkeypatch.setattr(device, "async_contexts", lambda: {SERVICE_DATA_EEV_PULSES})
    device._service_data_offset = timedelta(milliseconds=30)
    device._maybe_request_service_data()

    # Somebody reaches for the remote while the request waits out its offset.
    device._api.get_aircon_stats.return_value = _stats_response(FAN_SPEED_4_PAYLOAD)
    await asyncio.sleep(0.1)

    device.set_airco.assert_awaited()
    assert device.airco.AirFlow != stale_fan


async def test_a_request_that_does_not_carry_state_reads_nothing_extra(
    device, monkeypatch
):
    """A unit that ignores the empty block is not worth an extra request."""
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    device.set_airco = AsyncMock()

    monkeypatch.setattr(
        coordinator_module, "SERVICE_DATA_REQUEST_OFFSET", timedelta(milliseconds=30)
    )
    monkeypatch.setattr(coordinator_module, "SERVICE_DATA_MIN_SPACING", timedelta(0))
    monkeypatch.setattr(device, "async_contexts", lambda: {SERVICE_DATA_EEV_PULSES})
    device._service_data_offset = timedelta(milliseconds=30)
    device._api.get_aircon_stats.reset_mock()
    device._maybe_request_service_data()
    await asyncio.sleep(0.1)

    device._api.get_aircon_stats.assert_not_awaited()


async def test_a_unit_switched_off_inside_the_offset_gets_no_request(
    device, monkeypatch
):
    """The read taken just before the echo decides, not the last poll: the
    offset is up to half a minute, and the unit going off inside it is exactly
    the window this protects.
    """
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    device._parser.status_request_carries_state = True
    device.set_airco = set_airco = AsyncMock()

    monkeypatch.setattr(
        coordinator_module, "SERVICE_DATA_REQUEST_OFFSET", timedelta(milliseconds=30)
    )
    monkeypatch.setattr(coordinator_module, "SERVICE_DATA_MIN_SPACING", timedelta(0))
    monkeypatch.setattr(device, "async_contexts", lambda: {SERVICE_DATA_EEV_PULSES})
    device._service_data_offset = timedelta(milliseconds=30)
    device._maybe_request_service_data()

    # ... and the unit goes off while the request is still waiting out its
    # offset, which is what the read before the echo finds.
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    await asyncio.sleep(0.1)

    set_airco.assert_not_awaited()


async def test_the_home_leave_status_request_echoes_a_fresh_read(device):
    """The Home Leave status request is a carrying frame too.

    On an affected module RacParser encodes any status request as a full
    command block, so this action writes every field it holds - and a state
    left over from the last poll would undo whatever was done at the unit
    since.
    """
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    device._parser.status_request_carries_state = True
    stale_fan = device.airco.AirFlow
    device.set_airco = set_airco = AsyncMock()

    # Somebody reaches for the remote between the poll and the action.
    device._api.get_aircon_stats.return_value = _stats_response(FAN_SPEED_4_PAYLOAD)
    await device.async_request_home_leave_mode_status()

    set_airco.assert_awaited()
    assert device.airco.AirFlow != stale_fan


async def test_the_home_leave_status_request_does_not_send_a_state_it_could_not_read(
    device,
):
    """Sending the old state anyway is a write on this module. The caller
    asked for a reading, so the failure has to reach it."""
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    device._parser.status_request_carries_state = True
    device.set_airco = set_airco = AsyncMock()
    device._api.get_aircon_stats.side_effect = WfRacError("boom")

    with pytest.raises(HomeAssistantError):
        await device.async_request_home_leave_mode_status()

    set_airco.assert_not_awaited()


async def test_a_plain_home_leave_status_request_reads_nothing_extra(device):
    """Without the quirk the block holds no set-bits, so there is nothing to
    echo and no reason to spend a second round trip."""
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    device.set_airco = set_airco = AsyncMock()
    device._api.get_aircon_stats.reset_mock()

    await device.async_request_home_leave_mode_status()

    set_airco.assert_awaited()
    device._api.get_aircon_stats.assert_not_awaited()


async def test_a_carrying_request_writes_the_settings_back_instead_of_zeros(device):
    """The empty block writes a zero into every field an affected module
    applies; the carrying one writes the unit's own settings back instead.
    """
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    stat = AirconStat.from_aircon(device.airco)
    stat.ServiceDataStatusRequest = (SERVICE_DATA_EEV_PULSES,)

    empty = device._parser.status_request_to_byte(stat)

    device._parser.status_request_carries_state = True
    carried = device._parser.status_request_to_byte(stat)

    # Power and mode, fan step, setpoint, and the horizontal vane: each of
    # these is a setting the affected unit cleared on every request.
    for index in (2, 3, 4, 12):
        assert empty[index] == 0
        assert carried[index] != 0
    assert carried == device._parser.command_to_byte(stat)


async def test_the_request_moves_back_towards_the_poll_while_it_keeps_landing(
    device, monkeypatch
):
    """Half a cycle was a guess. Closer is better for everything except
    crowding: what the request carries, and any judgement about who changed
    the unit, is as old as the last poll.
    """
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    device._api.send_airco_command = AsyncMock(return_value=ON_COOL_PAYLOAD)
    monkeypatch.setattr(
        coordinator_module, "SERVICE_DATA_OFFSET_STEP", timedelta(milliseconds=1)
    )
    monkeypatch.setattr(
        coordinator_module, "SERVICE_DATA_OFFSET_MIN", timedelta(milliseconds=1)
    )
    device._service_data_offset = timedelta(milliseconds=5)

    for _ in range(coordinator_module.SERVICE_DATA_OFFSET_GOOD_CYCLES):
        await _run_service_data_request(device, monkeypatch, ceiling_ms=20)

    assert device.service_data_offset == timedelta(milliseconds=4)


async def test_a_refused_request_pushes_it_away_from_the_poll_again(
    device, monkeypatch
):
    """A refusal is the module saying the request came too close to something
    else, which is what the offset exists to prevent.
    """
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    device._service_data_offset = timedelta(milliseconds=1)
    device._api.send_airco_command = AsyncMock(side_effect=WfRacCommandError("501"))

    await _run_service_data_request(device, monkeypatch, ceiling_ms=20)

    assert device.service_data_offset == timedelta(milliseconds=2)


async def test_the_offset_never_goes_below_the_floor_or_above_the_ceiling(
    device, monkeypatch
):
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()

    device._api.send_airco_command = AsyncMock(return_value=ON_COOL_PAYLOAD)
    monkeypatch.setattr(
        coordinator_module, "SERVICE_DATA_OFFSET_MIN", timedelta(milliseconds=2)
    )
    monkeypatch.setattr(
        coordinator_module, "SERVICE_DATA_OFFSET_STEP", timedelta(milliseconds=1)
    )
    device._service_data_offset = timedelta(milliseconds=2)
    for _ in range(coordinator_module.SERVICE_DATA_OFFSET_GOOD_CYCLES * 2):
        await _run_service_data_request(device, monkeypatch, ceiling_ms=20)
    assert device.service_data_offset == timedelta(milliseconds=2)

    device._service_data_offset = timedelta(milliseconds=20)
    device._api.send_airco_command = AsyncMock(side_effect=WfRacCommandError("501"))
    await _run_service_data_request(device, monkeypatch, ceiling_ms=20)
    assert device.service_data_offset == timedelta(milliseconds=20)


async def test_a_setting_that_changed_with_no_write_is_read_as_the_unit_itself(
    device, caplog
):
    """Only a setAirconStat moves the write lock, so a setting that changed
    while expires stood still was not changed over the network.
    """
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)

    with caplog.at_level(logging.DEBUG):
        await device.update()

    assert "changed at the unit itself" in caplog.text
    assert device.foreign_activity is False


async def test_a_change_at_the_unit_survives_the_operation_data_request(
    device, monkeypatch, caplog
):
    """The request changes nothing, so what comes back is a reading - and in
    the 5-30s it sits behind the poll, that reading can already carry an IR
    command. Claiming it as our expectation would leave the next poll with
    nothing to compare and the change unreported.
    """
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()

    # Someone picks up the remote between the poll and the request: mode and
    # setpoint are already the new ones when the request's response arrives.
    device._api.send_airco_command = AsyncMock(return_value=ON_HEAT_PAYLOAD)
    await _run_service_data_request(device, monkeypatch)
    device._api.send_airco_command.assert_awaited_once()

    device._api.get_aircon_stats.return_value = _stats_response(ON_HEAT_PAYLOAD)
    with caplog.at_level(logging.DEBUG):
        await device.update()

    assert "changed at the unit itself" in caplog.text
    assert device.foreign_activity is False


async def test_our_own_command_is_not_read_as_somebody_elses(device, caplog):
    """The expectation moves with what the unit reports back to our own write,
    or every command we send would come back as a foreign one.
    """
    device._api.get_aircon_stats.return_value = _stats_response(ON_COOL_PAYLOAD)
    await device.update()
    device._api.send_airco_command = AsyncMock(return_value=OFF_PAYLOAD)
    await device.set_airco({AirconCommands.Operation: False})

    device._api.get_aircon_stats.return_value = _stats_response(OFF_PAYLOAD)
    with caplog.at_level(logging.DEBUG):
        await device.update()

    assert "changed at the unit" not in caplog.text
    assert device.foreign_activity is False
