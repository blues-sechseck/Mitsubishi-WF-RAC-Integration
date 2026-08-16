"""for sensor integration."""
# pylint: disable = too-few-public-methods

from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
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
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
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
from .wfrac.rac_parser import SERVICE_DATA_CODE_BY_FIELD
from .const import (
    ATTR_TARGET_TEMPERATURE,
    ATTR_COMPRESSOR_FREQUENCY,
    ATTR_COMPRESSOR_FREQUENCY_RAW,
    ATTR_OPERATING_CURRENT,
    ATTR_OPERATING_CURRENT_RAW,
    ATTR_HOT_GAS_TEMP,
    ATTR_HOT_GAS_TEMP_RAW,
    ATTR_EEV_PULSES,
    ATTR_EEV_POSITION,
    ATTR_INDOOR_COIL_TEMP,
    ATTR_INDOOR_COIL_OUTLET_TEMP,
    ATTR_INDOOR_COIL_RAW,
    ATTR_INDOOR_COIL_OUTLET_RAW,
    ATTR_OUTDOOR_COIL_RAW,
    ATTR_DISCHARGE_SUPERHEAT_RAW,
    ATTR_PROTECTION_RAW,
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
    SERVICE_SET_ENERGY_TOTAL,
    SIGNAL_SET_ENERGY_TOTAL,
)

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MitsubishiWfRacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
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
        DiagnosticsSensor(device, "Airco ID", CONF_AIRCO_ID),
        DiagnosticsSensor(device, "Operator ID", CONF_OPERATOR_ID),
        DiagnosticsSensor(device, "Device ID", ATTR_DEVICE_ID),
        DiagnosticsSensor(device, "IP", CONF_HOST),
        DiagnosticsSensor(device, "Accounts", ATTR_CONNECTED_ACCOUNTS),
        DiagnosticsSensor(device, "Error", CONF_ERROR, True),
        DiagnosticsSensor(device, "Updated By", ATTR_UPDATED_BY, True),
        DiagnosticsSensor(device, "Account Expires", ATTR_ACCOUNT_EXPIRES),
        # Off by default: mirrors the unit's own "LED ON" display-light
        # setting (see wf-rac-module-reference.md §2.8), which nobody has
        # switched off on either test unit - a constant 1 is the default,
        # not a broken read.
        DiagnosticsSensor(device, "LED Status", ATTR_LED_STATUS),
        DiagnosticsSensor(device, "Auto Heating", ATTR_AUTO_HEATING, True),
        DiagnosticsSensor(device, "Model Nr", ATTR_MODEL_NR),
        DiagnosticsSensor(device, "Cool Hot Judge", ATTR_COOL_HOT_JUDGE),
    ]
    if device.airco.Electric is not None:
        entities.append(EnergySensor(device))
        entities.append(EnergyTotalSensor(device))

    # Active operation-data entities register their segment code with Device,
    # which requests only the segments needed by enabled entities.
    entities += [
        ServiceDataSensor(device, ATTR_COMPRESSOR_FREQUENCY),
        ServiceDataSensor(device, ATTR_COMPRESSOR_FREQUENCY_RAW),
        ServiceDataSensor(device, ATTR_OPERATING_CURRENT),
        ServiceDataSensor(device, ATTR_OPERATING_CURRENT_RAW),
        ServiceDataSensor(device, ATTR_HOT_GAS_TEMP),
        ServiceDataSensor(device, ATTR_HOT_GAS_TEMP_RAW),
        ServiceDataSensor(device, ATTR_EEV_PULSES),
        ServiceDataSensor(device, ATTR_EEV_POSITION),
        ServiceDataSensor(device, ATTR_INDOOR_COIL_TEMP),
        ServiceDataSensor(device, ATTR_INDOOR_COIL_OUTLET_TEMP),
        ServiceDataSensor(device, ATTR_INDOOR_COIL_RAW),
        ServiceDataSensor(device, ATTR_INDOOR_COIL_OUTLET_RAW),
        ServiceDataSensor(device, ATTR_OUTDOOR_COIL_RAW),
        ServiceDataSensor(device, ATTR_DISCHARGE_SUPERHEAT_RAW),
        ServiceDataSensor(device, ATTR_PROTECTION_RAW),
    ]

    _async_remove_home_leave_mode_sensors(hass, device)

    async_add_entities(entities)

    entity_platform.async_get_current_platform().async_register_entity_service(
        SERVICE_SET_ENERGY_TOTAL,
        {vol.Required("value"): vol.All(vol.Coerce(float), vol.Range(min=0))},
        _async_set_energy_total,
    )


async def _async_set_energy_total(entity: SensorEntity, call: ServiceCall) -> None:
    """Entity-service handler for SERVICE_SET_ENERGY_TOTAL.

    Registered as a callable rather than a method name so targeting any other
    sensor of this integration fails with a readable message instead of an
    AttributeError - entity services apply to every entity on the platform.
    """
    if not isinstance(entity, EnergyTotalSensor):
        raise ServiceValidationError(
            f"{entity.entity_id} is not an Energy Usage Total sensor",
            translation_domain=DOMAIN,
            translation_key="entity_not_energy_total_sensor",
            translation_placeholders={"entity_id": entity.entity_id},
        )
    await entity.async_set_total(call.data["value"])


def _async_remove_home_leave_mode_sensors(hass: HomeAssistant, device: Device) -> None:
    """Drop the former Home Leave Mode diagnostic sensors from the entity
    registry.

    Replaced by writable entities on the Controls section of the device page:
    HomeLeaveModeNumber (TempRule/TempSetting) in number.py, the AirFlow
    selects in select.py - editable directly instead of only via the
    set_home_leave_mode action.
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
        self, device: Device, name: str, custom_type: str, enable: bool = False
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

    def __init__(self, device: Device, name: str, custom_type: str, enable: bool = True) -> None:
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
            indoor_offset = self._device.options.get(CONF_INDOOR_OFFSET, 0.0)
            self._attr_native_value = self._device.airco.IndoorTemp + indoor_offset
        elif self._custom_type == ATTR_OUTSIDE_TEMPERATURE:
            outdoor_offset = self._device.options.get(CONF_OUTDOOR_OFFSET, 0.0)
            self._attr_native_value = self._device.airco.OutdoorTemp + outdoor_offset
        elif self._custom_type == ATTR_TARGET_TEMPERATURE:
            # Kept symmetric with climate.py's target_temperature by going
            # through the same resolver, per-mode overrides included - see
            # WfRacEntity._resolve_target_offset().
            target_offset = self._resolve_target_offset(self._hvac_mode_from_operation)
            self._attr_native_value = self._device.airco.PresetTemp + target_offset


class EnergySensor(WfRacEntity, SensorEntity):
    """Representation of a Sensor."""

    _attr_translation_key = "energy_usage"
    _attr_native_unit_of_measurement: str | None = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class: SensorDeviceClass | None = SensorDeviceClass.ENERGY
    _attr_state_class: SensorStateClass | None = SensorStateClass.TOTAL_INCREASING
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
    _attr_device_class: SensorDeviceClass | None = SensorDeviceClass.ENERGY
    _attr_state_class: SensorStateClass | None = SensorStateClass.TOTAL_INCREASING
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
            # native_value's declared type also allows date/datetime, which
            # this sensor never stores but float() can't convert - restrict
            # to what a restored total can actually be instead of crashing
            # async_added_to_hass() on an unexpected stored type.
            if restored is not None and isinstance(
                restored.native_value, (int, float, str, Decimal)
            ):
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
    """Operation-data sensors, including converted values and raw bytes.
    Active sensors register their segment code with Device, which requests
    only those segments.
    """

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    _FIELD_BY_TYPE = {
        ATTR_COMPRESSOR_FREQUENCY: "CompressorFrequency",
        ATTR_COMPRESSOR_FREQUENCY_RAW: "CompressorFrequencyRaw",
        ATTR_OPERATING_CURRENT: "OperatingCurrent",
        ATTR_OPERATING_CURRENT_RAW: "OperatingCurrentRaw",
        ATTR_HOT_GAS_TEMP: "HotGasTemp",
        ATTR_HOT_GAS_TEMP_RAW: "HotGasTempRaw",
        ATTR_EEV_PULSES: "EevPulses",
        ATTR_EEV_POSITION: "EevPosition",
        ATTR_INDOOR_COIL_TEMP: "IndoorCoilTemp",
        ATTR_INDOOR_COIL_OUTLET_TEMP: "IndoorCoilOutletTemp",
        ATTR_INDOOR_COIL_RAW: "IndoorCoilRaw",
        ATTR_INDOOR_COIL_OUTLET_RAW: "IndoorCoilOutletRaw",
        ATTR_OUTDOOR_COIL_RAW: "OutdoorCoilRaw",
        ATTR_DISCHARGE_SUPERHEAT_RAW: "DischargeSuperheatRaw",
        ATTR_PROTECTION_RAW: "ProtectionRaw",
    }

    def __init__(self, device: Device, custom_type: str) -> None:
        """Initialize the sensor."""
        self._custom_type = custom_type
        super().__init__(
            device, context=SERVICE_DATA_CODE_BY_FIELD[self._FIELD_BY_TYPE[custom_type]]
        )
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
        elif custom_type in (ATTR_INDOOR_COIL_TEMP, ATTR_INDOOR_COIL_OUTLET_TEMP):
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._update_state()

    def _update_state(self) -> None:
        self._attr_native_value = getattr(self._device.airco, self._FIELD_BY_TYPE[self._custom_type])
