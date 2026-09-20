"""Who changed the unit, and what to do when it was not us.

Three questions that all rest on the same two observations - the module's
`expires` timestamp and a snapshot of the settings nothing but a write moves:

* did another client write, so that we should stand down and leave it the
  unit (`detect`),
* did a setting move without anybody taking the write lock, which is the IR
  remote or the unit's own doing (`_note_unexpected_settings`),
* did *our own* status request clear the settings it only meant to ask with,
  which is issue #329 (`check_request_was_applied`).

They stayed together because they share the snapshot and because the order in
which they run matters: the settings diff reads the state the poll just
refreshed, and the #329 check has to run after it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any, Final

from pywfrac import AirconCommands, WfRacError

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    STATUS_REQUEST_ECHO,
    STATUS_REQUEST_SILENT,
    STATUS_REQUEST_STRICT,
)

if TYPE_CHECKING:
    from .coordinator import Device

_LOGGER = logging.getLogger(__name__)

# How long another client's last write keeps us from sending operation-data
# requests at all. Someone who has just taken the lock is someone using the
# unit right now, and the free window SERVICE_DATA_REQUEST_INTERVAL leaves is
# only wide enough for one write - not for a session of them. Three minutes
# covers a typical app session. The operation-data sensors hold their last
# values throughout - a pause we chose is not the stale-data case
# SERVICE_DATA_MAX_AGE guards against, and External Control says plainly that
# it is happening. See pause_to_settle().
FOREIGN_ACTIVITY_BACKOFF = timedelta(minutes=3)

# What the settings look like on a unit that applied an all-zero command block.
# Every field in it decodes to the lowest value it can hold: power off, mode
# auto, fan and both vane axes to their first position. The setpoint is not
# listed because its zero is clamped by the unit to a floor we cannot know from
# here (10 C on the one module that does this) - it is checked as "moved down"
# instead. See check_request_was_applied().
EMPTY_BLOCK_SETTINGS: Final = {
    "Operation": False,
    "OperationMode": 0,
    "AirFlow": 1,
    "WindDirectionUD": 1,
    "WindDirectionLR": 1,
}

# How many of those fields have to land on their zero value at once before we
# call it applied rather than coincidence. Three, and only when no field moved
# anywhere else in the same answer: somebody turning the fan down to step 1 and
# the setpoint down with it would otherwise read as the fault, and the cost of
# believing that is undoing the change they just made.
EMPTY_BLOCK_MATCH_MIN: Final = 3

# The fields nothing but a write changes.
SETTINGS_WATCHED: Final = (
    "Operation",
    "OperationMode",
    "PresetTemp",
    "AirFlow",
    "WindDirectionUD",
    "WindDirectionLR",
    "Entrust",
)


def request_stops_unit_issue_id(entry_id: str) -> str:
    """Repair-issue id for a unit that applies the request meant only to ask."""
    return f"request_stops_unit_{entry_id}"


def status_request_unsupported_issue_id(entry_id: str) -> str:
    """Repair-issue id for a unit we have stopped asking altogether.

    Its own id rather than a second wording under the one above: the two say
    different things and the second replaces the first, so the first has to be
    withdrawn rather than overwritten.
    """
    return f"status_request_unsupported_{entry_id}"


class ForeignWriterWatch:
    """Attribution of setting changes for one unit."""

    def __init__(self, device: Device) -> None:
        """Bind the watch to its coordinator."""
        self._device = device
        # The flag is set by our own successful writes and consumed by the
        # next poll, so a rise in `expires` can be attributed to us or to
        # someone else.
        self._wrote_since_last_poll = False
        # Narrower than the flag above and consumed by the same poll: only a
        # frame that could actually move a setting sets this one. Our own
        # operation-data request moves `expires` every cycle without carrying
        # a single set-bit, and reading that as "a write happened" is what
        # would leave every change made at the unit unattributable.
        self._wrote_settings_since_last_poll = False
        self._expected_settings: dict[str, Any] | None = None
        # The settings a status request was built from, kept until something
        # has been able to answer for them - see check_request_was_applied().
        self._request_baseline: dict[str, Any] | None = None
        self._until: datetime | None = None
        self._reported = False
        self._since: datetime | None = None

    @property
    def active(self) -> bool:
        """Whether another client wrote recently enough that we stand down.

        See FOREIGN_ACTIVITY_BACKOFF for what counts as recently.
        """
        return self._until is not None and dt_util.utcnow() < self._until

    def note_write(self, *, could_move_a_setting: bool) -> None:
        """Record that a write of ours succeeded, for the next poll to read.

        Only a successful write moves the module's `expires`, so only a
        successful write may claim the next rise in it. A status request moves
        it too - it is a setAirconStat like any other - which is why the
        narrower flag is a separate one and not this same value read twice.
        """
        self._wrote_since_last_poll = True
        if could_move_a_setting:
            self._wrote_settings_since_last_poll = True

    def note_own_command(self) -> None:
        """Move the expectation to what the unit reported back to our command.

        Our own write is not a foreign one, or the next poll would read this
        command as somebody else's. A real command also explains any settings
        that move after it, so the status request before it no longer has to.
        """
        self._expected_settings = self.snapshot()
        self._request_baseline = None

    def note_status_request(self, before: Mapping[str, Any] | None) -> None:
        """Keep the state a status request was built from, for the poll.

        The module answers our request with its own cached state, and the
        frame's trip down the CNS bus to the indoor unit need not have
        finished by then - measured on the affected unit, the settings it
        cleared showed up a poll later, not in the answer (#329). Checking
        only the answer would have reproduced the blind spot this detector was
        rewritten to close.
        """
        self._request_baseline = dict(before) if before is not None else None

    def pause_to_settle(self) -> timedelta | None:
        """How long the stand-down that has just ended lasted, once.

        None while one is running or when there is nothing to settle. The
        caller moves the operation-data age anchor by it: the gap is one we
        chose, so counting it against SERVICE_DATA_MAX_AGE would throw away
        values that are perfectly good and about to be refreshed anyway.

        Kept out of _report() on purpose - that one only logs, and runs after
        the carry-forward has already decided. This has to have happened
        before that decision.
        """
        if self._since is None or self.active:
            return None
        span = self._until - self._since if self._until is not None else None
        self._since = None
        return span

    def detect(self, expires: Any) -> None:
        """Notice when someone else has written to the unit, from `expires`.

        The module reports the moment its 60-second write lock lapses, and
        that moment only moves when a setAirconStat succeeds. So a higher
        `expires` than the previous poll saw means a write happened in
        between - ours if we sent one, somebody else's if we did not. That is
        the whole detector: no extra request, and no dependence on the
        module's clock agreeing with ours, because only the difference is
        read.

        `updatedBy` cannot do this job. It reports the literal "local" for
        any account registered with remote=0, which is how this integration
        registers (remote=1 is what makes the module open its cloud
        connection, so it is not an option) - and a locally paired Smart
        M-Air app registers the same way. Both therefore show up as "local"
        and cannot be told apart.

        A write the module refused is a write that never happened as far as
        it is concerned: it reports neither the attempt nor who made it. So a
        client we lock out permanently is a client we never learn about, and
        this detector only works on top of a lock we let go of regularly -
        see SERVICE_DATA_REQUEST_INTERVAL.

        Known blind spot: someone else writing in the same gap in which we
        did hides behind our own write, and this poll says nothing. The next
        one catches them as soon as they act again, which for anyone actually
        using the app is seconds away.
        """
        wrote = self._wrote_since_last_poll
        self._wrote_since_last_poll = False
        wrote_settings = self._wrote_settings_since_last_poll
        self._wrote_settings_since_last_poll = False

        known = self._device.account_expires
        expires_moved = (
            isinstance(expires, int) and isinstance(known, int) and expires > known
        )
        if expires_moved and not wrote:
            self._note_foreign_write(f"expires moved {known} -> {expires}")
        # Not `expires_moved or wrote`: our own operation-data request moves
        # `expires` in most cycles and carries no set-bits, so that reading
        # would call every change unattributable and never name the one thing
        # this detector exists for. What is asked here is narrower - could a
        # write, ours or anyone's, have moved a setting in this gap?
        self._note_unexpected_settings(wrote_settings or (expires_moved and not wrote))
        # After the settings diff, because the poll that carries the evidence
        # is the one that has just refreshed the state it is read from.
        baseline = self._request_baseline
        self._request_baseline = None
        self.check_request_was_applied(baseline)
        self._report()

    def _note_foreign_write(self, evidence: str) -> None:
        if self._since is None:
            self._since = dt_util.utcnow()
        self._until = dt_util.utcnow() + FOREIGN_ACTIVITY_BACKOFF
        _LOGGER.debug(
            "Another client wrote to [%s]: %s", self._device.device_name, evidence
        )

    def snapshot(self) -> dict[str, Any] | None:
        """The fields nothing but a write changes.

        Deliberately none of the measurements: temperatures, currents and the
        operation-data values move on their own every cycle. Vacant and
        self-clean are left out for the same reason one step removed - the
        unit turns those on by itself, and both drag a setpoint with them.
        """
        airco = self._device.airco
        if airco is None:
            return None
        return {name: getattr(airco, name) for name in SETTINGS_WATCHED}

    def _note_unexpected_settings(self, someone_wrote: bool) -> None:
        """Notice a setting that changed without us changing it.

        The only signal our own traffic cannot erase: `expires` and
        `updatedBy` are overwritten by our next write, a changed setting is
        not. It adds the case no write lock was taken for - only a
        setAirconStat moves `expires`, so a setting that moved while `expires`
        stood still was not changed over the network at all, but at the IR
        remote, by a timer, or by one of the unit's own modes. No lock is held
        there, so this only reports; the stand-down stays with the writes.

        someone_wrote asks whether a set-bit could have been sent in this gap,
        not whether any frame was - our own operation-data request moves
        `expires` every cycle while changing nothing. The price is a wrong
        label rather than a missing message: a foreign client writing in the
        same gap as one of our requests reads as the unit itself, the module
        offering no second record to tell them apart.

        Not every one of these is somebody's doing - the unit resets its own
        setpoint after a power cycle, and Vacant and self-clean move settings
        with nobody asking. And one case is invisible by construction: a
        change we undo while carrying the power state back leaves the value
        exactly where we expect it.
        """
        current = self.snapshot()
        expected = self._expected_settings
        self._expected_settings = current
        if current is None or expected is None or current == expected:
            return
        changed = ", ".join(
            f"{name} {expected[name]} -> {value}"
            for name, value in current.items()
            if expected[name] != value
        )
        if someone_wrote:
            _LOGGER.debug(
                "[%s] changed while a write was in flight, so who did it "
                "cannot be told from here: %s",
                self._device.device_name,
                changed,
            )
            return
        _LOGGER.debug(
            "[%s] was changed at the unit itself - nothing took the write lock: %s",
            self._device.device_name,
            changed,
        )

    def _report(self) -> None:
        """Say so, once, when we start and stop holding back.

        Worth a log line rather than only the binary sensor: while this is on,
        the operation-data sensors report unknown, and someone reading the log
        to find out why deserves to find the reason there.
        """
        active = self.active
        if active == self._reported:
            return
        self._reported = active
        if active:
            _LOGGER.info(
                "Another client is controlling [%s]; pausing operation-data "
                "requests for up to %.0fs so it keeps working. Its sensors "
                "hold their last values meanwhile.",
                self._device.device_name,
                FOREIGN_ACTIVITY_BACKOFF.total_seconds(),
            )
        else:
            _LOGGER.info(
                "No other client active on [%s]; resuming operation-data requests",
                self._device.device_name,
            )

    def _empty_block_matches(self, before: Mapping[str, Any]) -> list[str]:
        """The settings that moved to exactly what an all-zero block encodes.

        Compares the state before our own status request with the one the unit
        answered it with. Empty when anything moved somewhere else.

        That veto is what separates this from an ordinary change. A block
        applied as zeros lands every field on its minimum at once; a person at
        the remote picks values, and one field moving to a value of its own is
        enough to say this was not our frame being applied.

        The setpoint counts when it moved *down*: its zero is clamped by the
        unit to a floor we do not know from here, so the value cannot be
        predicted - only the direction can. Upwards it is a contradiction like
        any other.
        """
        airco = self._device.airco
        if airco is None:
            return []
        matched: list[str] = []
        for name, zero in EMPTY_BLOCK_SETTINGS.items():
            if before.get(name) == getattr(airco, name):
                continue
            if getattr(airco, name) != zero:
                return []
            matched.append(name)
        setpoint_before = before.get("PresetTemp")
        if setpoint_before is not None and airco.PresetTemp != setpoint_before:
            if airco.PresetTemp > setpoint_before:
                return []
            matched.append("PresetTemp")
        return matched

    def check_request_was_applied(self, before: Mapping[str, Any] | None) -> None:
        """Notice a unit that applies the request we only meant to ask with.

        A status request has no set-bits, so on every unit we have measured it
        changes nothing. On at least one it is applied field by field instead
        (issue #329): the zeros become power off, mode auto, fan and both vanes
        to position 1, and a setpoint of zero clamped up to the unit's heating
        floor. Since the request repeats every minute, such a unit cannot be
        held at any setting at all while one operation-data sensor is enabled.

        What is watched for is that whole pattern, not the shutdown alone. The
        shutdown is only its most visible part and it is also the part a unit
        that is already off can no longer show - which is where the earlier
        version of this got stuck, on the one unit it was written for.

        One occurrence is enough. The test is not that something moved but that
        everything that moved landed on its minimum at the moment of our own
        request, and nothing else does that: the remote and the app write the
        values somebody chose.

        Escalates rather than gives up. First occurrence switches the request
        to carrying the unit's own settings, which is the shape the
        manufacturer's app uses and should make the frame confirm them. A
        second occurrence in that shape means the frame is not what the unit
        objects to, and then the only honest answer is to stop sending it.
        """
        if before is None or self._device.status_request_mode == STATUS_REQUEST_SILENT:
            return
        matched = self._empty_block_matches(before)
        if len(matched) < EMPTY_BLOCK_MATCH_MIN:
            return
        changed = ", ".join(
            f"{name} {before[name]} -> {getattr(self._device.airco, name)}"
            for name in matched
        )
        if self._device.status_request_mode == STATUS_REQUEST_STRICT:
            self._device.adopt_status_request_mode(STATUS_REQUEST_ECHO)
            _LOGGER.warning(
                "[%s] applied the settings in the request that asked it for "
                "operation data, which carries none: %s. From now on that "
                "request carries the unit's own settings back to it, so the "
                "frame confirms them instead of clearing them",
                self._device.device_name,
                changed,
            )
        else:
            self._device.adopt_status_request_mode(STATUS_REQUEST_SILENT)
            _LOGGER.warning(
                "[%s] changed its settings during an operation-data request "
                "that carried its own state: %s. Nothing this integration can "
                "put in that frame leaves the unit alone, so it will not be "
                "sent again. The operation-data sensors and the external "
                "temperature override stop working on this unit; everything "
                "else is unaffected",
                self._device.device_name,
                changed,
            )
        # Answered for: without this the poll would read the same change again
        # and escalate a step that has just been taken.
        self._request_baseline = None
        self._device.hass.async_create_task(self._async_restore_settings(before))
        if self._device.status_request_mode == STATUS_REQUEST_SILENT:
            # The advice in the first issue - watch it for a few minutes - has
            # been overtaken by what just happened.
            ir.async_delete_issue(
                self._device.hass,
                DOMAIN,
                request_stops_unit_issue_id(self._device.entry_id),
            )
        ir.async_create_issue(
            self._device.hass,
            DOMAIN,
            request_stops_unit_issue_id(self._device.entry_id)
            if self._device.status_request_mode == STATUS_REQUEST_ECHO
            else status_request_unsupported_issue_id(self._device.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=(
                "request_stops_unit"
                if self._device.status_request_mode == STATUS_REQUEST_ECHO
                else "status_request_unsupported"
            ),
            translation_placeholders={"device_name": self._device.device_name},
        )

    async def _async_restore_settings(self, before: Mapping[str, Any]) -> None:
        """Put back what the request just cleared.

        The values are one round trip old rather than current, which is the
        same freshness the echo itself runs on - and the alternative is
        leaving the unit on settings nobody chose until somebody notices. Only
        reached on the one frame that revealed the fault, so it cannot become
        a loop: by the time it runs, the mode has already changed.
        """
        try:
            await self._device.set_airco(
                {
                    AirconCommands.Operation: before["Operation"],
                    AirconCommands.OperationMode: before["OperationMode"],
                    AirconCommands.PresetTemp: before["PresetTemp"],
                    AirconCommands.AirFlow: before["AirFlow"],
                    AirconCommands.WindDirectionUD: before["WindDirectionUD"],
                    AirconCommands.WindDirectionLR: before["WindDirectionLR"],
                },
                log_failure=False,
            )
        except (WfRacError, KeyError, TypeError, ValueError) as ex:
            _LOGGER.warning(
                "Could not restore the settings [%s] lost to our own request: %s",
                self._device.device_name,
                ex,
            )
