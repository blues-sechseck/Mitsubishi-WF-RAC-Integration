"""Constants used by the mitsubishi-wf-rac component."""

from datetime import timedelta
from homeassistant.const import CONF_ICON, CONF_NAME, CONF_TYPE
from homeassistant.components.climate.const import (
    HVACMode,
    ClimateEntityFeature,
    FAN_AUTO,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_HIGH,
)

DOMAIN = "mitsubishi_wf_rac"
DEVICES = "wf-rac-devices"

MIN_TIME_BETWEEN_UPDATES=timedelta(seconds=60)

CONF_OPERATOR_ID = "operator_id"
CONF_AIRCO_ID = "airco_id"
CONF_AVAILABILITY_CHECK = "availability_check"
CONF_AVAILABILITY_RETRY_LIMIT = "availability_retry_limit"
CONF_CREATE_SWING_MODE_SELECT = "create_swing_mode_select"
CONF_CONNECTION_METHOD = "connection_method"
ATTR_DEVICE_ID = "device_id"
ATTR_CONNECTED_ACCOUNTS = "connected_accounts"
ATTR_UPDATED_BY = "updated_by"
ATTR_ACCOUNT_EXPIRES = "account_expires"
ATTR_LED_STATUS = "led_status"
ATTR_AUTO_HEATING = "auto_heating"
ATTR_MODEL_NR = "model_nr"
ATTR_COOL_HOT_JUDGE = "cool_hot_judge"

ATTR_INSIDE_TEMPERATURE = "inside_temperature"
ATTR_OUTSIDE_TEMPERATURE = "outside_temperature"
ATTR_TARGET_TEMPERATURE = "target_temperature"

# New offset constants
CONF_INDOOR_OFFSET = "indoor_offset"
CONF_OUTDOOR_OFFSET = "outdoor_offset"
CONF_TARGET_OFFSET = "target_offset"
CONF_TARGET_OFFSET_COOL = "target_offset_cool"
CONF_TARGET_OFFSET_HEAT = "target_offset_heat"

SENSOR_TYPE_TEMPERATURE = "temperature"

SENSOR_TYPES = {
    ATTR_INSIDE_TEMPERATURE: {
        CONF_NAME: "Inside Temperature",
        CONF_ICON: "mdi:thermometer",
        CONF_TYPE: SENSOR_TYPE_TEMPERATURE,
    },
    ATTR_OUTSIDE_TEMPERATURE: {
        CONF_NAME: "Outside Temperature",
        CONF_ICON: "mdi:thermometer",
        CONF_TYPE: SENSOR_TYPE_TEMPERATURE,
    },
}

SERVICE_SET_HORIZONTAL_SWING_MODE = "set_horizontal_swing_mode"
SERVICE_SET_VERTICAL_SWING_MODE = "set_vertical_swing_mode"

SUPPORT_FLAGS = (
    ClimateEntityFeature.FAN_MODE
    | ClimateEntityFeature.SWING_HORIZONTAL_MODE
    | ClimateEntityFeature.SWING_MODE
    | ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TURN_OFF
    | ClimateEntityFeature.TURN_ON
)

SUPPORTED_HVAC_MODES = [
    HVACMode.OFF,
    HVACMode.AUTO,
    HVACMode.COOL,
    HVACMode.DRY,
    HVACMode.HEAT,
    HVACMode.FAN_ONLY,
]

HVAC_TRANSLATION = {
    HVACMode.AUTO: 0,
    HVACMode.COOL: 1,
    HVACMode.HEAT: 2,
    HVACMode.FAN_ONLY: 3,
    HVACMode.DRY: 4,
}

SWING_3D_AUTO = "3d_auto"
SWING_VERTICAL_POSITION_1 = "highest"
SWING_VERTICAL_POSITION_2 = "middle"
SWING_VERTICAL_POSITION_3 = "normal"
SWING_VERTICAL_POSITION_4 = "lowest"
SWING_VERTICAL_AUTO = "up_down_auto"

SWING_HORIZONTAL_POSITION_1 = "left_left"
SWING_HORIZONTAL_POSITION_2 = "left_center"
SWING_HORIZONTAL_POSITION_3 = "center_center"
SWING_HORIZONTAL_POSITION_4 = "center_right"
SWING_HORIZONTAL_POSITION_5 = "right_right"
SWING_HORIZONTAL_POSITION_6 = "left_right"
SWING_HORIZONTAL_POSITION_7 = "right_left"
SWING_HORIZONTAL_AUTO = "left_right_auto"


SWING_MODE_TRANSLATION = {
    SWING_VERTICAL_AUTO: 0,
    SWING_VERTICAL_POSITION_1: 1,
    SWING_VERTICAL_POSITION_2: 2,
    SWING_VERTICAL_POSITION_3: 3,
    SWING_VERTICAL_POSITION_4: 4,
}

SUPPORT_SWING_MODES = [
    SWING_VERTICAL_AUTO,
    SWING_VERTICAL_POSITION_1,
    SWING_VERTICAL_POSITION_2,
    SWING_VERTICAL_POSITION_3,
    SWING_VERTICAL_POSITION_4,
    SWING_3D_AUTO,
]

SWING_HORIZONTAL_MODE_TRANSLATION = {
    SWING_HORIZONTAL_AUTO: 0,
    SWING_HORIZONTAL_POSITION_1: 1,
    SWING_HORIZONTAL_POSITION_2: 2,
    SWING_HORIZONTAL_POSITION_3: 3,
    SWING_HORIZONTAL_POSITION_4: 4,
    SWING_HORIZONTAL_POSITION_5: 5,
    SWING_HORIZONTAL_POSITION_6: 6,
    SWING_HORIZONTAL_POSITION_7: 7,
}

SUPPORT_SWING_HORIZONTAL_MODES = [
    SWING_HORIZONTAL_AUTO,
    SWING_HORIZONTAL_POSITION_1,
    SWING_HORIZONTAL_POSITION_2,
    SWING_HORIZONTAL_POSITION_3,
    SWING_HORIZONTAL_POSITION_4,
    SWING_HORIZONTAL_POSITION_5,
    SWING_HORIZONTAL_POSITION_6,
    SWING_HORIZONTAL_POSITION_7,
    SWING_3D_AUTO,
]


FAN_QUIET = "quiet"

FAN_MODE_TRANSLATION = {
    FAN_AUTO: 0,
    FAN_QUIET: 1,
    FAN_LOW: 2,
    FAN_MEDIUM: 3,
    FAN_HIGH: 4,
}

SUPPORTED_FAN_MODES = [
    FAN_AUTO,
    FAN_QUIET,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_HIGH,
]


OPERATION_LIST = {
    # HVAC_MODE_OFF: "Off",
    HVACMode.HEAT: "Heat",
    HVACMode.COOL: "Cool",
    HVACMode.AUTO: "Auto",
    HVACMode.DRY: "Dry",
    HVACMode.FAN_ONLY: "Fan",
}
