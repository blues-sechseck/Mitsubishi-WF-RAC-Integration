"""for sensor integration."""
# pylint: disable = too-few-public-methods

from __future__ import annotations
from dataclasses import dataclass
import logging
from typing import Any, Self

import voluptuous as vol

from . import MitsubishiWfRacConfigEntry
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorEntity,
    SensorExtraStoredData,
)
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfTemperature,
    EntityCategory,
    CONF_HOST,
    CONF_ERROR,
)
from homeassistant.helpers import entity_registry as er

from .entity import WfRacEntity
from .wfrac.device import Device
from .const import (
    ATTR_TARGET_TEMPERATURE,
    ATTR_COMPRESSOR_FREQUENCY,
    ATTR_OPERATING_CURRENT,
    ATTR_HOT_GAS_TEMP,
    ATTR_EEV_PULSES,
    ATTR_EEV_POSITION,
    DOMAIN,
    ATTR_INSIDE_TEMPERATURE,
    ATTR_OUTSIDE_TEMPERATURE,
    CONF_OPERATOR_ID,
    CONF_AIRCO_ID,
    ATTR_DEVICE_ID,
    ATTR_CONNECTED_ACCOUNTS,
    ATTR_UPDATED_BY,
    ATTR_ACCOUNT_EXPIRES,
    ATTR_LED_STATUS,
    ATTR_AUTO_HEATING,
    ATTR_MODEL_NR,
    ATTR_COOL_HOT_JUDGE,
    CONF_INDOOR_OFFSET,
    CONF_OUTDOOR_OFFSET,
    CONF_TARGET_OFFSET,
    CONF_SERVICE_DATA,
    SERVICE_SET_ENERGY_TOTAL,
    SIGNAL_SET_ENERGY_TOTAL,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry: MitsubishiWfRacConfigEntry, async_add_entities):
    """Setup sensor entries"""

    device: Device = entry.runtime_data.device

    _LOGGER.info("Setup: %s, %s", device.device_name, device.airco_id)
    entities = [
        TemperatureSensor(device, "Indoor", ATTR_INSIDE_TEMPERATURE),
        TemperatureSensor(device, "Outdoor", ATTR_OUTSIDE_TEMPERATURE),
        TemperatureSensor(device, "Target", ATTR_TARGET_TEMPERATURE, False),
        # The `enable` flag decides entity_registry_enabled_default: on for
        # readings that say something about the unit, off for the internal
        # plumbing (ids, session, addressing) that only matters when
        # diagnosing a connection problem. All of these come out of the
        # regular poll, so enabling one costs no extra request.
        DiagnosticsSensor(device, "Airco ID", CONF_AIRCO_ID, True),
        DiagnosticsSensor(device, "Operator ID", CONF_OPERATOR_ID),
        DiagnosticsSensor(device, "Device ID", ATTR_DEVICE_ID),
        DiagnosticsSensor(device, "IP", CONF_HOST),
        DiagnosticsSensor(device, "Accounts", ATTR_CONNECTED_ACCOUNTS),
        DiagnosticsSensor(device, "Error", CONF_ERROR, True),
        DiagnosticsSensor(device, "Updated By", ATTR_UPDATED_BY, True),
        DiagnosticsSensor(device, "Account Expires", ATTR_ACCOUNT_EXPIRES, True),
        # Off until it is understood: reads a constant 1 on both test units
        # regardless of what the machine is doing, so it does not currently
        # track the unit's LED - see todo.md.
        DiagnosticsSensor(device, "LED Status", ATTR_LED_STATUS),
        DiagnosticsSensor(device, "Auto Heating", ATTR_AUTO_HEATING, True),
        DiagnosticsSensor(device, "Model Nr", ATTR_MODEL_NR),
        DiagnosticsSensor(device, "Cool Hot Judge", ATTR_COOL_HOT_JUDGE),
    ]
    if device.airco.Electric is not None:
        entities.append(EnergySensor(device))
        entities.append(EnergyTotalSensor(device))

    # Tied to CONF_SERVICE_DATA rather than merely disabled by default: the
    # option is what makes Device request these values at all, so without it
    # the entities could only ever read `unknown`. Changing the option reloads
    # the entry (see async_update_options), so they appear and disappear with
    # it.
    if entry.options.get(CONF_SERVICE_DATA, False):
        entities += [
            ServiceDataSensor(device, ATTR_COMPRESSOR_FREQUENCY),
            ServiceDataSensor(device, ATTR_OPERATING_CURRENT),
            ServiceDataSensor(device, ATTR_HOT_GAS_TEMP),
            ServiceDataSensor(device, ATTR_EEV_PULSES),
            ServiceDataSensor(device, ATTR_EEV_POSITION),
        ]
    else:
        _async_remove_service_data_sensors(hass, device)

    _async_remove_home_leave_mode_sensors(hass, device)

    async_add_entities(entities)

    entity_platform.async_get_current_platform().async_register_entity_service(
        SERVICE_SET_ENERGY_TOTAL,
        {vol.Required("value"): vol.All(vol.Coerce(float), vol.Range(min=0))},
        _async_set_energy_total,
    )


async def _async_set_energy_total(entity: SensorEntity, call) -> None:
    """Entity-service handler for SERVICE_SET_ENERGY_TOTAL.

    Registered as a callable rather than a method name so targeting any other
    sensor of this integration fails with a readable message instead of an
    AttributeError - entity services apply to every entity on the platform.
    """
    if not isinstance(entity, EnergyTotalSensor):
        raise ServiceValidationError(
            f"{entity.entity_id} is not an Energy Usage Total sensor"
        )
    await entity.async_set_total(call.data["value"])


def _async_remove_service_data_sensors(hass, device: Device) -> None:
    """Drop the operation-data sensors once CONF_SERVICE_DATA is turned off.

    Without this they would linger in the registry reading `unknown` forever,
    since nothing requests their values any more.
    """
    registry = er.async_get(hass)
    for custom_type in (
        ATTR_COMPRESSOR_FREQUENCY,
        ATTR_OPERATING_CURRENT,
        ATTR_HOT_GAS_TEMP,
        ATTR_EEV_PULSES,
        ATTR_EEV_POSITION,
    ):
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}-{device.airco_id}-{custom_type}-sensor"
        )
        if entity_id:
            _LOGGER.debug("Removing service data sensor %s", entity_id)
            registry.async_remove(entity_id)


def _async_remove_home_leave_mode_sensors(hass, device: Device) -> None:
    """Drop the former Home Leave Mode diagnostic sensors from the entity
    registry.

    Replaced by writable entities on the Controls section of the device page:
    HomeLeaveModeNumber (TempRule/TempSetting) in number.py, the AirFlow
    selects in select.py - editable directly instead of only via the
    set_home_leave_mode action, see todo.md.
    """
    registry = er.async_get(hass)
    for mode in ("cooling", "heating"):
        for slug in ("temp_rule", "temp_setting", "air_flow"):
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{DOMAIN}-{device.airco_id}-home-leave-{mode}-{slug}-sensor"
            )
            if entity_id:
                _LOGGER.debug("Removing obsolete home leave mode sensor %s", entity_id)
                registry.async_remove(entity_id)


class DiagnosticsSensor(WfRacEntity, SensorEntity):
    # pylint: disable = too-many-instance-attributes
    """Representation of a Sensor."""

    _attr_entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name: bool = True

    def __init__(
        self, device: Device, name: str, custom_type: str, enable=False
    ) -> None:
        """Initialize the sensor."""
        super().__init__(device)
        self._attr_entity_registry_enabled_default = enable
        self._custom_type = custom_type
        self._attr_native_unit_of_measurement = (
            "Accounts" if custom_type == ATTR_CONNECTED_ACCOUNTS else None
        )
        self._attr_icon = (
            "mdi:account-group" if custom_type == ATTR_CONNECTED_ACCOUNTS else None
        )
        self._attr_unique_id = (
            f"{DOMAIN}-{self._device.airco_id}-{self._custom_type}-sensor"
        )
        # Map custom_type to translation key
        type_map = {
            "airco_id": "airco_id",
            "operator_id": "operator_id",
            "device_id": "device_id",
            "host": "host",
            "connected_accounts": "connected_accounts",
            "error": "error",
            "updated_by": "updated_by",
            "account_expires": "account_expires",
            "led_status": "led_status",
            "auto_heating": "auto_heating",
            "model_nr": "model_nr",
            "cool_hot_judge": "cool_hot_judge",
        }
        self._attr_translation_key = type_map.get(custom_type, custom_type)
        self._update_state()

    def _update_state(self) -> None:
        if self._custom_type == CONF_OPERATOR_ID:
            self._attr_native_value = self._device.operator_id
        elif self._custom_type == CONF_AIRCO_ID:
            self._attr_native_value = self._device.airco_id
        elif self._custom_type == CONF_HOST:
            self._attr_native_value = self._device.host
        elif self._custom_type == ATTR_DEVICE_ID:
            self._attr_native_value = self._device.device_id
        elif self._custom_type == ATTR_CONNECTED_ACCOUNTS:
            self._attr_native_value = self._device.num_accounts
        elif self._custom_type == CONF_ERROR:
            self._attr_native_value = self._device.airco.ErrorCode
        elif self._custom_type == ATTR_UPDATED_BY:
            self._attr_native_value = self._device.updated_by
        elif self._custom_type == ATTR_ACCOUNT_EXPIRES:
            self._attr_native_value = self._device.account_expires
        elif self._custom_type == ATTR_LED_STATUS:
            self._attr_native_value = self._device.led_status
        elif self._custom_type == ATTR_AUTO_HEATING:
            self._attr_native_value = self._device.auto_heating
        elif self._custom_type == ATTR_MODEL_NR:
            self._attr_native_value = self._device.airco.ModelNrRaw
        elif self._custom_type == ATTR_COOL_HOT_JUDGE:
            airco = self._device.airco
            if not airco.Operation or airco.OperationMode == 3:  # off or FAN_ONLY
                self._attr_native_value = None
            else:
                self._attr_native_value = "heating" if airco.CoolHotJudge else "cooling"


class TemperatureSensor(WfRacEntity, SensorEntity):
    """Representation of a Sensor."""

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_has_entity_name: bool = True

    def __init__(self, device: Device, name: str, custom_type: str, enable=True) -> None:
        """Initialize the sensor."""
        super().__init__(device)
        self._custom_type = custom_type
        self._attr_entity_registry_enabled_default = enable
        self._attr_unique_id = (
            f"{DOMAIN}-{self._device.airco_id}-{self._custom_type}-sensor"
        )
        # Map custom_type to translation key
        type_map = {
            "inside_temperature": "indoor",
            "outside_temperature": "outdoor",
            "target_temperature": "target"
        }
        self._attr_translation_key = type_map.get(custom_type, custom_type)
        self._update_state()

    def _update_state(self) -> None:
        if self._custom_type == ATTR_INSIDE_TEMPERATURE:
            indoor_offset = self._device.config_entry.options.get(CONF_INDOOR_OFFSET, 0.0)
            self._attr_native_value = self._device.airco.IndoorTemp + indoor_offset
        elif self._custom_type == ATTR_OUTSIDE_TEMPERATURE:
            outdoor_offset = self._device.config_entry.options.get(CONF_OUTDOOR_OFFSET, 0.0)
            self._attr_native_value = self._device.airco.OutdoorTemp + outdoor_offset
        elif self._custom_type == ATTR_TARGET_TEMPERATURE:
            # Kept symmetric with climate.py's target_temperature - see the
            # comment in ClimateEntity._update_state().
            target_offset = self._device.config_entry.options.get(CONF_TARGET_OFFSET, 0.0)
            self._attr_native_value = self._device.airco.PresetTemp + target_offset


class EnergySensor(WfRacEntity, SensorEntity):
    """Representation of a Sensor."""

    _attr_translation_key = "energy_usage"
    _attr_native_unit_of_measurement: str | None = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class: SensorDeviceClass | str | None = SensorDeviceClass.ENERGY
    _attr_state_class: SensorStateClass | str | None = SensorStateClass.TOTAL_INCREASING
    _attr_has_entity_name: bool = True

    def __init__(self, device: Device) -> None:
        """Initialize the sensor."""
        super().__init__(device)
        self._attr_unique_id = f"{DOMAIN}-{self._device.airco_id}-energy-sensor"
        self._update_state()

    def _update_state(self) -> None:
        self._attr_native_value = self._device.airco.Electric


@dataclass
class EnergyTotalExtraStoredData(SensorExtraStoredData):
    """Adds the raw reading the total was last derived from.

    Restoring the total alone is not enough: without knowing which raw value
    it already covers, the first poll after a restart would either re-add the
    whole current run or drop it.
    """

    last_raw: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {**super().as_dict(), "last_raw": self.last_raw}

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self | None:
        if (base := SensorExtraStoredData.from_dict(restored)) is None:
            return None
        return cls(
            base.native_value,
            base.native_unit_of_measurement,
            restored.get("last_raw"),
        )


class EnergyTotalSensor(WfRacEntity, RestoreSensor):
    """Lifetime energy total, accumulated from the unit's per-run counter.

    Aircon.Electric is cleared to 0 by the unit on every power-on, so it can
    never show a lifetime figure by itself (see EnergySensor). This sensor
    sums its upward steps instead - the same arithmetic utility_meter does for
    a resetting source - and restores its total across restarts.

    Energy consumed between two polls of the same run that is followed by a
    power-on before the next poll is lost. Home Assistant's own long-term
    statistics have that same gap, since they work off the identical state
    stream.
    """

    _attr_translation_key = "energy_usage_total"
    _attr_native_unit_of_measurement: str | None = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class: SensorDeviceClass | str | None = SensorDeviceClass.ENERGY
    _attr_state_class: SensorStateClass | str | None = SensorStateClass.TOTAL_INCREASING
    _attr_has_entity_name: bool = True
    _attr_suggested_display_precision: int | None = 2

    def __init__(self, device: Device) -> None:
        """Initialize the sensor."""
        super().__init__(device)
        self._attr_unique_id = f"{DOMAIN}-{self._device.airco_id}-energy-total-sensor"
        self._total = 0.0
        # Anchored to the current reading so a brand-new sensor starts at 0
        # instead of claiming whatever the running cycle already accumulated.
        self._last_raw: float | None = self._device.airco.Electric
        self._attr_native_value = 0.0

    @property
    def extra_restore_state_data(self) -> EnergyTotalExtraStoredData:
        return EnergyTotalExtraStoredData(
            self.native_value, self.native_unit_of_measurement, self._last_raw
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if (stored := await self.async_get_last_extra_data()) is not None:
            restored = EnergyTotalExtraStoredData.from_dict(stored.as_dict())
            if restored is not None and restored.native_value is not None:
                self._total = float(restored.native_value)
                self._last_raw = restored.last_raw
                self._attr_native_value = round(self._total, 2)

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_SET_ENERGY_TOTAL}_{self._device.airco_id}",
                self.async_set_total,
            )
        )

        self._update_state()
        self.async_write_ha_state()

    def _update_state(self) -> None:
        raw = self._device.airco.Electric
        if raw is None:
            return
        # Only upward steps count; a drop means the unit was switched on and
        # started a new run, and what came before is already in the total.
        if self._last_raw is not None and raw > self._last_raw:
            self._total += raw - self._last_raw
        self._last_raw = raw
        self._attr_native_value = round(self._total, 2)

    async def async_set_total(self, value: float) -> None:
        """Set the accumulated total - reset to 0, or carry over a reading
        from a meter the user kept before. Re-anchors last_raw so the next
        poll does not re-add the delta that led up to the change."""
        self._total = float(value)
        self._last_raw = self._device.airco.Electric
        self._attr_native_value = round(self._total, 2)
        self.async_write_ha_state()


class ServiceDataSensor(WfRacEntity, SensorEntity):
    """Operation-data sensors (compressor frequency, current, hot gas temp,
    EEV pulses/position) - see rac_parser.SERVICE_DATA_CODES. Only created
    while CONF_SERVICE_DATA is on, which is what makes Device request these
    values in the first place - see Device._maybe_request_service_data().

    Enabled once they exist: switching the option on is already the opt-in,
    so making the user enable five entities on top would be a second hurdle
    for nothing. Diagnostic because they describe how the machine is running
    rather than what it is set to.
    """

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _FIELD_BY_TYPE = {
        ATTR_COMPRESSOR_FREQUENCY: "CompressorFrequency",
        ATTR_OPERATING_CURRENT: "OperatingCurrent",
        ATTR_HOT_GAS_TEMP: "HotGasTemp",
        ATTR_EEV_PULSES: "EevPulses",
        ATTR_EEV_POSITION: "EevPosition",
    }

    def __init__(self, device: Device, custom_type: str) -> None:
        """Initialize the sensor."""
        super().__init__(device)
        self._custom_type = custom_type
        self._attr_unique_id = f"{DOMAIN}-{self._device.airco_id}-{custom_type}-sensor"
        self._attr_translation_key = custom_type
        if custom_type == ATTR_COMPRESSOR_FREQUENCY:
            self._attr_device_class = SensorDeviceClass.FREQUENCY
            self._attr_native_unit_of_measurement = UnitOfFrequency.HERTZ
        elif custom_type == ATTR_OPERATING_CURRENT:
            self._attr_device_class = SensorDeviceClass.CURRENT
            self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
        elif custom_type == ATTR_HOT_GAS_TEMP:
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        elif custom_type == ATTR_EEV_PULSES:
            self._attr_native_unit_of_measurement = "pulses"
            self._attr_icon = "mdi:pulse"
        elif custom_type == ATTR_EEV_POSITION:
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_icon = "mdi:valve"
        self._update_state()

    def _update_state(self) -> None:
        self._attr_native_value = getattr(self._device.airco, self._FIELD_BY_TYPE[self._custom_type])