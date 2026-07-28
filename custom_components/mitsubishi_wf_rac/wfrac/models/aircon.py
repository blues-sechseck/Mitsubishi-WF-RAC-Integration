"""Aircon Base"""

from dataclasses import dataclass
from enum import StrEnum


class AirconCommands(StrEnum):
    """Enum of all the supported airco commands"""

    Operation = "Operation"
    OperationMode = "OperationMode"
    AirFlow = "AirFlow"
    WindDirectionUD = "WindDirectionUD"
    WindDirectionLR = "WindDirectionLR"
    PresetTemp = "PresetTemp"
    Entrust = "Entrust"
    IsSelfCleanOperation = "IsSelfCleanOperation"
    IsSelfCleanReset = "IsSelfCleanReset"
    # CoolHotJudge = ''

    # Vacant = ''
    # ModelNr = ''


@dataclass
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


@dataclass
class Aircon(AirconBase):
    """Aircon (receive) class extends AirconBase class"""

    IndoorTemp: float = 0.0
    OutdoorTemp: float = 0.0
    Electric: float | None = None
    ErrorCode: str = ""
    IsSelfCleanOperation: bool = False


@dataclass
class AirconStat(AirconBase):
    """Aircon (command) class extends AirconBase class"""

    IsSelfCleanOperation: bool = False
    IsSelfCleanReset: bool = False

    @classmethod
    def from_aircon(cls, aircon: Aircon) -> "AirconStat":
        """Create a command object seeded from the current (received) state."""
        return cls(
            Operation=aircon.Operation,
            OperationMode=aircon.OperationMode,
            AirFlow=aircon.AirFlow,
            WindDirectionUD=aircon.WindDirectionUD,
            WindDirectionLR=aircon.WindDirectionLR,
            PresetTemp=aircon.PresetTemp,
            Entrust=aircon.Entrust,
            ModelNr=aircon.ModelNr,
            Vacant=aircon.Vacant,
            CoolHotJudge=aircon.CoolHotJudge,
            IsSelfCleanOperation=aircon.IsSelfCleanOperation,
            IsSelfCleanReset=False,
        )
