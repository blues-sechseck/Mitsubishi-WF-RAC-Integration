"""Silent operation switch and the beta-only research data.

Silent operation is written with a trailer segment the module refuses although
the unit applies it, so these tests pin the read-back that decides success.
The research request replaces a regular operation-data request now and then
and must never let an empty request turn into a full command.
"""

import asyncio
import base64
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pywfrac import AirconCommands
from pywfrac.parser import SERVICE_DATA_COMPRESSOR_FREQ, SERVICE_DATA_SILENT_OPERATION
from pywfrac.repository import WfRacWriteRefusedError

from custom_components.mitsubishi_wf_rac import (
    coordinator as coordinator_module,
    sensor,
    service_data as service_data_module,
    switch,
)
from custom_components.mitsubishi_wf_rac.const import DOMAIN, STATUS_REQUEST_SILENT
from custom_components.mitsubishi_wf_rac.service_data import (
    RESEARCH_BATCH_SIZE,
    RESEARCH_CONTEXT,
    RESEARCH_SERVICE_DATA_CODES,
    SERVICE_DATA_MAX_AGE,
    SERVICE_DATA_REQUEST_INTERVAL,
    ResearchData,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from tests.unit.live_captures import LIVE_CAPTURES

ON_COOL_PAYLOAD, _ = LIVE_CAPTURES["on_cool"]


def _stat_with_segments(segments: list[tuple[int, int, int, int]]) -> str:
    """A parseable airconStat whose trailer carries the given segments."""
    body = [0] * 21 + [0] * 18 + [0]
    for segment in segments:
        body += list(segment)
    return base64.b64encode(bytes(b & 0xFF for b in [*body, 0, 0])).decode()


def _silent_answer(on: bool) -> str:
    return _stat_with_segments(
        [(SERVICE_DATA_SILENT_OPERATION, 0x80, 0x20 if on else 0, 0)]
    )


def _trailer_of(command: str) -> list[int]:
    """The command half's trailer segments: count byte, then 4 bytes each."""
    raw = base64.b64decode(command)
    count = raw[18]
    return list(raw[19 : 19 + 4 * count])


@pytest.fixture
def fast_readback(monkeypatch):
    monkeypatch.setattr(coordinator_module, "SILENT_OPERATION_READBACK_DELAY", 0)


# --- silent operation write ------------------------------------------------


async def test_silent_write_refused_but_read_back_confirms(
    platform_device, fast_readback
):
    sent: list[str] = []

    async def send(_airco_id, command, **_kwargs):
        sent.append(command)
        if len(sent) == 1:
            raise WfRacWriteRefusedError("result 11")
        return _silent_answer(True)

    platform_device._api.send_airco_command = AsyncMock(side_effect=send)

    await platform_device.async_set_silent_operation(True)

    assert platform_device.airco.SilentOperation is True
    # The write frame carries the one segment and nothing else...
    assert _trailer_of(sent[0]) == [0x21, 0x01, 0xFF, 0xFF]
    # ...in a block without the power set-bit: it must not command the unit.
    assert base64.b64decode(sent[0])[2] & 0x02 == 0
    # The read-back is a plain 0xDD request.
    assert _trailer_of(sent[1]) == [SERVICE_DATA_SILENT_OPERATION, 0xFF, 0xFF, 0xFF]


async def test_silent_write_not_confirmed_raises(platform_device, fast_readback):
    platform_device._api.send_airco_command = AsyncMock(
        return_value=_silent_answer(False)
    )

    with pytest.raises(HomeAssistantError) as err:
        await platform_device.async_set_silent_operation(True)

    assert err.value.translation_key == "silent_operation_not_applied"
    # One write, then every read-back attempt.
    assert platform_device._api.send_airco_command.await_count == (
        1 + coordinator_module.SILENT_OPERATION_READBACK_ATTEMPTS
    )


async def test_silent_write_confirmed_on_a_later_read(platform_device, fast_readback):
    answers = iter([_silent_answer(False), _silent_answer(False), _silent_answer(True)])

    async def send(_airco_id, command, **_kwargs):
        if _trailer_of(command)[0] == 0x21:
            return _silent_answer(False)
        return next(answers)

    platform_device._api.send_airco_command = AsyncMock(side_effect=send)

    await platform_device.async_set_silent_operation(True)

    assert platform_device.airco.SilentOperation is True


async def test_silent_write_is_never_merged_with_a_queued_command(
    platform_device, fast_readback
):
    set_airco = AsyncMock()
    platform_device.set_airco = set_airco
    platform_device._airco.SilentOperation = False

    with pytest.raises(HomeAssistantError):
        await platform_device.async_set_silent_operation(True)

    assert set_airco.await_args_list[0].args[0] == {
        AirconCommands.SilentOperationSet: True
    }
    assert set_airco.await_args_list[0].kwargs["is_status_request"] is True


async def test_silent_write_refused_when_requests_are_off(platform_device):
    platform_device._status_request_mode = STATUS_REQUEST_SILENT
    platform_device._api.send_airco_command = AsyncMock()

    with pytest.raises(HomeAssistantError) as err:
        await platform_device.async_set_silent_operation(True)

    assert err.value.translation_key == "status_request_not_sent"
    platform_device._api.send_airco_command.assert_not_awaited()


# --- silent operation switch -----------------------------------------------


async def test_switch_follows_the_unit_and_waits_for_an_answer(hass, platform_device):
    entry = MagicMock(runtime_data=MagicMock(device=platform_device))
    added: list = []
    await switch.async_setup_entry(hass, entry, added.extend)
    (silent,) = added

    assert silent.coordinator_context == SERVICE_DATA_SILENT_OPERATION
    assert silent.available is False

    platform_device._airco.SilentOperation = True
    silent._apply_state()
    assert silent.available is True
    assert silent.is_on is True


# --- research request scheduling --------------------------------------------


@pytest.fixture
def due_every_time(platform_device, monkeypatch):
    monkeypatch.setattr(
        service_data_module, "SERVICE_DATA_REQUEST_OFFSET", timedelta(milliseconds=1)
    )
    monkeypatch.setattr(
        service_data_module, "SERVICE_DATA_OFFSET_MIN", timedelta(milliseconds=1)
    )
    monkeypatch.setattr(platform_device.service_data, "due", lambda: True)
    platform_device.set_airco = AsyncMock()
    return platform_device.set_airco


async def _cycles(device, count: int) -> list[tuple[int, ...]]:
    sent = []
    for _ in range(count):
        device.maybe_request_service_data()
        task = device.service_data.task
        if task is not None:
            await task
        call = device.set_airco.await_args
        sent.append(
            call.args[0][AirconCommands.ServiceDataStatusRequest] if call else ()
        )
        device.set_airco.reset_mock()
    return sent


async def test_every_fifth_request_carries_research_codes(
    platform_device, monkeypatch, due_every_time
):
    monkeypatch.setattr(
        platform_device,
        "async_contexts",
        lambda: {SERVICE_DATA_COMPRESSOR_FREQ, RESEARCH_CONTEXT},
    )

    sent = await _cycles(platform_device, 5)

    assert sent[:4] == [(SERVICE_DATA_COMPRESSOR_FREQ,)] * 4
    assert sent[4] == (RESEARCH_SERVICE_DATA_CODES[0],)


async def test_research_alone_asks_every_cycle(
    platform_device, monkeypatch, due_every_time
):
    monkeypatch.setattr(platform_device, "async_contexts", lambda: {RESEARCH_CONTEXT})

    sent = await _cycles(platform_device, 2)

    assert sent == [(RESEARCH_SERVICE_DATA_CODES[0],)] * 2


async def test_no_request_without_codes(platform_device, monkeypatch, due_every_time):
    """An empty request would go out as a full command block - never send one."""
    monkeypatch.setattr(platform_device, "async_contexts", lambda: {RESEARCH_CONTEXT})
    for code in RESEARCH_SERVICE_DATA_CODES:
        platform_device.research_data.states[code] = "refused"

    platform_device.maybe_request_service_data()
    await asyncio.sleep(0)

    due_every_time.assert_not_awaited()


def test_a_skipped_cycle_does_not_expire_the_regular_readings():
    assert SERVICE_DATA_MAX_AGE >= 2 * SERVICE_DATA_REQUEST_INTERVAL


# --- research qualification --------------------------------------------------


def _answer(*codes: int) -> MagicMock:
    return MagicMock(ServiceDataRaw=dict.fromkeys(codes, (16, 1, 255)))


def test_untested_codes_go_out_alone_and_qualify_on_an_answer():
    research = ResearchData()
    first = RESEARCH_SERVICE_DATA_CODES[0]

    assert research.request_codes() == (first,)
    research.note_answer(_answer(first))

    assert research.states[first] == "ok"
    assert research.values[first] == (0x10, 0x01, 0xFF)
    assert research.request_codes() == (RESEARCH_SERVICE_DATA_CODES[1],)


def test_a_lone_code_is_dropped_only_after_repeated_refusals():
    research = ResearchData()
    code = RESEARCH_SERVICE_DATA_CODES[0]

    research.note_refused((code,))
    assert research.states[code] == "untested"
    research.note_refused((code,))
    assert research.states[code] == "refused"


def test_a_refused_batch_goes_back_to_untested():
    research = ResearchData()
    for code in RESEARCH_SERVICE_DATA_CODES:
        research.states[code] = "ok"

    batch = research.request_codes()
    assert len(batch) == RESEARCH_BATCH_SIZE
    research.note_refused(batch)

    assert all(research.states[code] == "untested" for code in batch)


def test_batches_rotate_through_the_answering_codes():
    research = ResearchData()
    for code in RESEARCH_SERVICE_DATA_CODES:
        research.states[code] = "ok"

    seen: set[int] = set()
    for _ in range(3):
        seen.update(research.request_codes())

    assert seen == set(RESEARCH_SERVICE_DATA_CODES)


# --- beta gating ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "expected"),
    [("2026.10.1-beta1", True), ("2026.10.1-dev1", True), ("2026.10.1", False)],
)
async def test_prerelease_detection(hass, monkeypatch, version, expected):
    integration = MagicMock(manifest={"version": version})
    monkeypatch.setattr(
        sensor, "async_get_integration", AsyncMock(return_value=integration)
    )
    hass.data.pop(f"{DOMAIN}_prerelease_build", None)

    assert await sensor.is_prerelease_build(hass) is expected


async def test_final_build_removes_the_research_sensor(hass, platform_device):
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "sensor", DOMAIN, f"{DOMAIN}-airco-id-research-data"
    )

    sensor._async_remove_research_data_sensor(hass, platform_device)

    assert registry.async_get(entry.entity_id) is None


async def test_beta_build_offers_the_research_sensor(
    hass, platform_device, monkeypatch
):
    monkeypatch.setattr(sensor, "is_prerelease_build", AsyncMock(return_value=True))
    added: list = []
    await sensor.async_setup_entry(
        hass, MagicMock(runtime_data=MagicMock(device=platform_device)), added.extend
    )

    research = [e for e in added if e.unique_id == f"{DOMAIN}-airco-id-research-data"]
    assert len(research) == 1
    assert research[0].entity_registry_enabled_default is False


async def test_final_build_offers_no_research_sensor(
    hass, platform_device, monkeypatch
):
    monkeypatch.setattr(sensor, "is_prerelease_build", AsyncMock(return_value=False))
    added: list = []
    await sensor.async_setup_entry(
        hass, MagicMock(runtime_data=MagicMock(device=platform_device)), added.extend
    )

    assert not [e for e in added if e.unique_id == f"{DOMAIN}-airco-id-research-data"]
