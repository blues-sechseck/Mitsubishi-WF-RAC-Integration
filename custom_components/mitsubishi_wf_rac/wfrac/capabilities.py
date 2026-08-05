"""Per-model feature capability table, ported from the official app.

Ground truth for #187: `res/values/arrays.xml` (`model_no_type_function_*`,
5 tables x 17 flags) and `model/ModelNoType.java` (flag order, table
selection) in `smartMAir_apk/jadx-out/`. See `../../../../smartMAir_apk/FUNDE.md`
("Capabilities") for the reverse-engineering notes.
"""

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class ModelCapabilities:
    """Feature flags for one `model_no_type` table entry (17 booleans, in the
    app's own field order - see ModelNoType.java's getters)."""

    power_consumption: bool
    vacant_property: bool
    self_clean_operation: bool
    wind_direction_lr: bool
    entrust: bool
    led_light: bool
    auto_heating: bool
    home_leave_mode: bool
    temp_information: bool
    indoor_and_outdoor_temp: bool
    preset_temp_auto: bool
    call_center_phone_number: bool
    outdoor_temp_always_show: bool
    outdoor_temp_outside_tokyo: bool
    cool_hot_judge: bool
    preset_temp_range_2: bool
    operation_data: bool


# Transcribed 1:1 from arrays.xml (item order == flag order above).
_SEPARATE_2021: Final = ModelCapabilities(
    power_consumption=True, vacant_property=False, self_clean_operation=False,
    wind_direction_lr=True, entrust=True, led_light=True, auto_heating=True,
    home_leave_mode=False, temp_information=True, indoor_and_outdoor_temp=True,
    preset_temp_auto=False, call_center_phone_number=False,
    outdoor_temp_always_show=False, outdoor_temp_outside_tokyo=True,
    cool_hot_judge=True, preset_temp_range_2=False, operation_data=False,
)
_GLOBAL_2022: Final = ModelCapabilities(
    power_consumption=True, vacant_property=True, self_clean_operation=True,
    wind_direction_lr=True, entrust=True, led_light=False, auto_heating=True,
    home_leave_mode=True, temp_information=True, indoor_and_outdoor_temp=True,
    preset_temp_auto=False, call_center_phone_number=False,
    outdoor_temp_always_show=True, outdoor_temp_outside_tokyo=False,
    cool_hot_judge=True, preset_temp_range_2=False, operation_data=False,
)
_HIGH_END_FOR_JAPANESE_2023: Final = ModelCapabilities(
    power_consumption=True, vacant_property=False, self_clean_operation=True,
    wind_direction_lr=True, entrust=True, led_light=False, auto_heating=False,
    home_leave_mode=False, temp_information=True, indoor_and_outdoor_temp=True,
    preset_temp_auto=False, call_center_phone_number=True,
    outdoor_temp_always_show=False, outdoor_temp_outside_tokyo=False,
    cool_hot_judge=True, preset_temp_range_2=False, operation_data=False,
)
_ZT_2025: Final = ModelCapabilities(
    power_consumption=False, vacant_property=True, self_clean_operation=True,
    wind_direction_lr=True, entrust=True, led_light=False, auto_heating=True,
    home_leave_mode=True, temp_information=True, indoor_and_outdoor_temp=True,
    preset_temp_auto=False, call_center_phone_number=False,
    outdoor_temp_always_show=True, outdoor_temp_outside_tokyo=False,
    cool_hot_judge=True, preset_temp_range_2=True, operation_data=True,
)
_FDT_2023: Final = ModelCapabilities(
    power_consumption=False, vacant_property=False, self_clean_operation=False,
    wind_direction_lr=False, entrust=False, led_light=True, auto_heating=False,
    home_leave_mode=False, temp_information=False, indoor_and_outdoor_temp=True,
    preset_temp_auto=True, call_center_phone_number=False,
    outdoor_temp_always_show=True, outdoor_temp_outside_tokyo=False,
    cool_hot_judge=False, preset_temp_range_2=False, operation_data=False,
)

# Raw ModelNr byte -> table. Not a sequential scheme - hardcoded in
# ModelNoType.java:getArrayPosition(): 1/2/3/64 map to their own table,
# everything else (including 0) falls back to separate_2021.
_TABLE_BY_RAW: Final = {
    1: _GLOBAL_2022,
    2: _HIGH_END_FOR_JAPANESE_2023,
    3: _ZT_2025,
    64: _FDT_2023,
}


def get_capabilities(model_nr_raw: int) -> ModelCapabilities:
    """Return the capability table for a device's raw ModelNr byte
    (`content[0] & 127`, see rac_parser.py's `ModelNrRaw`)."""
    return _TABLE_BY_RAW.get(model_nr_raw, _SEPARATE_2021)
