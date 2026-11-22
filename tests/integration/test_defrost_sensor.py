"""The defrost binary sensor: judged from the indoor coil, 0x0C only for comparison."""

from datetime import timedelta

import pytest
from pywfrac import (
    SERVICE_DATA_COMPRESSOR_FREQ,
    SERVICE_DATA_DEFROST,
    SERVICE_DATA_HOT_START,
    SERVICE_DATA_INDOOR_COIL_RAW,
    Aircon,
    defrost_detected,
)

from custom_components.mitsubishi_wf_rac import (
    binary_sensor,
    service_data as service_data_module,
)
from custom_components.mitsubishi_wf_rac.const import DOMAIN
from custom_components.mitsubishi_wf_rac.service_data import (
    RESEARCH_SERVICE_DATA_CODES,
    SERVICE_DATA_INTERVAL_FAST,
    SERVICE_DATA_INTERVAL_MEDIUM,
    SERVICE_DATA_INTERVALS,
    SERVICE_DATA_ONLY_WHEN,
    SERVICE_DATA_PIGGYBACK_CODES,
)
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.util import dt as dt_util


def _airco(
    *, coil=None, on=True, mode=2, freq=30.0, outdoor=2.0, defrosting=None
) -> Aircon:
    airco = Aircon()
    airco.Operation = on
    airco.OperationMode = mode
    airco.IndoorCoilRaw = coil
    airco.CompressorFrequency = freq
    airco.OutdoorTemp = outdoor
    airco.Defrosting = defrosting
    return airco


def _sensor(device) -> binary_sensor.DefrostBinarySensor:
    return binary_sensor.DefrostBinarySensor(device)


# --- registration --------------------------------------------------------------


def test_defrost_flag_is_scheduled_every_two_minutes_and_decoded_not_researched():
    assert SERVICE_DATA_INTERVALS[SERVICE_DATA_DEFROST] == SERVICE_DATA_INTERVAL_MEDIUM
    assert SERVICE_DATA_INTERVALS[SERVICE_DATA_INDOOR_COIL_RAW] == (
        SERVICE_DATA_INTERVAL_FAST
    )
    assert SERVICE_DATA_DEFROST not in RESEARCH_SERVICE_DATA_CODES
    assert "Defrosting" in service_data_module.SERVICE_DATA_FIELDS
    # The climate entity never causes a request: it only reads the coil.
    assert SERVICE_DATA_DEFROST not in SERVICE_DATA_PIGGYBACK_CODES
    assert SERVICE_DATA_INDOOR_COIL_RAW in SERVICE_DATA_PIGGYBACK_CODES


async def test_entity_is_disabled_by_default_with_class_and_category(platform_device):
    entity = _sensor(platform_device)

    assert entity.unique_id == f"{DOMAIN}-airco-id-defrost"
    assert entity.translation_key == "defrost"
    assert entity.device_class == BinarySensorDeviceClass.RUNNING
    assert entity.entity_category == EntityCategory.DIAGNOSTIC
    assert entity.entity_registry_enabled_default is False
    # Nothing is requested until the entity is added, i.e. enabled.
    assert SERVICE_DATA_DEFROST not in platform_device.async_contexts()


async def test_subscribing_defrost_requests_the_coil_and_the_comparison_value(
    platform_device,
):
    assert platform_device._subscribed_service_data_codes() == ()

    remove = platform_device.async_add_listener(lambda: None, SERVICE_DATA_DEFROST)
    try:
        assert platform_device._subscribed_service_data_codes() == tuple(
            sorted(
                (
                    SERVICE_DATA_DEFROST,
                    SERVICE_DATA_COMPRESSOR_FREQ,
                    SERVICE_DATA_INDOOR_COIL_RAW,
                    SERVICE_DATA_HOT_START,
                )
            )
        )
    finally:
        remove()


async def test_the_coil_is_asked_every_cycle_while_the_sensor_is_enabled(
    platform_device,
):
    channel = platform_device.service_data
    channel._refusals.note_answered(SERVICE_DATA_INDOOR_COIL_RAW)
    codes = [SERVICE_DATA_INDOOR_COIL_RAW]

    # No condition: off, cooling and heating are all read, so a defrost is
    # never missed for lack of a reading.
    for airco in (_airco(on=False), _airco(mode=1), _airco(coil=150), None):
        assert channel.select(codes, airco) == (SERVICE_DATA_INDOOR_COIL_RAW,)


async def test_defrost_flag_is_asked_while_possible_or_detected(platform_device):
    channel = platform_device.service_data
    channel._refusals.note_answered(SERVICE_DATA_DEFROST)
    codes = [SERVICE_DATA_DEFROST]
    asked = (SERVICE_DATA_DEFROST,)

    assert SERVICE_DATA_ONLY_WHEN[SERVICE_DATA_DEFROST] is not None
    # Possible: compressor running, cold outdoor air.
    assert channel.select(codes, _airco(coil=150)) == asked
    # Ruled out: compressor stopped, warm air, no compressor reading.
    assert channel.select(codes, _airco(coil=150, freq=0.0)) == ()
    assert channel.select(codes, _airco(coil=150, outdoor=12.0)) == ()
    assert channel.select(codes, _airco(coil=150, freq=None)) == ()
    assert channel.select(codes, None) == ()
    # Detected defrost: asked although the bound rules it out (0 Hz while
    # the valve reverses, or warm outdoor air).
    assert channel.select(codes, _airco(coil=15, freq=0.0)) == asked
    assert channel.select(codes, _airco(coil=15, outdoor=12.0)) == asked
    # A cold coil without heating is no defrost.
    assert channel.select(codes, _airco(coil=15, mode=1, freq=0.0)) == ()


# --- the state -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("airco", "expected"),
    [
        pytest.param(_airco(coil=15), True, id="heating-collapsed-coil"),
        pytest.param(_airco(coil=189), False, id="heating-normal-coil"),
        pytest.param(_airco(coil=15, mode=1), False, id="cooling"),
        pytest.param(_airco(coil=15, on=False), None, id="unit-off"),
        pytest.param(_airco(coil=None), None, id="no-coil-reading"),
        pytest.param(_airco(coil=15, freq=0.0), True, id="compressor-not-consulted"),
    ],
)
async def test_state_follows_the_indoor_coil(platform_device, airco, expected):
    platform_device._airco = airco
    entity = _sensor(platform_device)

    assert entity.is_on is expected
    assert entity.is_on is defrost_detected(airco)


async def test_the_outdoor_flag_does_not_decide(platform_device):
    platform_device._airco = _airco(coil=189, defrosting=True)

    assert _sensor(platform_device).is_on is False


async def test_unknown_again_after_the_coil_reading_expires(platform_device):
    channel = platform_device.service_data
    old = dt_util.utcnow() - timedelta(minutes=10)
    channel._note_read(SERVICE_DATA_INDOOR_COIL_RAW, old)
    platform_device._airco = _airco(coil=15)
    new = _airco(coil=None)

    channel.carry_forward(new)

    assert new.IndoorCoilRaw is None
    platform_device._airco = new
    assert _sensor(platform_device).is_on is None
