"""The operation-data channel: when to ask, and how well it is answering.

The unit reports its compressor, current, coil temperatures and EEV position
only when asked, one extra frame per cycle. Deciding when that frame may go
out, how far after the poll it sits, whether the readings it brought are still
worth showing, and whether this unit has the channel at all is a bundle of
eight counters and timestamps that used to sit among the polling in the
coordinator.

The coordinator still sends the frame - that is wire work, and it needs the
parser, the settings snapshot and the #329 detection next to it. What is here
is everything around it that only concerns this one channel.
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
    SERVICE_DATA_DEFROST,
    SERVICE_DATA_DISCHARGE_SUPERHEAT_RAW,
    SERVICE_DATA_EEV_PULSES,
    SERVICE_DATA_HOT_GAS_TEMP,
    SERVICE_DATA_HUMIDITY,
    SERVICE_DATA_INDOOR_COIL_OUTLET_RAW,
    SERVICE_DATA_INDOOR_COIL_RAW,
    SERVICE_DATA_OPERATING_CURRENT,
    SERVICE_DATA_OUTDOOR_COIL_RAW,
    SERVICE_DATA_PROTECTION_RAW,
    SERVICE_DATA_SILENT_OPERATION,
    Aircon,
    ServiceDataRefusals,
    defrost_possible,
)

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MIN_TIME_BETWEEN_UPDATES

if TYPE_CHECKING:
    from .coordinator import Device

_LOGGER = logging.getLogger(__name__)

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
    SERVICE_DATA_OUTDOOR_COIL_RAW: SERVICE_DATA_INTERVAL_MEDIUM,
    SERVICE_DATA_HUMIDITY: SERVICE_DATA_INTERVAL_MEDIUM,
    SERVICE_DATA_DEFROST: SERVICE_DATA_INTERVAL_MEDIUM,
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
    SERVICE_DATA_DEFROST: defrost_possible,
}

# Codes whose condition above reads another code, which therefore has to be
# requested whenever the first one is subscribed, enabled sensor or not.
SERVICE_DATA_DEPENDS_ON: Final[dict[int, tuple[int, ...]]] = {
    SERVICE_DATA_DEFROST: (SERVICE_DATA_COMPRESSOR_FREQ,),
}


def service_data_interval(code: int) -> timedelta:
    """How often a code is read; the poll interval for one the table lacks."""
    return SERVICE_DATA_INTERVALS.get(code, SERVICE_DATA_REQUEST_INTERVAL)


def service_data_expiry(code: int) -> timedelta:
    """How long a code's last value may be carried before it reads unknown."""
    return SERVICE_DATA_EXPIRY_FACTOR * service_data_interval(code)


# Fields fed exclusively by those segments.
SERVICE_DATA_FIELDS = (
    "Defrosting",
    "DefrostingRaw",
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
    "OutdoorCoilRaw",
    "DischargeSuperheatRaw",
    "ProtectionRaw",
)

# These codes answer on at least one tested unit, meaning unknown.
RESEARCH_SERVICE_DATA_CODES: Final = (
    0x10,
    0x14,
    0x15,
    0x1C,
    0x1D,
    0x22,
    0x23,
    0x2A,
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
)
RESEARCH_CONTEXT: Final = "research_data"


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
        """One untested code alone, or else a rotating batch of answering ones.

        At most `slots` codes, the room the regular codes leave in the
        request. An untested code is only tried when nothing else in the
        request is unproven (`allow_untested`), so a refusal has one suspect.
        """
        if slots <= 0:
            return ()
        untested = [code for code, state in self.states.items() if state == "untested"]
        if untested and allow_untested:
            return (untested[0],)
        ok = [code for code, state in self.states.items() if state == "ok"]
        if not ok:
            return ()
        count = min(slots, len(ok))
        start = self._rotation % len(ok)
        self._rotation += count
        return tuple((ok + ok)[start : start + count])

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
        """Count a refusal against a lone code; a batch goes back to untested."""
        if len(requested) == 1:
            code = requested[0]
            self._refusals[code] = self._refusals.get(code, 0) + 1
            if self._refusals[code] >= RESEARCH_REFUSALS_TO_DROP:
                self.states[code] = "refused"
            return
        for code in requested:
            self.states[code] = "untested"


# Converted fields, and the raw field each is derived from. A conversion can
# fail while its segment arrives perfectly well - the coil temperatures are
# only calibrated over part of the byte range (see RacParser._coil_temp) - and
# carrying the last convertible value forward would then freeze a stale
# temperature on screen for as long as the unit stays out of range. Which is a
# whole heating season, and it is exactly what a frozen reading must never look
# like. So when the raw field arrived, its temperature is not carried: no value
# is the honest answer. The same holds for humidity, where the sensorless
# answer 30 converts to no value, and for counting a segment as an answer at
# all: the raw field is set whenever the segment arrived.
SERVICE_DATA_DERIVED_FROM = {
    "Defrosting": "DefrostingRaw",
    "IndoorHumidity": "IndoorHumidityRaw",
    "IndoorCoilTemp": "IndoorCoilRaw",
    "IndoorCoilOutletTemp": "IndoorCoilOutletRaw",
}


def service_data_unanswered_issue_id(entry_id: str) -> str:
    """Repair-issue id for a unit that answers operation-data requests empty."""
    return f"service_data_unanswered_{entry_id}"


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

    @property
    def supported(self) -> bool:
        """Whether this unit answers operation-data requests at all.

        False only after SERVICE_DATA_UNANSWERED_LIMIT requests it accepted and
        answered without a single segment. The sensors fed by that channel say
        unavailable rather than unknown then: unknown means "no reading right
        now", and this is "there will not be one".
        """
        return not self._unsupported

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

        Same rationale as Device._carry_forward_home_leave_mode(): the unit
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
