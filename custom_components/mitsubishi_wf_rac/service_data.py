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
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Final

from pywfrac import Aircon

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
# is worse for automations built on them than an honest gap.
SERVICE_DATA_MAX_AGE = 3 * SERVICE_DATA_REQUEST_INTERVAL

# Fields fed exclusively by those segments.
SERVICE_DATA_FIELDS = (
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

# Converted fields, and the raw field each is derived from. A conversion can
# fail while its segment arrives perfectly well - the coil temperatures are
# only calibrated over part of the byte range (see RacParser._coil_temp) - and
# carrying the last convertible value forward would then freeze a stale
# temperature on screen for as long as the unit stays out of range. Which is a
# whole heating season, and it is exactly what a frozen reading must never look
# like. So when the raw field arrived, its temperature is not carried: no value
# is the honest answer.
SERVICE_DATA_DERIVED_FROM = {
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
        resuming: the gap is one we chose, so counting it against
        SERVICE_DATA_MAX_AGE would throw away values that are perfectly good
        and about to be refreshed anyway.
        """
        if self._last_response is not None:
            self._last_response += by

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

        Unlike home/leave mode this expires: see SERVICE_DATA_MAX_AGE. Time
        spent standing down for another client is not counted against that
        age, though - see Device.foreign_activity. SERVICE_DATA_MAX_AGE guards
        against a value frozen by a unit that stopped answering, which is
        indistinguishable from a live one; a pause we chose ourselves is
        neither indistinguishable nor a fault, and External Control says so
        while it lasts. Dropping perfectly good readings for it would be a
        worse answer than carrying them a few minutes longer.
        """
        current = self._device.airco
        if current is None:
            return
        self._device.settle_service_data_pause()
        now = dt_util.utcnow()
        if any(getattr(new_airco, name) is not None for name in SERVICE_DATA_FIELDS):
            self._last_response = now
            if self._expired:
                self._expired = False
                _LOGGER.info(
                    "Operation data from [%s] is being reported again",
                    self._device.device_name,
                )
        elif self._device.foreign_activity:
            pass  # Not stale, just paused - carry the values below.
        elif (
            self._last_response is None
            or now - self._last_response > SERVICE_DATA_MAX_AGE
        ):
            # Nothing fresh for too long - leave the fields unset so entities
            # report unknown rather than a value that stopped being true.
            self._note_expired(now)
            return
        for name in SERVICE_DATA_FIELDS:
            if getattr(new_airco, name) is not None:
                continue
            source = SERVICE_DATA_DERIVED_FROM.get(name)
            if source is not None and getattr(new_airco, source) is not None:
                # Segment arrived, value unusable - see SERVICE_DATA_DERIVED_FROM.
                continue
            setattr(new_airco, name, getattr(current, name))

    def _note_expired(self, now: datetime) -> None:
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
        # Before the first response there is nothing to lose yet; anchor on the
        # first request instead so a module that never answers is still
        # reported, once, rather than silently leaving the sensors unknown.
        anchor = self._last_response or self._last_request
        if anchor is None or now - anchor <= SERVICE_DATA_MAX_AGE:
            return
        self._expired = True
        _LOGGER.warning(
            "No operation data from [%s] for over %.0fs; its compressor, "
            "current, temperature and EEV sensors now report unknown. A "
            "network interruption is the usual cause - check whether other "
            "devices dropped out at the same time",
            self._device.device_name,
            SERVICE_DATA_MAX_AGE.total_seconds(),
        )
