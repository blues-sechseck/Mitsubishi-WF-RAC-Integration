"""Device module"""

import asyncio
import logging
import re
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timedelta
from collections.abc import Callable
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    AC_CERT_FILENAME,
    CONF_EXTERNAL_TEMPERATURE_SOURCE,
    CONF_OVERSHOOT_COOL,
    CONF_OVERSHOOT_DRY,
    CONF_OVERSHOOT_HEAT,
    CONF_STATUS_REQUEST_MODE,
    DOMAIN,
    MIN_TIME_BETWEEN_UPDATES,
    OPERATION_MODE_COOL,
    OPERATION_MODE_DRY,
    OPERATION_MODE_HEAT,
    STATUS_REQUEST_ECHO,
    STATUS_REQUEST_SILENT,
    STATUS_REQUEST_STRICT,
)
from pywfrac import (
    Aircon,
    AirconCommands,
    AirconStat,
    HomeLeaveModeSetting,
    RacParser,
    Repository,
    WfRacCommandError,
    WfRacConnectionError,
    WfRacError,
    WfRacRegistrationError,
    WfRacWriteRefusedError,
)
from pywfrac.parser import SERVICE_DATA_CODES, SERVICE_DATA_INDOOR_COIL_RAW
from pywfrac.repository import MIN_TIME_BETWEEN_REQUESTS, REQUEST_TIMEOUT

from .firmware_check import fetch_latest_firmware

_LOGGER = logging.getLogger(__name__)

# Commands issued within this window of each other (from any entity) are
# coalesced into a single set_airco() call instead of being sent as separate
# requests. The unit expects a full state block per request, so two
# near-simultaneous separate commands can otherwise overwrite each other
# instead of merging (e.g. a fan-speed change followed shortly by a
# temperature change loses the fan change).
UPDATE_CONSOLIDATION_PERIOD = timedelta(milliseconds=500)

# The manufacturer's getFirmware endpoint is unauthenticated and cheap, but
# there's no reason to call it on every MIN_TIME_BETWEEN_UPDATES (60s) poll -
# firmware doesn't change that often. Rate-limit background checks to this
# interval instead.
FIRMWARE_CHECK_INTERVAL = timedelta(hours=24)

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
# let through is a client we never see (see _detect_foreign_activity).
SERVICE_DATA_REQUEST_INTERVAL = MIN_TIME_BETWEEN_UPDATES

# How far into the past the operation-data request is stamped, and so how much
# of the 60s lock it gives up. Nearly all of it, because the freed window is
# not how long the app stays usable - one write getting through is enough to
# trigger FOREIGN_ACTIVITY_BACKOFF, which then hands the unit over for minutes.
# It only decides how long someone waits for their *first* tap to land. Half
# the lock made that a coin flip per attempt (#294); a 5s grip per minute makes
# it land first try almost every time. Not the full 60s: the stamp is whole
# seconds and the deadline is compared as timestamp >= expires, so a little
# margin keeps the request holding a lock it can call its own rather than one
# that has already lapsed as it arrives. NOT applied right after one of our own
# real commands - see _async_request_service_data, where backdating would cut
# that command's own protection window instead of someone else's lock (same
# deviceId bypasses the lock check, so the request overwrites our own lease).
# That guard matters more at this setting than it did at 30s: a request that
# slipped past it would leave the command 5 seconds of protection, not 30.
SERVICE_DATA_STAMP_BACKDATE = timedelta(seconds=55)

# A guard against a second request landing in the same poll, not a skip of
# alternate polls (the backdate above is what frees the window now). Kept below
# one poll interval so every poll still asks, but far enough under it that a
# poll answering a few milliseconds faster than the one before it - polls are
# stamped when they finish, not when they were due - does not read as too soon
# and drop the cycle.
SERVICE_DATA_MIN_SPACING = SERVICE_DATA_REQUEST_INTERVAL * 0.75
# The segment an armed external temperature override subscribes to on its own
# behalf (see _sync_external_temperature_carrier). Any code would do - what
# matters is that a request goes out at all, since that is the frame the
# override rides on - so this is the one that answers under every condition:
# it is per indoor unit and reads a temperature whatever the system is doing,
# which is why it is also the sensor the README recommends enabling first.

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

# A refused request costs a full cycle of every operation-data sensor, and
# these refusals are transient, so one retry is worth the extra request.
SERVICE_DATA_RETRY_DELAY = timedelta(seconds=5)

# How long another client's last write keeps us from sending operation-data
# requests at all. Someone who has just taken the lock is someone using the
# unit right now, and the free window SERVICE_DATA_REQUEST_INTERVAL leaves is
# only wide enough for one write - not for a session of them. Three minutes
# covers a typical app session. The operation-data sensors hold their last
# values throughout - a pause we chose is not the stale-data case
# SERVICE_DATA_MAX_AGE guards against, and External Control says plainly that
# it is happening. See _settle_service_data_pause().
FOREIGN_ACTIVITY_BACKOFF = timedelta(minutes=3)

# One retry for a user command refused because someone else holds the lock,
# timed to land just after the lock lapses (see _async_write_lock_delay). Used
# as-is only when the remaining lock time cannot be established, where a short
# retry is still worth more than none: the common case is an app action already
# most of the way through its 60s. A retry that still fails is reported rather
# than repeated - two clients are genuinely fighting over the unit at that
# point.
WRITE_LOCK_RETRY_DELAY = timedelta(seconds=10)

# What the settings look like on a unit that applied an all-zero command block.
# Every field in it decodes to the lowest value it can hold: power off, mode
# auto, fan and both vane axes to their first position. The setpoint is not
# listed because its zero is clamped by the unit to a floor we cannot know from
# here (10 C on the one module that does this) - it is checked as "moved down"
# instead. See _check_request_was_applied().
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

# The lock runs 60 seconds, so a longer wait than that means the deadline was
# stamped by a client whose clock is off rather than that the lock is really
# still running - cap it instead of leaving a service call hanging on someone
# else's clock. See _async_write_lock_delay().
WRITE_LOCK_MAX_WAIT = timedelta(seconds=61)

# The unit answers these segments only when asked, so they are carried across
# the polls in between (see Device._carry_forward_service_data()) - but not
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

# Room for both legs of protocol discovery plus the minimum spacing between
# requests, so a poll that has to fall back to the other protocol is not
# cancelled halfway through.
#
# Sized as more than a single per-request timeout: a unit that accepts a
# plaintext connection without answering it consumes the whole window on the
# first leg, so an equal-sized budget would never reach the second leg. A
# unit that only speaks the second protocol would then fail every poll the
# same way and never recover on its own.
#
# Stays under MIN_TIME_BETWEEN_UPDATES so a slow poll cannot still be running
# when the next one is due.
POLL_TIMEOUT = 2 * REQUEST_TIMEOUT + MIN_TIME_BETWEEN_REQUESTS + timedelta(seconds=4)

# Consecutive failed polls before the device is reported unavailable, and the
# floor under the configurable value. The module reassociates to WiFi about
# once an hour and is unreachable while it does (see the README's
# Troubleshooting section); reporting that as an outage every time is noise.
# Three polls at MIN_TIME_BETWEEN_UPDATES is roughly three minutes of grace,
# which rides through the reassociation without hiding a device that is
# genuinely gone. Raising it is a legitimate choice on a weak link; lowering it
# only ever produced the phantom outages this floor exists to prevent.
AVAILABILITY_FAILURE_LIMIT_MIN = 3


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


def registration_full_issue_id(entry_id: str) -> str:
    """Repair-issue id for a full account table on this entry's airco.

    Shared between Device (which raises/clears it) and async_unload_entry
    (which clears it on removal, so a deleted entry doesn't leave a dangling
    issue behind) - one format, so the two can never drift apart.
    """
    return f"too_many_devices_{entry_id}"


class Device(DataUpdateCoordinator[Aircon]):  # pylint: disable=too-many-instance-attributes
    """Device Class"""

    # Narrowed from the base class's optional: this integration never builds a
    # Device without one.
    config_entry: ConfigEntry

    def __init__(  # pylint: disable=too-many-arguments
            self,
            hass: HomeAssistant,
            config_entry: ConfigEntry,
            name: str,
            hostname: str,
            port: int,
            device_id: str,
            operator_id: str,
            airco_id: str,
            swing_selects_enabled_default: bool,
            availability_failure_limit: int = AVAILABILITY_FAILURE_LIMIT_MIN,
            firmware_update_check_enabled: bool = False,
            connection_method: str | None = None,
            status_request_mode: str = STATUS_REQUEST_STRICT,
    ) -> None:
        self._api = Repository(
            async_get_clientsession(hass),
            hostname,
            port,
            operator_id,
            device_id,
            method=connection_method,
            cert_path=hass.config.path(AC_CERT_FILENAME),
        )
        self._parser = RacParser()
        # Carried over from a previous run: a module that applies a frame it
        # was not asked to apply has always done so, and relearning costs the
        # unit the same disturbance every time.
        self._status_request_mode = status_request_mode
        self._parser.status_request_carries_state = (
            status_request_mode == STATUS_REQUEST_ECHO
        )

        # Protected state
        self._airco = Aircon()
        self._operator_id = operator_id
        self._device_id = device_id
        self._host = hostname
        self._port = port
        self._airco_id = airco_id
        self._available = False
        self._name = name
        self._firmware = ""
        self._connected_accounts = -1
        self._updated_by: str | None = None
        self._account_expires: int | None = None
        self._led_status: int | None = None
        self._auto_heating: int | None = None
        self._firm_type: str | None = None
        self._wireless_firmware_ver: str | None = None
        self._latest_wireless_firmware_ver: str | None = None
        self._firmware_update_available: bool | None = None
        self._last_firmware_check: datetime | None = None
        self._firmware_update_check_enabled = firmware_update_check_enabled
        self._last_service_data_request: datetime | None = None
        self._last_service_data_response: datetime | None = None
        self._service_data_expired = False
        # Foreign-write detection, see _detect_foreign_activity(). The flag is
        # set by our own successful writes and consumed by the next poll, so a
        # rise in `expires` can be attributed to us or to someone else.
        self._wrote_since_last_poll = False
        # Narrower than the flag above and consumed by the same poll: only a
        # frame that could actually move a setting sets this one. Our own
        # operation-data request moves `expires` every cycle without carrying
        # a single set-bit, and reading that as "a write happened" is what
        # would leave every change made at the unit unattributable.
        self._wrote_settings_since_last_poll = False
        # None until the adaptation has moved it, so the ceiling stays a
        # single source of truth.
        self._service_data_offset: timedelta | None = None
        self._service_data_good_cycles = 0
        self._expected_settings: dict[str, Any] | None = None
        # When we last sent a real (set-bit) command, so an operation-data
        # request within one lock's span of it stamps honestly instead of
        # trimming that command's lease - see _service_data_stamp_backdate().
        self._last_command_at: datetime | None = None
        self._foreign_activity_until: datetime | None = None
        self._foreign_activity_reported = False
        self._foreign_activity_since: datetime | None = None
        self._service_data_task: asyncio.Task[None] | None = None
        self._external_temperature_override: float | None = None
        # The byte-5 values recent frames actually carried. Two, not one: a
        # frame carrying a new value goes out before the unit reports it back,
        # so during that one cycle the previous value is still the one the
        # unit is regulating on. Comparing against only the newest would make
        # external_temperature_applied - and with it the indoor offset - flip
        # off and on again on every value a source sensor feeds in.
        self._external_temperature_written: deque[int] = deque(maxlen=2)
        self._external_temperature_carrier: Callable[[], None] | None = None
        self._consecutive_failures = 0
        # Clamped rather than validated: an entry can carry a lower value from
        # an older version, and refusing to set up over it would be worse than
        # quietly giving it the tolerance it should have had.
        self._availability_failure_limit = max(
            AVAILABILITY_FAILURE_LIMIT_MIN, availability_failure_limit
        )
        self._swing_selects_enabled_default = swing_selects_enabled_default
        # Serializes set_airco() calls end-to-end (snapshot build through
        # self._airco update) so a call can never build its diff from a
        # snapshot that's stale because another set_airco() is still in
        # flight - see set_airco() below.
        self._send_lock = asyncio.Lock()
        self._consolidated_params: dict[AirconCommands, Any] = {}
        self._consolidation_task: asyncio.Task[None] | None = None

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=name,
            update_interval=MIN_TIME_BETWEEN_UPDATES,
        )

    @property
    def options(self) -> Mapping[str, Any]:
        """Options of the config entry that owns this device."""
        return self.config_entry.options

    @property
    def entry_id(self) -> str:
        """Id of the config entry that owns this device - see options above."""
        return self.config_entry.entry_id

    @property
    def external_temperature_override(self) -> float | None:
        """Return the integration-side external temperature override, if any.

        This is tracked by the integration rather than read back from the unit,
        because the wire byte reports the temperature the controller is working
        with regardless of its source and provides no flag for whether that
        value originated from an external override.
        """
        return self._external_temperature_override

    def set_external_temperature_override(self, value: float | None) -> None:
        """Set the integration-side override state.

        Used by the climate entity when restoring persisted state and whenever
        the configured source reports. Arming asks for the operation-data frame
        the value rides on (see _sync_external_temperature_carrier), since the
        poll that would otherwise schedule one runs before the entities exist -
        but that frame writes no setting of its own, so nothing here commands
        the unit. Which is exactly why the value counts as unapplied until a
        frame has carried it: it says what we intend to send, not what the unit
        currently regulates on.

        Nothing here says the unit has been told - see
        external_temperature_applied, which reads that off the wire.
        """
        if value is None:
            self._external_temperature_written.clear()
        self._external_temperature_override = value
        self._sync_external_temperature_carrier()

    async def async_shutdown(self) -> None:
        """Release the subscription and both tasks along with the coordinator.

        Neither task is owned by DataUpdateCoordinator, and hass only cancels
        background tasks when hass itself stops - which an entry unload is
        not. The operation-data one matters most: it spends most of its life
        asleep waiting out its offset, so a reload catches it mid-sleep and
        its request would go out from the old entry through a second
        Repository while the new one is already polling. Two connections at
        once is what the module will not take.

        Failures are logged and swallowed: an unload that raises leaves
        entities loaded on an entry that no longer updates, and both tasks
        report what matters elsewhere.
        """
        self._release_external_temperature_carrier()
        for task in (self._consolidation_task, self._service_data_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pylint: disable=broad-except
                _LOGGER.debug(
                    "Task cancelled at shutdown for [%s] had already failed",
                    self.device_name,
                    exc_info=True,
                )
        self._consolidation_task = None
        self._service_data_task = None
        await super().async_shutdown()

    def _release_external_temperature_carrier(self) -> None:
        if self._external_temperature_carrier is not None:
            self._external_temperature_carrier()
            self._external_temperature_carrier = None

    def _sync_external_temperature_carrier(self) -> None:
        """Subscribe to an operation-data segment for as long as an override is
        armed, and drop the subscription again when it is cleared.

        The override needs a frame to ride on, and the operation-data request
        is the one frame that goes out on its own without writing anything
        else. Rather than making that the user's problem - enable a diagnostic
        sensor or the feature quietly does nothing - the override subscribes
        like any other consumer of that request, and _maybe_request_service_data()
        starts asking for the same reason it does for an enabled sensor.

        Costs what an enabled operation-data sensor costs: one extra request
        per poll cycle, holding the unit's write lock for part of it.
        """
        if self._external_temperature_override is not None:
            if self._external_temperature_carrier is None:
                self._external_temperature_carrier = self.async_add_listener(
                    lambda: None, context=SERVICE_DATA_INDOOR_COIL_RAW
                )
                # Ask for the carrier frame now rather than waiting for a poll
                # to schedule one. Only a poll calls this otherwise, and the
                # poll that runs during setup happens before the entities
                # exist - so nothing is subscribed for it and the request is
                # skipped. An entry reload is exactly that path, and saving
                # the options reloads the entry (OptionsFlowWithReload): a
                # changed overshoot would sit unsent for a full poll interval
                # on top of the offset, until some other frame happened to
                # carry it. The spacing and in-flight guards inside still
                # apply, so a source flapping in and out cannot turn this into
                # a second request per cycle.
                self._maybe_request_service_data()
            return
        self._release_external_temperature_carrier()

    def _corrected_external_temperature(
        self, temperature: float | None, operation_mode: int
    ) -> float | None:
        """Bend the room temperature we hand the unit by its overshoot.

        The unit's thermostat band sits below the setting in cooling (measured
        across four units: it keeps calling for cooling until roughly 1-2 K
        under it, see issue #218). Telling it the room is that much colder than
        it is moves its stop point to where the room actually reaches the
        setting - and unlike the setpoint, which the unit rounds to whole
        degrees, this lever has the protocol's 0.25 K resolution.

        Heating is the mirror image, and zero - the default - changes nothing.

        Dry has a correction of its own rather than sharing the cooling one.
        It cools too, so the sign matches, but its airflow and its thermostat
        band are not the cooling ones, and nobody has measured what it does -
        which is why its field opens on zero where cooling opens on the figure
        four units needed. Auto is left uncorrected: which direction it is
        running in is CoolHotJudge, a value some units never report.
        """
        if temperature is None:
            return None
        overshoot = self._resolve_overshoot(operation_mode)
        if not overshoot:
            return temperature
        if operation_mode in (OPERATION_MODE_COOL, OPERATION_MODE_DRY):
            return temperature - overshoot
        if operation_mode == OPERATION_MODE_HEAT:
            return temperature + overshoot
        return temperature

    def _resolve_overshoot(self, operation_mode: int) -> float:
        """The configured overshoot for the mode a frame is going out in."""
        if operation_mode == OPERATION_MODE_COOL:
            key = CONF_OVERSHOOT_COOL
        elif operation_mode == OPERATION_MODE_HEAT:
            key = CONF_OVERSHOOT_HEAT
        elif operation_mode == OPERATION_MODE_DRY:
            key = CONF_OVERSHOOT_DRY
        else:
            return 0.0
        value = self.options.get(key, 0.0)
        return float(value) if isinstance(value, (int, float)) else 0.0

    @property
    def external_temperature_room_value(self) -> float | None:
        """The room temperature we handed the unit, or None while it is
        regulating on its own sensor.

        Whoever supplies a room temperature has said what "the room" means for
        this unit, so that is what the climate entity shows for as long as the
        unit is actually using it. What comes back from the unit is not it: an
        overshoot correction hands it a value that is deliberately not the
        room, and even without one the echo sits half a kelvin off in the
        protocol's coarser segment. Deciding this per overshoot - as this did
        until the reading was found to move half a kelvin when an unrelated
        option changed - makes the displayed room temperature depend on a
        setting that has nothing to do with it, and every automation comparing
        it against a threshold inherits that silently.

        The Indoor Temperature sensor keeps reporting the unit verbatim, so
        what the unit thinks is still visible - the two disagree exactly while
        the unit is being fed.

        With a source entity configured this holds even while the unit is not
        using the value - off, in fan_only, or a restart away from having sent
        one. A source keeps measuring the room whatever the unit is doing, and
        deciding whether to switch the unit on is exactly when someone reads
        that number (#218). The unit's own reading is at its least meaningful
        then anyway: nothing is drawing air past its sensor.

        A value armed from an automation is different and keeps the stricter
        rule. There is no source behind it, so it is a number someone pushed
        once, and showing it as the room while the unit is not even using it
        would be showing an intention rather than a measurement.
        """
        if self._external_temperature_override is None or self._airco is None:
            return None
        source = self.options.get(CONF_EXTERNAL_TEMPERATURE_SOURCE)
        if isinstance(source, str) and source:
            return self._external_temperature_override
        if not self.external_temperature_applied:
            return None
        return self._external_temperature_override

    @property
    def external_temperature_applied(self) -> bool:
        """Whether the unit is currently regulating on a value we supplied.

        Read off the wire rather than remembered: the unit echoes an injected
        value back in byte 5 unchanged, so the byte it reports matching one a
        recent frame carried is exactly the question - false after a restart
        until a frame has gone out, false while the unit is off or in fan_only
        (nothing writes the byte there), and false once another controller
        takes the unit off the override without telling us.

        One blind spot, and it is harmless: if the room happens to sit within
        a quarter kelvin of the armed value, the unit's own reading encodes to
        the same byte and this reads true early. Both branches show the same
        temperature then, and the calibration offset it suppresses is at most
        that far from being right anyway.
        """
        if self._external_temperature_override is None or self._airco is None:
            return False
        raw = self._airco.ControllerRoomTempRaw
        return raw is not None and raw in self._external_temperature_written

    def _subscribed_service_data_codes(self) -> tuple[int, ...]:
        """Operation-data codes currently subscribed, sorted: one per enabled
        diagnostic sensor, plus the carrier an armed external temperature
        override holds (see _sync_external_temperature_carrier).
        """
        return tuple(sorted(set(self.async_contexts()).intersection(SERVICE_DATA_CODES)))

    async def update(self) -> bool:
        """Update the device information from API.

        Called both directly (initial fetch in __init__.py before entities
        exist, and set_airco()'s own fallback fetch) and by the coordinator
        via _async_update_data() below. Deliberately does not call
        async_refresh()/async_set_updated_data() itself: on the coordinator
        poll path, listeners are already notified automatically once
        _async_update_data() returns, and calling async_refresh() here would
        re-enter _async_update_data() -> update() from within that same path.
        The other two call sites don't need a notification either - the
        initial fetch runs before any entity/listener exists, and
        set_airco()'s fallback fetch is immediately followed by a command
        whose completion already triggers async_set_updated_data() (see
        Device.async_queue_command()).
        """

        try:
            response = await self._api.get_aircon_stats(self._airco_id)

            if response is None:
                self._set_availability(False)
                _LOGGER.warning("Received no data for device %s", self._airco_id)
                return False
        except WfRacConnectionError as ex:
            self._record_connection_failure(ex)
            return False
        except (WfRacError, KeyError) as ex:
            self._set_availability(False)
            _LOGGER.warning(
                "Error: something went wrong updating the airco [%s] values",
                self.device_name,
                exc_info=ex,
            )
            # The WF-RAC module keeps only a small, fixed-size table of registered
            # accounts (operator ids). Opening the official app or adding phones can
            # silently evict Home Assistant from that table, after which polls fail
            # until the integration is reloaded. Proactively re-register our account
            # on failure so we recover automatically on the next poll if we were
            # evicted. An evicted account still answers (HTTP 400 / result:2, see
            # Repository.get_aircon_stats), so this is skipped above when the unit
            # was simply unreachable - re-registering can't succeed over a
            # connection that isn't there. add_account() swallows its own errors.
            await self.add_account()
            return False

        try:
            self._connected_accounts = int(response["numOfAccount"])
            new_airco = self._parser.translate_bytes(response["airconStat"])
            self._carry_forward_home_leave_mode(new_airco)
            self._carry_forward_service_data(new_airco)
            self._airco = new_airco
            # Not part of the airconStat blob, present alongside it in the same
            # response. Tolerate absence (.get()) since it's undocumented and
            # could be missing on older firmware.
            self._updated_by = response.get("updatedBy")
            self._detect_foreign_activity(response.get("expires"))
            self._account_expires = response.get("expires")
            self._led_status = response.get("ledStat")
            self._auto_heating = response.get("autoHeating")
            became_available = self._set_availability(True)
            if became_available:
                _LOGGER.info("Airco [%s] is available again", self.device_name)
        except (KeyError, TypeError, ValueError) as ex:
            _LOGGER.warning("Could not parse airco data", exc_info=ex)
            self._set_availability(False)
            return False

        # Cosmetic (diagnostic sensor only). Some firmware revisions omit the
        # "mcu"/"wireless" sub-keys entirely, so their versions are optional
        # and fall back to "unknown" instead of failing the update.
        firm_type = response.get("firmType", "unknown")
        mcu_ver = (response.get("mcu") or {}).get("firmVer", "unknown")
        wireless_ver = (response.get("wireless") or {}).get("firmVer", "unknown")
        firmware = f"{firm_type}, mcu: {mcu_ver}, wireless: {wireless_ver}"
        if firmware != self._firmware:
            # BETA DEBUG (#329) - remove before the final release. Which
            # firmware branch a report comes from decided the whole diagnosis
            # there, and it was two rounds of asking to find out.
            _LOGGER.debug("[%s] reports firmware %s", self.device_name, firmware)
        self._firmware = firmware

        self._firm_type = response.get("firmType")
        self._wireless_firmware_ver = (response.get("wireless") or {}).get("firmVer")
        self._maybe_check_firmware_update()
        self._maybe_request_service_data()
        return True

    def _maybe_check_firmware_update(self) -> None:
        """Kick off a background cloud firmware check if one is due (see
        FIRMWARE_CHECK_INTERVAL). Fire-and-forget: the result lands whenever
        the request completes and reaches entities via async_set_updated_data()
        in _async_check_firmware_update() below, independent of the regular
        60s poll cycle that triggered this check.
        """
        # Hard opt-in gate, checked first and unconditionally: this is the
        # only outbound internet call anywhere in this integration (every
        # other request stays on the local network) - users who leave the
        # option off must get zero cloud traffic, not just a less frequent one.
        if not self._firmware_update_check_enabled:
            return
        if not self._firm_type or not self._wireless_firmware_ver:
            return
        now = dt_util.utcnow()
        if (
            self._last_firmware_check is not None
            and now - self._last_firmware_check < FIRMWARE_CHECK_INTERVAL
        ):
            return
        self._last_firmware_check = now
        self.hass.async_create_task(
            self._async_check_firmware_update(
                self._firm_type, self._wireless_firmware_ver
            )
        )

    async def _async_check_firmware_update(
        self, firm_type: str, wireless_firmware_ver: str
    ) -> None:
        """Compare the locally-reported wireless firmware version against the
        manufacturer's latest for this firmType."""
        latest = await fetch_latest_firmware(self.hass, firm_type)
        if latest is None or latest.get("wireless") is None:
            return

        try:
            # Strictly-greater-than only: the module treats a requested
            # firmVer <= its current one as "nothing to do" and returns 200 OK
            # without flashing - a `!=` check would misreport that harmless
            # case as an available downgrade.
            update_available = int(latest["wireless"]) > int(wireless_firmware_ver)
        except (TypeError, ValueError):
            _LOGGER.debug(
                "Could not compare firmware versions: local=%r latest=%r",
                wireless_firmware_ver,
                latest["wireless"],
            )
            return

        self._latest_wireless_firmware_ver = latest["wireless"]
        self._firmware_update_available = update_available
        self.async_set_updated_data(self._airco)

    def _detect_foreign_activity(self, expires: Any) -> None:
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

        expires_moved = (
            isinstance(expires, int)
            and isinstance(self._account_expires, int)
            and expires > self._account_expires
        )
        if expires_moved and not wrote:
            self._note_foreign_write(f"expires moved {self._account_expires} -> {expires}")
        # Not `expires_moved or wrote`: our own operation-data request moves
        # `expires` in most cycles and carries no set-bits, so that reading
        # would call every change unattributable and never name the one thing
        # this detector exists for. What is asked here is narrower - could a
        # write, ours or anyone's, have moved a setting in this gap?
        self._note_unexpected_settings(
            wrote_settings or (expires_moved and not wrote)
        )
        self._report_foreign_activity()

    def _note_foreign_write(self, evidence: str) -> None:
        if self._foreign_activity_since is None:
            self._foreign_activity_since = dt_util.utcnow()
        self._foreign_activity_until = dt_util.utcnow() + FOREIGN_ACTIVITY_BACKOFF
        _LOGGER.debug("Another client wrote to [%s]: %s", self.device_name, evidence)

    def _settings_snapshot(self) -> dict[str, Any] | None:
        """The fields nothing but a write changes.

        Deliberately none of the measurements: temperatures, currents and the
        operation-data values move on their own every cycle. Vacant and
        self-clean are left out for the same reason one step removed - the
        unit turns those on by itself, and both drag a setpoint with them.
        """
        if self._airco is None:
            return None
        return {
            name: getattr(self._airco, name)
            for name in (
                "Operation",
                "OperationMode",
                "PresetTemp",
                "AirFlow",
                "WindDirectionUD",
                "WindDirectionLR",
                "Entrust",
            )
        }

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
        current = self._settings_snapshot()
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
                self.device_name,
                changed,
            )
            return
        _LOGGER.debug(
            "[%s] was changed at the unit itself - nothing took the write "
            "lock: %s",
            self.device_name,
            changed,
        )

    async def _async_write_lock_delay(self) -> float:
        """Seconds to wait before retrying a write the unit just refused.

        The refusal carries no deadline with it, and the `expires` from the
        last poll is our own stale one - the lock in the way was taken after
        that poll, which is why we did not see it coming. So ask: a
        getAirconStat is cheap and takes no lock of its own, and it reports
        when the lock currently held lapses.

        That deadline can be read against our own clock directly, because the
        module has none: it takes its time from the `timestamp` field of every
        request it receives, so the request asking the question sets the clock
        the answer is measured against. What that cannot fix is a deadline
        stamped by a client whose own clock was off - hence the cap.

        Falls back to WRITE_LOCK_RETRY_DELAY when the unit does not answer or
        reports no `expires` at all.
        """
        try:
            response = await self._api.get_aircon_stats(self._airco_id)
            expires = response["expires"]
        except (WfRacError, KeyError, TypeError, ValueError):
            return WRITE_LOCK_RETRY_DELAY.total_seconds()
        if not isinstance(expires, int):
            return WRITE_LOCK_RETRY_DELAY.total_seconds()
        # The module compares whole seconds and refuses while `expires` still
        # equals the current one, so land on the far side of the lapse.
        remaining = expires - dt_util.utcnow().timestamp() + 1
        return max(0.0, min(remaining, WRITE_LOCK_MAX_WAIT.total_seconds()))

    def _report_foreign_activity(self) -> None:
        """Say so, once, when we start and stop holding back.

        Worth a log line rather than only the binary sensor: while this is on,
        the operation-data sensors report unknown, and someone reading the log
        to find out why deserves to find the reason there.
        """
        active = self.foreign_activity
        if active == self._foreign_activity_reported:
            return
        self._foreign_activity_reported = active
        if active:
            _LOGGER.info(
                "Another client is controlling [%s]; pausing operation-data "
                "requests for up to %.0fs so it keeps working. Its sensors "
                "hold their last values meanwhile.",
                self.device_name,
                FOREIGN_ACTIVITY_BACKOFF.total_seconds(),
            )
        else:
            _LOGGER.info(
                "No other client active on [%s]; resuming operation-data requests",
                self.device_name,
            )

    def _settle_service_data_pause(self) -> None:
        """Once a stand-down ends, move the operation-data age anchor forward
        by however long it lasted.

        Without this the readings would expire on the very first poll after
        resuming: the gap is one we chose, so counting it against
        SERVICE_DATA_MAX_AGE would throw away values that are perfectly good
        and about to be refreshed anyway.

        Kept out of _report_foreign_activity() on purpose - that one only
        logs, and runs after _carry_forward_service_data() has already
        decided. This has to have happened before that decision.
        """
        if self._foreign_activity_since is None or self.foreign_activity:
            return
        if (
            self._last_service_data_response is not None
            and self._foreign_activity_until is not None
        ):
            self._last_service_data_response += (
                self._foreign_activity_until - self._foreign_activity_since
            )
        self._foreign_activity_since = None

    @property
    def foreign_activity(self) -> bool:
        """Whether another client wrote to the unit recently enough that we
        are still standing down - see FOREIGN_ACTIVITY_BACKOFF."""
        return (
            self._foreign_activity_until is not None
            and dt_util.utcnow() < self._foreign_activity_until
        )

    def _status_request_is_allowed(self) -> bool:
        """Whether an operation-data request may go out right now.

        False on a unit we have given up asking (#329), and false on one that
        carries its state while we believe it is off: there the block is
        applied rather than ignored, so an "off" read a moment before the frame
        goes out would be written back as a command if the remote switched the
        unit on in between. A reading taken while the unit is off is worth
        little anyway, so it waits for a poll that finds it running.
        """
        if self._status_request_mode == STATUS_REQUEST_SILENT:
            return False
        return not self._parser.status_request_carries_state or bool(
            self._airco is not None and self._airco.Operation
        )

    def _maybe_request_service_data(self) -> None:
        """Kick off a background request for active operation-data segments
        when due (see SERVICE_DATA_MIN_SPACING).
        """
        service_data_codes = self._subscribed_service_data_codes()
        if not service_data_codes:
            return
        if not self._status_request_is_allowed():
            return
        if self.foreign_activity:
            # Skipped entirely rather than deferred: this request would take
            # the write lock for another 60s and is worth far less than
            # leaving the unit controllable from whatever is using it.
            return
        if self._service_data_task is not None and not self._service_data_task.done():
            # A retry from the previous cycle is still in flight; piling a
            # second request on top is exactly the crowding this avoids.
            return
        now = dt_util.utcnow()
        if (
            self._last_service_data_request is not None
            and now - self._last_service_data_request < SERVICE_DATA_MIN_SPACING
        ):
            return
        # Stamped now, not when the request actually goes out, so the offset
        # below shifts the request within the cycle instead of stretching the
        # interval between requests.
        self._last_service_data_request = now
        # Background task, not a plain one: it spends most of its life asleep
        # waiting out the offset, and HA cancels background tasks at shutdown
        # instead of waiting for them.
        self._service_data_task = self.hass.async_create_background_task(
            self._async_request_service_data(service_data_codes),
            name=f"{DOMAIN} service data request {self._airco_id}",
        )

    def _service_data_stamp_backdate(self) -> timedelta:
        """How far to backdate the next operation-data request's timestamp.

        Normally SERVICE_DATA_STAMP_BACKDATE, so the request's write lock runs
        short and leaves the app a window. But an operation-data request shares
        our deviceId with our real commands, and the module never blocks a
        writer from its own lease (the deviceId check passes) - so a backdated
        request sent just after a real command would overwrite that command's
        full 60s lock with a short one, cutting the very protection the command
        needs against being reverted. Within one lock's span of a real command,
        stamp the request honestly instead: it renews the command's lease
        rather than trimming it, at the cost of holding the lock for that one
        cycle.
        """
        if self._last_command_at is not None and (
            dt_util.utcnow() - self._last_command_at < MIN_TIME_BETWEEN_UPDATES
        ):
            return timedelta(0)
        return SERVICE_DATA_STAMP_BACKDATE

    async def _async_read_before_echo(self) -> bool:
        """Read the unit's state immediately before sending it back.

        A carrying request writes every field it contains, so anything changed
        at the unit since the last poll would be undone by a state that old.
        Reading here narrows that window from a poll cycle to one round trip.
        Only the state is taken: availability, foreign-activity attribution and
        the firmware fields belong to the poll, and running them from here
        would attribute this read's own effects to somebody else.
        """
        try:
            response = await self._api.get_aircon_stats(self._airco_id)
            new_airco = self._parser.translate_bytes(response["airconStat"])
        except (WfRacError, KeyError, TypeError, ValueError) as ex:
            _LOGGER.debug(
                "Could not read [%s] before echoing its state back, "
                "skipping the request: %s",
                self.device_name,
                ex,
            )
            return False
        self._carry_forward_home_leave_mode(new_airco)
        self._carry_forward_service_data(new_airco)
        self._airco = new_airco
        return True

    async def _async_request_service_data(self, service_data_codes: tuple[int, ...]) -> None:
        """Ask the unit for operation-data segments, offset from the poll and
        retried once if the unit refuses it (see SERVICE_DATA_REQUEST_OFFSET).
        Sends directly rather than through async_queue_command() so the
        refusal is visible here: a queued command is flushed by a detached
        task that deliberately swallows its errors.
        """
        await asyncio.sleep(self.service_data_offset.total_seconds())
        # What the frame carries depends on the module: no set-bits at all
        # without the #329 quirk, a full command with it (see
        # RacParser.status_request_to_byte). Byte 5 goes out either way,
        # having no set-bit to leave out, which is how an active external
        # temperature override stays alive between commands - and what makes
        # this request a write in the strict sense, for which the backdated
        # timestamp below gives back part of the lock.
        if (
            self._parser.status_request_carries_state
            and not await self._async_read_before_echo()
        ):
            return
        if not self._status_request_is_allowed():
            # Re-checked after the sleep, not only when the request was
            # scheduled: the offset is up to half a minute, and the unit going
            # off inside it is exactly the window this protects.
            _LOGGER.debug(
                "Skipping the operation-data request for [%s]: it is either "
                "not sent to this unit at all, or the unit is off and the "
                "request would carry that state back to it",
                self.device_name,
            )
            return
        params = {AirconCommands.ServiceDataStatusRequest: service_data_codes}
        timestamp_offset = -round(self._service_data_stamp_backdate().total_seconds())
        # Taken here rather than at the poll: what the response has to be read
        # against is the state this very frame was built from, and on the echo
        # path _async_read_before_echo() has just refreshed it.
        before = self._settings_snapshot()
        for attempt in (1, 2):
            try:
                await self.set_airco(
                    params,
                    log_failure=False,
                    timestamp_offset=timestamp_offset,
                    is_status_request=True,
                    retry_when_locked=False,
                )
                if attempt > 1:
                    _LOGGER.debug("Service data request succeeded on retry")
                self._check_request_was_applied(before)
                self._note_service_data_offset_survived()
                # Notify, but deliberately not through async_set_updated_data():
                # that resets the refresh timer, and this runs half a cycle
                # after the poll - every cycle - so it would push the next poll
                # to 90s and keep doing so, silently turning the documented
                # 60s cadence into something else. Listeners get the fresh
                # state (set_airco() has already stored it) without the
                # schedule moving.
                self.async_update_listeners()
                return
            except WfRacWriteRefusedError as ex:
                # Someone else may hold the write lock. Unlike a user command
                # this is not worth contesting: give the cycle up immediately
                # rather than retrying into a lock we would only be renewing
                # for ourselves if we won it. set_airco()'s own wait-and-retry
                # is off for this call (retry_when_locked=False), or the
                # refusal would arrive here already contested.
                _LOGGER.debug(
                    "Service data request declined for [%s], skipping this "
                    "cycle: %s",
                    self.device_name,
                    ex,
                )
                return
            except WfRacCommandError as ex:
                if attempt == 1:
                    # A refusal here is the module saying the request came too
                    # close to something else, which is exactly what the offset
                    # is for - so widen it, whether or not the retry gets
                    # through.
                    self._widen_service_data_offset()
                    _LOGGER.debug("Service data request refused (%s); retrying", ex)
                    await asyncio.sleep(SERVICE_DATA_RETRY_DELAY.total_seconds())
                    continue
                # Debug, not a warning: the module refuses these requests
                # transiently and a single skipped cycle changes nothing the
                # user can see - the values survive SERVICE_DATA_MAX_AGE. The
                # warning belongs where they actually expire, see
                # _note_service_data_expired().
                _LOGGER.debug(
                    "Service data request refused twice, skipping this cycle "
                    "for [%s]: %s",
                    self.device_name,
                    ex,
                )
            except (WfRacError, KeyError, TypeError, ValueError):
                # Unreachable or unparseable: the poll itself reports that, and
                # this request is an optional extra on top of it.
                return
        # Entities keep their previous operation-data values on a skipped cycle
        # (see _carry_forward_service_data), so there is nothing to push here.

    def _carry_forward_service_data(self, new_airco: Aircon) -> None:
        """Same rationale as _carry_forward_home_leave_mode() above: the unit
        reports these extension segments exactly once, so without this the
        sensors would flash the real value for one update cycle and then
        revert to unknown.

        Unlike home/leave mode this expires: see SERVICE_DATA_MAX_AGE. Time
        spent standing down for another client is not counted against that
        age, though - see foreign_activity. SERVICE_DATA_MAX_AGE guards
        against a value frozen by a unit that stopped answering, which is
        indistinguishable from a live one; a pause we chose ourselves is
        neither indistinguishable nor a fault, and External Control says so
        while it lasts. Dropping perfectly good readings for it would be a
        worse answer than carrying them a few minutes longer.
        """
        if self._airco is None:
            return
        self._settle_service_data_pause()
        now = dt_util.utcnow()
        if any(getattr(new_airco, name) is not None for name in SERVICE_DATA_FIELDS):
            self._last_service_data_response = now
            if self._service_data_expired:
                self._service_data_expired = False
                _LOGGER.info(
                    "Operation data from [%s] is being reported again",
                    self.device_name,
                )
        elif self.foreign_activity:
            pass  # Not stale, just paused - carry the values below.
        elif (
            self._last_service_data_response is None
            or now - self._last_service_data_response > SERVICE_DATA_MAX_AGE
        ):
            # Nothing fresh for too long - leave the fields unset so entities
            # report unknown rather than a value that stopped being true.
            self._note_service_data_expired(now)
            return
        for name in SERVICE_DATA_FIELDS:
            if getattr(new_airco, name) is not None:
                continue
            source = SERVICE_DATA_DERIVED_FROM.get(name)
            if source is not None and getattr(new_airco, source) is not None:
                # Segment arrived, value unusable - see SERVICE_DATA_DERIVED_FROM.
                continue
            setattr(new_airco, name, getattr(self._airco, name))

    def _note_service_data_expired(self, now: datetime) -> None:
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
        if self._service_data_expired:
            return
        # Before the first response there is nothing to lose yet; anchor on the
        # first request instead so a module that never answers is still
        # reported, once, rather than silently leaving the sensors unknown.
        anchor = self._last_service_data_response or self._last_service_data_request
        if anchor is None or now - anchor <= SERVICE_DATA_MAX_AGE:
            return
        self._service_data_expired = True
        _LOGGER.warning(
            "No operation data from [%s] for over %.0fs; its compressor, "
            "current, temperature and EEV sensors now report unknown. A "
            "network interruption is the usual cause - check whether other "
            "devices dropped out at the same time",
            self.device_name,
            SERVICE_DATA_MAX_AGE.total_seconds(),
        )

    async def delete_account(self) -> dict[str, Any] | None:
        """Delete account (operator id) from the airco"""
        try:
            return await self._api.del_account_info(self._airco_id)
        except (WfRacError, KeyError, TypeError):
            _LOGGER.warning("Could not delete account from airco %s", self._airco_id)
            return None

    async def add_account(self) -> dict[str, Any] | None:
        """Add account (operator id) from the airco"""
        try:
            result = await self._api.update_account_info(
                self._airco_id, self.hass.config.time_zone
            )
        except (WfRacError, KeyError, TypeError):
            _LOGGER.warning("Could not add account from airco %s", self._airco_id)
            return None

        # On updateAccountInfo specifically, result:2 does mean the account
        # table is full: the module answers it when no slot matches our id and
        # none is free. (The same code means other things on setAirconStat -
        # see RESULT_CODES - but this endpoint never talks to the indoor unit,
        # so those paths cannot reach it here.)
        #
        # Nothing frees a slot on its own: registrations do not expire and are
        # never evicted, so re-registering cannot succeed until someone
        # removes one from the official app - or the module is set up afresh.
        # That is a standing condition worth a repair issue rather than a
        # warning that scrolls out of the log every cycle; a normal-looking
        # response means whatever caused it is gone, so the issue (if any)
        # clears itself.
        if result and int(result.get("result", 0)) == 2:
            self._report_registration_full()
        else:
            self._clear_registration_full_issue()
        return result

    @property
    def service_data_offset(self) -> timedelta:
        """How long after a poll the operation-data request goes out."""
        if self._service_data_offset is None:
            return SERVICE_DATA_REQUEST_OFFSET
        return self._service_data_offset

    def _widen_service_data_offset(self) -> None:
        """Put more distance between the poll and the request after a refusal.

        Doubling rather than stepping: a refused request costs every
        operation-data sensor a reading, and several in a row is what an
        offset that is much too short looks like, so overshooting once is
        cheaper than creeping up on it.
        """
        if self.service_data_offset >= SERVICE_DATA_REQUEST_OFFSET:
            return
        self._service_data_good_cycles = 0
        self._service_data_offset = min(
            self.service_data_offset * 2, SERVICE_DATA_REQUEST_OFFSET
        )
        _LOGGER.debug(
            "Moving the operation-data request for [%s] to %.0fs after the "
            "poll: the module refused it where it was",
            self.device_name,
            self.service_data_offset.total_seconds(),
        )

    def _note_service_data_offset_survived(self) -> None:
        """Move the request back towards the poll while requests keep landing.

        Closer is better for everything except crowding: what the request
        carries, and what any judgement about who changed the unit rests on,
        is as old as the last poll.
        """
        if self.service_data_offset <= SERVICE_DATA_OFFSET_MIN:
            return
        self._service_data_good_cycles += 1
        if self._service_data_good_cycles < SERVICE_DATA_OFFSET_GOOD_CYCLES:
            return
        self._service_data_good_cycles = 0
        self._service_data_offset = max(
            self.service_data_offset - SERVICE_DATA_OFFSET_STEP,
            SERVICE_DATA_OFFSET_MIN,
        )
        _LOGGER.debug(
            "Moving the operation-data request for [%s] to %.0fs after the "
            "poll: %s cycles without a refusal",
            self.device_name,
            self.service_data_offset.total_seconds(),
            SERVICE_DATA_OFFSET_GOOD_CYCLES,
        )

    def _empty_block_matches(self, before: Mapping[str, Any]) -> list[str]:
        """The settings that moved to exactly what an all-zero command block
        encodes, comparing the state before our own status request with the one
        the unit answered it with. Empty when anything moved somewhere else.

        That veto is what separates this from an ordinary change. A block
        applied as zeros lands every field on its minimum at once; a person at
        the remote picks values, and one field moving to a value of its own is
        enough to say this was not our frame being applied.

        The setpoint counts when it moved *down*: its zero is clamped by the
        unit to a floor we do not know from here, so the value cannot be
        predicted - only the direction can. Upwards it is a contradiction like
        any other.
        """
        if self._airco is None:
            return []
        matched: list[str] = []
        for name, zero in EMPTY_BLOCK_SETTINGS.items():
            if before.get(name) == getattr(self._airco, name):
                continue
            if getattr(self._airco, name) != zero:
                return []
            matched.append(name)
        setpoint_before = before.get("PresetTemp")
        if setpoint_before is not None and self._airco.PresetTemp != setpoint_before:
            if self._airco.PresetTemp > setpoint_before:
                return []
            matched.append("PresetTemp")
        return matched

    def _check_request_was_applied(self, before: Mapping[str, Any] | None) -> None:
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
        if before is None or self._status_request_mode == STATUS_REQUEST_SILENT:
            return
        matched = self._empty_block_matches(before)
        if len(matched) < EMPTY_BLOCK_MATCH_MIN:
            return
        changed = ", ".join(
            f"{name} {before[name]} -> {getattr(self._airco, name)}"
            for name in matched
        )
        if self._status_request_mode == STATUS_REQUEST_STRICT:
            self._adopt_status_request_mode(STATUS_REQUEST_ECHO)
            _LOGGER.warning(
                "[%s] applied the settings in the request that asked it for "
                "operation data, which carries none: %s. From now on that "
                "request carries the unit's own settings back to it, so the "
                "frame confirms them instead of clearing them",
                self.device_name,
                changed,
            )
        else:
            self._adopt_status_request_mode(STATUS_REQUEST_SILENT)
            _LOGGER.warning(
                "[%s] changed its settings during an operation-data request "
                "that carried its own state: %s. Nothing this integration can "
                "put in that frame leaves the unit alone, so it will not be "
                "sent again. The operation-data sensors and the external "
                "temperature override stop working on this unit; everything "
                "else is unaffected",
                self.device_name,
                changed,
            )
        self.hass.async_create_task(self._async_restore_settings(before))
        if self._status_request_mode == STATUS_REQUEST_SILENT:
            # The advice in the first issue - watch it for a few minutes - has
            # been overtaken by what just happened.
            ir.async_delete_issue(
                self.hass, DOMAIN, request_stops_unit_issue_id(self.entry_id)
            )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            request_stops_unit_issue_id(self.entry_id)
            if self._status_request_mode == STATUS_REQUEST_ECHO
            else status_request_unsupported_issue_id(self.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=(
                "request_stops_unit"
                if self._status_request_mode == STATUS_REQUEST_ECHO
                else "status_request_unsupported"
            ),
            translation_placeholders={"device_name": self.device_name},
        )

    def _adopt_status_request_mode(self, mode: str) -> None:
        """Switch the shape of the status request and write the choice down.

        Persisted because it is a property of the unit in front of us, not of
        this run: a restart that forgot it would put the unit through the same
        disturbance again to learn the same thing - which is exactly what the
        counter this replaced did, every time an update restarted Home
        Assistant.
        """
        self._status_request_mode = mode
        self._parser.status_request_carries_state = mode == STATUS_REQUEST_ECHO
        entry = self.config_entry
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_STATUS_REQUEST_MODE: mode}
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
            await self.set_airco(
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
                self.device_name,
                ex,
            )

    def _report_registration_full(self) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            registration_full_issue_id(self.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="too_many_devices",
            translation_placeholders={"device_name": self.device_name},
        )

    def _clear_registration_full_issue(self) -> None:
        ir.async_delete_issue(
            self.hass, DOMAIN, registration_full_issue_id(self.entry_id)
        )

    async def set_airco(
        self,
        params: dict[AirconCommands, Any],
        *,
        log_failure: bool = True,
        timestamp_offset: int = 0,
        is_status_request: bool = False,
        retry_when_locked: bool = True,
    ) -> None:
        """Method to send airco command.

        log_failure=False leaves the reporting to the caller, for requests that
        have their own retry and a quieter failure story than a user command
        that never reached the unit - see _async_request_service_data().

        is_status_request marks a frame that asks for readings instead of
        changing anything: its block carries no set-bits (see
        RacParser.status_request_to_byte), so the settings it echoes back are
        the unit's own and claiming them as our expectation would hide
        whatever the IR remote did while the request was in flight.

        retry_when_locked=False hands the refusal straight back to the caller
        instead of waiting the foreign write lock out below. For an optional
        read that is the whole answer - the caller skips the cycle - and the
        wait itself is the harm: it runs inside _send_lock, where a real user
        command would be stuck behind it for up to WRITE_LOCK_MAX_WAIT.

        timestamp_offset shifts the `timestamp` this request stamps, and so the
        write lock it takes (deadline is timestamp + 60, the module has no RTC).
        Negative for operation-data requests, to give up part of the lock - see
        SERVICE_DATA_STAMP_BACKDATE. Left at 0 for real commands.
        """
        _LOGGER.debug("Setting airco: %s", params)
        # Held for the whole read-modify-send-update sequence, not just the
        # send: the snapshot below must only ever be built from self._airco
        # once no other set_airco() call is still in flight, otherwise a
        # queued command (see async_queue_command()) could snapshot state
        # from before a concurrent call's response landed and, once sent,
        # silently revert whatever that call had just changed.
        async with self._send_lock:
            if self.airco is None:
                # update() is a coroutine function; async_add_executor_job is for
                # blocking sync calls and would not actually run it (no event loop
                # in the executor thread), so the coroutine was silently never
                # awaited. Await it directly instead.
                await self.update()

            if self._airco is None:
                raise ValueError("Airco object is empty")

            airco_stat = AirconStat.from_aircon(self._airco)

            # Not a command parameter: the override has no set-bit of its own
            # and is never written for its own sake, it only rides along on
            # frames that were going out anyway (see AircoClimate.
            # async_set_external_temperature). Applied to every frame, since
            # one that leaves byte 5 alone reverts the unit to its own sensor.
            airco_stat.ExternalTemperature = self._external_temperature_override

            for key, value in params.items():
                setattr(airco_stat, key, value)

            # After the parameters, not before: the correction depends on the
            # mode this frame is putting the unit into, which a command in
            # params may just have changed.
            airco_stat.ExternalTemperature = self._corrected_external_temperature(
                airco_stat.ExternalTemperature, airco_stat.OperationMode
            )

            try:
                command = self._parser.to_base64(airco_stat)
                try:
                    response = await self._api.send_airco_command(
                        self._airco_id, command, timestamp_offset=timestamp_offset
                    )
                except WfRacWriteRefusedError:
                    # Most likely another client's 60-second write lock - the
                    # Smart M-Air app was used moments ago. Waiting it
                    # out is the only thing that helps: our registration is
                    # fine, so re-registering would just cost a request. One
                    # retry, placed where the lock lapses rather than at a
                    # guessed interval - a retry that lands inside the same
                    # lock is a request spent on a refusal that was certain.
                    if not retry_when_locked:
                        raise
                    await asyncio.sleep(await self._async_write_lock_delay())
                    response = await self._api.send_airco_command(
                        self._airco_id, command, timestamp_offset=timestamp_offset
                    )
                except WfRacRegistrationError:
                    # Our operator id is not in the airco's account table.
                    # Re-register and try once more rather than losing the
                    # command outright. If the table is full instead,
                    # add_account() has already raised the repair issue.
                    await self.add_account()
                    response = await self._api.send_airco_command(
                        self._airco_id, command, timestamp_offset=timestamp_offset
                    )
                # Only a successful write moves the module's `expires`, so
                # only a successful write may claim the next rise in it. A
                # status request moves it too - it is a setAirconStat like any
                # other - which is why the narrower flag below is a separate
                # one and not this same value read twice.
                self._wrote_since_last_poll = True
                if not is_status_request or self._parser.status_request_carries_state:
                    # A status request's block carries no set-bits, so nothing
                    # in it can explain a setting that moved - unless this is
                    # one of the units we carry the power state for (#329),
                    # where the frame really does write Operation.
                    self._wrote_settings_since_last_poll = True
                new_airco = self._parser.translate_bytes(response)
                self._carry_forward_home_leave_mode(new_airco)
                self._carry_forward_service_data(new_airco)
                self._airco = new_airco
                # Our own write is not a foreign one: move the expectation to
                # what the unit reports back, or the next poll would read this
                # command as somebody else's (see _note_unexpected_settings).
                # Not for a status request: it changes nothing, so its response
                # is a reading of whatever the unit is doing now - including a
                # change made at the unit since the poll, which taking it as
                # our expectation would swallow whole.
                if not is_status_request:
                    self._expected_settings = self._settings_snapshot()
                # After the write, and only for what the frame really carried:
                # a command sent while the unit is off writes the sentinel, not
                # the override.
                written = self._parser.external_temperature_raw_in_frame(airco_stat)
                if written is None:
                    self._external_temperature_written.clear()
                else:
                    self._external_temperature_written.append(written)
            except (WfRacError, KeyError, TypeError, ValueError) as ex:
                if log_failure:
                    _LOGGER.warning("Could not send airco data: %s", str(ex))
                raise

    async def async_queue_command(self, params: dict[AirconCommands, Any]) -> None:
        """Queue an airco command, coalescing with any other calls made within
        UPDATE_CONSOLIDATION_PERIOD into a single set_airco() call. Used by all
        entities instead of calling set_airco() directly, so that e.g. a fan
        speed change and a temperature change issued moments apart end up in
        the same request instead of racing each other.
        """
        self._consolidated_params.update(params)
        if self._consolidation_task is None:
            self._consolidation_task = self.hass.async_create_task(
                self._async_flush_queued_command()
            )
        # Every caller awaits the one flush its parameters ended up in, so a
        # refusal by the unit reaches the action that caused it instead of
        # being logged into the void. Shielded because the task is shared: a
        # caller giving up (a cancelled service call) must not take the other
        # callers' command down with it.
        await asyncio.shield(self._consolidation_task)

    def _carry_forward_home_leave_mode(self, new_airco: Aircon) -> None:
        """The unit reports the Tag-248 HomeLeaveMode extension segment exactly
        once per HomeLeaveModeStatusRequest, then stops: the bridge MCU clears
        its response cache after handing it to the WiFi side, so the segment is
        present in a short window's worth of status blocks and absent from every
        later poll. Observed effect: translate_bytes() builds a fresh Aircon()
        with both fields back at their None default, which made the diagnostic
        sensors flash the real value for one update cycle and then revert to
        unknown. Carry the last known reading forward instead so it survives
        until the next explicit request or a fresh None response (e.g.
        reconnect).
        """
        if self._airco is None:
            return
        if new_airco.HomeLeaveModeForCooling is None:
            new_airco.HomeLeaveModeForCooling = self._airco.HomeLeaveModeForCooling
        if new_airco.HomeLeaveModeForHeating is None:
            new_airco.HomeLeaveModeForHeating = self._airco.HomeLeaveModeForHeating

    async def async_request_home_leave_mode_status(self) -> None:
        """Ask the unit to report its current HomeLeaveMode (Tag 248,
        capability index 7) thresholds/airflow. Does not change any AC
        setting by itself - but the unit only reports this extension segment
        in response to this request, never on an unprompted poll, and matches
        byte-for-byte against the official app's own display.

        Timing, measured: the value shows up only on a later scheduled poll -
        up to MIN_TIME_BETWEEN_UPDATES (60s) later - not in the response to
        this call's own setAirconStat POST. A *single* extension request does
        come back inside that same POST response (see the service-data
        path), so the delay here is most likely because this request sends
        six segments and the unit answers them one bus frame at a time.
        Unconfirmed - if it matters, measure it rather than trusting this
        paragraph.

        _carry_forward_home_leave_mode() keeps the reading available on every
        following poll instead of it reverting to unknown.

        Sent directly through set_airco() rather than async_queue_command():
        the latter coalesces this with any command queued in the same
        window, and on a module whose status request holds no set-bits (see
        RacParser.status_request_to_byte), a coalesced real command - e.g. a
        setpoint change - would go out in that same block without its
        set-bit and be silently ignored by the unit.

        On a module that carries its state (#329) the block is a full command
        instead, so the state it is built from is read fresh first, exactly as
        the operation-data path does. Without that this action would write back
        a setting up to a poll old and undo whatever was done at the unit since
        - including switching a unit off that somebody just turned on.
        """
        if self._status_request_mode == STATUS_REQUEST_SILENT:
            # This unit changes its settings whatever we put in the frame, so
            # the frame is not sent at all any more (#329).
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="status_request_not_sent",
                translation_placeholders={"device": self.device_name},
            )
        if (
            self._parser.status_request_carries_state
            and not await self._async_read_before_echo()
        ):
            # Sending the state we have would be a write on this module, and
            # the caller asked for a reading.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="status_request_read_failed",
                translation_placeholders={"device": self.device_name},
            )
        await self.set_airco(
            {AirconCommands.HomeLeaveModeStatusRequest: True},
            is_status_request=True,
        )

    async def async_set_home_leave_mode(
        self, cooling: HomeLeaveModeSetting, heating: HomeLeaveModeSetting
    ) -> None:
        """Write new HomeLeaveMode thresholds/airflow (Tag 248, sub-codes
        27-32). Written values round-trip exactly through a subsequent
        read."""
        await self.async_queue_command(
            {
                AirconCommands.HomeLeaveModeForCooling: cooling,
                AirconCommands.HomeLeaveModeForHeating: heating,
            }
        )

    async def _async_flush_queued_command(self) -> None:
        await asyncio.sleep(UPDATE_CONSOLIDATION_PERIOD.total_seconds())
        params = self._consolidated_params.copy()
        self._consolidated_params.clear()
        self._consolidation_task = None
        # A real, set-bit command: mark it so the next operation-data request
        # stamps honestly and renews this command's lock rather than trimming
        # it (see _service_data_stamp_backdate).
        self._last_command_at = dt_util.utcnow()
        try:
            await self.set_airco(params)
        except (WfRacError, KeyError, TypeError, ValueError) as ex:
            # Already logged in set_airco(). Push the current state out first
            # so entities pick up self.available if the same failure flipped
            # it, then report: async_queue_command() awaits this task, so the
            # error lands on the action that issued the command instead of
            # becoming an orphaned "Task exception was never retrieved".
            # Wrapped rather than re-raised - a library exception in a service
            # call is a traceback, not something the user can read.
            self.async_set_updated_data(self._airco)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={
                    "device": self.device_name,
                    "error": str(ex),
                },
            ) from ex
        # Immediately push the (possibly unchanged, on failure) state to all
        # entities instead of leaving them to wait for the next poll (up to
        # MIN_TIME_BETWEEN_UPDATES later).
        self.async_set_updated_data(self._airco)

    def _set_availability(self, available: bool) -> bool:
        """Mark the device available, or unavailable once it has missed
        self._availability_failure_limit polls in a row.

        Return True only when the failure threshold is first reached or a
        later successful poll recovers from that threshold. Keeping the
        counter saturated while offline prevents a long outage from looking
        like a new transition every few polls.
        """
        if available:
            became_available = (
                self._consecutive_failures >= self._availability_failure_limit
            )
            self._consecutive_failures = 0
            self._available = True
            return became_available

        previous_failures = self._consecutive_failures
        self._consecutive_failures = min(
            previous_failures + 1, self._availability_failure_limit
        )
        if self._consecutive_failures >= self._availability_failure_limit:
            self._available = False
        return (
            previous_failures < self._availability_failure_limit
            <= self._consecutive_failures
        )

    def _record_connection_failure(self, error: BaseException) -> None:
        """Count one failed poll, and log it at the level it deserves.

        Every poll still reaches entities (_async_update_data returns the last
        data on an expected failure), so crossing the threshold needs no
        notification of its own - only the line that says it happened.
        """
        became_unavailable = self._set_availability(False)
        if became_unavailable:
            _LOGGER.warning(
                "Airco [%s] is unavailable after %s failed polls",
                self.device_name,
                self._availability_failure_limit,
            )
            _LOGGER.debug("Update of [%s] failed", self.device_name, exc_info=error)
        else:
            _LOGGER.debug(
                "Could not reach the airco [%s]: %s", self.device_name, error
            )

    @property
    def device_info(self) -> DeviceInfo:
        """Return a device description for device registry.

        No "model": the only model field the protocol offers is ModelNr, a
        capability grouping (0/1/2/3/64...), not a type name - it would put a
        bare digit where users expect "SRK35ZS-WF". It goes into model_id
        instead, which is what a machine-readable model identifier is for, and
        stays available as its own diagnostic sensor.
        """
        info: DeviceInfo = {
            "sw_version": self._firmware,
            "identifiers": {(DOMAIN, self.airco_id)},
            "manufacturer": "Mitsubishi Heavy Industries",
            "name": self.device_name,
        }
        # airconId is MAC-derived, and on every module seen so far it is the
        # bare MAC. Only claim it when it has exactly that shape - a differently
        # shaped id would otherwise register as somebody else's hardware and
        # merge two unrelated devices in the registry.
        if re.fullmatch(r"[0-9a-fA-F]{12}", self.airco_id):
            info["connections"] = {(CONNECTION_NETWORK_MAC, format_mac(self.airco_id))}
        model_nr = getattr(self.airco, "ModelNrRaw", None)
        if model_nr is not None:
            info["model_id"] = str(model_nr)
        return info

    @property
    def operator_id(self) -> str:
        """Return Airco Operator ID"""
        return self._operator_id

    @property
    def num_accounts(self) -> int:
        """Return Accounts connected"""
        return self._connected_accounts

    @property
    def updated_by(self) -> str | None:
        """Return what last updated the airco's state ('local' or a foreign account)"""
        return self._updated_by

    @property
    def account_expires(self) -> int | None:
        """Return the raw 'expires' timestamp reported alongside our account registration"""
        return self._account_expires

    @property
    def led_status(self) -> int | None:
        """Return the airco's front panel LED status"""
        return self._led_status

    @property
    def auto_heating(self) -> int | None:
        """Return the airco's auto-heating flag"""
        return self._auto_heating

    @property
    def wireless_firmware_version(self) -> str | None:
        """Return the locally-reported wireless-module firmware version"""
        return self._wireless_firmware_ver

    @property
    def latest_wireless_firmware_version(self) -> str | None:
        """Return the latest wireless-module firmware version known from the
        manufacturer's cloud, or None if not yet checked/unknown"""
        return self._latest_wireless_firmware_ver

    @property
    def firmware_update_available(self) -> bool | None:
        """Return whether a newer wireless-module firmware is available, or
        None if that hasn't been determined yet"""
        return self._firmware_update_available

    @property
    def firmware_update_check_enabled(self) -> bool:
        """Return whether the (online, cloud) firmware update check is enabled"""
        return self._firmware_update_check_enabled

    @property
    def device_id(self) -> str:
        """Return Airco device ID"""
        return self._device_id

    @property
    def host(self) -> str:
        """Get Host (IP)"""
        return self._host

    @property
    def port(self) -> int:
        """Get Port"""
        return self._port

    @property
    def device_name(self) -> str:
        """Get given Airco name"""
        return self._name

    @property
    def airco_id(self) -> str:
        """Return Airco ID"""
        return self._airco_id

    @property
    def airco(self) -> Aircon:
        """Return parsed Aircon object if set otherwise None"""
        return self._airco

    @property
    def available(self) -> bool:
        """Return True if device is available"""
        return self._available

    @property
    def swing_selects_enabled_default(self) -> bool:
        """Return the registry default for the standalone swing selects."""
        return self._swing_selects_enabled_default

    @property
    def connection_method(self) -> str | None:
        """Return the discovered/persisted communication method (http/https), if known."""
        return self._api.method

    @property
    def result_codes(self) -> dict[str, dict[str, int]]:
        """How often the unit refused each command, per `result` code.

        Refusals themselves are a debug-level event: the common ones clear on
        the next request and there is nothing for a user to do. Surfacing the
        tally here keeps them available to whoever is actually investigating.
        """
        return self._api.result_codes

    async def _async_update_data(self) -> Aircon:
        """Update data via library.

        A missed poll is not an update failure. These modules restart their
        WiFi about once an hour on their own, so single failures are routine
        and carry no consequence: _set_availability() rides them out, and
        entities follow Device.available rather than the coordinator's own
        success flag. Raising UpdateFailed for one would put an error in every
        user's log once an hour for a condition nobody can act on - and the
        entities would flick to unavailable a poll before our own threshold
        says they should. So an expected failure returns the last data instead,
        and only the availability transition is worth a line.
        """
        try:
            async with asyncio.timeout(POLL_TIMEOUT.total_seconds()):
                await self.update()
        except asyncio.TimeoutError as error:
            # The outer deadline can expire before the repository's individual
            # connection attempts do. Treat that exactly like any other missed
            # poll so transient outages stay quiet and the entity only becomes
            # unavailable at the configured threshold.
            self._record_connection_failure(
                WfRacConnectionError(
                    f"did not answer within {POLL_TIMEOUT.total_seconds():.0f}s"
                )
            )
        except Exception as error:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={
                    "device": self.device_name,
                    "error": str(error),
                },
            ) from error

        return self._airco
