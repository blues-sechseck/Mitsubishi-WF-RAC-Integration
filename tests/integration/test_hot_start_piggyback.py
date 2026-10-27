"""Hot start and defrost ride on requests that go out anyway, never cause one."""

from pywfrac import (
    SERVICE_DATA_COMPRESSOR_FREQ,
    SERVICE_DATA_DEFROST,
    SERVICE_DATA_HOT_START,
    SERVICE_DATA_INDOOR_COIL_RAW,
    Aircon,
    hot_start_possible,
)

from custom_components.mitsubishi_wf_rac import service_data as service_data_module
from custom_components.mitsubishi_wf_rac.service_data import (
    RESEARCH_CONTEXT,
    RESEARCH_PINNED_CODES,
    SERVICE_DATA_INTERVAL_FAST,
    SERVICE_DATA_INTERVALS,
    SERVICE_DATA_ONLY_WHEN,
    SERVICE_DATA_PIGGYBACK_CODES,
)

from .test_silent_and_research import _cycles, due_every_time  # noqa: F401


def test_hot_start_is_scheduled_every_minute_and_only_while_heating():
    assert SERVICE_DATA_INTERVALS[SERVICE_DATA_HOT_START] == SERVICE_DATA_INTERVAL_FAST
    assert SERVICE_DATA_ONLY_WHEN[SERVICE_DATA_HOT_START] is hot_start_possible
    assert "HotStart" in service_data_module.SERVICE_DATA_FIELDS
    assert service_data_module.SERVICE_DATA_DERIVED_FROM["HotStart"] == "HotStartRaw"
    assert set(SERVICE_DATA_PIGGYBACK_CODES) == {
        SERVICE_DATA_HOT_START,
        SERVICE_DATA_DEFROST,
    }


async def test_nothing_is_subscribed_without_an_enabled_sensor(platform_device):
    assert platform_device._subscribed_service_data_codes() == ()


async def test_piggyback_codes_join_once_another_code_is_subscribed(platform_device):
    remove = platform_device.async_add_listener(
        lambda: None, SERVICE_DATA_INDOOR_COIL_RAW
    )
    try:
        assert platform_device._subscribed_service_data_codes() == tuple(
            sorted(
                (
                    SERVICE_DATA_INDOOR_COIL_RAW,
                    SERVICE_DATA_HOT_START,
                    SERVICE_DATA_DEFROST,
                    SERVICE_DATA_COMPRESSOR_FREQ,
                )
            )
        )
    finally:
        remove()


async def test_research_alone_pulls_in_no_piggyback_codes(platform_device):
    remove = platform_device.async_add_listener(lambda: None, RESEARCH_CONTEXT)
    try:
        assert platform_device._subscribed_service_data_codes() == ()
    finally:
        remove()


async def test_a_code_in_both_selections_is_requested_once(
    platform_device,
    monkeypatch,
    due_every_time,  # noqa: F811
):
    pinned = RESEARCH_PINNED_CODES[0]
    assert pinned == SERVICE_DATA_HOT_START
    monkeypatch.setattr(
        platform_device,
        "async_contexts",
        lambda: {SERVICE_DATA_INDOOR_COIL_RAW, RESEARCH_CONTEXT},
    )
    platform_device._airco = Aircon(Operation=True, OperationMode=2)
    platform_device.service_data._refusals.note_answered(SERVICE_DATA_HOT_START)
    platform_device.service_data._refusals.note_answered(SERVICE_DATA_INDOOR_COIL_RAW)
    platform_device.research_data.states[pinned] = "ok"

    (sent,) = await _cycles(platform_device, 1)

    assert sent.count(pinned) == 1
    assert len(sent) == len(set(sent))
