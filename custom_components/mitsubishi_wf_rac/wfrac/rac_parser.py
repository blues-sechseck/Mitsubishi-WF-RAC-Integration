"""WF-RAC parser to decode and encode wf-rac strings"""

from base64 import b64decode, b64encode
from typing import Final
from .utils import find_match, indoorTempList, outdoorTempList
from .models.aircon import Aircon, AirconStat

# Constants
INITIAL_BYTE_ARRAY: Final = bytearray([0, 0, 0, 0, 0, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
VARIABLE_SUFFIX: Final = bytearray([1, 255, 255, 255, 255])
CRC_POLYNOMIAL: Final = 4129
CRC_INITIAL: Final = 65535

# Bit masks
OPERATION_MASK: Final = 3
MODE_MASKS: Final = {
    0: 32,
    1: 40,
    2: 48,
    3: 44,
    4: 36
}

class RacParser:
    """Parser class that is used to parse WF-RAC data"""

    def to_base64(self, aircon_stat: AirconStat) -> str:
        """Convert AirconStat to Base64 string."""
        try:
            command = self.add_crc16(self.add_variable(self.command_to_byte(aircon_stat)))
            receive = self.add_crc16(self.add_variable(self.receive_to_bytes(aircon_stat)))
            return str(b64encode(bytes(command + receive)))[2:-1]
        except Exception as e:
            raise ValueError(f"Failed to encode aircon state: {e}") from e

    def add_variable(self, byte_buffer: bytearray) -> bytearray:
        """Concat byte_buffer with variable suffix."""
        return byte_buffer + VARIABLE_SUFFIX

    def command_to_byte(self, _aircon_stat: AirconStat) -> bytearray:
        """Command to bytes"""

        aircon_stat: AirconStat = _aircon_stat
        stat_byte: bytearray = bytearray(
            [0, 0, 0, 0, 0, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        )

        # On/Off
        if aircon_stat.Operation:
            stat_byte[2] |= 3
        else:
            stat_byte[2] |= 2

        # Operating Mode
        if aircon_stat.OperationMode == 0:
            stat_byte[2] |= 32
        elif aircon_stat.OperationMode == 1:
            stat_byte[2] |= 40
        elif aircon_stat.OperationMode == 2:
            stat_byte[2] |= 48
        elif aircon_stat.OperationMode == 3:
            stat_byte[2] |= 44
        elif aircon_stat.OperationMode == 4:
            stat_byte[2] |= 36

        # airflow
        if aircon_stat.AirFlow == 0:
            stat_byte[3] |= 15
        elif aircon_stat.AirFlow == 1:
            stat_byte[3] |= 8
        elif aircon_stat.AirFlow == 2:
            stat_byte[3] |= 9
        elif aircon_stat.AirFlow == 3:
            stat_byte[3] |= 10
        elif aircon_stat.AirFlow == 4:
            stat_byte[3] |= 14

        # Vertical wind direction
        if aircon_stat.WindDirectionUD == 0:
            stat_byte[2] |= 192
            stat_byte[3] |= 128
        elif aircon_stat.WindDirectionUD == 1:
            stat_byte[2] |= 128
            stat_byte[3] |= 128
        elif aircon_stat.WindDirectionUD == 2:
            stat_byte[2] |= 128
            stat_byte[3] |= 144
        elif aircon_stat.WindDirectionUD == 3:
            stat_byte[2] |= 128
            stat_byte[3] |= 160
        elif aircon_stat.WindDirectionUD == 4:
            stat_byte[2] |= 128
            stat_byte[3] |= 176

        # Horizontal wind direction
        if aircon_stat.WindDirectionLR == 0:
            stat_byte[12] |= 3
            stat_byte[11] |= 16
        elif aircon_stat.WindDirectionLR == 1:
            stat_byte[12] |= 2
            stat_byte[11] |= 16
        elif aircon_stat.WindDirectionLR == 2:
            stat_byte[12] |= 2
            stat_byte[11] |= 17
        elif aircon_stat.WindDirectionLR == 3:
            stat_byte[12] |= 2
            stat_byte[11] |= 18
        elif aircon_stat.WindDirectionLR == 4:
            stat_byte[12] |= 2
            stat_byte[11] |= 19
        elif aircon_stat.WindDirectionLR == 5:
            stat_byte[12] |= 2
            stat_byte[11] |= 20
        elif aircon_stat.WindDirectionLR == 6:
            stat_byte[12] |= 2
            stat_byte[11] |= 21
        elif aircon_stat.WindDirectionLR == 7:
            stat_byte[12] |= 2
            stat_byte[11] |= 22

        # preset temp
        # preset_temp = 25.0 if aircon_stat.OperationMode == 3 else aircon_stat.PresetTemp
        preset_temp = aircon_stat.PresetTemp
        stat_byte[4] |= int(preset_temp / 0.5) + 128

        # entrust
        if not aircon_stat.Entrust:
            stat_byte[12] |= 8
        else:
            stat_byte[12] |= 12

        if not aircon_stat.CoolHotJudge:
            stat_byte[8] |= 8

        if aircon_stat.ModelNr == 1:
            stat_byte[10] |= 1 if aircon_stat.Vacant else 0

        if aircon_stat.ModelNr != 1 and aircon_stat.ModelNr != 2:
            return stat_byte

        stat_byte[10] |= 4 if aircon_stat.IsSelfCleanReset else 0
        stat_byte[10] |= 144 if aircon_stat.IsSelfCleanOperation else 128

        return stat_byte

    def receive_to_bytes(self, _aircon_stat: AirconStat) -> bytearray:
        """Receive command to bytes"""

        aircon_stat: AirconStat = _aircon_stat
        stat_byte: bytearray = bytearray(
            [0, 0, 0, 0, 0, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        )

        # On/Off
        if aircon_stat.Operation:
            stat_byte[2] |= 1

        # Operating Mode
        if aircon_stat.OperationMode == 1:
            stat_byte[2] |= 8
        elif aircon_stat.OperationMode == 2:
            stat_byte[2] |= 16
        elif aircon_stat.OperationMode == 3:
            stat_byte[2] |= 12
        elif aircon_stat.OperationMode == 4:
            stat_byte[2] |= 4

        # airflow
        if aircon_stat.AirFlow == 0:
            stat_byte[3] |= 7
        elif aircon_stat.AirFlow == 2:
            stat_byte[3] |= 1
        elif aircon_stat.AirFlow == 3:
            stat_byte[3] |= 2
        elif aircon_stat.AirFlow == 4:
            stat_byte[3] |= 6

        # Vertical wind direction
        if aircon_stat.WindDirectionUD == 0:
            stat_byte[2] |= 64
        elif aircon_stat.WindDirectionUD == 2:
            stat_byte[3] |= 16
        elif aircon_stat.WindDirectionUD == 3:
            stat_byte[3] |= 32
        elif aircon_stat.WindDirectionUD == 4:
            stat_byte[3] |= 48

        # Horizontal wind direction
        if aircon_stat.WindDirectionLR == 0:
            stat_byte[12] |= 1
        elif aircon_stat.WindDirectionLR == 1:
            stat_byte[11] |= 0
        elif aircon_stat.WindDirectionLR == 2:
            stat_byte[11] |= 1
        elif aircon_stat.WindDirectionLR == 3:
            stat_byte[11] |= 2
        elif aircon_stat.WindDirectionLR == 4:
            stat_byte[11] |= 3
        elif aircon_stat.WindDirectionLR == 5:
            stat_byte[11] |= 4
        elif aircon_stat.WindDirectionLR == 6:
            stat_byte[11] |= 5
        elif aircon_stat.WindDirectionLR == 7:
            stat_byte[11] |= 6

        # preset temp
        # preset_temp = 25.0 if aircon_stat.OperationMode == 3 else aircon_stat.PresetTemp
        preset_temp = aircon_stat.PresetTemp
        stat_byte[4] |= int(preset_temp / 0.5)

        # entrust
        if aircon_stat.Entrust:
            stat_byte[12] |= 4

        if not aircon_stat.CoolHotJudge:
            stat_byte[8] |= 8

        if aircon_stat.ModelNr == 1:
            stat_byte[0] |= 1
        elif aircon_stat.ModelNr == 2:
            stat_byte[0] |= 2

        if aircon_stat.ModelNr == 1:
            stat_byte[10] |= 1 if aircon_stat.Vacant else 0

        if aircon_stat.ModelNr not in (1, 2):
            return stat_byte

        stat_byte[15] |= 1 if aircon_stat.IsSelfCleanOperation else 0

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
        ac_device.ModelNr = find_match(content[0] & 127, 0, 1, 2)
        ac_device.Vacant = (content[10] & 1) != 0
        code = content[6] & 127
        ac_device.ErrorCode = (
            "00"
            if code == 0
            else f"M{code:02d}"
            if (content[6] & -128) <= 0
            else "E" + str(code)
        )

    def _parse_temperatures(self, ac_device: Aircon, vals: list[int]) -> None:
        """Parse temperature and electric values."""
        ac_device.Electric = None
        for i in range(0, len(vals), 4):
            if vals[i] == -128:
                if vals[i + 1] == 16:
                    ac_device.OutdoorTemp = outdoorTempList[vals[i + 2] & 0xFF]
                elif vals[i + 1] == 32:
                    ac_device.IndoorTemp = indoorTempList[vals[i + 2] & 0xFF]
            elif vals[i] == -108 and vals[i + 1] == 16:
                ac_device.Electric = self._calculate_electric(vals[i + 2:i + 4])

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
