"""The defrost trace kept for the diagnostics download."""

from pywfrac import Aircon

from custom_components.mitsubishi_wf_rac import defrost_trace as trace_module
from custom_components.mitsubishi_wf_rac.defrost_trace import (
    KEPT_EPISODES,
    MAX_EPISODE_SAMPLES,
    POST_SAMPLES,
    PRE_SAMPLES,
    DefrostTrace,
)


def _airco(coil=150, *, status=0x02, defrost_flag=None, **kwargs) -> Aircon:
    airco = Aircon(
        Operation=True,
        OperationMode=2,
        IndoorCoilRaw=coil,
        OperationStatusRaw=status,
        **kwargs,
    )
    if defrost_flag is not None:
        airco.ServiceDataRaw[0x0C] = (defrost_flag, 0xFF, 0xFF)
    return airco


def _feed(trace: DefrostTrace, coils, source="poll") -> None:
    for coil in coils:
        trace.record(_airco(coil), f"frame-{coil}", source)


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
        DefrostingRaw=0x10,
        HotStartRaw=0,
    )

    trace.record(airco, "QUJD", "service_data")
    trace.record(_airco(150), "x", "poll")
    trace.record(_airco(15), "y", "poll")

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
        "DefrostingRaw": 0x10,
        "HotStartRaw": 0,
        "OperationStatusRaw": 0x01,
        "defrost_detected": True,
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
        trace.record(_airco(15 + number), f"d{number}", "poll")
        _feed(trace, [150] * POST_SAMPLES)
    trace.record(_airco(14), "open", "poll")

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
    trace.record(_airco(150, defrost_flag=0x10), "a", "poll")
    trace.record(_airco(150, defrost_flag=0x10), "b", "poll")
    trace.record(_airco(15, status=0x01, defrost_flag=0x11), "c", "poll")
    trace.record(_airco(15, status=0x02), "d", "poll")
    trace.record(_airco(None, status=0x03), "e", "poll")
    # Not heating: bit set but nothing detected, so it is not counted.
    trace.record(
        Aircon(Operation=True, OperationMode=1, IndoorCoilRaw=15, OperationStatusRaw=1),
        "f",
        "poll",
    )

    data = trace.as_dict()

    assert data["defrost_flag_answers"] == {"0x10": 2, "0x11": 1}
    assert data["operation_status_values"] == {"0x02": 3, "0x01": 2, "0x03": 1}
    assert data["samples_with_indoor_coil"] == 5
    assert data["samples_without_indoor_coil"] == 1
    assert data["operation_status_bit_0x01_while_detected"] == 1
    assert data["episodes_detected"] == 1


def test_constants_are_what_the_report_format_promises():
    assert (PRE_SAMPLES, POST_SAMPLES, MAX_EPISODE_SAMPLES, KEPT_EPISODES) == (
        6,
        6,
        120,
        5,
    )
    assert trace_module.OPERATION_STATUS_DEFROST_BIT == 0x01
