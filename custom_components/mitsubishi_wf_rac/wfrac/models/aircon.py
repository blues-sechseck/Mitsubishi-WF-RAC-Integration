"""Aircon Base"""

from dataclasses import dataclass, field
from enum import StrEnum

from ..capabilities import ModelCapabilities, get_capabilities


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

    # HomeLeaveMode (Tag 248 extension segment, capability index 7) - not a
    # fixed-byte field like the ones above, encoded/decoded as a variable
    # trailer in rac_parser.py. HomeLeaveModeStatusRequest asks the unit to
    # report its current values (it omits this segment from a plain
    # getAirconStat otherwise - confirmed empirically, see #187 notes in
    # FUNDE.md); the ForCooling/ForHeating pair writes new ones. Both
    # directions verified live (05.08.2026), see todo.md.
    HomeLeaveModeStatusRequest = "HomeLeaveModeStatusRequest"
    HomeLeaveModeForCooling = "HomeLeaveModeForCooling"
    HomeLeaveModeForHeating = "HomeLeaveModeForHeating"

    # Service data (operation-data codes 0x11/0x90/0x85/0x13) - same one-shot
    # request pattern as HomeLeaveModeStatusRequest, see rac_parser.py's
    # SERVICE_DATA_CODES. Triggered periodically by Device, not by a climate
    # action - see Device._maybe_request_service_data().
    ServiceDataStatusRequest = "ServiceDataStatusRequest"


@dataclass
class HomeLeaveModeSetting:
    """One side (cooling or heating) of the HomeLeaveMode extension segment
    (Tag 248, sub-codes 27-32): temp threshold ("TempRule"), temp setting and
    airflow. AirFlow is the app's own 0=auto/1-4=volume index for this
    feature specifically - not the same encoding as the main AirFlow field.
    """

    TempRule: float
    TempSetting: float
    AirFlow: int


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
    # content[9] & 0x02 - not gated by ModelNr/Capabilities, present in every
    # ordinary status poll. See rac_parser.COMPRESSOR_RUNNING_MASK.
    CompressorRunning: bool = False
    # Raw ModelNr byte (content[0] & 127) before mapping to the known 0/1/2
    # values - kept around so an unrecognized value (ModelNr == -1) is still
    # visible as a diagnostic, e.g. for reports like #189.
    ModelNrRaw: int = 0
    # Feature availability per the app's own model_no_type table (#187),
    # looked up from ModelNrRaw - independent of the ModelNr 0/1/2 grouping
    # above, which instead reflects the wire-protocol byte layout.
    Capabilities: ModelCapabilities = field(default_factory=lambda: get_capabilities(0))
    # Populated only after a HomeLeaveModeStatusRequest round-trip (see
    # AirconCommands) - stays None otherwise, including on units that
    # support the feature but haven't been asked yet.
    HomeLeaveModeForCooling: HomeLeaveModeSetting | None = None
    HomeLeaveModeForHeating: HomeLeaveModeSetting | None = None
    # Populated only after a ServiceDataStatusRequest round-trip (see
    # AirconCommands) - stays None otherwise. Carried forward across polls by
    # Device._carry_forward_service_data(), same rationale as HomeLeaveMode
    # above: the unit reports these extension segments exactly once.
    CompressorFrequency: float | None = None  # Hz
    OperatingCurrent: float | None = None  # A
    HotGasTemp: float | None = None  # deg C
    EevPulses: int | None = None
    EevPosition: int | None = None  # % of raw pulse range 0-255, full-open pulse count unknown


@dataclass
class AirconStat(AirconBase):
    """Aircon (command) class extends AirconBase class"""

    IsSelfCleanOperation: bool = False
    IsSelfCleanReset: bool = False
    # See AirconCommands - only ever set explicitly via
    # Device.async_request_home_leave_mode_status()/async_set_home_leave_mode(),
    # never carried over from from_aircon() below.
    HomeLeaveModeStatusRequest: bool = False
    HomeLeaveModeForCooling: HomeLeaveModeSetting | None = None
    HomeLeaveModeForHeating: HomeLeaveModeSetting | None = None
    # See AirconCommands - only ever set via Device._maybe_request_service_data().
    ServiceDataStatusRequest: bool = False

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
