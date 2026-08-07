"""Unit tests for sensor.EnergyTotalSensor's accumulator.

The unit's own kWh counter is per run: it counts up while the indoor unit
runs, holds its value while the unit is off, and is cleared to 0 at the next
power-on. EnergyTotalSensor turns that into a lifetime figure by summing only
the upward steps. The sequences below are taken from a real two-day recorder
history of two units, resets included.

Only _update_state()/async_set_total() are exercised, on an instance built
without HA's entity machinery - both touch nothing but the four attributes
set up here.
"""

import pytest

from custom_components.mitsubishi_wf_rac.sensor import (
    EnergyTotalExtraStoredData,
    EnergyTotalSensor,
)


class _FakeAirco:
    Electric: float | None = None


class _FakeDevice:
    def __init__(self) -> None:
        self.airco = _FakeAirco()


@pytest.fixture
def sensor() -> EnergyTotalSensor:
    """An EnergyTotalSensor with no reading seen yet."""
    entity = object.__new__(EnergyTotalSensor)
    entity._device = _FakeDevice()
    entity._total = 0.0
    entity._last_raw = None
    entity._attr_native_value = 0.0
    entity.async_write_ha_state = lambda: None
    return entity


def _feed(entity: EnergyTotalSensor, values) -> float:
    for value in values:
        entity._device.airco.Electric = value
        entity._update_state()
    return entity._attr_native_value


def test_sums_upward_steps(sensor: EnergyTotalSensor) -> None:
    assert _feed(sensor, [0.0, 0.25, 0.5, 0.75]) == 0.75


def test_reset_to_zero_is_not_counted_as_consumption(sensor: EnergyTotalSensor) -> None:
    """A drop means a new run started; what came before is already counted."""
    assert _feed(sensor, [0.0, 0.25, 0.5, 0.0, 0.25]) == 0.75


def test_value_held_while_unit_is_off(sensor: EnergyTotalSensor) -> None:
    """Repeated identical readings during an off period add nothing."""
    assert _feed(sensor, [0.0, 0.25, 0.25, 0.25, 0.25]) == 0.25


def test_none_readings_do_not_break_the_delta(sensor: EnergyTotalSensor) -> None:
    """Electric is None whenever a poll carries no energy segment - that must
    not be read as a reset, nor make the next reading count from zero."""
    assert _feed(sensor, [0.0, 0.25, None, None, 0.5]) == 0.5


def test_first_reading_only_anchors(sensor: EnergyTotalSensor) -> None:
    """A brand-new sensor must not claim what the running cycle already had."""
    assert _feed(sensor, [8.0, 8.25]) == 0.25


def test_real_history_wohnzimmer(sensor: EnergyTotalSensor) -> None:
    """Recorded 2026-08-05..07, three power-ons, with the poll gaps that
    reported no value."""
    history = (
        [0.0]
        + [round(0.25 * step, 2) for step in range(1, 34)]  # 0.25 .. 8.25
        + [None, 8.25, None, 8.25]
        + [0.0, 0.25, 0.5, 0.75]  # power-on #1
        + [0.0, 0.25, 0.5]  # power-on #2
        + [0.0]  # power-on #3
    )
    assert _feed(sensor, history) == 8.25 + 0.75 + 0.5


async def test_set_total_reanchors(sensor: EnergyTotalSensor) -> None:
    """After a reset the next poll must not re-add the run so far."""
    _feed(sensor, [0.0, 0.25, 0.5])
    await sensor.async_set_total(0.0)
    assert sensor._attr_native_value == 0.0
    assert _feed(sensor, [0.5, 0.75]) == 0.25


async def test_set_total_carries_over_a_previous_meter(
    sensor: EnergyTotalSensor,
) -> None:
    _feed(sensor, [0.0, 0.25])
    await sensor.async_set_total(412.5)
    assert _feed(sensor, [0.25, 0.5]) == 412.75


def test_stored_data_round_trip() -> None:
    stored = EnergyTotalExtraStoredData(12.5, "kWh", 0.75)
    assert EnergyTotalExtraStoredData.from_dict(stored.as_dict()) == stored


def test_stored_data_without_last_raw() -> None:
    """Restoring a state written before last_raw existed must not crash."""
    restored = EnergyTotalExtraStoredData.from_dict(
        {"native_value": 3.0, "native_unit_of_measurement": "kWh"}
    )
    assert restored is not None
    assert restored.last_raw is None
