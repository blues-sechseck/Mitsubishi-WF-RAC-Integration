"""Device module."""

import asyncio
from collections.abc import Mapping
from datetime import datetime, timedelta
import logging
import re
from typing import Any

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
    CONF_STATUS_REQUEST_MODE,
    DOMAIN,
    MIN_TIME_BETWEEN_UPDATES,
    STATUS_REQUEST_ECHO,
    STATUS_REQUEST_SILENT,
    STATUS_REQUEST_STRICT,
)
from .external_temperature import ExternalTemperatureFeed
from .firmware_check import fetch_latest_firmware
from .foreign_writers import ForeignWriterWatch
from .service_data import ServiceDataChannel

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

# The segment an armed external temperature override subscribes to on its own
# behalf (see _sync_external_temperature_carrier). Any code would do - what
# matters is that a request goes out at all, since that is the frame the
# override rides on - so this is the one that answers under every condition:
# it is per indoor unit and reads a temperature whatever the system is doing,
# which is why it is also the sensor the README recommends enabling first.

# A refused request costs a full cycle of every operation-data sensor, and
# these refusals are transient, so one retry is worth the extra request.
SERVICE_DATA_RETRY_DELAY = timedelta(seconds=5)

# One retry for a user command refused because someone else holds the lock,
# timed to land just after the lock lapses (see _async_write_lock_delay). Used
# as-is only when the remaining lock time cannot be established, where a short
# retry is still worth more than none: the common case is an app action already
# most of the way through its 60s. A retry that still fails is reported rather
# than repeated - two clients are genuinely fighting over the unit at that
# point.
WRITE_LOCK_RETRY_DELAY = timedelta(seconds=10)

# The lock runs 60 seconds, so a longer wait than that means the deadline was
# stamped by a client whose clock is off rather than that the lock is really
# still running - cap it instead of leaving a service call hanging on someone
# else's clock. See _async_write_lock_delay().
WRITE_LOCK_MAX_WAIT = timedelta(seconds=61)

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


def registration_full_issue_id(entry_id: str) -> str:
    """Repair-issue id for a full account table on this entry's airco.

    Shared with async_remove_entry, which clears it when the entry is deleted.
    An unload leaves it standing: the condition outlives a reload.
    """
    return f"too_many_devices_{entry_id}"


def _revision(value: Any) -> str:
    """One firmware string of a status answer, or "unknown" where none came."""
    return str(value) if value else "unknown"


def _firmware_version(section: Any) -> str:
    """The firmVer of one section of a status answer, or "unknown"."""
    if not isinstance(section, dict):
        return "unknown"
    return _revision(section.get("firmVer"))


def result_code(answer: Any) -> int | None:
    """The result code of a module answer, or None if it carries none.

    The parsed body arrives as it came, so neither its shape nor the field's
    type is guaranteed.
    """
    if not isinstance(answer, dict):
        return None
    try:
        return int(answer["result"])
    except (KeyError, TypeError, ValueError):
        return None


class Device(DataUpdateCoordinator[Aircon]):  # pylint: disable=too-many-instance-attributes
    """Device Class."""

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
        """Set up the coordinator for one airco."""
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
        self._poll_counted = False
        self._last_poll_error: BaseException | None = None
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
        # When we last sent a real (set-bit) command, so an operation-data
        # request within one lock's span of it stamps honestly instead of
        # trimming that command's lease - see _service_data_stamp_backdate().
        self._last_command_at: datetime | None = None
        self.foreign_writers = ForeignWriterWatch(self)
        self.service_data = ServiceDataChannel(self)
        self.external_temperature = ExternalTemperatureFeed(self)
        self._consecutive_failures = 0
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
        # _consolidation_task is only the one still taking parameters.
        self._running_flushes: set[asyncio.Task[None]] = set()

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
        """Id of the config entry that owns this device."""
        return self.config_entry.entry_id

    @property
    def external_temperature_override(self) -> float | None:
        """Return the integration-side external temperature override, if any."""
        return self.external_temperature.override

    def set_external_temperature_override(self, value: float | None) -> None:
        """Set the integration-side override state.

        See ExternalTemperatureFeed.set_override for what arming and clearing
        each mean on the wire.
        """
        self.external_temperature.set_override(value)

    async def async_release_external_temperature(self) -> None:
        """Hand the unit back to its own sensor before we stop writing.

        The injected value has no expiry at the unit: whatever byte 5 last
        carried is what it regulates on, until another frame replaces it or it
        loses power. While the entry runs, the carrier frame refreshes it every
        cycle and a source that stops reporting clears it - but once Home
        Assistant stops, nothing writes at all and the value would simply
        stand. So an orderly stop spends one last frame on clearing it, and the
        unit measures for itself again until the entry comes back and re-arms
        the override.

        Only an orderly stop: a reload is not one (saving the options reloads
        the entry, and the override is re-armed seconds later), and a crash or
        a lost network cannot send anything at all. That residue is the
        unit's, not ours to fix.
        """
        if self.external_temperature.override is None:
            return
        applied = self.external_temperature.applied
        self.external_temperature.set_override(None)
        if not applied:
            # Nothing on the unit to undo: it is on its own sensor already,
            # because it is off, in fan_only, or no frame ever carried the
            # value (see external_temperature_applied).
            return
        if not self._status_request_is_allowed():
            # The guard the periodic request answers to as well - a unit we
            # have given up asking, or one that applies the state a carrying
            # frame echoes while we believe it is off (#329). Handing control
            # back is not worth a frame that might switch the unit.
            return
        if (
            self._parser.status_request_carries_state
            and not await self._async_read_before_echo()
        ):
            return
        try:
            await self.set_airco(
                {
                    AirconCommands.ServiceDataStatusRequest: (
                        SERVICE_DATA_INDOOR_COIL_RAW,
                    )
                },
                log_failure=False,
                timestamp_offset=-round(
                    self._service_data_stamp_backdate().total_seconds()
                ),
                is_status_request=True,
                retry_when_locked=False,
            )
        except (WfRacError, KeyError, TypeError, ValueError) as ex:
            # Debug, not a warning: this runs while Home Assistant is going
            # down, there is nobody to act on it, and the next start re-arms
            # the override anyway.
            _LOGGER.debug(
                "Could not hand [%s] back to its own sensor before stopping: %s",
                self.device_name,
                ex,
            )
        else:
            _LOGGER.debug(
                "Handed [%s] back to its own room sensor before stopping",
                self.device_name,
            )

    def request_external_temperature_release(self) -> None:
        """Ask for a frame that hands the unit back to its own sensor.

        For the case this coordinator cannot see for itself - see
        ExternalTemperatureFeed.request_release.
        """
        self.external_temperature.request_release()

    async def async_shutdown(self) -> None:
        """Shut the coordinator down.

        Both tasks run on hass rather than under DataUpdateCoordinator, so
        they are cancelled here: one that survived would publish to entities
        that are gone, and take the single connection the reload needs.
        Failures are logged and swallowed - an unload that raises leaves
        entities on an entry that no longer updates.
        """
        self.external_temperature.release_carrier()
        # A flush that has taken its parameters lets go of
        # _consolidation_task, so that alone leaves nothing to cancel: the
        # send finishes afterwards and publishes to entities that are gone,
        # over the one connection the reload needs.
        self._consolidation_task = None
        for task in (*self._running_flushes, self.service_data.task):
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
        self.service_data.forget_task()
        await super().async_shutdown()

    @property
    def external_temperature_room_value(self) -> float | None:
        """The room temperature we handed the unit, or None while it regulates.

        See ExternalTemperatureFeed.room_value for why this is not simply what
        the unit reports back.
        """
        return self.external_temperature.room_value

    @property
    def external_temperature_applied(self) -> bool:
        """Whether the unit is currently regulating on a value we supplied."""
        return self.external_temperature.applied

    def _subscribed_service_data_codes(self) -> tuple[int, ...]:
        """Operation-data codes currently subscribed, sorted.

        One per enabled diagnostic sensor, plus the carrier an armed external
        temperature override holds (see _sync_external_temperature_carrier).
        """
        return tuple(
            sorted(set(self.async_contexts()).intersection(SERVICE_DATA_CODES))
        )

    async def update(self) -> bool:
        """Fetch one status block, and say whether the unit answered.

        Notifies nobody: _async_update_data() does that when it returns, the
        initial fetch runs before any entity exists, and set_airco()'s
        fallback fetch is followed by a command that notifies.
        """
        try:
            response = await self._api.get_aircon_stats(self._airco_id)

            if response is None:
                self._record_failed_poll(WfRacError("answered without any data"))
                _LOGGER.warning("Received no data for device %s", self._airco_id)
                return False
        except WfRacConnectionError as ex:
            self._record_failed_poll(ex)
            return False
        except (WfRacError, KeyError) as ex:
            # Not logged here: being dropped from the account table is one
            # outage, not one per poll, and _record_failed_poll() reports it
            # on the transition.
            self._record_failed_poll(ex)
            # The official app can evict us from the module's small account
            # table, and polls fail until we register again. An evicted
            # account still answers - unlike the branch above.
            await self.add_account()
            return False

        try:
            # .get(): this only feeds a diagnostic sensor, and a revision that
            # does not send it must not cost the poll that read the state block.
            self._connected_accounts = int(response.get("numOfAccount", -1))
            new_airco = self._parser.translate_bytes(response["airconStat"])
            self._carry_forward_home_leave_mode(new_airco)
            self.service_data.carry_forward(new_airco)
            self._airco = new_airco
            # Not part of the airconStat blob, present alongside it in the same
            # response. Tolerate absence (.get()) since it's undocumented and
            # could be missing on older firmware.
            self._updated_by = response.get("updatedBy")
            self.foreign_writers.detect(response.get("expires"))
            self._account_expires = response.get("expires")
            self._led_status = response.get("ledStat")
            self._auto_heating = response.get("autoHeating")
            self._record_reachable()
        except (KeyError, TypeError, ValueError) as ex:
            _LOGGER.warning("Could not parse airco data", exc_info=ex)
            self._record_failed_poll(ex)
            return False

        # Cosmetic (diagnostic sensor only). Some firmware revisions omit the
        # "mcu"/"wireless" sub-keys entirely, so their versions are optional
        # and fall back to "unknown" instead of failing the update.
        firmware = (
            f"{_revision(response.get('firmType'))}, "
            f"mcu: {_firmware_version(response.get('mcu'))}, "
            f"wireless: {_firmware_version(response.get('wireless'))}"
        )
        if firmware != self._firmware:
            # Logged because which firmware branch a report comes from
            # decided the whole diagnosis in #329, and finding it out cost two
            # rounds of asking. Debug level, once per change, so it is only
            # ever there when somebody is already collecting a log.
            _LOGGER.debug("[%s] reports firmware %s", self.device_name, firmware)
        self._firmware = firmware

        self._firm_type = response.get("firmType")
        self._wireless_firmware_ver = (response.get("wireless") or {}).get("firmVer")
        self._maybe_check_firmware_update()
        self.maybe_request_service_data()
        return True

    def _maybe_check_firmware_update(self) -> None:
        """Kick off a background cloud firmware check if one is due.

        Due is FIRMWARE_CHECK_INTERVAL. Fire-and-forget: the result lands
        whenever the request completes and reaches entities via
        async_set_updated_data() in _async_check_firmware_update() below,
        independent of the regular 60s poll cycle that triggered this check.
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
        """Compare the locally-reported wireless firmware version.

        Compared against the manufacturer's latest for this firmType.
        """
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

    async def _async_write_lock_delay(self) -> float:
        """Seconds to wait before retrying a write the unit just refused.

        The refusal carries no deadline and the last poll's `expires` is
        stale, so ask: a getAirconStat takes no lock of its own and reports
        when the one in the way lapses. It reads against our own clock, since
        the module takes its time from each request's `timestamp` - what that
        cannot fix is a deadline stamped by a client whose clock was off,
        hence the cap. Falls back to WRITE_LOCK_RETRY_DELAY when the unit does
        not answer or reports no `expires`.

        The answer is kept, not just its deadline: it carries what the other
        client wrote, and the retry's block is built from it.
        """
        try:
            response = await self._api.get_aircon_stats(self._airco_id)
            fresh = self._parser.translate_bytes(response["airconStat"])
            # Through the carry-forward helpers, not straight onto _airco: a
            # fresh block has no HomeLeaveMode and no service data in it, and
            # dropping those here would blank the diagnostic sensors for a
            # cycle exactly as an unprompted poll once did.
            self._carry_forward_home_leave_mode(fresh)
            self.service_data.carry_forward(fresh)
            self._airco = fresh
            expires = response["expires"]
        except (WfRacError, KeyError, TypeError, ValueError):
            return WRITE_LOCK_RETRY_DELAY.total_seconds()
        if not isinstance(expires, int):
            return WRITE_LOCK_RETRY_DELAY.total_seconds()
        # The module compares whole seconds and refuses while `expires` still
        # equals the current one, so land on the far side of the lapse.
        remaining = expires - dt_util.utcnow().timestamp() + 1
        return max(0.0, min(remaining, WRITE_LOCK_MAX_WAIT.total_seconds()))

    def settle_service_data_pause(self) -> None:
        """Move the operation-data age anchor past a stand-down we chose.

        See ForeignWriterWatch.pause_to_settle for why the gap is not counted
        against SERVICE_DATA_MAX_AGE.
        """
        span = self.foreign_writers.pause_to_settle()
        if span is not None:
            self.service_data.shift_anchor(span)

    @property
    def foreign_activity(self) -> bool:
        """Whether another client wrote recently enough that we stand down."""
        return self.foreign_writers.active

    @property
    def status_request_mode(self) -> str:
        """Which shape of operation-data request this unit gets, if any.

        Learned from the unit and persisted - see adopt_status_request_mode.
        """
        return self._status_request_mode

    @property
    def service_data_supported(self) -> bool:
        """Whether this unit answers operation-data requests at all."""
        return self.service_data.supported

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

    def maybe_request_service_data(self) -> None:
        """Kick off a background request for active operation-data segments.

        When due, that is - see SERVICE_DATA_MIN_SPACING.
        """
        service_data_codes = self._subscribed_service_data_codes()
        if not service_data_codes:
            return
        if (
            not self.service_data.supported
            and self.external_temperature.override is None
        ):
            # Nothing to read here. The frame still goes out for an armed
            # temperature override, which rides on it without needing an
            # answer.
            return
        if not self._status_request_is_allowed():
            return
        if self.foreign_activity:
            # Skipped entirely rather than deferred: this request would take
            # the write lock for another 60s and is worth far less than
            # leaving the unit controllable from whatever is using it.
            return
        if not self.service_data.due():
            return
        # Background task, not a plain one: it spends most of its life asleep
        # waiting out the offset, and HA cancels background tasks at shutdown
        # instead of waiting for them.
        self.service_data.adopt_task(
            self.hass.async_create_background_task(
                self._async_request_service_data(service_data_codes),
                name=f"{DOMAIN} service data request {self._airco_id}",
            )
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
        self.service_data.carry_forward(new_airco)
        self._airco = new_airco
        return True

    async def _async_request_service_data(
        self, service_data_codes: tuple[int, ...]
    ) -> None:
        """Ask the unit for operation-data segments.

        Offset from the poll and retried once if the unit refuses it (see
        SERVICE_DATA_REQUEST_OFFSET). Sends directly rather than through
        async_queue_command() so the refusal is visible here: a queued command
        is flushed by a detached task that deliberately swallows its errors.
        """
        await asyncio.sleep(self.service_data.offset.total_seconds())
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
        # Taken here rather than at the poll: what the answer has to be read
        # against is the state this very frame was built from, and on the echo
        # path _async_read_before_echo() has just refreshed it.
        before = self.foreign_writers.snapshot()
        # Kept for the poll as well. The module answers our request with its
        # own cached state, and the frame's trip down the CNS bus to the indoor
        # unit need not have finished by then - measured on the affected unit,
        # the settings it cleared showed up a poll later, not in the answer
        # (#329). Checking only the answer would have reproduced the blind spot
        # this detector was rewritten to close.
        self.foreign_writers.note_status_request(before)
        # _carry_forward_service_data() moves this whenever a segment arrives,
        # so comparing it across the request says whether this one was answered
        # - which the state itself cannot, the previous reading being carried
        # forward into it.
        answered_before = self.service_data.last_response
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
                self.foreign_writers.check_request_was_applied(before)
                self.service_data.note_whether_anything_answered(answered_before)
                self.service_data.note_offset_survived()
                # Notify, but deliberately not through async_set_updated_data():
                # that resets the refresh timer, and this runs half a cycle
                # after the poll - every cycle - so it would push the next poll
                # to 90s and keep doing so, silently turning the documented
                # 60s cadence into something else. Listeners get the fresh
                # state (set_airco() has already stored it) without the
                # schedule moving.
                self.async_update_listeners()
                # Not an else block: the two handlers below are what decide
                # whether the loop runs again, and splitting the success path
                # away from them would put that decision in two places.
                return  # noqa: TRY300
            except WfRacWriteRefusedError as ex:  # noqa: PERF203
                # Someone else may hold the write lock. Unlike a user command
                # this is not worth contesting: give the cycle up immediately
                # rather than retrying into a lock we would only be renewing
                # for ourselves if we won it. set_airco()'s own wait-and-retry
                # is off for this call (retry_when_locked=False), or the
                # refusal would arrive here already contested.
                _LOGGER.debug(
                    "Service data request declined for [%s], skipping this cycle: %s",
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
                    self.service_data.widen_offset()
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

    async def delete_account(self) -> dict[str, Any] | None:
        """Delete account (operator id) from the airco.

        None means the slot was not released - the request failed, or the
        answer did not confirm it. Nothing but a result code of 0 does: the
        refusal, the rate limit and the module's internal error all leave the
        slot where it was.
        """
        try:
            result = await self._api.del_account_info(self._airco_id)
        except (WfRacError, KeyError, TypeError):
            _LOGGER.warning("Could not delete account from airco %s", self._airco_id)
            return None
        if result_code(result) != 0:
            return None
        return result

    async def add_account(self) -> dict[str, Any] | None:
        """Add account (operator id) from the airco."""
        try:
            result = await self._api.update_account_info(
                self._airco_id, self.hass.config.time_zone
            )
        except (WfRacError, KeyError, TypeError):
            _LOGGER.debug("Could not add account from airco %s", self._airco_id)
            return None

        # Here result:2 means the account table is full, and nothing frees a
        # slot but the official app - a standing condition for Repairs, ended
        # by a registration that went through and by nothing else.
        code = result_code(result)
        if code == 2:
            self._report_registration_full()
        elif code == 0:
            self._clear_registration_full_issue()
        return result

    def adopt_status_request_mode(self, mode: str) -> None:
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

    def _build_command(self, params: dict[AirconCommands, Any]) -> AirconStat:
        """Build the full state block for a command.

        A block, not a delta: every field the caller did not name goes out as
        we last saw it. It is therefore only as current as self._airco, which
        is why the retry below builds a second one instead of re-sending this.
        """
        if self._airco is None:
            raise ValueError("Airco object is empty")

        airco_stat = AirconStat.from_aircon(self._airco)

        # Not a command parameter: the override has no set-bit of its own
        # and is never written for its own sake, it only rides along on
        # frames that were going out anyway (see AircoClimate.
        # async_set_external_temperature). Applied to every frame, since
        # one that leaves byte 5 alone reverts the unit to its own sensor.
        airco_stat.ExternalTemperature = self.external_temperature.override

        for key, value in params.items():
            setattr(airco_stat, key, value)

        # After the parameters, not before: the correction depends on the
        # mode this frame is putting the unit into, which a command in
        # params may just have changed.
        airco_stat.ExternalTemperature = self.external_temperature.corrected(
            airco_stat.ExternalTemperature, airco_stat.OperationMode
        )
        return airco_stat

    async def set_airco(
        self,
        params: dict[AirconCommands, Any],
        *,
        log_failure: bool = True,
        timestamp_offset: int = 0,
        is_status_request: bool = False,
        retry_when_locked: bool = True,
    ) -> None:
        """Send one command frame to the airco.

        log_failure=False leaves the reporting to the caller, for requests
        that have their own retry.

        is_status_request marks a frame that asks for readings instead of
        changing anything: its block carries no set-bits, so what it echoes
        back is the unit's own state rather than our expectation.

        retry_when_locked=False hands a refusal straight back instead of
        waiting the foreign write lock out below - that wait runs inside
        _send_lock, where a user command would sit behind it for up to
        WRITE_LOCK_MAX_WAIT.

        timestamp_offset shifts the `timestamp` this request stamps, and so
        the write lock it takes (deadline is timestamp + 60, the module has no
        clock). Negative for operation-data requests, to give up part of the
        lock; 0 for real commands.
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

            airco_stat = self._build_command(params)

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
                    # Rebuilt rather than re-sent: the wait read the unit
                    # again, so self._airco now carries what the other client
                    # wrote while it held the lock. The block encoded before
                    # the refusal still carries the state from before that,
                    # and sending it would hand their change straight back.
                    airco_stat = self._build_command(params)
                    response = await self._api.send_airco_command(
                        self._airco_id,
                        self._parser.to_base64(airco_stat),
                        timestamp_offset=timestamp_offset,
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
                # A status request's block carries no set-bits, so nothing in
                # it can explain a setting that moved - unless this is one of
                # the units we carry the power state for (#329), where the
                # frame really does write Operation.
                self.foreign_writers.note_write(
                    could_move_a_setting=not is_status_request
                    or self._parser.status_request_carries_state
                )
                new_airco = self._parser.translate_bytes(response)
                self._carry_forward_home_leave_mode(new_airco)
                self.service_data.carry_forward(new_airco)
                self._airco = new_airco
                # Our own write is not a foreign one: move the expectation to
                # what the unit reports back, or the next poll would read this
                # command as somebody else's (see _note_unexpected_settings).
                # Not for a status request: it changes nothing, so its response
                # is a reading of whatever the unit is doing now - including a
                # change made at the unit since the poll, which taking it as
                # our expectation would swallow whole.
                if not is_status_request:
                    self.foreign_writers.note_own_command()
                # After the write, and only for what the frame really carried
                # - see ExternalTemperatureFeed.note_frame.
                self.external_temperature.note_frame(
                    self._parser.external_temperature_raw_in_frame(airco_stat)
                )
                # Proof of reachability like a poll: once a unit counts as
                # away, the service layer drops the calls that would show it
                # is there.
                self._record_reachable()
            except (WfRacError, KeyError, TypeError, ValueError) as ex:
                if log_failure:
                    _LOGGER.warning("Could not send airco data: %s", str(ex))
                raise

    async def async_queue_command(self, params: dict[AirconCommands, Any]) -> None:
        """Queue an airco command, coalescing calls made close together.

        Calls within UPDATE_CONSOLIDATION_PERIOD become one set_airco(). Every
        entity uses this rather than set_airco(), so a fan change and a
        setpoint change issued together share a request instead of racing.
        """
        self._consolidated_params.update(params)
        if (flush := self._consolidation_task) is None:
            flush = self.hass.async_create_task(self._async_flush_queued_command())
            self._consolidation_task = flush
            self._running_flushes.add(flush)
            flush.add_done_callback(self._running_flushes.discard)
        # Every caller awaits the one flush its parameters ended up in, so a
        # refusal by the unit reaches the action that caused it instead of
        # being logged into the void. Shielded because the task is shared: a
        # caller giving up (a cancelled service call) must not take the other
        # callers' command down with it.
        await asyncio.shield(flush)

    def _carry_forward_home_leave_mode(self, new_airco: Aircon) -> None:
        """Carry the last known HomeLeaveMode reading forward.

        The unit reports the Tag-248 HomeLeaveMode extension segment exactly
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
        """Ask the unit to report its current HomeLeaveMode.

        That is Tag 248, capability index 7: thresholds and airflow. Does not
        change any AC setting by itself - but the unit only reports this
        extension segment in response to this request, never on an unprompted
        poll, and matches byte-for-byte against the official app's own display.

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
        """Write new HomeLeaveMode thresholds and airflow.

        That is Tag 248, sub-codes 27-32. Written values round-trip exactly
        through a subsequent read.
        """
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
            # Already logged in set_airco(). A failed command says nothing
            # about the poll before it, so the listeners hear the state without
            # the coordinator being declared successful. Wrapped rather than
            # re-raised - a library exception in a service call is a traceback,
            # not something the user can read - and async_queue_command()
            # awaits this task, so it lands on the action that issued it.
            self.async_update_listeners()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={
                    "device": self.device_name,
                    "error": str(ex),
                },
            ) from ex
        # The unit's answer reaches the entities now, not a poll later.
        self.async_set_updated_data(self._airco)

    def _record_reachable(self) -> None:
        """Start the tolerance over, after the unit has answered."""
        self._consecutive_failures = 0

    def _record_failed_poll(self, error: BaseException) -> None:
        """Count one failed poll and keep what went wrong with it.

        Once per poll: the re-registration that follows a rejected answer is
        a second request under the same deadline. Saturated at the limit, and
        the error is kept for the poll that crosses it.
        """
        if self._poll_counted:
            return
        self._poll_counted = True
        self._consecutive_failures = min(
            self._consecutive_failures + 1, self._availability_failure_limit
        )
        self._last_poll_error = error
        _LOGGER.debug("Could not reach the airco [%s]: %s", self.device_name, error)

    @property
    def device_info(self) -> DeviceInfo:
        """Return a device description for device registry.

        No "model": ModelNr is a capability grouping (0/1/2/3/64...), not a
        type name, so it would put a bare digit where users expect
        "SRK35ZS-WF". It goes into model_id instead.
        """
        info: DeviceInfo = {
            "sw_version": self._firmware,
            "identifiers": {(DOMAIN, self.airco_id)},
            "manufacturer": "Mitsubishi Heavy Industries",
            "name": self.device_name,
        }
        if re.fullmatch(r"[0-9a-fA-F]{12}", self.airco_id):
            info["connections"] = {(CONNECTION_NETWORK_MAC, format_mac(self.airco_id))}
        model_nr = getattr(self.airco, "ModelNrRaw", None)
        if model_nr is not None:
            info["model_id"] = str(model_nr)
        return info

    @property
    def operator_id(self) -> str:
        """Return Airco Operator ID."""
        return self._operator_id

    @property
    def num_accounts(self) -> int:
        """Return Accounts connected."""
        return self._connected_accounts

    @property
    def updated_by(self) -> str | None:
        """Return what last updated the airco's state ('local' or a foreign account)."""
        return self._updated_by

    @property
    def account_expires(self) -> int | None:
        """Return the raw 'expires' timestamp reported with our registration."""
        return self._account_expires

    @property
    def led_status(self) -> int | None:
        """Return the airco's front panel LED status."""
        return self._led_status

    @property
    def auto_heating(self) -> int | None:
        """Return the airco's auto-heating flag."""
        return self._auto_heating

    @property
    def wireless_firmware_version(self) -> str | None:
        """Return the locally-reported wireless-module firmware version."""
        return self._wireless_firmware_ver

    @property
    def latest_wireless_firmware_version(self) -> str | None:
        """Return the latest wireless-module firmware version from the cloud.

        None if not yet checked or unknown.
        """
        return self._latest_wireless_firmware_ver

    @property
    def firmware_update_available(self) -> bool | None:
        """Return whether a newer wireless-module firmware is available.

        None if that hasn't been determined yet.
        """
        return self._firmware_update_available

    @property
    def firmware_update_check_enabled(self) -> bool:
        """Return whether the (online, cloud) firmware update check is enabled."""
        return self._firmware_update_check_enabled

    @property
    def device_id(self) -> str:
        """Return Airco device ID."""
        return self._device_id

    @property
    def host(self) -> str:
        """Get Host (IP)."""
        return self._host

    @property
    def port(self) -> int:
        """Get Port."""
        return self._port

    @property
    def device_name(self) -> str:
        """Get given Airco name."""
        return self.name

    @property
    def airco_id(self) -> str:
        """Return Airco ID."""
        return self._airco_id

    @property
    def airco(self) -> Aircon:
        """Return parsed Aircon object if set otherwise None."""
        return self._airco

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

        One missed poll is not an update failure yet - the modules restart
        their WiFi about once an hour. A failure below the threshold returns
        the last data; a run of them fails the update.
        """
        # The poll starts here, not in update(): set_airco() calls that too,
        # and the count has to hold for exactly one poll.
        self._poll_counted = False
        try:
            async with asyncio.timeout(POLL_TIMEOUT.total_seconds()):
                answered = await self.update()
        except asyncio.TimeoutError:
            # The outer deadline can expire before the repository's own
            # attempts do. That is a missed poll like any other.
            self._record_failed_poll(
                WfRacConnectionError(
                    f"did not answer within {POLL_TIMEOUT.total_seconds():.0f}s"
                )
            )
        except Exception as error:
            # Not a device that went quiet but a fault, and it has to stay an
            # UpdateFailed: DataUpdateCoordinator logs that one once, with the
            # traceback at debug, while its own catch-all writes a traceback
            # on every poll for as long as the fault lasts.
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={
                    "device": self.device_name,
                    "error": str(error),
                },
            ) from error
        else:
            if answered:
                return self._airco

        # Within tolerance and no failure reported yet. A reported one ends
        # when a poll answers, not on the next routine dropout.
        if (
            self._consecutive_failures < self._availability_failure_limit
            and self.last_update_success
        ):
            return self._airco
        raise UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="update_failed",
            translation_placeholders={
                "device": self.device_name,
                "error": str(self._last_poll_error),
            },
        )
