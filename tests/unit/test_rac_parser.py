"""Unit tests for wfrac/rac_parser.py - pure logic, no HA/network involved.

Two kinds of ground truth are used here:

1. Live device captures (live_captures.py): real airconStat payloads from a
   physical unit, cross-checked against fields independently computed by
   wfrac_live_monitor.py's own (separately hand-written) decode_fields() at
   capture time - not derived by calling into rac_parser.py itself.
2. Synthetic AirconStat round-trips through receive_to_bytes() ->
   _parse_basic_settings(): these two use a shared "receive" byte layout by
   design (unlike command_to_byte(), which uses a different layout for the
   *command* direction - see test_command_to_byte_self_clean_byte12 below),
   so encoding then decoding should reproduce the original values.
"""

import pytest

from custom_components.mitsubishi_wf_rac.wfrac.models.aircon import (
    Aircon,
    AirconStat,
    HomeLeaveModeSetting,
)
from custom_components.mitsubishi_wf_rac.wfrac.rac_parser import RacParser
from custom_components.mitsubishi_wf_rac.wfrac.utils import find_match

from .live_captures import LIVE_CAPTURES


@pytest.fixture
def parser() -> RacParser:
    return RacParser()


# --- find_match (utils.py) ---------------------------------------------


def test_find_match_returns_index_of_first_match():
    assert find_match(16, 8, 16, 12, 4) == 1


def test_find_match_returns_minus_one_when_absent():
    assert find_match(99, 8, 16, 12, 4) == -1


# --- translate_bytes() against real live captures -----------------------


@pytest.mark.parametrize("name", LIVE_CAPTURES.keys())
def test_translate_bytes_matches_live_capture(parser, name):
    payload, expected = LIVE_CAPTURES[name]
    aircon = parser.translate_bytes(payload)

    assert aircon.Operation == expected["Operation"]
    assert aircon.OperationMode == expected["OperationMode"]
    assert aircon.PresetTemp == expected["PresetTemp"]
    assert aircon.AirFlow == expected["AirFlow"]
    assert aircon.ModelNrRaw == expected["ModelNrRaw"]
    assert aircon.Vacant == expected["Vacant"]


def test_translate_bytes_temperatures_are_in_table_range(parser):
    # Real captures don't have an independently-known exact IndoorTemp/
    # OutdoorTemp (the monitor script didn't record those separately), but
    # both must land inside the respective lookup table's range - anything
    # else means the segment offset math or table indexing is broken.
    payload, _ = LIVE_CAPTURES["on_cool"]
    aircon = parser.translate_bytes(payload)
    assert -50.0 <= aircon.OutdoorTemp <= 43.0
    assert -30.0 <= aircon.IndoorTemp <= 52.0


def test_translate_bytes_model_nr_1_maps_directly(parser):
    payload, _ = LIVE_CAPTURES["on_cool"]
    aircon = parser.translate_bytes(payload)
    assert aircon.ModelNrRaw == 1
    assert aircon.ModelNr == 1


# --- ModelNr edge cases (_parse_basic_settings) --------------------------


def test_model_nr_raw_3_maps_to_model_nr_2(parser):
    # ZT series (2026 model line, see #189) - same wire-protocol byte layout
    # as ModelNr 2, but a different raw byte value. Its #187 capability table
    # (Capabilities) is looked up from ModelNrRaw directly and does *not*
    # collapse the same way - see test_capabilities.py.
    ac = Aircon()
    content = [3] + [0] * 17
    parser._parse_basic_settings(ac, content)
    assert ac.ModelNrRaw == 3
    assert ac.ModelNr == 2
    assert ac.Capabilities.vacant_property is True


def test_model_nr_raw_unrecognized_yields_minus_one(parser):
    ac = Aircon()
    content = [99] + [0] * 17
    parser._parse_basic_settings(ac, content)
    assert ac.ModelNrRaw == 99
    assert ac.ModelNr == -1


@pytest.mark.parametrize("raw,expected", [(0, 0), (1, 1), (2, 2)])
def test_model_nr_raw_known_values(parser, raw, expected):
    ac = Aircon()
    content = [raw] + [0] * 17
    parser._parse_basic_settings(ac, content)
    assert ac.ModelNr == expected


# --- ErrorCode (byte 6: bit7 = M vs E, low 7 bits = code) ----------------
# Ground truth cross-checked against the official app's AirconStatCoder.java:
# bit7 set -> "M<code>" (maintenance code), bit7 clear + code!=0 -> "E<code>".
# A prior `(content[6] & -128) <= 0` check was always true (AND of a signed
# byte with -128 can only yield 0 or -128, both <= 0), so the "E" branch was
# dead code and every non-zero code surfaced as "M".


@pytest.mark.parametrize(
    "byte6,expected",
    [
        (0, "00"),
        (5, "E5"),
        (127, "E127"),
        (-128, "M00"),
        (-127, "M01"),
        (-1, "M127"),
    ],
)
def test_error_code_decodes_m_vs_e_by_bit7(parser, byte6, expected):
    ac = Aircon()
    content = [1] + [0] * 5 + [byte6] + [0] * 11
    parser._parse_basic_settings(ac, content)
    assert ac.ErrorCode == expected


# --- OperationMode's "-1 => AUTO" edge case ------------------------------


def test_operation_mode_no_bit_set_decodes_to_auto(parser):
    # RCV_MODE_MASKS[0] (AUTO) == 0, i.e. AUTO is legitimately encoded as
    # "none of the cool/heat/fan/dry bits are set" - find_match() therefore
    # returns -1 (no match in the candidate list), and +1 recovers AUTO (0).
    # Confirmed against a real live capture (LIVE_CAPTURES["on_auto"]).
    ac = Aircon()
    content = [0, 0, 0b00000001, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    parser._parse_basic_settings(ac, content)
    assert ac.OperationMode == 0


# --- _parse_temperatures: index-bound regression + electric -------------


def test_parse_temperatures_trailing_partial_segment_is_ignored(parser):
    # Regression test for the `len(vals) - 3` loop bound (previously
    # `len(vals)`): a trailing segment shorter than 4 bytes must be skipped,
    # not raise IndexError on vals[i+1]/[i+2]/[i+3].
    ac = Aircon()
    full_outdoor_segment = [-128, 16, 100, 0]  # tag=-128 sub=16 -> OutdoorTemp
    trailing_partial = [-128, 16]  # only 2 of 4 bytes - must not be read
    parser._parse_temperatures(ac, full_outdoor_segment + trailing_partial)
    assert ac.OutdoorTemp == pytest.approx(2.7)  # outdoorTempList[100]


def test_parse_temperatures_indoor_and_outdoor(parser):
    ac = Aircon()
    vals = [-128, 16, 100, 0, -128, 32, 50, 0]
    parser._parse_temperatures(ac, vals)
    assert ac.OutdoorTemp == pytest.approx(2.7)  # outdoorTempList[100]
    assert ac.IndoorTemp == pytest.approx(-7.5)  # indoorTempList[50]


def test_parse_temperatures_electric(parser):
    ac = Aircon()
    # tag=-108, sub=16 -> electric, value bytes little-endian * 0.25
    vals = [-108, 16, 40, 0]
    parser._parse_temperatures(ac, vals)
    assert ac.Electric == pytest.approx(10.0)  # 40 * 0.25


def test_parse_temperatures_unknown_segment_does_not_raise(parser):
    ac = Aircon()
    parser._parse_temperatures(ac, [1, 2, 3, 4])
    assert ac.Electric is None


def test_parse_temperatures_unknown_subtag_under_temp_tag_does_not_raise(parser):
    # tag=-128 (the indoor/outdoor temp tag) but an unrecognized sub-tag -
    # a different code path than an entirely unknown tag above.
    ac = Aircon()
    parser._parse_temperatures(ac, [-128, 99, 0, 0])
    assert ac.OutdoorTemp == 0.0
    assert ac.IndoorTemp == 0.0


# --- HomeLeaveMode (Tag 248 extension segment, #187 capability index 7) --


def test_parse_temperatures_home_leave_mode_all_six_subcodes_present(parser):
    ac = Aircon()
    # tag=-8 (248 signed), sub=16 (status marker), subcodes 27-32 in order:
    # cool rule, heat rule, cool setting, heat setting, cool airflow, heat airflow.
    vals = []
    for sub, value in zip((27, 28, 29, 30, 31, 32), (70, 0, 66, 20, 3, 7)):
        vals += [-8, 16, sub, value]
    parser._parse_temperatures(ac, vals)
    assert ac.HomeLeaveModeForCooling == HomeLeaveModeSetting(
        TempRule=35.0, TempSetting=33.0, AirFlow=1
    )
    assert ac.HomeLeaveModeForHeating == HomeLeaveModeSetting(
        TempRule=0.0, TempSetting=10.0, AirFlow=3
    )


def test_parse_temperatures_home_leave_mode_partial_does_not_commit(parser):
    # Mirrors AirconStatCoder.byteToStat's all-or-nothing commit: five of six
    # subcodes present must leave both sides None, not a half-filled result.
    ac = Aircon()
    vals = []
    for sub, value in zip((27, 28, 29, 30, 31), (70, 0, 66, 20, 0)):
        vals += [-8, 16, sub, value]
    parser._parse_temperatures(ac, vals)
    assert ac.HomeLeaveModeForCooling is None
    assert ac.HomeLeaveModeForHeating is None


def test_home_leave_mode_trailer_default_is_the_plain_sentinel(parser):
    # Every command that doesn't touch HomeLeaveMode (i.e. every command this
    # integration sent before this feature existed) must keep producing the
    # same 5-byte "nothing to send" sentinel.
    stat = _base_stat()
    assert parser._variable_trailer(stat) == bytearray([1, 255, 255, 255, 255])


def test_home_leave_mode_trailer_status_request(parser):
    stat = _base_stat(HomeLeaveModeStatusRequest=True)
    trailer = parser._variable_trailer(stat)
    assert trailer[0] == 6  # six 4-byte groups
    groups = [trailer[1 + i * 4:5 + i * 4] for i in range(6)]
    for group, sub in zip(groups, (27, 28, 29, 30, 31, 32)):
        assert list(group) == [248, 255, sub, 0]


def test_home_leave_mode_trailer_set_values(parser):
    stat = _base_stat(
        HomeLeaveModeForCooling=HomeLeaveModeSetting(TempRule=35.0, TempSetting=33.0, AirFlow=0),
        HomeLeaveModeForHeating=HomeLeaveModeSetting(TempRule=0.0, TempSetting=10.0, AirFlow=4),
    )
    trailer = parser._variable_trailer(stat)
    assert trailer[0] == 6
    groups = [trailer[1 + i * 4:5 + i * 4] for i in range(6)]
    expected = [
        (27, 70), (28, 0), (29, 66), (30, 20), (31, 0), (32, 14),
    ]
    for group, (sub, value) in zip(groups, expected):
        assert list(group) == [248, 0, sub, value]


def test_home_leave_mode_encode_decode_round_trip(parser):
    # Encode a "set" trailer, then feed the same 4-byte groups (with the
    # status marker 16 substituted for the write marker 0/255, as a real
    # response would use) back through the decoder.
    stat = _base_stat(
        HomeLeaveModeForCooling=HomeLeaveModeSetting(TempRule=35.0, TempSetting=33.0, AirFlow=2),
        HomeLeaveModeForHeating=HomeLeaveModeSetting(TempRule=0.0, TempSetting=10.0, AirFlow=1),
    )
    trailer = parser._variable_trailer(stat)
    signed = lambda b: b - 256 if b > 127 else b
    groups = [trailer[1 + i * 4:5 + i * 4] for i in range(6)]
    vals = []
    for group in groups:
        tag, _marker, sub, value = group
        vals += [signed(tag), 16, sub, value]

    ac = Aircon()
    parser._parse_temperatures(ac, vals)
    assert ac.HomeLeaveModeForCooling == stat.HomeLeaveModeForCooling
    assert ac.HomeLeaveModeForHeating == stat.HomeLeaveModeForHeating


def test_service_data_trailer_status_request(parser):
    stat = _base_stat(ServiceDataStatusRequest=True)
    trailer = parser._variable_trailer(stat)
    assert trailer[0] == 4  # four 4-byte segments
    groups = [trailer[1 + i * 4:5 + i * 4] for i in range(4)]
    for group, code in zip(groups, (0x11, 0x90, 0x85, 0x13)):
        # OP1=OP2=OP3=255 -> "report current value", never 0 (a write to the
        # climate MCU) - see CLAUDE.md's telemetry-segment guardrail.
        assert list(group) == [code, 255, 255, 255]


def test_parse_temperatures_service_data_segments(parser):
    # Real values from a live batched request (06.08.2026, Klima
    # Schlafzimmer) - see wf-rac-module-reference.md §5.4/todo.md. Unlike
    # HomeLeaveMode's Tag 248, op1 carries data (part of the frequency
    # formula) rather than a fixed status marker.
    signed = lambda b: b - 256 if b > 127 else b
    vals = []
    for code, op1, op2 in (
        (0x11, 0x10, 0xC8),
        (0x90, 0x10, 0x04),
        (0x85, 0x10, 0x15),
        (0x13, 0x10, 0x6A),
    ):
        vals += [signed(code), op1, op2, 0]

    ac = Aircon()
    parser._parse_temperatures(ac, vals)

    assert ac.CompressorFrequency == pytest.approx(20.0)
    assert ac.OperatingCurrent == pytest.approx(4 * 14 / 51)
    assert ac.HotGasTemp == pytest.approx(42.5)
    assert ac.EevPulses == 106
    assert ac.EevPosition == 42


def test_parse_temperatures_service_data_absent_by_default(parser):
    # A plain poll without a prior ServiceDataStatusRequest must leave all
    # five fields at their None default (see AirconCommands), not e.g. 0.
    ac = Aircon()
    parser._parse_temperatures(ac, [])
    assert ac.CompressorFrequency is None
    assert ac.OperatingCurrent is None
    assert ac.HotGasTemp is None
    assert ac.EevPulses is None
    assert ac.EevPosition is None


def test_to_base64_default_length_unchanged_by_home_leave_mode(parser):
    from base64 import b64decode

    stat = _base_stat()
    decoded = b64decode(parser.to_base64(stat))
    assert len(decoded) == 50


def test_calculate_electric_little_endian():
    # 0x0100 little-endian = 256, * 0.25 = 64.0
    assert RacParser._calculate_electric([0, 1]) == pytest.approx(64.0)


def test_calculate_electric_handles_negative_signed_bytes():
    # Signed-byte input (as produced by translate_bytes's sign conversion)
    # must be normalized back to unsigned before the little-endian read.
    assert RacParser._calculate_electric([-1, 0]) == pytest.approx(63.75)  # 255 * 0.25


# --- CRC16 (regression lock - see module docstring for why) -------------


def test_crc16_of_empty_input_is_the_initial_value(parser):
    # No bytes -> the bit loop never runs, so this holds by construction,
    # not just as a snapshot of current behavior.
    assert parser.crc16ccitt([]) == 0xFFFF


def test_crc16_known_value(parser):
    assert parser.crc16ccitt(list(b"123456789")) == 0x29B1


def test_add_crc16_appends_little_endian(parser):
    buf = parser.add_crc16(bytearray([1, 2, 3]))
    crc = parser.crc16ccitt([1, 2, 3])
    assert buf[-2:] == bytearray([crc & 0xFF, (crc >> 8) & 0xFF])


# --- encode/decode round trip via the shared "receive" byte layout ------


@pytest.mark.parametrize(
    "stat_kwargs",
    [
        {"Operation": True, "OperationMode": 0, "AirFlow": 0, "WindDirectionUD": 0, "WindDirectionLR": 0},
        {"Operation": True, "OperationMode": 1, "AirFlow": 1, "WindDirectionUD": 2, "WindDirectionLR": 3},
        {"Operation": True, "OperationMode": 2, "AirFlow": 4, "WindDirectionUD": 4, "WindDirectionLR": 7},
        {"Operation": False, "OperationMode": 3, "AirFlow": 2, "WindDirectionUD": 3, "WindDirectionLR": 5},
        {"Operation": True, "OperationMode": 4, "AirFlow": 3, "WindDirectionUD": 1, "WindDirectionLR": 1},
    ],
)
def test_receive_to_bytes_round_trips_through_parse_basic_settings(parser, stat_kwargs):
    stat = AirconStat(
        PresetTemp=23.5,
        Entrust=False,
        ModelNr=1,
        Vacant=True,
        CoolHotJudge=True,
        **stat_kwargs,
    )
    content = parser.receive_to_bytes(stat)
    ac = Aircon()
    parser._parse_basic_settings(ac, content)

    assert ac.Operation == stat.Operation
    assert ac.OperationMode == stat.OperationMode
    assert ac.AirFlow == stat.AirFlow
    assert ac.WindDirectionUD == stat.WindDirectionUD
    assert ac.WindDirectionLR == stat.WindDirectionLR
    assert ac.PresetTemp == stat.PresetTemp
    assert ac.Entrust == stat.Entrust
    assert ac.ModelNr == stat.ModelNr
    assert ac.Vacant == stat.Vacant
    assert ac.CoolHotJudge == stat.CoolHotJudge


def test_receive_to_bytes_entrust_round_trips(parser):
    stat = AirconStat(Operation=True, OperationMode=0, AirFlow=0, WindDirectionUD=0,
                       WindDirectionLR=0, PresetTemp=22.0, Entrust=True, ModelNr=0,
                       Vacant=False, CoolHotJudge=False)
    content = parser.receive_to_bytes(stat)
    ac = Aircon()
    parser._parse_basic_settings(ac, content)
    assert ac.Entrust is True


# --- command_to_byte(): command-direction self-clean encoding -----------
#
# IsSelfCleanOperation is *not* part of the round-trip tests above: the
# command direction (command_to_byte/receive_to_bytes, byte 12, masks
# 144/128) and the status direction (_parse_basic_settings, byte 15 bit 0)
# are genuinely different fields in the wire protocol, confirmed against
# the decompiled official app (fremde-projekte/WF-RAC/AirconStatCoder.py:
# COMMAND_OPERATION_MODE2_ON/OFF write byte 12, STATUS_OPERATION_MODE2_ON
# reads byte 15). They are not meant to mirror each other.


def _base_stat(**overrides) -> AirconStat:
    defaults = dict(
        Operation=True, OperationMode=1, AirFlow=0, WindDirectionUD=0,
        WindDirectionLR=0, PresetTemp=22.0, Entrust=False, ModelNr=1,
        Vacant=False, CoolHotJudge=True, IsSelfCleanOperation=False,
        IsSelfCleanReset=False,
    )
    defaults.update(overrides)
    return AirconStat(**defaults)


def test_command_to_byte_cool_hot_judge_false_sets_byte8(parser):
    stat_byte = parser.command_to_byte(_base_stat(CoolHotJudge=False))
    assert stat_byte[8] & 8 == 8


def test_command_to_byte_cool_hot_judge_true_leaves_byte8_bit_unset(parser):
    stat_byte = parser.command_to_byte(_base_stat(CoolHotJudge=True))
    assert stat_byte[8] & 8 == 0


def test_command_to_byte_self_clean_on_sets_byte12(parser):
    stat_byte = parser.command_to_byte(_base_stat(IsSelfCleanOperation=True, ModelNr=1))
    assert stat_byte[12] & 144 == 144


def test_command_to_byte_self_clean_off_sets_byte12(parser):
    stat_byte = parser.command_to_byte(_base_stat(IsSelfCleanOperation=False, ModelNr=1))
    assert stat_byte[12] & 144 == 128


def test_command_to_byte_self_clean_skipped_for_unsupported_model(parser):
    on = parser.command_to_byte(_base_stat(IsSelfCleanOperation=True, ModelNr=0))
    off = parser.command_to_byte(_base_stat(IsSelfCleanOperation=False, ModelNr=0))
    # ModelNr not in (1, 2): command_to_byte returns before touching the
    # self-clean bits at all, so both must produce identical byte 12.
    assert on[12] == off[12]


def test_parse_basic_settings_self_clean_reads_byte15_bit0(parser):
    ac_on = Aircon()
    content_on = [1] + [0] * 14 + [1, 0, 0]  # ModelNr=1, byte[15] bit0 set
    parser._parse_basic_settings(ac_on, content_on)
    assert ac_on.IsSelfCleanOperation is True

    ac_off = Aircon()
    content_off = [1] + [0] * 17
    parser._parse_basic_settings(ac_off, content_off)
    assert ac_off.IsSelfCleanOperation is False


def test_parse_basic_settings_compressor_running_reads_byte9_bit1(parser):
    ac_on = Aircon()
    content_on = [0] * 9 + [2] + [0] * 8  # byte[9] bit 0x02 set
    parser._parse_basic_settings(ac_on, content_on)
    assert ac_on.CompressorRunning is True

    ac_off = Aircon()
    content_off = [0] * 18
    parser._parse_basic_settings(ac_off, content_off)
    assert ac_off.CompressorRunning is False


# --- to_base64() smoke test ----------------------------------------------


def test_to_base64_roundtrips_through_b64decode(parser):
    from base64 import b64decode

    stat = _base_stat()
    encoded = parser.to_base64(stat)
    decoded = b64decode(encoded)
    # command (18 + 5 + 2) + receive (18 + 5 + 2) = 50 bytes
    assert len(decoded) == 50


def test_to_base64_wraps_failures_in_value_error(parser):
    with pytest.raises(ValueError):
        parser.to_base64(None)


def test_translate_bytes_wraps_failures_in_value_error(parser):
    with pytest.raises(ValueError):
        parser.translate_bytes("not valid base64!!!")
