"""for sensor integration."""
# pylint: disable = too-few-public-methods

from __future__ import annotations
import logging

from . import MitsubishiWfRacConfigEntry
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    UnitOfEnergy,
    UnitOfTemperature,
    EntityCategory,
    CONF_HOST,
    CONF_ERROR,
)

from .entity import WfRacEntity
from .wfrac.device import Device
from .const import (
    ATTR_TARGET_TEMPERATURE,
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
    CONF_TARGET_OFFSET
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
        DiagnosticsSensor(device, "Airco ID", CONF_AIRCO_ID),
        DiagnosticsSensor(device, "Operator ID", CONF_OPERATOR_ID, True),
        DiagnosticsSensor(device, "Device ID", ATTR_DEVICE_ID, True),
        DiagnosticsSensor(device, "IP", CONF_HOST, True),
        DiagnosticsSensor(device, "Accounts", ATTR_CONNECTED_ACCOUNTS, True),
        DiagnosticsSensor(device, "Error", CONF_ERROR),
        DiagnosticsSensor(device, "Updated By", ATTR_UPDATED_BY),
        DiagnosticsSensor(device, "Account Expires", ATTR_ACCOUNT_EXPIRES),
        DiagnosticsSensor(device, "LED Status", ATTR_LED_STATUS),
        DiagnosticsSensor(device, "Auto Heating", ATTR_AUTO_HEATING),
        DiagnosticsSensor(device, "Model Nr", ATTR_MODEL_NR),
        DiagnosticsSensor(device, "Cool Hot Judge", ATTR_COOL_HOT_JUDGE),
    ]
    if device.airco.Electric is not None:
        entities.append(EnergySensor(device))

    async_add_entities(entities)


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