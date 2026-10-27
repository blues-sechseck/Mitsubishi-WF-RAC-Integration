"""The defrost binary sensor: when 0x0C is asked, and what the sensor says."""

from datetime import timedelta

import pytest
from pywfrac import (
    SERVICE_DATA_COMPRESSOR_FREQ,
    SERVICE_DATA_DEFROST,
    SERVICE_DATA_HOT_START,
    Aircon,
    defrost_possible,
)

from custom_components.mitsubishi_wf_rac import (
    binary_sensor,
    service_data as service_data_module,
)
from custom_components.mitsubishi_wf_rac.const import DOMAIN
from custom_components.mitsubishi_wf_rac.service_data import (
    RESEARCH_SERVICE_DATA_CODES,
    SERVICE_DATA_INTERVAL_MEDIUM,
    SERVICE_DATA_INTERVALS,
    SERVICE_DATA_ONLY_WHEN,
)
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.util import dt as dt_util


def _airco(freq, outdoor, defrosting=None) -> Aircon:
    airco = Aircon()
    airco.CompressorFrequency = freq
    airco.OutdoorTemp = outdoor
    airco.Defrosting = defrosting
    return airco


def _sensor(device) -> binary_sensor.DefrostBinarySensor:
    return binary_sensor.DefrostBinarySensor(device)


# --- registration --------------------------------------------------------------


def test_defrost_is_scheduled_every_two_minutes_and_decoded_not_researched():
    assert SERVICE_DATA_INTERVALS[SERVICE_DATA_DEFROST] == SERVICE_DATA_INTERVAL_MEDIUM
    assert SERVICE_DATA_ONLY_WHEN[SERVICE_DATA_DEFROST] is defrost_possible
    assert SERVICE_DATA_DEFROST not in RESEARCH_SERVICE_DATA_CODES
    assert "Defrosting" in service_data_module.SERVICE_DATA_FIELDS


async def test_entity_is_disabled_by_default_with_class_and_category(platform_device):
    entity = _sensor(platform_device)

    assert entity.unique_id == f"{DOMAIN}-airco-id-defrost"
    assert entity.translation_key == "defrost"
    assert entity.device_class == BinarySensorDeviceClass.RUNNING
    assert entity.entity_category == EntityCategory.DIAGNOSTIC
    assert entity.entity_registry_enabled_default is False
    # Nothing is requested until the entity is added, i.e. enabled.
    assert SERVICE_DATA_DEFROST not in platform_device.async_contexts()


async def test_subscribing_defrost_also_requests_the_compressor_reading(
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
                    SERVICE_DATA_HOT_START,
                )
            )
        )
    finally:
        remove()


async def test_defrost_is_asked_only_while_it_is_possible(platform_device):
    channel = platform_device.service_data
    channel._refusals.note_answered(SERVICE_DATA_DEFROST)
    codes = [SERVICE_DATA_DEFROST]

    assert channel.select(codes, _airco(30.0, 2.0)) == (SERVICE_DATA_DEFROST,)
    assert channel.select(codes, _airco(0.0, 2.0)) == ()
    assert channel.select(codes, _airco(30.0, 12.0)) == ()
    assert channel.select(codes, _airco(None, 2.0)) == ()
    assert channel.select(codes, None) == ()


# --- the state -----------------------------------------------------------------------


@pytest.mark.parametrize("defrosting", [True, False])
async def test_reports_the_reading_while_a_defrost_is_possible(
    platform_device, defrosting
):
    platform_device._airco = _airco(30.0, 2.0, defrosting)
    entity = _sensor(platform_device)

    assert entity.is_on is defrosting


async def test_unknown_while_possible_and_nothing_arrived(platform_device):
    platform_device._airco = _airco(30.0, 2.0, None)

    assert _sensor(platform_device).is_on is None


@pytest.mark.parametrize(("freq", "outdoor"), [(0.0, 2.0), (30.0, 12.0), (0.0, 20.0)])
async def test_off_when_a_defrost_is_ruled_out(platform_device, freq, outdoor):
    platform_device._airco = _airco(freq, outdoor, None)

    assert _sensor(platform_device).is_on is False


async def test_a_stale_on_reading_turns_off_once_it_is_ruled_out(platform_device):
    platform_device._airco = _airco(30.0, 2.0, True)
    entity = _sensor(platform_device)
    assert entity.is_on is True

    # The compressor stops; the carried "on" is still on the model.
    platform_device._airco.CompressorFrequency = 0.0
    entity._update_state()
    assert entity.is_on is False

    # Warm outdoor air rules it out as well.
    platform_device._airco.CompressorFrequency = 30.0
    platform_device._airco.OutdoorTemp = 12.0
    entity._update_state()
    assert entity.is_on is False


async def test_unknown_when_the_compressor_reading_is_missing(platform_device):
    platform_device._airco = _airco(None, 2.0, True)

    assert _sensor(platform_device).is_on is None


async def test_unknown_again_after_the_reading_expires(platform_device):
    channel = platform_device.service_data
    old = dt_util.utcnow() - timedelta(minutes=10)
    channel._note_read(SERVICE_DATA_DEFROST, old)
    channel._note_read(SERVICE_DATA_COMPRESSOR_FREQ, dt_util.utcnow())
    platform_device._airco = _airco(30.0, 2.0, True)
    new = _airco(30.0, 2.0, None)
    new.CompressorFrequency = 30.0

    channel.carry_forward(new)

    assert new.Defrosting is None
    platform_device._airco = new
    assert _sensor(platform_device).is_on is None
