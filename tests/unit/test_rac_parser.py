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

from custom_components.mitsubishi_wf_rac.wfrac.models.aircon import Aircon, AirconStat
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
    # ZT series (2026 model line, see #189) - same feature set as ModelNr 2,
    # but a different raw byte value.
    ac = Aircon()
    content = [3] + [0] * 17
    parser._parse_basic_settings(ac, content)
    assert ac.ModelNrRaw == 3
    assert ac.ModelNr == 2


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
