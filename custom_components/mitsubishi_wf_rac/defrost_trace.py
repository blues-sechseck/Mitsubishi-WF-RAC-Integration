"""In-memory trace of defrosts, for the diagnostics download.

The outdoor unit's own defrost flag (0x0C) has answered "not defrosting"
through real defrosts, so the detection rests on the indoor coil. Which units
and firmware branches agree with that is not known yet; this keeps the raw
frames around every detected defrost so a report can show it. Memory only: a
reload starts empty.
"""

from __future__ import annotations

from collections import Counter, deque
from typing import Any, Final

from pywfrac import SERVICE_DATA_DEFROST, Aircon, defrost_detected

from homeassistant.util import dt as dt_util

PRE_SAMPLES: Final = 6
POST_SAMPLES: Final = 6
MAX_EPISODE_SAMPLES: Final = 120
KEPT_EPISODES: Final = 5

# Bit 0x01 of the status byte has been seen set as a defrost ends; counted
# while a defrost is detected to learn whether it marks one.
OPERATION_STATUS_DEFROST_BIT: Final = 0x01

_DECODED_FIELDS: Final = (
    "Operation",
    "OperationMode",
    "CoolHotJudge",
    "OutdoorTemp",
    "CompressorFrequency",
    "OperatingCurrent",
    "IndoorCoilRaw",
    "IndoorCoilOutletRaw",
    "OutdoorCoilRaw",
    "EevPulses",
    "DefrostingRaw",
    "HotStartRaw",
    "OperationStatusRaw",
)


class DefrostTrace:
    """Samples of every parsed status frame, grouped around detected defrosts."""

    def __init__(self) -> None:
        """Start empty."""
        self._pre: deque[dict[str, Any]] = deque(maxlen=PRE_SAMPLES)
        self._open: list[dict[str, Any]] | None = None
        self._post_left = 0
        self._closed: deque[list[dict[str, Any]]] = deque(maxlen=KEPT_EPISODES)
        self._episodes_detected = 0
        self._defrost_answers: Counter[str] = Counter()
        self._status_values: Counter[str] = Counter()
        self._with_coil = 0
        self._without_coil = 0
        self._status_bit_while_detected = 0

    def record(self, airco: Aircon, raw_frame: str, source: str) -> None:
        """Take a sample from one parsed frame, with its carried-forward values."""
        detected = defrost_detected(airco)
        sample: dict[str, Any] = {
            "timestamp": dt_util.utcnow().isoformat(),
            "source": source,
            "airconStat": raw_frame,
            **{name: getattr(airco, name, None) for name in _DECODED_FIELDS},
            "defrost_detected": detected,
        }
        self._count(airco, detected)

        if self._open is None:
            if detected is True:
                self._episodes_detected += 1
                self._open = [*self._pre, sample]
                self._pre.clear()
                self._post_left = POST_SAMPLES
            else:
                self._pre.append(sample)
            return

        if len(self._open) < MAX_EPISODE_SAMPLES:
            self._open.append(sample)
        if detected is True:
            self._post_left = POST_SAMPLES
            return
        self._post_left -= 1
        if self._post_left <= 0:
            self._closed.append(self._open)
            self._open = None

    def _count(self, airco: Aircon, detected: bool | None) -> None:
        if airco.IndoorCoilRaw is None:
            self._without_coil += 1
        else:
            self._with_coil += 1
        status = airco.OperationStatusRaw
        if status is not None:
            self._status_values[f"0x{status:02X}"] += 1
            if detected is True and status & OPERATION_STATUS_DEFROST_BIT:
                self._status_bit_while_detected += 1
        # Only a 0x0C segment that arrived in this very frame; the carried
        # value would count the same answer once per poll.
        segment = airco.ServiceDataRaw.get(SERVICE_DATA_DEFROST)
        if segment is not None:
            self._defrost_answers[f"0x{segment[0]:02X}"] += 1

    def as_dict(self) -> dict[str, Any]:
        """The trace for the diagnostics download."""
        episodes = [list(episode) for episode in self._closed]
        if self._open is not None:
            episodes.append(list(self._open))
        return {
            "episodes_detected": self._episodes_detected,
            "defrost_flag_answers": dict(self._defrost_answers),
            "operation_status_values": dict(self._status_values),
            "samples_with_indoor_coil": self._with_coil,
            "samples_without_indoor_coil": self._without_coil,
            "operation_status_bit_0x01_while_detected": self._status_bit_while_detected,
            "episodes": episodes,
            "open_episode": self._open is not None,
        }
