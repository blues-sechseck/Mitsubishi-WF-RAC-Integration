"""WF-RAC parser to decode and encode wf-rac strings"""

import logging
from base64 import b64decode, b64encode
from typing import Final

from .capabilities import get_capabilities
from .utils import find_match, indoorTempList, outdoorTempList
from .models.aircon import Aircon, AirconStat, HomeLeaveModeSetting

_LOGGER = logging.getLogger(__name__)

# Constants
VARIABLE_SUFFIX: Final = bytearray([1, 255, 255, 255, 255])
CRC_POLYNOMIAL: Final = 4129
CRC_INITIAL: Final = 65535

# --- HomeLeaveMode extension segment (Tag 248, capability index 7) ---
# Ground truth: AirconStatCoder.java (byteToStat/addCommandVariableData) in
# the official app, see FUNDE.md. Same 4-byte tag/sub/value scheme as
# OutdoorTemp/IndoorTemp (tag -128) and Electric (tag -108) below, decoded by
# the same _parse_temperatures() loop - 248 as a signed byte is -8.
HOME_LEAVE_MODE_TAG_SIGNED: Final = -8
HOME_LEAVE_MODE_TAG_BYTE: Final = 248
HOME_LEAVE_MODE_READ_MARKER: Final = 16  # matches the other tags' status marker
HOME_LEAVE_MODE_STATUS_REQUEST_MARKER: Final = 255  # "report current values"
HOME_LEAVE_MODE_SET_MARKER: Final = 0  # "apply these values"
# Sub-code order: cool/heat TempRule, cool/heat TempSetting, cool/heat AirFlow.
HOME_LEAVE_MODE_SUBCODES: Final = (27, 28, 29, 30, 31, 32)
# Raw extension byte <-> the app's own 0=auto/1-4=volume index for this
# feature - unrelated to CMD_AIRFLOW_MASKS/RCV_AIRFLOW_MASKS above, which
# encode the main AirFlow field.
HOME_LEAVE_MODE_AIRFLOW_BYTES: Final = (0, 3, 5, 7, 14)

# --- service data extension segments (operation-data codes) ---
# Ground truth: live-measured against the official app's own service-data
# screen equivalent (there isn't one - MHI-AC-Ctrl's code/formula naming),
# cross-checked in a load test and a batched request, see
# wf-rac-module-reference.md §5.4 and todo.md. Unlike HomeLeaveMode's Tag 248,
# the second byte carries data (part of the value, see the frequency formula
# below) rather than a fixed status marker - do not gate on it like
# HOME_LEAVE_MODE_READ_MARKER.
SERVICE_DATA_COMPRESSOR_FREQ: Final = 0x11
SERVICE_DATA_OPERATING_CURRENT: Final = 0x90
SERVICE_DATA_HOT_GAS_TEMP: Final = 0x85
SERVICE_DATA_EEV_PULSES: Final = 0x13
SERVICE_DATA_CODES: Final = (
    SERVICE_DATA_COMPRESSOR_FREQ,
    SERVICE_DATA_OPERATING_CURRENT,
    SERVICE_DATA_HOT_GAS_TEMP,
    SERVICE_DATA_EEV_PULSES,
)

# Bit masks
OPERATION_MASK: Final = 3
# Not in any vendor doc - correlated live against operation-data code 0x11
# (compressor frequency), see wf-rac-module-reference.md §4.6. Distinguishes
# "unit on" from "compressor actually running" (e.g. temperature satisfied).
COMPRESSOR_RUNNING_MASK: Final = 2

# --- command (send) lookup tables ---
CMD_MODE_MASKS: Final = {0: 32, 1: 40, 2: 48, 3: 44, 4: 36}
CMD_AIRFLOW_MASKS: Final = {0: 15, 1: 8, 2: 9, 3: 10, 4: 14}
# WindDirectionUD -> (byte2 mask, byte3 mask)
CMD_WIND_UD_MASKS: Final = {
    0: (192, 128),
    1: (128, 128),
    2: (128, 144),
    3: (128, 160),
    4: (128, 176),
}
# WindDirectionLR -> (byte12 mask, byte11 mask)
CMD_WIND_LR_MASKS: Final = {
    0: (3, 16),
    1: (2, 16),
    2: (2, 17),
    3: (2, 18),
    4: (2, 19),
    5: (2, 20),
    6: (2, 21),
    7: (2, 22),
}

# --- receive lookup tables ---
RCV_MODE_MASKS: Final = {0: 0, 1: 8, 2: 16, 3: 12, 4: 4}
RCV_AIRFLOW_MASKS: Final = {0: 7, 1: 0, 2: 1, 3: 2, 4: 6}


def _empty_stat_bytes() -> bytearray:
    return bytearray([0, 0, 0, 0, 0, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])


class RacParser:
    """Parser class that is used to parse WF-RAC data"""

    def to_base64(self, aircon_stat: AirconStat) -> str:
        """Convert AirconStat to Base64 string."""
        try:
            command = self.add_crc16(
                self.command_to_byte(aircon_stat) + self._variable_trailer(aircon_stat)
            )
            receive = self.add_crc16(self.add_variable(self.receive_to_bytes(aircon_stat)))
            return b64encode(bytes(command + receive)).decode("ascii")
        except Exception as e:
            raise ValueError(f"Failed to encode aircon state: {e}") from e

    def add_variable(self, byte_buffer: bytearray) -> bytearray:
        """Concat byte_buffer with variable suffix."""
        return byte_buffer + VARIABLE_SUFFIX

    @staticmethod
    def _build_trailer(segments: list[tuple[int, int, int, int]]) -> bytearray:
        """Generic variable-trailer encoding: a count byte followed by that
        many 4-byte (tag, op1, op2, op3) segments, or the plain "nothing to
        send" sentinel if there's nothing to encode."""
        if not segments:
            return VARIABLE_SUFFIX
        trailer = bytearray([len(segments)])
        for segment in segments:
            trailer += bytearray(b & 0xFF for b in segment)
        return trailer

    @classmethod
    def _variable_trailer(cls, aircon_stat: AirconStat) -> bytearray:
        """Variable trailer for the command stream - whichever one-shot
        extension request/set is queued on aircon_stat (HomeLeaveMode,
        service data), or the plain sentinel otherwise (the default for every
        command that doesn't touch either, including every command this
        integration sent before these features existed). Mirrors
        AirconStatCoder.addCommandVariableData(); unverified on real hardware
        except where noted - see todo.md.
        """
        if aircon_stat.HomeLeaveModeStatusRequest:
            segments = [
                (HOME_LEAVE_MODE_TAG_BYTE, HOME_LEAVE_MODE_STATUS_REQUEST_MARKER, sub, 0)
                for sub in HOME_LEAVE_MODE_SUBCODES
            ]
            return cls._build_trailer(segments)

        if (
            aircon_stat.HomeLeaveModeForCooling is not None
            and aircon_stat.HomeLeaveModeForHeating is not None
        ):
            cooling = aircon_stat.HomeLeaveModeForCooling
            heating = aircon_stat.HomeLeaveModeForHeating
            values = (
                int(cooling.TempRule * 2),
                int(heating.TempRule * 2),
                int(cooling.TempSetting * 2),
                int(heating.TempSetting * 2),
                HOME_LEAVE_MODE_AIRFLOW_BYTES[cooling.AirFlow],
                HOME_LEAVE_MODE_AIRFLOW_BYTES[heating.AirFlow],
            )
            segments = [
                (HOME_LEAVE_MODE_TAG_BYTE, HOME_LEAVE_MODE_SET_MARKER, sub, value)
                for sub, value in zip(HOME_LEAVE_MODE_SUBCODES, values)
            ]
            return cls._build_trailer(segments)

        if aircon_stat.ServiceDataStatusRequest:
            # OP1=255 -> "report current value" (see rac_parser docstring/
            # CLAUDE.md guardrail: never 0, that would be a write to the
            # climate MCU). Confirmed live (06.08.2026) to answer all four in
            # the direct setAirconStat response - see todo.md.
            segments = [(code, 255, 255, 255) for code in SERVICE_DATA_CODES]
            return cls._build_trailer(segments)

        return VARIABLE_SUFFIX

    def command_to_byte(self, aircon_stat: AirconStat) -> bytearray:
        """Command to bytes"""
        stat_byte = _empty_stat_bytes()

        # On/Off
        stat_byte[2] |= 3 if aircon_stat.Operation else 2

        # Operating Mode
        stat_byte[2] |= CMD_MODE_MASKS.get(aircon_stat.OperationMode, 0)

        # Airflow
        stat_byte[3] |= CMD_AIRFLOW_MASKS.get(aircon_stat.AirFlow, 0)

        # Vertical wind direction
        mask2, mask3 = CMD_WIND_UD_MASKS.get(aircon_stat.WindDirectionUD, (0, 0))
        stat_byte[2] |= mask2
        stat_byte[3] |= mask3

        # Horizontal wind direction
        mask12, mask11 = CMD_WIND_LR_MASKS.get(aircon_stat.WindDirectionLR, (0, 0))
        stat_byte[12] |= mask12
        stat_byte[11] |= mask11

        # Preset temp
        stat_byte[4] |= int(aircon_stat.PresetTemp / 0.5) + 128

        # Entrust (3D auto)
        stat_byte[12] |= 12 if aircon_stat.Entrust else 8

        if not aircon_stat.CoolHotJudge:
            stat_byte[8] |= 8

        if aircon_stat.ModelNr == 1:
            stat_byte[10] |= 1 if aircon_stat.Vacant else 0

        if aircon_stat.ModelNr not in (1, 2):
            return stat_byte

        stat_byte[10] |= 4 if aircon_stat.IsSelfCleanReset else 0
        # Byte 12, not 10 - confirmed against the decompiled official app
        # (fremde-projekte/WF-RAC's COMMAND_OPERATION_MODE2_ON/OFF). Byte 10
        # here only carries Vacant/SelfCleanReset.
        stat_byte[12] |= 144 if aircon_stat.IsSelfCleanOperation else 128

        return stat_byte

    def receive_to_bytes(self, aircon_stat: AirconStat) -> bytearray:
        """Receive command to bytes"""
        stat_byte = _empty_stat_bytes()

        # On/Off
        if aircon_stat.Operation:
            stat_byte[2] |= 1

        # Operating Mode
        stat_byte[2] |= RCV_MODE_MASKS.get(aircon_stat.OperationMode, 0)

        # Airflow
        stat_byte[3] |= RCV_AIRFLOW_MASKS.get(aircon_stat.AirFlow, 0)

        # Vertical wind direction
        if aircon_stat.WindDirectionUD == 0:
            stat_byte[2] |= 64
        elif aircon_stat.WindDirectionUD in (2, 3, 4):
            stat_byte[3] |= (aircon_stat.WindDirectionUD - 1) * 16

        # Horizontal wind direction
        if aircon_stat.WindDirectionLR == 0:
            stat_byte[12] |= 1
        elif 1 <= aircon_stat.WindDirectionLR <= 7:
            stat_byte[11] |= aircon_stat.WindDirectionLR - 1

        # Preset temp
        stat_byte[4] |= int(aircon_stat.PresetTemp / 0.5)

        # Entrust (3D auto)
        if aircon_stat.Entrust:
            stat_byte[12] |= 4

        if not aircon_stat.CoolHotJudge:
            stat_byte[8] |= 8

        if aircon_stat.ModelNr in (1, 2):
            stat_byte[0] |= aircon_stat.ModelNr

        if aircon_stat.ModelNr == 1:
            stat_byte[10] |= 1 if aircon_stat.Vacant else 0

        if aircon_stat.ModelNr not in (1, 2):
            return stat_byte

        # Same byte 12 encoding as command_to_byte - the decompiled official
        # app writes this field into both segments identically.
        stat_byte[12] |= 144 if aircon_stat.IsSelfCleanOperation else 128

        return stat_byte

    def translate_bytes(self, input_string: str) -> Aircon:
        """Translate base64 string to Aircon object."""
        try:
            ac_device = Aircon()
            content_byte_array = b64decode(bytearray(input_string, encoding="UTF-8"))
            signed_array = [(256 - a) * (-1) if a > 127 else a for a in content_byte_array]

            start_length = signed_array[18] * 4 + 21
            content = signed_array[start_length:start_length + 18]

            self._parse_basic_settings(ac_device, content)
            self._parse_temperatures(ac_device, signed_array[start_length + 19:-2])

            return ac_device
        except Exception as e:
            raise ValueError(f"Failed to decode input string: {e}") from e

    def _parse_basic_settings(self, ac_device: Aircon, content: list[int]) -> None:
        """Parse basic AC settings from content."""
        ac_device.Operation = 1 == (OPERATION_MASK & content[2])
        ac_device.PresetTemp = content[4] / 2
        ac_device.OperationMode = find_match(60 & content[2], 8, 16, 12, 4) + 1
        ac_device.AirFlow = find_match(15 & content[3], 7, 0, 1, 2, 6)
        ac_device.WindDirectionUD = (
            0
            if content[2] & 192 == 64
            else find_match(240 & content[3], 0, 16, 32, 48) + 1
        )
        ac_device.WindDirectionLR = (
            0
            if content[12] & 3 == 1
            else find_match(31 & content[11], 0, 1, 2, 3, 4, 5, 6) + 1
        )
        ac_device.Entrust = 4 == (12 & content[12])
        ac_device.CoolHotJudge = (content[8] & 8) <= 0
        ac_device.ModelNrRaw = content[0] & 127
        ac_device.Capabilities = get_capabilities(ac_device.ModelNrRaw)
        if ac_device.ModelNrRaw == 3:
            # ZT series (new 2026 model line) - confirmed via #189 to use the
            # same wire-protocol byte layout as ModelNr 2 (self-clean bits
            # etc.). This grouping is protocol-only, not a feature-capability
            # claim: per #187's capability table (see Capabilities above),
            # ZT-2025 *does* have VacantProperty - unlike real ModelNr 2 units
            # - so occupancy/Home Leave gating must use Capabilities, not
            # this ModelNr value.
            ac_device.ModelNr = 2
        else:
            ac_device.ModelNr = find_match(ac_device.ModelNrRaw, 0, 1, 2)
            if ac_device.ModelNr == -1:
                _LOGGER.debug(
                    "Unrecognized ModelNr raw byte %d (content[0]=%d) - "
                    "model-gated features (occupancy, Home Leave) will be "
                    "unavailable",
                    ac_device.ModelNrRaw,
                    content[0],
                )
        ac_device.Vacant = (content[10] & 1) != 0
        ac_device.CompressorRunning = (content[9] & COMPRESSOR_RUNNING_MASK) != 0
        if ac_device.ModelNr in (1, 2):
            # Mirrors the self-clean bit written in receive_to_bytes() above.
            # No longer exposed as an entity: the real cycle can only be
            # started locally via the IR remote, the WiFi module offers no way
            # to trigger it (see #209). Kept because it's read-only and would
            # be needed again if a triggerable path ever turns up.
            ac_device.IsSelfCleanOperation = (content[15] & 1) != 0
        code = content[6] & 127
        ac_device.ErrorCode = (
            f"M{code:02d}"
            if content[6] < 0
            else "00"
            if code == 0
            else "E" + str(code)
        )

    def _parse_temperatures(self, ac_device: Aircon, vals: list[int]) -> None:
        """Parse temperature, electric and HomeLeaveMode values."""
        ac_device.Electric = None
        home_leave_mode_raw: dict[int, int] = {}
        # `len(vals) - 3` (not `len(vals)`) so a trailing partial 4-byte segment
        # (segment length not a multiple of 4) can't make vals[i+1]/[i+2]/[i+3]
        # below raise IndexError.
        for i in range(0, len(vals) - 3, 4):
            if vals[i] == -128:
                if vals[i + 1] == 16:
                    ac_device.OutdoorTemp = outdoorTempList[vals[i + 2] & 0xFF]
                elif vals[i + 1] == 32:
                    ac_device.IndoorTemp = indoorTempList[vals[i + 2] & 0xFF]
                else:
                    self._log_unknown_segment(vals, i)
            elif vals[i] == -108 and vals[i + 1] == 16:
                ac_device.Electric = self._calculate_electric(vals[i + 2:i + 4])
            elif (
                vals[i] == HOME_LEAVE_MODE_TAG_SIGNED
                and vals[i + 1] == HOME_LEAVE_MODE_READ_MARKER
            ):
                home_leave_mode_raw[vals[i + 2] & 0xFF] = vals[i + 3] & 0xFF
            elif (vals[i] & 0xFF) in SERVICE_DATA_CODES:
                self._apply_service_data_segment(
                    ac_device, vals[i] & 0xFF, vals[i + 1] & 0xFF, vals[i + 2] & 0xFF
                )
            else:
                self._log_unknown_segment(vals, i)
        self._apply_home_leave_mode(ac_device, home_leave_mode_raw)

    @staticmethod
    def _apply_home_leave_mode(ac_device: Aircon, raw: dict[int, int]) -> None:
        """Populate HomeLeaveMode fields once all six sub-codes were seen -
        mirrors AirconStatCoder.byteToStat's all-or-nothing commit. Absent
        (e.g. a plain poll without a prior status request, see
        AirconCommands.HomeLeaveModeStatusRequest) leaves both None."""
        if not all(sub in raw for sub in HOME_LEAVE_MODE_SUBCODES):
            return
        ac_device.HomeLeaveModeForCooling = HomeLeaveModeSetting(
            TempRule=raw[27] / 2,
            TempSetting=raw[29] / 2,
            AirFlow=find_match(raw[31] & 15, *HOME_LEAVE_MODE_AIRFLOW_BYTES),
        )
        ac_device.HomeLeaveModeForHeating = HomeLeaveModeSetting(
            TempRule=raw[28] / 2,
            TempSetting=raw[30] / 2,
            AirFlow=find_match(raw[32] & 15, *HOME_LEAVE_MODE_AIRFLOW_BYTES),
        )

    @staticmethod
    def _apply_service_data_segment(ac_device: Aircon, code: int, op1: int, op2: int) -> None:
        """Decode one operation-data segment (see SERVICE_DATA_CODES).
        Formulas and op1/op2 naming from MHI-AC-Ctrl, cross-checked live
        against a load test (varying compressor frequency) and a batched
        request - see wf-rac-module-reference.md §5.4/todo.md. Op3 carries no
        known data for any of these four codes."""
        if code == SERVICE_DATA_COMPRESSOR_FREQ:
            ac_device.CompressorFrequency = (op1 - 0x10) * 25.6 + 0.1 * op2
        elif code == SERVICE_DATA_OPERATING_CURRENT:
            ac_device.OperatingCurrent = op2 * 14 / 51
        elif code == SERVICE_DATA_HOT_GAS_TEMP:
            ac_device.HotGasTemp = op2 / 2 + 32
        elif code == SERVICE_DATA_EEV_PULSES:
            ac_device.EevPulses = op2
            ac_device.EevPosition = round(op2 * 100 / 255)

    @staticmethod
    def _log_unknown_segment(vals: list[int], i: int) -> None:
        """Log unknown airconStat segments to help future reverse engineering."""
        _LOGGER.debug(
            "Unknown airconStat segment: tag=%s sub=%s data=%s",
            vals[i],
            vals[i + 1],
            vals[i + 2:i + 4],
        )

    @staticmethod
    def _calculate_electric(values: list[int]) -> float:
        """Calculate electric value from bytes."""
        return int.from_bytes([(v + 256) % 256 for v in values], "little", signed=False) * 0.25

    def crc16ccitt(self, data: list[int]) -> int:
        """Compute CRC16-CCITT checksum."""
        crc = CRC_INITIAL
        for byte in [(256 - a) * (-1) if a > 127 else a for a in data]:
            for bit in range(8):
                should_xor = ((byte >> (7 - bit)) & 1) == 1
                if ((crc >> 15) & 1) == 1:
                    should_xor = not should_xor
                crc = (crc << 1) & 0xFFFF
                if should_xor:
                    crc ^= CRC_POLYNOMIAL
        return crc

    def add_crc16(self, byte_buffer: bytearray) -> bytearray:
        """Add crc to buffer"""
        crc = self.crc16ccitt(list(byte_buffer))

        crc_bytes = bytearray([crc & 255, (crc >> 8) & 255])  # Convert CRC to bytearray
        return byte_buffer + crc_bytes
