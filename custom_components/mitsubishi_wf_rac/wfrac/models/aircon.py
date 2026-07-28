"""Aircon Base"""

from aenum import StrEnum


class AirconCommands(StrEnum):
    """Enum of all the supported airco commands"""

    Operation = "Operation"
    OperationMode = "OperationMode"
    AirFlow = "AirFlow"
    WindDirectionUD = "WindDirectionUD"
    WindDirectionLR = "WindDirectionLR"
    PresetTemp = "PresetTemp"
    Entrust = "Entrust"
    # CoolHotJudge = ''

    # Vacant = ''
    # ModelNr = ''
    # IsSelfCleanOperation = ''
    # IsSelfCleanReset = ''


class AirconBase:
    """Base class of the aircon class"""

    Operation: bool = False
    OperationMode: int = 0
    AirFlow: int = 0
    WindDirectionUD: int = 0
    WindDirectionLR: int = 0
    PresetTemp: float = 18.0
    Entrust: bool = False
    ModelNr: int = 0
    Vacant: bool = False
    CoolHotJudge: bool = False


class Aircon(AirconBase):
    """Aircon (recieve) class extends AirconBase class"""

    IndoorTemp: float = 0.0
    OutdoorTemp: float = 0.0
    Electric: float | None = None
    ErrorCode: str = ""


class AirconStat(AirconBase):
    """Aircon (command) class extends AirconBase class"""

    def __init__(self, aircon: Aircon) -> None:
        self.Operation = aircon.Operation
        self.OperationMode = aircon.OperationMode
        self.AirFlow = aircon.AirFlow
        self.WindDirectionUD = aircon.WindDirectionUD
        self.WindDirectionLR = aircon.WindDirectionLR
        self.PresetTemp = aircon.PresetTemp
        self.Entrust = aircon.Entrust
        self.ModelNr = aircon.ModelNr
        self.Vacant = aircon.Vacant
        self.CoolHotJudge = aircon.CoolHotJudge
        self.IsSelfCleanOperation = False
        self.IsSelfCleanReset = False

    IsSelfCleanOperation: bool = False
    IsSelfCleanReset: bool = False
