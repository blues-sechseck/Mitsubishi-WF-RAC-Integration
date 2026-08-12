"""Unit tests for wfrac/capabilities.py - the model_no_type table.

Ground truth is the app's own `res/values/arrays.xml`
(`model_no_type_function_*`) and `model/ModelNoType.java` (table selection,
flag order) - see capabilities.py's own docstring.
"""

from custom_components.mitsubishi_wf_rac.wfrac.capabilities import get_capabilities


def test_raw_0_falls_back_to_separate_2021():
    caps = get_capabilities(0)
    assert caps.power_consumption is True
    assert caps.vacant_property is False
    assert caps.operation_data is False


def test_raw_1_is_global_2022():
    caps = get_capabilities(1)
    assert caps.vacant_property is True
    assert caps.home_leave_mode is True
    assert caps.self_clean_operation is True


def test_raw_2_is_high_end_for_japanese_2023():
    caps = get_capabilities(2)
    assert caps.vacant_property is False
    assert caps.call_center_phone_number is True


def test_raw_3_is_zt_2025_and_keeps_vacant_property():
    # The #187 regression this table fixes: raw=3 previously collapsed into
    # the ModelNr=2 wire-protocol bucket (see rac_parser.py), which cost it
    # VacantProperty even though the app's own zt_2025 table grants it.
    caps = get_capabilities(3)
    assert caps.vacant_property is True
    assert caps.preset_temp_range_2 is True
    assert caps.operation_data is True


def test_raw_64_is_fdt_2023():
    caps = get_capabilities(64)
    assert caps.led_light is True
    assert caps.preset_temp_auto is True
    assert caps.vacant_property is False


def test_unrecognized_raw_falls_back_to_separate_2021():
    caps = get_capabilities(99)
    assert caps == get_capabilities(0)
