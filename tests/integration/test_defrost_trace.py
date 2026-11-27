"""The defrost trace kept for the diagnostics download."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pywfrac import OPERATION_STATUS_DEFROST_BIT, Aircon

from custom_components.mitsubishi_wf_rac.defrost_trace import (
    KEPT_EPISODES,
    MAX_EPISODE_SAMPLES,
    POST_SAMPLES,
    PRE_SAMPLES,
    DefrostTrace,
    create_store,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store


def _airco(coil=150, *, status=0x02, **kwargs) -> Aircon:
    return Aircon(
        Operation=True,
        OperationMode=2,
        IndoorCoilRaw=coil,
        OperationStatusRaw=status,
        **kwargs,
    )


def _feed(trace: DefrostTrace, coils, source="poll") -> None:
    for coil in coils:
        trace.record(_airco(coil, status=None), f"frame-{coil}", source)


def test_sample_carries_the_raw_frame_and_the_decoded_fields():
    trace = DefrostTrace()
    airco = _airco(
        15,
        status=0x01,
        CompressorFrequency=0.0,
        OutdoorTemp=3.0,
        OperatingCurrent=0.4,
        IndoorCoilOutletRaw=40,
        OutdoorCoilRaw=60,
        EevPulses=115,
        HotStartRaw=0,
        StatusDb5Raw=0x10,
    )

    trace.record(airco, "QUJD", "service_data")
    trace.record(_airco(150), "x", "poll")
    trace.record(_airco(15, status=None), "y", "poll")

    (episode,) = trace.as_dict()["episodes"]
    first = episode[0]
    assert first["airconStat"] == "QUJD"
    assert first["source"] == "service_data"
    assert first["timestamp"].endswith("+00:00")
    assert {
        k: v for k, v in first.items() if k not in ("airconStat", "source", "timestamp")
    } == {
        "Operation": True,
        "OperationMode": 2,
        "CoolHotJudge": False,
        "OutdoorTemp": 3.0,
        "CompressorFrequency": 0.0,
        "OperatingCurrent": 0.4,
        "IndoorCoilRaw": 15,
        "IndoorCoilOutletRaw": 40,
        "OutdoorCoilRaw": 60,
        "EevPulses": 115,
        "HotStartRaw": 0,
        "OperationStatusRaw": 0x01,
        "StatusDb5Raw": 0x10,
        "defrost_detected": True,
        "defrost_status_bit": True,
        "defrost_from_indoor_coil": True,
    }


def test_no_episode_without_a_detected_defrost():
    trace = DefrostTrace()
    _feed(trace, [150] * 20)

    data = trace.as_dict()

    assert data["episodes"] == []
    assert data["episodes_detected"] == 0
    assert data["open_episode"] is False


def test_episode_holds_the_pre_buffer_the_defrost_and_the_post_samples():
    trace = DefrostTrace()
    _feed(trace, [150] * 10)
    _feed(trace, [15, 12, 20])

    data = trace.as_dict()
    assert data["open_episode"] is True
    (episode,) = data["episodes"]
    assert [s["IndoorCoilRaw"] for s in episode] == [150] * PRE_SAMPLES + [15, 12, 20]

    # The first POST_SAMPLES - 1 normal samples keep it open...
    _feed(trace, [150] * (POST_SAMPLES - 1))
    assert trace.as_dict()["open_episode"] is True
    # ...the last one closes it, and later samples are not added.
    _feed(trace, [150] * 3)
    data = trace.as_dict()
    assert data["open_episode"] is False
    (episode,) = data["episodes"]
    assert [s["IndoorCoilRaw"] for s in episode] == (
        [150] * PRE_SAMPLES + [15, 12, 20] + [150] * POST_SAMPLES
    )
    assert data["episodes_detected"] == 1


def test_unknown_after_a_defrost_counts_towards_the_post_samples():
    trace = DefrostTrace()
    _feed(trace, [15])
    _feed(trace, [None] * POST_SAMPLES)

    data = trace.as_dict()

    assert data["open_episode"] is False
    assert len(data["episodes"][0]) == 1 + POST_SAMPLES


def test_a_defrost_that_resumes_during_the_post_samples_stays_one_episode():
    trace = DefrostTrace()
    _feed(trace, [15, 150, 150, 15, 150])

    data = trace.as_dict()

    assert data["episodes_detected"] == 1
    assert len(data["episodes"]) == 1
    assert data["open_episode"] is True


def test_episode_is_capped():
    trace = DefrostTrace()
    _feed(trace, [15] * (MAX_EPISODE_SAMPLES + 50))

    (episode,) = trace.as_dict()["episodes"]

    assert len(episode) == MAX_EPISODE_SAMPLES


def test_only_the_last_closed_episodes_are_kept_plus_the_open_one():
    trace = DefrostTrace()
    for number in range(KEPT_EPISODES + 3):
        trace.record(_airco(15 + number, status=None), f"d{number}", "poll")
        _feed(trace, [150] * POST_SAMPLES)
    trace.record(_airco(14, status=None), "open", "poll")

    data = trace.as_dict()

    assert data["episodes_detected"] == KEPT_EPISODES + 4
    assert len(data["episodes"]) == KEPT_EPISODES + 1
    assert data["episodes"][-1][-1]["airconStat"] == "open"
    # The oldest closed ones are gone.
    kept = {s["airconStat"] for episode in data["episodes"] for s in episode}
    assert "d0" not in kept
    assert f"d{KEPT_EPISODES + 2}" in kept


def test_counters():
    trace = DefrostTrace()
    trace.record(_airco(150), "a", "poll")
    trace.record(_airco(150), "b", "poll")
    trace.record(_airco(15, status=0x01), "c", "poll")
    trace.record(_airco(15, status=0x02), "d", "poll")
    trace.record(_airco(None, status=0x03), "e", "poll")
    # Not heating: bit set but nothing detected, so it is not counted.
    trace.record(
        Aircon(Operation=True, OperationMode=1, IndoorCoilRaw=15, OperationStatusRaw=1),
        "f",
        "poll",
    )

    data = trace.as_dict()

    assert "defrost_flag_answers" not in data
    assert data["operation_status_values"] == {"0x02": 3, "0x01": 2, "0x03": 1}
    assert data["samples_with_indoor_coil"] == 5
    assert data["samples_without_indoor_coil"] == 1
    assert data["signals_while_heating"] == {
        "bit_and_coil": 1,
        "bit_only": 1,
        "coil_only": 1,
    }
    assert data["episodes_detected"] == 1


def test_samples_record_each_signal_on_its_own():
    trace = DefrostTrace()
    trace.record(_airco(150, status=0x01), "bit", "poll")
    trace.record(_airco(15, status=None), "coil", "poll")

    (episode,) = trace.as_dict()["episodes"]

    assert [
        (s["defrost_status_bit"], s["defrost_from_indoor_coil"], s["defrost_detected"])
        for s in episode
    ] == [(True, False, True), (None, True, True)]


def test_the_status_bit_alone_opens_an_episode():
    trace = DefrostTrace()
    _feed(trace, [150] * 3)
    trace.record(_airco(150, status=0x01), "stopped", "poll")

    data = trace.as_dict()

    assert data["episodes_detected"] == 1
    assert data["signals_while_heating"]["bit_only"] == 1


def _fill(trace: DefrostTrace) -> None:
    _feed(trace, [150] * 3)
    trace.record(_airco(15, status=0x01), "a", "poll")
    trace.record(_airco(150, status=0x01), "b", "poll")
    _feed(trace, [150] * POST_SAMPLES)
    trace.record(_airco(15, status=None), "open", "poll")


async def test_persistence_round_trip(hass: HomeAssistant, hass_storage):
    store = create_store(hass, "entry-1")
    trace = DefrostTrace(store)
    _fill(trace)
    before = trace.as_dict()

    await trace.async_flush()

    assert "mitsubishi_wf_rac.defrost_trace.entry-1" in hass_storage
    restored = DefrostTrace(create_store(hass, "entry-1"))
    await restored.async_load()
    after = restored.as_dict()
    # The episode that was still open comes back closed.
    assert before["open_episode"] is True
    assert after["open_episode"] is False
    assert {k: v for k, v in after.items() if k != "open_episode"} == {
        k: v for k, v in before.items() if k != "open_episode"
    }
    # And it keeps working: a new sample does not touch the restored ones.
    restored.record(_airco(150), "new", "poll")
    assert restored.as_dict()["episodes"] == after["episodes"]


async def test_a_delayed_save_is_scheduled_when_an_episode_opens(
    hass: HomeAssistant, hass_storage, freezer
):
    trace = DefrostTrace(create_store(hass, "entry-2"))
    trace.record(_airco(150), "idle", "poll")
    trace.record(_airco(15, status=None), "defrost", "poll")

    await hass.async_block_till_done()
    assert "mitsubishi_wf_rac.defrost_trace.entry-2" not in hass_storage

    freezer.tick(timedelta(seconds=120))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    saved = hass_storage["mitsubishi_wf_rac.defrost_trace.entry-2"]["data"]
    assert saved["episodes_detected"] == 1
    assert "open_episode" not in saved


@pytest.mark.parametrize(
    "stored",
    [
        None,
        [],
        "garbage",
        {"episodes": "x", "episodes_detected": "many"},
        {"episodes": [None, ["a"], 3], "signals_while_heating": [1]},
        {
            "defrost_flag_answers": {"0x10": "a", 5: 2},
            "operation_status_values": [],
            "samples_with_indoor_coil": -3,
            "signals_while_heating": {"bit_only": True, "coil_only": "2"},
        },
    ],
)
async def test_a_garbled_store_starts_empty(hass: HomeAssistant, hass_storage, stored):
    hass_storage["mitsubishi_wf_rac.defrost_trace.entry-3"] = {
        "version": 1,
        "minor_version": 1,
        "key": "mitsubishi_wf_rac.defrost_trace.entry-3",
        "data": stored,
    }
    trace = DefrostTrace(create_store(hass, "entry-3"))

    await trace.async_load()

    data = trace.as_dict()
    assert data["episodes"] == []
    assert data["episodes_detected"] == 0
    assert "defrost_flag_answers" not in data
    assert data["signals_while_heating"] == {
        "bit_and_coil": 0,
        "bit_only": 0,
        "coil_only": 0,
    }


async def test_a_store_with_the_retired_db5_counter_still_restores(
    hass: HomeAssistant, hass_storage
):
    hass_storage["mitsubishi_wf_rac.defrost_trace.entry-4"] = {
        "version": 1,
        "minor_version": 1,
        "key": "mitsubishi_wf_rac.defrost_trace.entry-4",
        "data": {
            "episodes_detected": 2,
            "operation_status_values": {"0x02": 4},
            "status_db5_by_state": {"0x10 on 0x02": 3},
        },
    }
    trace = DefrostTrace(create_store(hass, "entry-4"))

    await trace.async_load()

    data = trace.as_dict()
    assert data["episodes_detected"] == 2
    assert "status_db5_by_state" not in data


async def test_a_store_that_cannot_be_read_starts_empty(hass: HomeAssistant):
    class _Broken(Store[dict]):
        async def async_load(self):
            raise ValueError("boom")

    trace = DefrostTrace(_Broken(hass, 1, "mitsubishi_wf_rac.defrost_trace.x"))

    await trace.async_load()

    assert trace.as_dict()["episodes"] == []


async def test_without_a_store_load_and_flush_do_nothing():
    trace = DefrostTrace()
    await trace.async_load()
    await trace.async_flush()
    assert trace.as_dict()["episodes_detected"] == 0


def test_constants_are_what_the_report_format_promises():
    assert (PRE_SAMPLES, POST_SAMPLES, MAX_EPISODE_SAMPLES, KEPT_EPISODES) == (
        6,
        6,
        120,
        5,
    )
    assert OPERATION_STATUS_DEFROST_BIT == 0x01


# --- the defrost statistics ------------------------------------------------


def _defrost(trace: DefrostTrace, freezer, minutes: int) -> None:
    """One defrost of the given length, polled once a minute, then a frame without."""
    trace.record(_airco(150, status=0x01), "on", "poll")
    for _ in range(minutes):
        freezer.tick(timedelta(minutes=1))
        trace.record(_airco(150, status=0x01), "on", "poll")
    freezer.tick(timedelta(minutes=1))
    trace.record(_airco(150, status=0x02), "off", "poll")


def test_statistics_start_empty_and_wait_for_the_first_completed_defrost(freezer):
    trace = DefrostTrace()
    trace.record(_airco(150, status=0x02), "idle", "poll")
    assert trace.defrost_count == 0
    assert trace.last_defrost_duration is None
    assert trace.last_defrost_end is None

    # Running is not completed.
    trace.record(_airco(150, status=0x01), "on", "poll")
    assert trace.defrost_count == 0
    assert trace.last_defrost_end is None


def test_a_completed_defrost_is_counted_with_duration_and_end(freezer):
    freezer.move_to("2026-12-01 06:00:00+00:00")
    trace = DefrostTrace()
    _defrost(trace, freezer, minutes=6)

    assert trace.defrost_count == 1
    # Started 06:00, first frame without it 06:07.
    assert trace.last_defrost_duration == 7.0
    assert trace.last_defrost_end is not None
    assert trace.last_defrost_end.isoformat() == "2026-12-01T06:07:00+00:00"


def test_two_defrosts_count_twice_and_keep_the_last_one(freezer):
    trace = DefrostTrace()
    _defrost(trace, freezer, minutes=3)
    first_end = trace.last_defrost_end
    freezer.tick(timedelta(hours=1))
    _defrost(trace, freezer, minutes=8)

    assert trace.defrost_count == 2
    assert trace.last_defrost_duration == 9.0
    assert trace.last_defrost_end > first_end


def test_frames_the_unit_could_not_judge_do_not_end_a_defrost(freezer):
    trace = DefrostTrace()
    trace.record(_airco(150, status=0x01), "on", "poll")
    freezer.tick(timedelta(minutes=2))
    trace.record(Aircon(Operation=False), "unknown", "poll")
    assert trace.defrost_count == 0

    freezer.tick(timedelta(minutes=2))
    trace.record(_airco(150, status=0x02), "off", "poll")
    assert trace.defrost_count == 1
    assert trace.last_defrost_duration == 4.0


async def test_statistics_survive_a_reload(hass: HomeAssistant, hass_storage, freezer):
    trace = DefrostTrace(create_store(hass, "entry-5"))
    _defrost(trace, freezer, minutes=4)
    _defrost(trace, freezer, minutes=2)
    await trace.async_flush()

    restored = DefrostTrace(create_store(hass, "entry-5"))
    await restored.async_load()

    assert restored.defrost_count == 2
    assert restored.last_defrost_duration == 3.0
    assert restored.last_defrost_end == trace.last_defrost_end


async def test_a_defrost_open_at_shutdown_is_not_counted_twice(
    hass: HomeAssistant, hass_storage, freezer
):
    trace = DefrostTrace(create_store(hass, "entry-6"))
    _defrost(trace, freezer, minutes=2)
    trace.record(_airco(150, status=0x01), "on", "poll")
    freezer.tick(timedelta(minutes=1))
    trace.record(_airco(150, status=0x01), "on", "poll")
    await trace.async_flush()

    restored = DefrostTrace(create_store(hass, "entry-6"))
    await restored.async_load()
    # Not counted while it was running, and not remembered as running either.
    assert restored.defrost_count == 1

    # Still going after the restart: it starts afresh and counts once, at its end.
    restored.record(_airco(150, status=0x01), "on", "poll")
    freezer.tick(timedelta(minutes=1))
    restored.record(_airco(150, status=0x01), "on", "poll")
    assert restored.defrost_count == 1
    freezer.tick(timedelta(minutes=1))
    restored.record(_airco(150, status=0x02), "off", "poll")
    assert restored.defrost_count == 2
    assert restored.last_defrost_duration == 2.0


async def test_a_store_from_before_the_statistics_restores_at_zero(
    hass: HomeAssistant, hass_storage
):
    hass_storage["mitsubishi_wf_rac.defrost_trace.entry-7"] = {
        "version": 1,
        "minor_version": 1,
        "key": "mitsubishi_wf_rac.defrost_trace.entry-7",
        "data": {"episodes_detected": 4},
    }
    trace = DefrostTrace(create_store(hass, "entry-7"))

    await trace.async_load()

    assert trace.defrost_count == 0
    assert trace.last_defrost_duration is None
    assert trace.last_defrost_end is None


@pytest.mark.parametrize(
    "stats",
    [
        "x",
        {"count": -1, "last_duration_minutes": "long", "last_end": "yesterday"},
        {"count": True, "last_duration_minutes": -2, "last_end": "2026-12-01T06:00:00"},
    ],
)
async def test_garbled_statistics_start_at_zero(
    hass: HomeAssistant, hass_storage, stats
):
    hass_storage["mitsubishi_wf_rac.defrost_trace.entry-8"] = {
        "version": 1,
        "minor_version": 1,
        "key": "mitsubishi_wf_rac.defrost_trace.entry-8",
        "data": {"defrost_stats": stats},
    }
    trace = DefrostTrace(create_store(hass, "entry-8"))

    await trace.async_load()

    assert trace.defrost_count == 0
    assert trace.last_defrost_duration is None
    assert trace.last_defrost_end is None
