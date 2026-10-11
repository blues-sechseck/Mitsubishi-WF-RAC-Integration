"""Which operation-data codes each poll asks for, and what a refusal costs.

The request is capped at a budget of segments; each code has an interval, the
most overdue due codes go first, research fills the slots left over, and a
code the unit does not know is suspended instead of taking the request down.
"""

import base64
from datetime import timedelta
import logging
from unittest.mock import AsyncMock

import pytest
from pywfrac import Aircon, AirconCommands
from pywfrac.parser import (
    SERVICE_DATA_CODES,
    SERVICE_DATA_COMPRESSOR_FREQ,
    SERVICE_DATA_DISCHARGE_SUPERHEAT_RAW,
    SERVICE_DATA_EEV_PULSES,
    SERVICE_DATA_HUMIDITY,
    SERVICE_DATA_INDOOR_COIL_RAW,
    SERVICE_DATA_OUTDOOR_COIL_RAW,
    SERVICE_DATA_SILENT_OPERATION,
)
from pywfrac.repository import WfRacWriteRefusedError

from custom_components.mitsubishi_wf_rac import (
    coordinator as coordinator_module,
    service_data as service_data_module,
)
from custom_components.mitsubishi_wf_rac.service_data import (
    RESEARCH_CONTEXT,
    RESEARCH_SERVICE_DATA_CODES,
    SERVICE_DATA_BUDGET,
    SERVICE_DATA_INTERVALS,
    service_data_expiry,
)
from homeassistant.util import dt as dt_util

FAST = SERVICE_DATA_COMPRESSOR_FREQ  # 60 s
MEDIUM = SERVICE_DATA_OUTDOOR_COIL_RAW  # 120 s
SLOW = SERVICE_DATA_DISCHARGE_SUPERHEAT_RAW  # 300 s


def _read_ago(channel, code: int, seconds: float) -> None:
    channel._note_read(code, dt_util.utcnow() - timedelta(seconds=seconds))


def _read_before_send(channel, code: int, seconds: float) -> None:
    """Read `seconds` before the moment this poll's request goes out."""
    sent = dt_util.utcnow() + channel.offset
    channel._note_read(code, sent - timedelta(seconds=seconds))


def _prove(channel, *codes: int) -> None:
    for code in codes:
        channel._states[code] = "ok"


# --- selection ---------------------------------------------------------------


def test_the_table_only_names_codes_the_parser_decodes():
    assert set(SERVICE_DATA_INTERVALS) == set(SERVICE_DATA_CODES)


def test_most_overdue_ratio_goes_first_and_never_read_codes_lead(
    platform_device, monkeypatch
):
    monkeypatch.setattr(service_data_module, "SERVICE_DATA_BUDGET", 2)
    channel = platform_device.service_data
    _read_before_send(channel, FAST, 200)  # ratio 3.3
    _read_before_send(channel, MEDIUM, 300)  # ratio 2.5
    _read_before_send(channel, SLOW, 400)  # ratio 1.3
    _prove(channel, FAST, MEDIUM, SLOW, SERVICE_DATA_HUMIDITY)
    codes = [FAST, MEDIUM, SLOW]

    assert channel.select(codes, None) == (FAST, MEDIUM)
    # A code never read is maximally overdue and beats them all.
    assert channel.select([*codes, SERVICE_DATA_HUMIDITY], None) == (
        FAST,
        SERVICE_DATA_HUMIDITY,
    )


def test_the_request_never_exceeds_the_budget(platform_device):
    channel = platform_device.service_data
    codes = list(range(0x20, 0x20 + SERVICE_DATA_BUDGET + 3))
    _prove(channel, *codes)

    chosen = channel.select(codes, platform_device.airco)

    assert len(chosen) == SERVICE_DATA_BUDGET
    assert chosen == tuple(sorted(codes[:SERVICE_DATA_BUDGET]))


def test_the_codes_left_out_of_a_full_request_lead_the_next(platform_device):
    channel = platform_device.service_data
    codes = list(SERVICE_DATA_CODES)  # none read yet
    assert len(codes) == SERVICE_DATA_BUDGET + 1
    _prove(channel, *codes)

    first = channel.select(codes, platform_device.airco)
    for code in first:
        _read_before_send(channel, code, 0)
    second = channel.select(codes, platform_device.airco)

    (left_out,) = set(codes) - set(first)
    assert left_out in second


def test_with_ten_or_fewer_due_codes_everything_goes_sorted(platform_device):
    channel = platform_device.service_data
    codes = sorted(SERVICE_DATA_CODES)[:SERVICE_DATA_BUDGET]

    assert channel.select(reversed(codes), platform_device.airco) == tuple(codes)


@pytest.mark.parametrize(
    ("code", "age", "due"),
    [
        # A 60 s code read at the previous poll is a few seconds off 60 s old,
        # either way; it must be asked every poll, not every second one.
        (FAST, 56, True),
        (FAST, 64, True),
        (FAST, 31, True),
        (FAST, 29, False),
        (MEDIUM, 61, False),
        (MEDIUM, 91, True),
        (SLOW, 240, False),
        (SLOW, 271, True),
    ],
)
def test_a_code_is_due_within_half_a_poll_of_its_interval(
    platform_device, code, age, due
):
    channel = platform_device.service_data
    _read_before_send(channel, code, age)
    _prove(channel, code)

    assert (code in channel.select([code], platform_device.airco)) is due


@pytest.mark.parametrize("offset", [5, 25, 30])
def test_a_fast_code_rides_every_poll_whatever_the_offset(platform_device, offset):
    channel = platform_device.service_data
    channel._offset = timedelta(seconds=offset)
    _prove(channel, FAST)
    # Answered two seconds after the previous request, which went out the
    # offset after the previous poll; this poll is a minute after that one.
    channel._note_read(
        FAST,
        dt_util.utcnow()
        - timedelta(seconds=60)
        + channel.offset
        + timedelta(seconds=2),
    )

    assert channel.select([FAST], platform_device.airco) == (FAST,)


def test_slow_codes_ride_every_second_and_fifth_poll(platform_device):
    channel = platform_device.service_data
    codes = [FAST, MEDIUM, SLOW]
    _prove(channel, *codes)
    for code in codes:
        _read_before_send(channel, code, 0)

    asked: dict[int, int] = dict.fromkeys(codes, 0)
    for poll in range(1, 11):
        # Each poll lands a minute after the last, give or take a few seconds.
        gap = 60 + (3 if poll % 2 else -3)
        for code in codes:
            channel._read[code] -= timedelta(seconds=gap)
        for code in channel.select(codes, platform_device.airco):
            asked[code] += 1
            _read_before_send(channel, code, 0)

    assert asked == {FAST: 10, MEDIUM: 5, SLOW: 2}


def test_an_overdue_mark_beats_the_interval_until_the_code_is_read(platform_device):
    channel = platform_device.service_data
    _read_ago(channel, SERVICE_DATA_SILENT_OPERATION, 1)
    _prove(channel, SERVICE_DATA_SILENT_OPERATION)
    assert channel.select([SERVICE_DATA_SILENT_OPERATION], None) == ()

    channel.mark_overdue(SERVICE_DATA_SILENT_OPERATION)
    assert channel.select([SERVICE_DATA_SILENT_OPERATION], None) == (
        SERVICE_DATA_SILENT_OPERATION,
    )

    _read_ago(channel, SERVICE_DATA_SILENT_OPERATION, 0)
    assert channel.select([SERVICE_DATA_SILENT_OPERATION], None) == ()


def test_a_condition_can_hold_a_code_back(platform_device, monkeypatch):
    monkeypatch.setitem(
        service_data_module.SERVICE_DATA_ONLY_WHEN,
        SERVICE_DATA_EEV_PULSES,
        lambda _: False,
    )
    channel = platform_device.service_data

    assert channel.select([SERVICE_DATA_EEV_PULSES, FAST], None) == (FAST,)


# --- override carrier --------------------------------------------------------


def test_a_carrier_is_chosen_when_nothing_is_due(platform_device):
    channel = platform_device.service_data
    _read_before_send(channel, SERVICE_DATA_INDOOR_COIL_RAW, 1)
    _prove(channel, SERVICE_DATA_INDOOR_COIL_RAW)

    codes = [SERVICE_DATA_INDOOR_COIL_RAW]
    assert channel.select(codes, None) == ()
    assert channel.select(codes, None, need_frame=True) == (
        SERVICE_DATA_INDOOR_COIL_RAW,
    )


def test_a_suspended_carrier_still_rides_when_the_override_needs_a_frame(
    platform_device,
):
    channel = platform_device.service_data
    channel._states[SERVICE_DATA_INDOOR_COIL_RAW] = "suspended"

    assert channel.select([SERVICE_DATA_INDOOR_COIL_RAW], None) == ()
    assert channel.select([SERVICE_DATA_INDOOR_COIL_RAW], None, need_frame=True) == (
        SERVICE_DATA_INDOOR_COIL_RAW,
    )


# --- the request as the coordinator builds it ---------------------------------


@pytest.fixture
def requests(platform_device, monkeypatch):
    """Cycle driver: every poll is due and set_airco answers like a unit would."""
    monkeypatch.setattr(
        service_data_module, "SERVICE_DATA_REQUEST_OFFSET", timedelta(milliseconds=1)
    )
    monkeypatch.setattr(
        service_data_module, "SERVICE_DATA_OFFSET_MIN", timedelta(milliseconds=1)
    )
    monkeypatch.setattr(platform_device.service_data, "due", lambda: True)
    refuse: set[int] = set()

    async def answer(params, **_kwargs):
        codes = params[AirconCommands.ServiceDataStatusRequest]
        if refuse & set(codes):
            raise WfRacWriteRefusedError("result 11")
        for code in codes:
            platform_device.service_data._note_read(code, dt_util.utcnow())
        platform_device.service_data._last_response = dt_util.utcnow()

    platform_device.set_airco = AsyncMock(side_effect=answer)

    async def run(count: int) -> list[tuple[int, ...]]:
        sent = []
        for _ in range(count):
            platform_device.maybe_request_service_data()
            task = platform_device.service_data.task
            if task is not None:
                await task
            call = platform_device.set_airco.await_args
            sent.append(
                call.args[0][AirconCommands.ServiceDataStatusRequest] if call else ()
            )
            platform_device.set_airco.reset_mock()
            # The next poll is a minute on.
            for code in platform_device.service_data._read:
                platform_device.service_data._read[code] -= timedelta(seconds=60)
        return sent

    run.refuse = refuse
    return run


async def test_research_fills_the_free_slots_and_no_regular_cycle_is_lost(
    platform_device, monkeypatch, requests
):
    monkeypatch.setattr(
        platform_device,
        "async_contexts",
        lambda: {SERVICE_DATA_COMPRESSOR_FREQ, RESEARCH_CONTEXT},
    )
    _prove(platform_device.service_data, SERVICE_DATA_COMPRESSOR_FREQ)
    for code in RESEARCH_SERVICE_DATA_CODES:
        platform_device.research_data.states[code] = "ok"

    sent = await requests(6)

    assert all(request[0] == SERVICE_DATA_COMPRESSOR_FREQ for request in sent)
    assert all(len(request) == SERVICE_DATA_BUDGET for request in sent)
    assert len({code for request in sent for code in request[1:]}) > SERVICE_DATA_BUDGET


async def test_an_untested_research_code_waits_for_proven_regular_codes(
    platform_device, monkeypatch, requests
):
    monkeypatch.setattr(
        platform_device,
        "async_contexts",
        lambda: {SERVICE_DATA_COMPRESSOR_FREQ, RESEARCH_CONTEXT},
    )

    sent = await requests(2)

    # First the regular code on its own, until the unit has answered it...
    assert sent[0] == (SERVICE_DATA_COMPRESSOR_FREQ,)
    # ...then one untested research code beside it, a single suspect.
    assert sent[1] == (SERVICE_DATA_COMPRESSOR_FREQ, RESEARCH_SERVICE_DATA_CODES[0])


async def test_a_code_the_unit_refuses_no_longer_takes_the_request_down(
    platform_device, monkeypatch, requests, caplog
):
    caplog.set_level(logging.DEBUG, logger=service_data_module.__name__)
    monkeypatch.setattr(
        platform_device,
        "async_contexts",
        lambda: {SERVICE_DATA_COMPRESSOR_FREQ, SERVICE_DATA_HUMIDITY},
    )
    requests.refuse.add(SERVICE_DATA_HUMIDITY)

    sent = await requests(6)

    assert sent[0] == (SERVICE_DATA_COMPRESSOR_FREQ, SERVICE_DATA_HUMIDITY)
    # Both are suspects after the shared refusal and go one at a time.
    assert sent[1] == (SERVICE_DATA_COMPRESSOR_FREQ,)
    assert sent[-1] == (SERVICE_DATA_COMPRESSOR_FREQ,)
    assert platform_device.service_data._states[SERVICE_DATA_HUMIDITY] == "suspended"
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]
    assert any("0xAD" in r.message for r in caplog.records)
    # The readable sensor kept reading throughout.
    assert platform_device.service_data._states[SERVICE_DATA_COMPRESSOR_FREQ] == "ok"


async def test_an_armed_override_keeps_a_code_in_every_poll(
    platform_device, monkeypatch, requests
):
    monkeypatch.setattr(
        platform_device, "async_contexts", lambda: {SERVICE_DATA_INDOOR_COIL_RAW}
    )
    platform_device.external_temperature._override = 22.0
    _prove(platform_device.service_data, SERVICE_DATA_INDOOR_COIL_RAW)

    # The reading is brand new each time - nothing is due by interval.
    sent = []
    for _ in range(3):
        platform_device.service_data._note_read(
            SERVICE_DATA_INDOOR_COIL_RAW, dt_util.utcnow()
        )
        sent += await requests(1)
        platform_device.service_data._note_read(
            SERVICE_DATA_INDOOR_COIL_RAW, dt_util.utcnow()
        )

    assert sent == [(SERVICE_DATA_INDOOR_COIL_RAW,)] * 3


async def test_without_an_override_nothing_due_sends_nothing(
    platform_device, monkeypatch, requests
):
    monkeypatch.setattr(
        platform_device, "async_contexts", lambda: {SERVICE_DATA_INDOOR_COIL_RAW}
    )
    _prove(platform_device.service_data, SERVICE_DATA_INDOOR_COIL_RAW)
    platform_device.service_data._note_read(
        SERVICE_DATA_INDOOR_COIL_RAW, dt_util.utcnow()
    )

    platform_device.maybe_request_service_data()

    platform_device.set_airco.assert_not_awaited()


# --- refusal bookkeeping -----------------------------------------------------


def test_a_refusal_with_only_proven_codes_blames_nobody(platform_device):
    channel = platform_device.service_data
    _prove(channel, FAST, MEDIUM)

    channel.note_refused((FAST, MEDIUM))

    assert channel._states == {FAST: "ok", MEDIUM: "ok"}


def test_one_unproven_code_among_proven_ones_is_the_suspect(platform_device):
    channel = platform_device.service_data
    _prove(channel, FAST)

    channel.note_refused((FAST, MEDIUM))
    assert channel._states[MEDIUM] == "suspect"
    channel.note_refused((FAST, MEDIUM))

    assert channel._states[MEDIUM] == "suspended"
    assert channel._states[FAST] == "ok"
    assert channel.select([FAST, MEDIUM], None) == (FAST,)


def test_an_answer_lifts_a_suspension(platform_device):
    channel = platform_device.service_data
    channel._states[MEDIUM] = "suspended"

    answer = Aircon()
    answer.ServiceDataRaw[MEDIUM] = (0x10, 0x01, 0xFF)
    channel.carry_forward(answer)

    assert channel._states[MEDIUM] == "ok"


def test_one_suspect_rides_at_a_time(platform_device):
    channel = platform_device.service_data
    _prove(channel, FAST)
    channel._states[MEDIUM] = "suspect"
    channel._states[SLOW] = "suspect"

    chosen = channel.select([FAST, MEDIUM, SLOW], None)

    assert FAST in chosen
    assert len({MEDIUM, SLOW} & set(chosen)) == 1


# --- per-code expiry ---------------------------------------------------------


def test_the_expiry_is_three_intervals_of_the_code():
    assert service_data_expiry(FAST) == timedelta(seconds=180)
    assert service_data_expiry(MEDIUM) == timedelta(seconds=360)
    assert service_data_expiry(SLOW) == timedelta(seconds=900)
    assert service_data_expiry(0x20) == service_data_module.SERVICE_DATA_MAX_AGE


async def test_a_slow_code_survives_the_age_that_expires_a_fast_one(platform_device):
    channel = platform_device.service_data
    platform_device._airco.CompressorFrequency = 40.0
    platform_device._airco.DischargeSuperheatRaw = 12
    _read_ago(channel, FAST, 200)
    _read_ago(channel, SLOW, 200)

    new_airco = Aircon()
    channel.carry_forward(new_airco)

    assert new_airco.CompressorFrequency is None
    assert new_airco.DischargeSuperheatRaw == 12


async def test_a_slow_code_expires_after_three_of_its_own_intervals(platform_device):
    channel = platform_device.service_data
    platform_device._airco.DischargeSuperheatRaw = 12
    _read_ago(channel, SLOW, 901)

    new_airco = Aircon()
    channel.carry_forward(new_airco)

    assert new_airco.DischargeSuperheatRaw is None
    assert channel._expired is True


async def test_a_fresh_code_does_not_keep_the_others_alive(platform_device):
    channel = platform_device.service_data
    platform_device._airco.CompressorFrequency = 40.0
    platform_device._airco.HotGasTemp = 50.0
    _read_ago(channel, FAST, 300)
    _read_ago(channel, service_data_module.SERVICE_DATA_HOT_GAS_TEMP, 300)

    new_airco = Aircon()
    new_airco.OperatingCurrent = 1.0  # a different code arrived
    channel.carry_forward(new_airco)

    assert new_airco.CompressorFrequency is None
    assert new_airco.HotGasTemp is None


# --- silent operation -------------------------------------------------------


async def test_an_unconfirmed_silent_switch_is_read_again_at_the_next_poll(
    platform_device, monkeypatch
):
    monkeypatch.setattr(coordinator_module, "SILENT_OPERATION_READBACK_DELAY", 0)
    stat = base64.b64encode(bytes(21 + 18 + 1 + 4 + 2)).decode()
    platform_device._api.send_airco_command = AsyncMock(return_value=stat)

    with pytest.raises(Exception, match="."):
        await platform_device.async_set_silent_operation(True)

    assert SERVICE_DATA_SILENT_OPERATION in platform_device.service_data._overdue
