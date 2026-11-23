"""Trace of defrosts, for the diagnostics download.

The outdoor unit's own defrost flag (0x0C) has answered "not defrosting"
through real defrosts, so detection rests on two other signals: bit 0x01 of
the status byte (content[9]) and the indoor coil thermistor, see
pywfrac.defrost_detected(). Which units and firmware branches show each is not
known yet. This keeps the raw frames around every detected defrost, and every
sample records what each signal said on its own, so a report shows which one
fired and which one was late or silent. The counters in signals_while_heating
sum that up over all heating frames in which at least one signal was set.

The closed episodes and the counters are stored, so they survive restarts and
reloads (a defrost is an hours-apart event and the report is read afterwards).
The pre-buffer is not stored; an episode still open when the entry stops is
restored as closed.
"""

from __future__ import annotations

from collections import Counter, deque
from typing import Any, Final

from pywfrac import (
    SERVICE_DATA_DEFROST,
    Aircon,
    defrost_detected,
    defrost_from_indoor_coil,
    defrost_status_bit,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

STORE_VERSION: Final = 1
SAVE_DELAY: Final = 60

PRE_SAMPLES: Final = 6
POST_SAMPLES: Final = 6
MAX_EPISODE_SAMPLES: Final = 120
KEPT_EPISODES: Final = 5

SIGNAL_KEYS: Final = ("bit_and_coil", "bit_only", "coil_only")

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


def create_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """The store a config entry keeps its trace in."""
    return Store(
        hass, STORE_VERSION, f"{DOMAIN}.defrost_trace.{entry_id}", private=True
    )


async def async_remove_stored_trace(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the stored trace of an entry that is being removed."""
    await create_store(hass, entry_id).async_remove()


class DefrostTrace:
    """Samples of every parsed status frame, grouped around detected defrosts."""

    def __init__(self, store: Store[dict[str, Any]] | None = None) -> None:
        """Start empty; with a store, async_load() brings back what it holds."""
        self._store = store
        self._pre: deque[dict[str, Any]] = deque(maxlen=PRE_SAMPLES)
        self._open: list[dict[str, Any]] | None = None
        self._post_left = 0
        self._closed: deque[list[dict[str, Any]]] = deque(maxlen=KEPT_EPISODES)
        self._episodes_detected = 0
        self._defrost_answers: Counter[str] = Counter()
        self._status_values: Counter[str] = Counter()
        self._with_coil = 0
        self._without_coil = 0
        self._signals_while_heating: dict[str, int] = dict.fromkeys(SIGNAL_KEYS, 0)

    async def async_load(self) -> None:
        """Restore the stored trace; anything unreadable leaves it empty."""
        if self._store is None:
            return
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a damaged file must not stop the setup
            return
        self._restore(stored)

    async def async_flush(self) -> None:
        """Write the trace now, ahead of an unload."""
        if self._store is not None:
            await self._store.async_save(self._to_store())

    def _schedule_save(self) -> None:
        if self._store is not None:
            self._store.async_delay_save(self._to_store, SAVE_DELAY)

    def record(self, airco: Aircon, raw_frame: str, source: str) -> None:
        """Take a sample from one parsed frame, with its carried-forward values."""
        detected = defrost_detected(airco)
        bit = defrost_status_bit(airco)
        coil = defrost_from_indoor_coil(airco)
        sample: dict[str, Any] = {
            "timestamp": dt_util.utcnow().isoformat(),
            "source": source,
            "airconStat": raw_frame,
            **{name: getattr(airco, name, None) for name in _DECODED_FIELDS},
            "defrost_detected": detected,
            "defrost_status_bit": bit,
            "defrost_from_indoor_coil": coil,
        }
        self._count(airco, bit, coil)

        if self._open is None:
            if detected is True:
                self._episodes_detected += 1
                self._open = [*self._pre, sample]
                self._pre.clear()
                self._post_left = POST_SAMPLES
                self._schedule_save()
            else:
                self._pre.append(sample)
            return

        if len(self._open) < MAX_EPISODE_SAMPLES:
            self._open.append(sample)
        self._schedule_save()
        if detected is True:
            self._post_left = POST_SAMPLES
            return
        self._post_left -= 1
        if self._post_left <= 0:
            self._closed.append(self._open)
            self._open = None

    def _count(self, airco: Aircon, bit: bool | None, coil: bool | None) -> None:
        if airco.IndoorCoilRaw is None:
            self._without_coil += 1
        else:
            self._with_coil += 1
        status = airco.OperationStatusRaw
        if status is not None:
            self._status_values[f"0x{status:02X}"] += 1
        if bit is True and coil is True:
            self._signals_while_heating["bit_and_coil"] += 1
            self._schedule_save()
        elif bit is True:
            self._signals_while_heating["bit_only"] += 1
            self._schedule_save()
        elif coil is True:
            self._signals_while_heating["coil_only"] += 1
            self._schedule_save()
        # Only a 0x0C segment that arrived in this very frame; the carried
        # value would count the same answer once per poll.
        segment = airco.ServiceDataRaw.get(SERVICE_DATA_DEFROST)
        if segment is not None:
            self._defrost_answers[f"0x{segment[0]:02X}"] += 1

    def _episodes(self) -> list[list[dict[str, Any]]]:
        episodes = [list(episode) for episode in self._closed]
        if self._open is not None:
            episodes.append(list(self._open))
        return episodes

    def as_dict(self) -> dict[str, Any]:
        """The trace for the diagnostics download."""
        return {
            "episodes_detected": self._episodes_detected,
            "defrost_flag_answers": dict(self._defrost_answers),
            "operation_status_values": dict(self._status_values),
            "samples_with_indoor_coil": self._with_coil,
            "samples_without_indoor_coil": self._without_coil,
            "signals_while_heating": dict(self._signals_while_heating),
            "episodes": self._episodes(),
            "open_episode": self._open is not None,
        }

    def _to_store(self) -> dict[str, Any]:
        data = self.as_dict()
        del data["open_episode"]
        return data

    def _restore(self, stored: Any) -> None:
        """Take over a stored trace, ignoring whatever does not look right."""
        if not isinstance(stored, dict):
            return
        episodes = stored.get("episodes")
        if isinstance(episodes, list):
            for episode in episodes[-KEPT_EPISODES:]:
                if isinstance(episode, list) and all(
                    isinstance(sample, dict) for sample in episode
                ):
                    self._closed.append(episode[:MAX_EPISODE_SAMPLES])
        self._episodes_detected = _count_of(stored.get("episodes_detected"))
        self._defrost_answers = _counter_of(stored.get("defrost_flag_answers"))
        self._status_values = _counter_of(stored.get("operation_status_values"))
        self._with_coil = _count_of(stored.get("samples_with_indoor_coil"))
        self._without_coil = _count_of(stored.get("samples_without_indoor_coil"))
        signals = stored.get("signals_while_heating")
        if isinstance(signals, dict):
            for key in SIGNAL_KEYS:
                self._signals_while_heating[key] = _count_of(signals.get(key))


def _count_of(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _counter_of(value: Any) -> Counter[str]:
    counter: Counter[str] = Counter()
    if isinstance(value, dict):
        for key, count in value.items():
            if isinstance(key, str) and _count_of(count):
                counter[key] = count
    return counter
