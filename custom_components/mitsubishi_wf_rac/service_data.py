"""The operation-data channel: when to ask, and how well it is answering.

The unit reports its compressor, current, coil temperatures and EEV position
only when asked, one extra frame per cycle. Deciding when that frame may go
out, how far after the poll it sits, whether the readings it brought are still
worth showing, and whether this unit has the channel at all is a bundle of
eight counters and timestamps that used to sit among the polling in the
coordinator.

The frame itself goes out through the coordinator's set_airco(), which holds
the parser, the settings snapshot and the #329 detection; async_request() here
drives it. Everything around that only concerns this one channel.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Final

from pywfrac import (
    SERVICE_DATA_BUDGET,
    SERVICE_DATA_CODE_BY_FIELD,
    SERVICE_DATA_CODES,
    SERVICE_DATA_COMPRESSOR_FREQ,
    SERVICE_DATA_DISCHARGE_SUPERHEAT_RAW,
    SERVICE_DATA_EEV_PULSES,
    SERVICE_DATA_HOT_GAS_TEMP,
    SERVICE_DATA_HOT_START,
    SERVICE_DATA_HUMIDITY,
    SERVICE_DATA_INDOOR_COIL_OUTLET_RAW,
    SERVICE_DATA_INDOOR_COIL_RAW,
    SERVICE_DATA_OPERATING_CURRENT,
    SERVICE_DATA_OUTDOOR_COIL_RAW,
    SERVICE_DATA_PROTECTION_RAW,
    SERVICE_DATA_SILENT_OPERATION,
    Aircon,
    AirconCommands,
    ServiceDataRefusals,
    WfRacCommandError,
    WfRacError,
    WfRacWriteRefusedError,
    hot_start_possible,
)

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MIN_TIME_BETWEEN_UPDATES

if TYPE_CHECKING:
    from .coordinator import Device

_LOGGER = logging.getLogger(__name__)

# How long the channel stays quiet after a refusal, by consecutive refusal. A
# unit that refuses operation-data requests renews its write lock with every
# one of them, so asking again at the normal cadence keeps it locked out; the
# last step is held for as long as the refusals go on.
SERVICE_DATA_PAUSE_LADDER: Final = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(minutes=60),
)

# A refused request costs a full cycle of every operation-data sensor, and
# these refusals are transient, so one retry is worth the extra request.
SERVICE_DATA_RETRY_DELAY = timedelta(seconds=5)

# Operation data is requested for active operation-data entities and costs a
# second request per poll. It stays on the local network, and on most modules
# its block carries no set-bits (see RacParser.status_request_to_byte) - but it
# is a setAirconStat, so it takes the module's 60-second write lock all the
# same, and while we hold that lock no one else can control the unit at all.
# On a module that needs its state carried (#329) the block is a full command
# and the request really does write.
#
# The lock's deadline is `now + 60`, where `now` is the `timestamp` field of
# the request that took it - the module has no RTC and reads its clock from
# whatever the asking client stamps (see _async_write_lock_delay). Stamping the
# request SERVICE_DATA_STAMP_BACKDATE in the past therefore makes it take a lock
# that expires SERVICE_DATA_STAMP_BACKDATE sooner: at 55s back the lock runs 5
# of its 60 seconds, leaving the other 55 of every poll free for the app or the
# IR remote to get a write in. That is what keeps an enabled operation-data
# entity from locking the Smart M-Air app out for good (#294) while still asking
# on every poll. Confirmed against a real module. Detecting the other client
# cannot substitute for the free window:
# a refused write changes nothing the module reports back, so a client we never
# let through is a client we never see (see ForeignWriterWatch.detect).
SERVICE_DATA_REQUEST_INTERVAL = MIN_TIME_BETWEEN_UPDATES

# A guard against a second request landing in the same poll, not a skip of
# alternate polls (SERVICE_DATA_STAMP_BACKDATE in coordinator.py is what frees the window now). Kept below
# one poll interval so every poll still asks, but far enough under it that a
# poll answering a few milliseconds faster than the one before it - polls are
# stamped when they finish, not when they were due - does not read as too soon
# and drop the cycle.
SERVICE_DATA_MIN_SPACING = SERVICE_DATA_REQUEST_INTERVAL * 0.75
# ...but it does matter *where* in the cycle it lands. Issued straight off the
# back of a poll it reached the module about a second after the getAirconStat
# (consolidation delay plus the minimum spacing between requests), and modules
# answer a second request that soon with HTTP 501 "Not supported this command"
# often enough to lose whole cycles of operation data - roughly one poll in
# seven on an affected unit, sometimes several minutes in a row. Offsetting it
# into the quiet middle of the cycle keeps the cadence but stops it from
# crowding the poll. Measured against the poll interval, not the request
# interval: what has to stay clear is the poll, and the polls in between are
# just as much in the way as the one the request was scheduled from.
SERVICE_DATA_REQUEST_OFFSET = MIN_TIME_BETWEEN_UPDATES / 2

# ...but half a cycle is a guess, and an expensive one. What we hold when the
# request goes out is that old, and everything that happened in between is
# invisible: a command from the remote in that gap is neither seen nor
# attributable afterwards, because our own write moves both `expires` and
# `updatedBy` past it. How much distance a module actually needs differs
# between installations, so measure it per device instead of assuming the
# worst everywhere: start at the safe end, walk down while requests keep
# succeeding, and jump back up the moment one is refused for being too close.
SERVICE_DATA_OFFSET_MIN = timedelta(seconds=5)
SERVICE_DATA_OFFSET_STEP = timedelta(seconds=5)
# Down slowly, up sharply: a lost cycle costs every operation-data sensor a
# reading, while sitting one step wider than necessary costs only freshness.
SERVICE_DATA_OFFSET_GOOD_CYCLES = 5

# How many requests may come back answered but empty before we accept that this
# unit does not have the operation-data channel at all. Ten, and only counting
# requests the module accepted: on the unit this was written for (#329) not one
# reading has ever arrived, in any beta, with either shape of frame - so every
# minute spent asking is a write that takes the unit's lock for nothing.
#
# Not persisted, unlike the frame shape. Relearning that costs the user a unit
# that clears its settings; relearning this costs ten requests, and leaving it
# in memory means a module that starts answering is picked up again on the next
# reload instead of being written off for good.
SERVICE_DATA_UNANSWERED_LIMIT: Final = 10

# The unit answers these segments only when asked, so they are carried across
# the polls in between (see carry_forward() below) - but not
# indefinitely. A unit that keeps refusing the request would otherwise leave
# entities reporting a frozen number indistinguishable from a live one, which
# is worse for automations built on them than an honest gap. A code keeps its
# value for SERVICE_DATA_EXPIRY_FACTOR of its own intervals (see
# service_data_expiry()); this is that span for a code the table does not know.
SERVICE_DATA_EXPIRY_FACTOR: Final = 3
SERVICE_DATA_MAX_AGE = SERVICE_DATA_EXPIRY_FACTOR * SERVICE_DATA_REQUEST_INTERVAL

# How often each code is worth reading. Fast-moving values and those feeding
# energy or efficiency figures every poll; slow ones less often, so the slots
# they leave free go to research codes. Adding a code is one line here.
SERVICE_DATA_INTERVAL_FAST: Final = timedelta(seconds=60)
SERVICE_DATA_INTERVAL_MEDIUM: Final = timedelta(seconds=120)
SERVICE_DATA_INTERVAL_SLOW: Final = timedelta(seconds=300)
SERVICE_DATA_INTERVALS: Final[dict[int, timedelta]] = {
    SERVICE_DATA_COMPRESSOR_FREQ: SERVICE_DATA_INTERVAL_FAST,
    SERVICE_DATA_OPERATING_CURRENT: SERVICE_DATA_INTERVAL_FAST,
    SERVICE_DATA_EEV_PULSES: SERVICE_DATA_INTERVAL_FAST,
    SERVICE_DATA_INDOOR_COIL_RAW: SERVICE_DATA_INTERVAL_FAST,
    SERVICE_DATA_INDOOR_COIL_OUTLET_RAW: SERVICE_DATA_INTERVAL_FAST,
    SERVICE_DATA_HOT_GAS_TEMP: SERVICE_DATA_INTERVAL_FAST,
    # A defrost lasts only a few minutes, so the outdoor coil is read every poll.
    SERVICE_DATA_OUTDOOR_COIL_RAW: SERVICE_DATA_INTERVAL_FAST,
    SERVICE_DATA_HUMIDITY: SERVICE_DATA_INTERVAL_MEDIUM,
    # A hot start lasts about two to six minutes, so anything slower can miss it.
    SERVICE_DATA_HOT_START: SERVICE_DATA_INTERVAL_FAST,
    SERVICE_DATA_DISCHARGE_SUPERHEAT_RAW: SERVICE_DATA_INTERVAL_SLOW,
    SERVICE_DATA_PROTECTION_RAW: SERVICE_DATA_INTERVAL_SLOW,
    SERVICE_DATA_SILENT_OPERATION: SERVICE_DATA_INTERVAL_SLOW,
}

# A code is due once it is within this much of its interval. Polls are stamped
# when they finish and the answer lands some seconds into the cycle, so a code
# read every 60s is a few seconds younger than 60s at the next poll as often as
# not; demanding the full interval would skip every second poll for it.
SERVICE_DATA_DUE_TOLERANCE: Final = MIN_TIME_BETWEEN_UPDATES / 2


# Codes that are only worth asking while a condition holds, as a predicate on
# the last reading. Codes without an entry are always eligible.
SERVICE_DATA_ONLY_WHEN: Final[dict[int, Callable[[Aircon | None], bool]]] = {
    SERVICE_DATA_HOT_START: hot_start_possible,
}

# Codes whose condition above reads another code, which therefore has to be
# requested whenever the first one is subscribed, enabled sensor or not.
SERVICE_DATA_DEPENDS_ON: Final[dict[int, tuple[int, ...]]] = {}


# Codes the climate entity's hvac_action reads. It must never cause a request
# of its own: every operation-data request takes the unit's 60-second write
# lock and locks other clients out. They are asked only inside a request that
# goes out anyway, for an enabled sensor or an armed temperature override.
SERVICE_DATA_PIGGYBACK_CODES: Final = (
    SERVICE_DATA_HOT_START,
    SERVICE_DATA_INDOOR_COIL_RAW,
)


def service_data_interval(code: int) -> timedelta:
    """How often a code is read; the poll interval for one the table lacks."""
    return SERVICE_DATA_INTERVALS.get(code, SERVICE_DATA_REQUEST_INTERVAL)


def service_data_expiry(code: int) -> timedelta:
    """How long a code's last value may be carried before it reads unknown."""
    return SERVICE_DATA_EXPIRY_FACTOR * service_data_interval(code)


# Fields fed exclusively by those segments.
SERVICE_DATA_FIELDS = (
    "HotStart",
    "HotStartRaw",
    "SilentOperation",
    "IndoorHumidity",
    "IndoorHumidityRaw",
    "CompressorFrequency",
    "CompressorFrequencyRaw",
    "OperatingCurrent",
    "OperatingCurrentRaw",
    "HotGasTemp",
    "HotGasTempRaw",
    "EevPulses",
    "EevPosition",
    "IndoorCoilTemp",
    "IndoorCoilOutletTemp",
    "IndoorCoilRaw",
    "IndoorCoilOutletRaw",
    "OutdoorCoilTemp",
    "OutdoorCoilRaw",
    "DischargeSuperheatRaw",
    "ProtectionRaw",
)

# These codes answer on at least one tested unit, meaning unknown.
RESEARCH_SERVICE_DATA_CODES: Final = (
    0x0D,
    0x10,
    0x14,
    0x15,
    0x1C,
    0x1D,
    0x21,
    0x22,
    0x23,
    0x2A,
    0x32,
    0x34,
    0x35,
    0x3E,
    0x45,
    0x7B,
    0x84,
    0x86,
    0x88,
    0x8D,
    0xA0,
    0xA1,
    0xA3,
    0xA4,
    0xA5,
    0xB0,
    0xD2,
    0xD5,
    0x05,  # the setpoint the unit works with internally
)
RESEARCH_CONTEXT: Final = "research_data"

# Research codes asked in every request once they answer, instead of waiting
# for their turn in the rotation: 0x0D shows a state for about two to six
# minutes after some heating starts, shorter than one trip through the rotation.
RESEARCH_PINNED_CODES: Final = (0x0D,)


# A single code is written off only after this many refusals: result 11 is
# also what a request gets while another client holds the write lock.
RESEARCH_REFUSALS_TO_DROP: Final = 2
RESEARCH_BATCH_SIZE: Final = SERVICE_DATA_BUDGET


class ResearchData:
    """Which research codes a unit answers, and what they said last.

    A unit that does not know one requested code refuses the whole request,
    so a code is tried alone before it may join a batch: untested -> ok, or
    refused once it was turned down alone often enough. A refused batch puts
    its codes back to untested. Memory only - a reload starts over.
    """

    def __init__(self) -> None:
        """Start with every candidate untested."""
        self.states: dict[int, str] = dict.fromkeys(
            RESEARCH_SERVICE_DATA_CODES, "untested"
        )
        self.values: dict[int, tuple[int, int, int]] = {}
        self.seen: dict[int, datetime] = {}
        self._refusals: dict[int, int] = {}
        self._rotation = 0

    def request_codes(
        self, slots: int = RESEARCH_BATCH_SIZE, *, allow_untested: bool = True
    ) -> tuple[int, ...]:
        """Pinned codes, then one untested code or a rotating batch.

        At most `slots` codes, the room the regular codes leave in the
        request. An untested code is only tried when nothing else in the
        request is unproven (`allow_untested`), so a refusal has one suspect;
        an answering pinned code is proven and does not count.
        """
        if slots <= 0:
            return ()
        pinned = self._pinned_ok()[:slots]
        slots -= len(pinned)
        untested = [code for code, state in self.states.items() if state == "untested"]
        if untested and allow_untested and slots:
            return (*pinned, untested[0])
        ok = [
            code
            for code, state in self.states.items()
            if state == "ok" and code not in pinned
        ]
        if not ok or not slots:
            return pinned
        count = min(slots, len(ok))
        start = self._rotation % len(ok)
        self._rotation += count
        return (*pinned, *(ok + ok)[start : start + count])

    def _pinned_ok(self) -> tuple[int, ...]:
        return tuple(
            code for code in RESEARCH_PINNED_CODES if self.states.get(code) == "ok"
        )

    def note_answer(self, airco: Aircon) -> None:
        """Keep every research segment the frame carried."""
        now = dt_util.utcnow()
        for code, value in airco.ServiceDataRaw.items():
            if code not in self.states:
                continue
            self.values[code] = value
            self.seen[code] = now
            self.states[code] = "ok"
            self._refusals.pop(code, None)

    def note_refused(self, requested: tuple[int, ...]) -> None:
        """Count a refusal against a lone code; a batch goes back to untested.

        Answering pinned codes are not suspects, unless nothing else was asked.
        """
        pinned = self._pinned_ok()
        suspects = tuple(code for code in requested if code not in pinned)
        if suspects:
            requested = suspects
        if len(requested) == 1:
            code = requested[0]
            self._refusals[code] = self._refusals.get(code, 0) + 1
            if self._refusals[code] >= RESEARCH_REFUSALS_TO_DROP:
                self.states[code] = "refused"
            return
        for code in requested:
            self.states[code] = "untested"


# Converted fields, and the raw field each is derived from. A conversion can
# fail while its segment arrives perfectly well - the coil and hot-gas
# temperatures are only calibrated over part of the byte range (see
# RacParser._coil_temp and RacParser._hot_gas_temp) - and
# carrying the last convertible value forward would then freeze a stale
# temperature on screen for as long as the unit stays out of range. Which is a
# whole heating season, and it is exactly what a frozen reading must never look
# like. So when the raw field arrived, its temperature is not carried: no value
# is the honest answer. The same holds for humidity, where the sensorless
# answer 30 converts to no value, and for counting a segment as an answer at
# all: the raw field is set whenever the segment arrived.
SERVICE_DATA_DERIVED_FROM = {
    "HotStart": "HotStartRaw",
    "IndoorHumidity": "IndoorHumidityRaw",
    "IndoorCoilTemp": "IndoorCoilRaw",
    "IndoorCoilOutletTemp": "IndoorCoilOutletRaw",
    "OutdoorCoilTemp": "OutdoorCoilRaw",
    "HotGasTemp": "HotGasTempRaw",
}


def service_data_unanswered_issue_id(entry_id: str) -> str:
    """Repair-issue id for a unit that answers operation-data requests empty."""
    return f"service_data_unanswered_{entry_id}"


def service_data_refused_issue_id(entry_id: str) -> str:
    """Repair-issue id for a unit that keeps refusing operation-data requests."""
    return f"service_data_refused_{entry_id}"


class ServiceDataChannel:
    """Timing and health of one unit's operation-data channel."""

    def __init__(self, device: Device) -> None:
        """Bind the channel to its coordinator."""
        self._device = device
        self._task: asyncio.Task[None] | None = None
        self._last_request: datetime | None = None
        self._last_response: datetime | None = None
        self._expired = False
        self._unanswered = 0
        self._unsupported = False
        # None until the adaptation has moved it, so the ceiling stays a
        # single source of truth.
        self._offset: timedelta | None = None
        self._good_cycles = 0
        # Per code: when its segment last arrived, and whether the module has
        # ever answered it. Memory only - a reload starts over.
        self._read: dict[int, datetime] = {}
        self._refusals = ServiceDataRefusals()
        self._overdue: set[int] = set()
        self._humidity_sensor_seen = False
        # Circuit breaker: consecutive refused requests, and until when the
        # channel stays quiet because of them. Memory only - a reload asks
        # again, which is also what clears a lock-out the user ended by
        # power-cycling the unit.
        self._refusal_streak = 0
        self._paused_until: datetime | None = None

    @property
    def paused(self) -> bool:
        """Whether the channel is standing down after a refusal."""
        return self._paused_until is not None and dt_util.utcnow() < self._paused_until

    def breaker_state(self) -> dict[str, int | str | bool | None]:
        """The circuit breaker's state, for the diagnostics."""
        return {
            "pause_level": min(self._refusal_streak, len(SERVICE_DATA_PAUSE_LADDER)),
            "paused_until": (
                None if self._paused_until is None else self._paused_until.isoformat()
            ),
            "consecutive_refusals": self._refusal_streak,
            "override_released": self._device.external_temperature.suspended,
        }

    def _holds_unproven(
        self, regular_codes: tuple[int, ...], research_codes: tuple[int, ...]
    ) -> bool:
        """Whether a request held a code the unit has never answered."""
        return self._refusals.has_unproven(regular_codes) or any(
            self._device.research_data.states.get(code) != "ok"
            for code in research_codes
        )

    def _note_request_refused(self, *, during_pause: bool, unproven: bool) -> None:
        """Back off after a refused request, one step further each time.

        Not before the unit has answered once: a module that has never
        reported anything is the first-answer case handled elsewhere, and
        pausing it would hide the one request that could prove it works. Nor
        for a request that held a code the unit never answered: a unit
        without that code refuses the whole request, which the per-code
        suspicion above already isolates, and is no lock-out.

        A refused carrier-only request means even the minimal frame is not
        accepted, so the override is released - the unit goes back to its own
        sensor instead of regulating on a value that can no longer be
        refreshed.
        """
        if self._last_response is None or unproven:
            return
        self._refusal_streak += 1
        level = min(self._refusal_streak, len(SERVICE_DATA_PAUSE_LADDER))
        pause = SERVICE_DATA_PAUSE_LADDER[level - 1]
        self._paused_until = dt_util.utcnow() + pause
        name = self._device.device_name
        feed = self._device.external_temperature
        if during_pause and feed.needs_frame:
            feed.suspend()
            _LOGGER.info(
                "[%s] refused even the request carrying the room temperature "
                "from the indoor temperature source; the unit is back on its "
                "own sensor until it answers again",
                name,
            )
        if level < len(SERVICE_DATA_PAUSE_LADDER):
            _LOGGER.debug(
                "[%s] refused an operation-data request; pausing the channel "
                "for %.0f minutes",
                name,
                pause.total_seconds() / 60,
            )
            return
        if self._refusal_streak == len(SERVICE_DATA_PAUSE_LADDER):
            _LOGGER.info(
                "[%s] keeps refusing operation-data requests; its diagnostic "
                "sensors stay unknown and it is asked only once an hour now. "
                "Power-cycling the unit clears the lock-out",
                name,
            )
            ir.async_create_issue(
                self._device.hass,
                DOMAIN,
                service_data_refused_issue_id(self._device.entry_id),
                data={"entry_id": self._device.entry_id},
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="service_data_refused",
                translation_placeholders={"device_name": name},
            )

    def _note_request_answered(self, *, during_pause: bool) -> None:
        """Lift the pause and re-arm the override once a request is answered.

        An answer to the carrier-only request sent during a pause says nothing
        about the full request, so it leaves the ladder alone.
        """
        if during_pause or (self._refusal_streak == 0 and self._paused_until is None):
            return
        if self._refusal_streak >= len(SERVICE_DATA_PAUSE_LADDER):
            _LOGGER.info(
                "[%s] answers operation-data requests again",
                self._device.device_name,
            )
            ir.async_delete_issue(
                self._device.hass,
                DOMAIN,
                service_data_refused_issue_id(self._device.entry_id),
            )
        else:
            _LOGGER.debug(
                "[%s] answers operation-data requests again",
                self._device.device_name,
            )
        self._refusal_streak = 0
        self._paused_until = None
        self._device.external_temperature.resume()

    @property
    def supported(self) -> bool:
        """Whether this unit answers operation-data requests at all.

        False only after SERVICE_DATA_UNANSWERED_LIMIT requests it accepted and
        answered without a single segment. The sensors fed by that channel say
        unavailable rather than unknown then: unknown means "no reading right
        now", and this is "there will not be one". The same
        goes for a unit the user switched operation data off for.
        """
        return not self._unsupported and self._device.service_data_enabled

    @property
    def last_response(self) -> datetime | None:
        """When a segment last arrived, or None if none ever has."""
        return self._last_response

    @property
    def task(self) -> asyncio.Task[None] | None:
        """The request in flight, if one is."""
        return self._task

    def forget_task(self) -> None:
        """Drop the reference to a request that is over."""
        self._task = None

    def adopt_task(self, task: asyncio.Task[None]) -> None:
        """Take the background task the coordinator just started."""
        self._task = task

    def due(self) -> bool:
        """Whether a request may be started now, stamping it if so.

        Stamped here rather than when the request actually goes out, so the
        offset shifts the request within the cycle instead of stretching the
        interval between requests.
        """
        if self._task is not None and not self._task.done():
            # A retry from the previous cycle is still in flight; piling a
            # second request on top is exactly the crowding this avoids.
            return False
        now = dt_util.utcnow()
        if (
            self._last_request is not None
            and now - self._last_request < SERVICE_DATA_MIN_SPACING
        ):
            return False
        self._last_request = now
        return True

    def mark_overdue(self, code: int) -> None:
        """Ask for this code at the next poll, whatever its interval says."""
        self._overdue.add(code)

    def _ratio(self, code: int, now: datetime) -> float:
        """How many of its intervals the code has waited; infinite if never read."""
        if code in self._overdue or code not in self._read:
            return float("inf")
        return (now - self._read[code]) / service_data_interval(code)

    def _is_due(self, code: int, now: datetime) -> bool:
        if code in self._overdue or code not in self._read:
            return True
        return now - self._read[code] >= (
            service_data_interval(code) - SERVICE_DATA_DUE_TOLERANCE
        )

    def select(
        self,
        subscribed: Iterable[int],
        airco: Aircon | None,
        *,
        need_frame: bool = False,
    ) -> tuple[int, ...]:
        """The codes this poll's request carries, sorted, at most the budget.

        The due codes with the largest ratio of age to interval go first. A
        code the module has never answered rides only if the request holds no
        other unproven code, and a suspect only if it holds none at all, so a
        refusal can be pinned on one code. `need_frame` is for an armed
        external temperature override, which rides on the request: with
        nothing due it still takes the most overdue code rather than leaving
        the poll without one.
        """
        # Ages are taken at the moment the request goes out, not now: it waits
        # out the offset after the poll, and the previous answer arrived that
        # same offset after the previous poll. Measured from the poll, a 60 s
        # code read at the last request is only 60 s minus the offset old and
        # falls out of every second request once the offset reaches half a poll.
        now = dt_util.utcnow() + self.offset
        candidates = [
            code
            for code in subscribed
            if not self._refusals.is_suspended(code)
            and (
                (only_when := SERVICE_DATA_ONLY_WHEN.get(code)) is None
                or only_when(airco)
            )
        ]
        by_overdue = sorted(candidates, key=lambda c: (-self._ratio(c, now), c))
        due = (code for code in by_overdue if self._is_due(code, now))
        chosen = list(self._refusals.admit(due, SERVICE_DATA_BUDGET))
        if not chosen and need_frame:
            fallback = by_overdue or sorted(subscribed)
            chosen = fallback[:1]
        return tuple(sorted(chosen))

    def has_unproven(self, codes: Iterable[int]) -> bool:
        """Whether a request with these codes holds one the module never answered."""
        return self._refusals.has_unproven(codes)

    def note_refused(self, requested: Iterable[int]) -> None:
        """Pin a refused request on its unproven codes, if it had any.

        A unit that does not know one requested code refuses the whole request,
        so a code only counts against itself when it was the one the module had
        not answered before. Several unproven codes become suspects instead and
        are then asked one at a time. Refused alone twice, a code is suspended.
        """
        for code in self._refusals.note_refused(requested):
            # Debug: a unit without the code is normal, not a fault - the
            # sensor staying unknown is the visible part.
            _LOGGER.debug(
                "[%s] refused operation-data code 0x%02X on its own twice; not "
                "asking for it again until the integration reloads",
                self._device.device_name,
                code,
            )

    def _note_read(self, code: int, now: datetime) -> None:
        """Record that a code's segment arrived."""
        self._read[code] = now
        self._refusals.note_answered(code)
        self._overdue.discard(code)

    def _age_of(self, code: int, now: datetime) -> timedelta | None:
        """How old a code's value is; None if nothing has ever arrived."""
        stamp = self._read.get(code, self._last_response)
        return None if stamp is None else now - stamp

    @property
    def offset(self) -> timedelta:
        """How long after a poll the operation-data request goes out."""
        if self._offset is None:
            return SERVICE_DATA_REQUEST_OFFSET
        return self._offset

    def widen_offset(self) -> None:
        """Put more distance between the poll and the request after a refusal.

        Doubling rather than stepping: a refused request costs every
        operation-data sensor a reading, and several in a row is what an
        offset that is much too short looks like, so overshooting once is
        cheaper than creeping up on it.
        """
        if self.offset >= SERVICE_DATA_REQUEST_OFFSET:
            return
        self._good_cycles = 0
        self._offset = min(self.offset * 2, SERVICE_DATA_REQUEST_OFFSET)
        _LOGGER.debug(
            "Moving the operation-data request for [%s] to %.0fs after the "
            "poll: the module refused it where it was",
            self._device.device_name,
            self.offset.total_seconds(),
        )

    def note_offset_survived(self) -> None:
        """Move the request back towards the poll while requests keep landing.

        Closer is better for everything except crowding: what the request
        carries, and what any judgement about who changed the unit rests on,
        is as old as the last poll.
        """
        if self.offset <= SERVICE_DATA_OFFSET_MIN:
            return
        self._good_cycles += 1
        if self._good_cycles < SERVICE_DATA_OFFSET_GOOD_CYCLES:
            return
        self._good_cycles = 0
        self._offset = max(
            self.offset - SERVICE_DATA_OFFSET_STEP, SERVICE_DATA_OFFSET_MIN
        )
        _LOGGER.debug(
            "Moving the operation-data request for [%s] to %.0fs after the "
            "poll: %s cycles without a refusal",
            self._device.device_name,
            self.offset.total_seconds(),
            SERVICE_DATA_OFFSET_GOOD_CYCLES,
        )

    def shift_anchor(self, by: timedelta) -> None:
        """Move the age anchor forward past a stand-down we chose ourselves.

        Without this the readings would expire on the very first poll after
        resuming: the gap is one we chose, so counting it against the expiry
        would throw away values that are perfectly good and about to be
        refreshed anyway.
        """
        if self._last_response is not None:
            self._last_response += by
        for code in self._read:
            self._read[code] += by

    def note_whether_anything_answered(self, answered_before: datetime | None) -> None:
        """Give up on a unit that takes the request and answers nothing.

        Answering with no segment at all is not a refusal - the module accepted
        the frame and replied - so nothing else in the request path notices it.
        On the unit this was written for that is every request ever sent, which
        leaves five sensors permanently unknown and a write going out every
        minute to keep them that way.
        """
        if self._unsupported:
            return
        if self._last_response != answered_before:
            self._unanswered = 0
            return
        self._unanswered += 1
        if self._unanswered < SERVICE_DATA_UNANSWERED_LIMIT:
            return
        self._unsupported = True
        _LOGGER.warning(
            "[%s] has answered %s operation-data requests without reporting a "
            "single value. This unit does not have that channel, so it will "
            "not be asked again and its compressor, current, temperature and "
            "EEV sensors are now unavailable. Nothing else is affected",
            self._device.device_name,
            SERVICE_DATA_UNANSWERED_LIMIT,
        )
        ir.async_create_issue(
            self._device.hass,
            DOMAIN,
            service_data_unanswered_issue_id(self._device.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="service_data_unanswered",
            translation_placeholders={"device_name": self._device.device_name},
        )
        self._device.async_update_listeners()

    def carry_forward(self, new_airco: Aircon) -> None:
        """Carry the extension segments forward, as home/leave mode is.

        Same rationale as HomeLeaveMode.carry_forward(): the unit
        reports these extension segments exactly once, so without this the
        sensors would flash the real value for one update cycle and then
        revert to unknown.

        Unlike home/leave mode this expires, per code: see
        service_data_expiry(). Time spent standing down for another client is
        not counted against that age, though - see Device.foreign_activity.
        The expiry guards against a value frozen by a unit that stopped
        answering, which is indistinguishable from a live one; a pause we
        chose ourselves is neither indistinguishable nor a fault, and External
        Control says so while it lasts. Dropping perfectly good readings for it
        would be a worse answer than carrying them a few minutes longer.
        """
        current = self._device.airco
        if current is None:
            return
        self._device.settle_service_data_pause()
        self._accept_real_humidity_30(new_airco)
        now = dt_util.utcnow()
        arrived = {
            code for code in new_airco.ServiceDataRaw if code in SERVICE_DATA_CODES
        }
        arrived.update(
            SERVICE_DATA_CODE_BY_FIELD[name]
            for name in SERVICE_DATA_FIELDS
            if getattr(new_airco, name) is not None
        )
        if arrived:
            self._last_response = now
            for code in arrived:
                self._note_read(code, now)
            if self._expired:
                self._expired = False
                _LOGGER.info(
                    "Operation data from [%s] is being reported again",
                    self._device.device_name,
                )
        dropped: list[timedelta] = []
        for name in SERVICE_DATA_FIELDS:
            if getattr(new_airco, name) is not None:
                continue
            source = SERVICE_DATA_DERIVED_FROM.get(name)
            if source is not None and getattr(new_airco, source) is not None:
                # Segment arrived, value unusable - see SERVICE_DATA_DERIVED_FROM.
                continue
            code = SERVICE_DATA_CODE_BY_FIELD[name]
            if not self._device.foreign_activity:
                age = self._age_of(code, now)
                if age is None or age > service_data_expiry(code):
                    # Nothing fresh for too long - leave the field unset so
                    # the entity reports unknown rather than a value that
                    # stopped being true.
                    if getattr(current, name) is not None:
                        dropped.append(service_data_expiry(code))
                    continue
            setattr(new_airco, name, getattr(current, name))
        if dropped:
            self._note_expired(min(dropped))
        elif (
            self._last_response is None
            and self._last_request is not None
            and now - self._last_request > SERVICE_DATA_MAX_AGE
        ):
            # Before the first response there is nothing to lose yet; the
            # first request is the anchor then, so a module that never answers
            # is still reported, once, rather than silently leaving the sensors
            # unknown.
            self._note_expired(SERVICE_DATA_MAX_AGE)

    def _accept_real_humidity_30(self, new_airco: Aircon) -> None:
        """Show 30 % once the unit has proved it has a humidity sensor.

        A unit without the sensor answers a constant 30, so the parser turns
        30 into no value. A unit with one reads 30 too, in dry heating air,
        and that reading is real. Any other valid value tells the two apart;
        until one has arrived since setup, 30 stays unknown.
        """
        if new_airco.IndoorHumidity is not None:
            self._humidity_sensor_seen = True
        elif self._humidity_sensor_seen and new_airco.IndoorHumidityRaw == 30:
            new_airco.IndoorHumidity = 30

    def _note_expired(self, limit: timedelta) -> None:
        """Warn once, when the operation-data sensors actually go unknown.

        A refused request costs a cycle and nothing else, so it stays on debug:
        at roughly one an hour per unit it would otherwise be a permanent
        warning about a module behaviour no one can act on. Running out of
        values is the part a user can see, and it is worth exactly one line -
        with a matching one when they come back.

        Every occurrence measured so far coincided with network maintenance
        (a controller update, an access point restarting), not with anything
        the unit did, so the message points there rather than at the air
        conditioner.
        """
        if self._expired:
            return
        self._expired = True
        # Info, like its recovery: Home Assistant's rule for an outage is one
        # line when it starts and one when it ends, both at info.
        _LOGGER.info(
            "No operation data from [%s] for over %.0fs; its compressor, "
            "current, temperature and EEV sensors now report unknown. A "
            "network interruption is the usual cause - check whether other "
            "devices dropped out at the same time",
            self._device.device_name,
            limit.total_seconds(),
        )

    async def async_request(
        self,
        service_data_codes: tuple[int, ...],
        research_codes: tuple[int, ...] = (),
    ) -> None:
        """Ask the unit for operation-data segments.

        Offset from the poll and retried once if the unit refuses it (see
        SERVICE_DATA_REQUEST_OFFSET). Sends directly rather than through
        async_queue_command() so the refusal is visible here: a queued command
        is flushed by a detached task that deliberately swallows its errors.
        """
        during_pause = self.paused
        await asyncio.sleep(self.offset.total_seconds())
        # What the frame carries depends on the module: no set-bits at all
        # without the #329 quirk, a full command with it (see
        # RacParser.status_request_to_byte). Byte 5 goes out either way,
        # having no set-bit to leave out, which is how an active external
        # temperature override stays alive between commands - and what makes
        # this request a write in the strict sense, for which the backdated
        # timestamp below gives back part of the lock.
        if (
            self._device.status_request_carries_state
            and not await self._device.async_read_before_echo()
        ):
            return
        if not self._device.status_request_is_allowed():
            # Re-checked after the sleep, not only when the request was
            # scheduled: the offset is up to half a minute, and the unit going
            # off inside it is exactly the window this protects.
            _LOGGER.debug(
                "Skipping the operation-data request for [%s]: it is either "
                "not sent to this unit at all, or the unit is off and the "
                "request would carry that state back to it",
                self._device.device_name,
            )
            return
        params = {AirconCommands.ServiceDataStatusRequest: service_data_codes}
        timestamp_offset = -round(
            self._device.service_data_stamp_backdate().total_seconds()
        )
        # _carry_forward_service_data() moves this whenever a segment arrives,
        # so comparing it across the request says whether this one was answered
        # - which the state itself cannot, the previous reading being carried
        # forward into it.
        answered_before = self.last_response
        regular_codes = tuple(c for c in service_data_codes if c not in research_codes)
        for attempt in (1, 2):
            try:
                await self._device.set_airco(
                    params,
                    log_failure=False,
                    timestamp_offset=timestamp_offset,
                    is_status_request=True,
                    retry_when_locked=False,
                )
                if attempt > 1:
                    _LOGGER.debug("Service data request succeeded on retry")
                if regular_codes:
                    self.note_whether_anything_answered(answered_before)
                if research_codes and self._device.airco is not None:
                    self._device.research_data.note_answer(self._device.airco)
                self.note_offset_survived()
                self._note_request_answered(during_pause=during_pause)
                # Notify, but deliberately not through async_set_updated_data():
                # that resets the refresh timer, and this runs half a cycle
                # after the poll - every cycle - so it would push the next poll
                # to 90s and keep doing so, silently turning the documented
                # 60s cadence into something else. Listeners get the fresh
                # state (set_airco() has already stored it) without the
                # schedule moving.
                self._device.async_update_listeners()
                # Not an else block: the two handlers below are what decide
                # whether the loop runs again, and splitting the success path
                # away from them would put that decision in two places.
                return  # noqa: TRY300
            except WfRacWriteRefusedError as ex:  # noqa: PERF203
                # The unit's answer to a code it does not know - or a foreign
                # write lock, which the refusal limits allow for.
                unproven = self._holds_unproven(regular_codes, research_codes)
                if research_codes:
                    self._device.research_data.note_refused(research_codes)
                self.note_refused(regular_codes)
                self._note_request_refused(during_pause=during_pause, unproven=unproven)
                # Someone else may hold the write lock. Unlike a user command
                # this is not worth contesting: give the cycle up immediately
                # rather than retrying into a lock we would only be renewing
                # for ourselves if we won it. set_airco()'s own wait-and-retry
                # is off for this call (retry_when_locked=False), or the
                # refusal would arrive here already contested.
                _LOGGER.debug(
                    "Service data request declined for [%s], skipping this cycle: %s",
                    self._device.device_name,
                    ex,
                )
                return
            except WfRacCommandError as ex:
                if attempt == 1:
                    # A refusal here is the module saying the request came too
                    # close to something else, which is exactly what the offset
                    # is for - so widen it, whether or not the retry gets
                    # through.
                    self.widen_offset()
                    _LOGGER.debug("Service data request refused (%s); retrying", ex)
                    await asyncio.sleep(SERVICE_DATA_RETRY_DELAY.total_seconds())
                    continue
                self._note_request_refused(
                    during_pause=during_pause,
                    unproven=self._holds_unproven(regular_codes, research_codes),
                )
                # Debug, not a warning: the module refuses these requests
                # transiently and a single skipped cycle changes nothing the
                # user can see - the values survive their expiry. The
                # warning belongs where they actually expire, see
                # _note_service_data_expired().
                _LOGGER.debug(
                    "Service data request refused twice, skipping this cycle "
                    "for [%s]: %s",
                    self._device.device_name,
                    ex,
                )
            except (WfRacError, KeyError, TypeError, ValueError) as ex:
                # Unreachable or unparseable: the poll itself reports that, and
                # this request is an optional extra on top of it. Still logged:
                # with an external temperature source this request is what
                # carries the override, and a lost one shows up only as the
                # unit falling back to its own sensor.
                _LOGGER.debug(
                    "Service data request failed for [%s], skipping this cycle: %s: %s",
                    self._device.device_name,
                    type(ex.__cause__ or ex).__name__,
                    ex,
                )
                return
        # Entities keep their previous operation-data values on a skipped cycle
        # (see _carry_forward_service_data), so there is nothing to push here.
