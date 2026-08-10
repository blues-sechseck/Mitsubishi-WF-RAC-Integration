"""Device module"""
import asyncio
from datetime import datetime, timedelta
from typing import Any
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .firmware_check import fetch_latest_firmware
from .rac_parser import RacParser
from .repository import (
    MIN_TIME_BETWEEN_REQUESTS,
    REQUEST_TIMEOUT,
    AirconApiError,
    AirconCommandError,
    AirconConnectionError,
    Repository,
)
from .models.aircon import Aircon, AirconCommands, AirconStat, HomeLeaveModeSetting

from ..const import DOMAIN, MIN_TIME_BETWEEN_UPDATES

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

# Service data (operation-data codes) is opt-in and costs an extra
# setAirconStat write on top of the regular read-only poll, but it stays on
# the local network and a batched request answers all four codes in one
# round trip (see todo.md), so there's no reason to throttle it below the
# regular poll cadence. See Device._maybe_request_service_data().
SERVICE_DATA_REQUEST_INTERVAL = MIN_TIME_BETWEEN_UPDATES

# The rate limit is a guard against a second request inside the same cycle, not
# a cadence of its own: the request is scheduled once per poll anyway. It has to
# stay clear of the poll interval it is measured against, because the stamp is
# taken when a poll finishes, not when it was due. Polls arrive exactly
# MIN_TIME_BETWEEN_UPDATES apart, so a poll answering a few milliseconds faster
# than the one before it leaves marginally less than that between the two
# stamps - and with the full interval as the limit, that dropped the cycle. On
# the unit in #230 it cost 6 of 36 cycles of operation data, every one of them
# short by under 100ms.
SERVICE_DATA_MIN_SPACING = SERVICE_DATA_REQUEST_INTERVAL * 0.75

# ...but it does matter *where* in the cycle it lands. Issued straight off the
# back of a poll it reached the module about a second after the getAirconStat
# (consolidation delay plus the minimum spacing between requests), and modules
# answer a second request that soon with HTTP 501 "Not supported this command"
# often enough to lose whole cycles of operation data - roughly one poll in
# seven on the unit reported in #230, sometimes several minutes in a row.
# Offsetting it into the quiet middle of the cycle keeps the cadence but stops
# it from crowding the poll.
SERVICE_DATA_REQUEST_OFFSET = SERVICE_DATA_REQUEST_INTERVAL / 2

# A refused request costs a full cycle of every operation-data sensor, and the
# refusals seen in #230 are transient, so one retry is worth the extra write.
SERVICE_DATA_RETRY_DELAY = timedelta(seconds=5)

# The unit answers these segments only when asked, so they are carried across
# the polls in between (see Device._carry_forward_service_data()) - but not
# indefinitely. A unit that keeps refusing the request (#230) would otherwise
# leave entities reporting a frozen number indistinguishable from a live one,
# which is worse for automations built on them than an honest gap.
SERVICE_DATA_MAX_AGE = 3 * SERVICE_DATA_REQUEST_INTERVAL

# Fields fed exclusively by those segments.
SERVICE_DATA_FIELDS = (
    "CompressorFrequency",
    "OperatingCurrent",
    "HotGasTemp",
    "EevPulses",
    "EevPosition",
)

# Room for both legs of protocol discovery plus the minimum spacing between
# requests, so a poll that has to fall back to the other protocol is not
# cancelled halfway through.
#
# It used to equal the per-request timeout, and that combination has a trap
# (#236): a unit that accepts a plaintext connection without answering it
# consumes the whole window on the first leg, so the second protocol is never
# reached. If that unit only speaks the second protocol, every poll fails the
# same way and the device never recovers on its own.
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


class Device(DataUpdateCoordinator):  # pylint: disable=too-many-instance-attributes
    """Device Class"""

    def __init__(  # pylint: disable=too-many-arguments
            self,
            hass: HomeAssistant,
            name: str,
            hostname: str,
            port: int,
            device_id: str,
            operator_id: str,
            airco_id: str,
            create_swing_mode_select: bool,
            availability_failure_limit: int = AVAILABILITY_FAILURE_LIMIT_MIN,
            firmware_update_check_enabled: bool = False,
            service_data_enabled: bool = False,
            connection_method: str | None = None,
    ) -> None:
        self._api = Repository(
            hass, hostname, port, operator_id, device_id, method=connection_method
        )
        self._parser = RacParser()
        self._hass = hass

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
        self._service_data_enabled = service_data_enabled
        self._last_service_data_request: datetime | None = None
        self._last_service_data_response: datetime | None = None
        self._service_data_task: asyncio.Task | None = None
        self._consecutive_failures = 0
        # Clamped rather than validated: an entry can carry a lower value from
        # an older version, and refusing to set up over it would be worse than
        # quietly giving it the tolerance it should have had.
        self._availability_failure_limit = max(
            AVAILABILITY_FAILURE_LIMIT_MIN, availability_failure_limit
        )
        self._create_swing_mode_select = create_swing_mode_select
        # Serializes set_airco() calls end-to-end (snapshot build through
        # self._airco update) so a call can never build its diff from a
        # snapshot that's stale because another set_airco() is still in
        # flight - see set_airco() below.
        self._send_lock = asyncio.Lock()
        self._consolidated_params: dict[str, Any] = {}
        self._consolidation_task: asyncio.Task | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=name,
            update_interval=MIN_TIME_BETWEEN_UPDATES,
        )

    async def update(self):
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
                return
        except (AirconApiError, KeyError) as ex:
            self._set_availability(False)
            # These modules restart their WiFi on their own, so a poll landing
            # in that window is routine rather than a fault. Report it as one
            # line and keep the traceback for debug; a unit that answered and
            # then failed is the interesting case and keeps the full trace.
            if isinstance(ex, AirconConnectionError):
                _LOGGER.warning(
                    "Could not reach the airco [%s]: %s", self.device_name, ex
                )
                _LOGGER.debug("Update of [%s] failed", self.device_name, exc_info=ex)
                return
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
            return

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
            self._account_expires = response.get("expires")
            self._led_status = response.get("ledStat")
            self._auto_heating = response.get("autoHeating")
            self._set_availability(True)
        except (KeyError, TypeError, ValueError) as ex:
            _LOGGER.warning("Could not parse airco data", exc_info=ex)
            self._set_availability(False)
            return

        # Cosmetic (diagnostic sensor only). Some firmware revisions omit the
        # "mcu"/"wireless" sub-keys entirely (see #189), so their versions are
        # optional and fall back to "unknown" instead of failing the update.
        firm_type = response.get("firmType", "unknown")
        mcu_ver = (response.get("mcu") or {}).get("firmVer", "unknown")
        wireless_ver = (response.get("wireless") or {}).get("firmVer", "unknown")
        self._firmware = f"{firm_type}, mcu: {mcu_ver}, wireless: {wireless_ver}"

        self._firm_type = response.get("firmType")
        self._wireless_firmware_ver = (response.get("wireless") or {}).get("firmVer")
        self._maybe_check_firmware_update()
        self._maybe_request_service_data()

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
        now = datetime.now()
        if (
            self._last_firmware_check is not None
            and now - self._last_firmware_check < FIRMWARE_CHECK_INTERVAL
        ):
            return
        self._last_firmware_check = now
        self._hass.async_create_task(self._async_check_firmware_update())

    async def _async_check_firmware_update(self) -> None:
        """Compare the locally-reported wireless firmware version against the
        manufacturer's latest for this firmType."""
        latest = await fetch_latest_firmware(self._hass, self._firm_type)
        if latest is None or latest.get("wireless") is None:
            return

        try:
            # Strictly-greater-than only: the module treats a requested
            # firmVer <= its current one as "nothing to do" and returns 200 OK
            # without flashing (see FUNDE.md, updateFirmware) - a `!=` check
            # would misreport that harmless case as an available downgrade.
            update_available = int(latest["wireless"]) > int(self._wireless_firmware_ver)
        except (TypeError, ValueError):
            _LOGGER.debug(
                "Could not compare firmware versions: local=%r latest=%r",
                self._wireless_firmware_ver,
                latest["wireless"],
            )
            return

        self._latest_wireless_firmware_ver = latest["wireless"]
        self._firmware_update_available = update_available
        self.async_set_updated_data(self._airco)

    def _maybe_request_service_data(self) -> None:
        """Kick off a background service-data request if due and enabled (see
        SERVICE_DATA_MIN_SPACING). Opt-in like the firmware check above,
        but for a different reason: this stays on the local network, but it's
        an extra setAirconStat write (see rac_parser.SERVICE_DATA_CODES) on
        top of the regular read-only poll, not just a cheap read.
        """
        if not self._service_data_enabled:
            return
        if self._service_data_task is not None and not self._service_data_task.done():
            # A retry from the previous cycle is still in flight; piling a
            # second request on top is exactly the crowding this avoids.
            return
        now = datetime.now()
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
        self._service_data_task = self._hass.async_create_background_task(
            self._async_request_service_data(),
            name=f"{DOMAIN} service data request {self._airco_id}",
        )

    async def _async_request_service_data(self) -> None:
        """Ask the unit for the operation-data block, offset from the poll and
        retried once if the unit refuses it (see SERVICE_DATA_REQUEST_OFFSET
        and #230). Sends directly rather than through async_queue_command() so
        the refusal is visible here: a queued command is flushed by a detached
        task that deliberately swallows its errors.
        """
        await asyncio.sleep(SERVICE_DATA_REQUEST_OFFSET.total_seconds())
        params = {AirconCommands.ServiceDataStatusRequest: True}
        for attempt in (1, 2):
            try:
                await self.set_airco(params, log_failure=False)
                if attempt > 1:
                    _LOGGER.debug("Service data request succeeded on retry")
                return
            except AirconCommandError as ex:
                if attempt == 1:
                    _LOGGER.debug("Service data request refused (%s); retrying", ex)
                    await asyncio.sleep(SERVICE_DATA_RETRY_DELAY.total_seconds())
                    continue
                _LOGGER.warning(
                    "Service data request refused twice, skipping this cycle "
                    "for [%s]: %s",
                    self.device_name,
                    ex,
                )
            except (AirconApiError, KeyError, TypeError, ValueError):
                # Unreachable or unparseable: the poll itself reports that, and
                # this request is an optional extra on top of it.
                return
        # Entities keep their previous operation-data values on a skipped cycle
        # (see _carry_forward_service_data), so there is nothing to push here.

    def _carry_forward_service_data(self, new_airco: Aircon) -> None:
        """Same rationale as _carry_forward_home_leave_mode() above: the unit
        reports these extension segments exactly once (confirmed live
        06.08.2026, see todo.md), so without this the sensors would flash the
        real value for one update cycle and then revert to unknown.

        Unlike home/leave mode this expires: see SERVICE_DATA_MAX_AGE.
        """
        if self._airco is None:
            return
        now = datetime.now()
        if any(getattr(new_airco, name) is not None for name in SERVICE_DATA_FIELDS):
            self._last_service_data_response = now
        elif (
            self._last_service_data_response is None
            or now - self._last_service_data_response > SERVICE_DATA_MAX_AGE
        ):
            # Nothing fresh for too long - leave the fields unset so entities
            # report unknown rather than a value that stopped being true.
            return
        for name in SERVICE_DATA_FIELDS:
            if getattr(new_airco, name) is None:
                setattr(new_airco, name, getattr(self._airco, name))

    async def delete_account(self):
        """Delete account (operator id) from the airco"""
        try:
            return await self._api.del_account_info(self._airco_id)
        except (AirconApiError, KeyError, TypeError):
            _LOGGER.warning("Could not delete account from airco %s", self._airco_id)
            return None

    async def add_account(self):
        """Add account (operator id) from the airco"""
        try:
            return await self._api.update_account_info(
                self._airco_id, self._hass.config.time_zone
            )
        except (AirconApiError, KeyError, TypeError):
            _LOGGER.warning("Could not add account from airco %s", self._airco_id)
            return None

    async def set_airco(
        self, params: dict[str, Any], *, log_failure: bool = True
    ) -> None:
        """Method to send airco command.

        log_failure=False leaves the reporting to the caller, for requests that
        have their own retry and a quieter failure story than a user command
        that never reached the unit - see _async_request_service_data().
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

            for key, value in params.items():
                setattr(airco_stat, key, value)

            try:
                command = self._parser.to_base64(airco_stat)
                response = await self._api.send_airco_command(self._airco_id, command)
                new_airco = self._parser.translate_bytes(response)
                self._carry_forward_home_leave_mode(new_airco)
                self._carry_forward_service_data(new_airco)
                self._airco = new_airco
            except (AirconApiError, KeyError, TypeError, ValueError) as ex:
                if log_failure:
                    _LOGGER.warning("Could not send airco data: %s", str(ex))
                raise

    async def async_queue_command(self, params: dict[str, Any]) -> None:
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

    def _carry_forward_home_leave_mode(self, new_airco: Aircon) -> None:
        """The unit reports the Tag-248 HomeLeaveMode extension segment exactly
        once per HomeLeaveModeStatusRequest, then stops: the bridge MCU clears
        its response cache after handing it to the WiFi side, so the segment is
        present in a short window's worth of status blocks and absent from every
        later poll (firmware-confirmed 06.08.2026, see the workspace's
        firmware-kompatibilitaet.md). Observed effect (05.08.2026 live test):
        translate_bytes() builds a fresh Aircon() with both fields back at their
        None default, which made the diagnostic sensors flash the real value for
        one update cycle and then revert to unknown. Carry the last known
        reading forward instead so it survives until the next explicit request
        or a fresh None response (e.g. reconnect).
        """
        if self._airco is None:
            return
        if new_airco.HomeLeaveModeForCooling is None:
            new_airco.HomeLeaveModeForCooling = self._airco.HomeLeaveModeForCooling
        if new_airco.HomeLeaveModeForHeating is None:
            new_airco.HomeLeaveModeForHeating = self._airco.HomeLeaveModeForHeating

    async def async_request_home_leave_mode_status(self) -> None:
        """Ask the unit to report its current HomeLeaveMode (Tag 248, #187
        capability index 7) thresholds/airflow. Does not change any AC
        setting by itself - but the unit only reports this extension segment
        in response to this request, never on an unprompted poll (confirmed
        empirically, 05.08.2026 live test, matched byte-for-byte against the
        official app's own display).

        Timing, measured: the value showed up only on a later scheduled poll -
        up to MIN_TIME_BETWEEN_UPDATES (60s) later - not in the response to
        this call's own setAirconStat POST. Note that a *single* extension
        request does come back inside that same POST response (verified
        06.08.2026 with operation-data codes), so the delay here is most
        likely because this request sends six segments and the unit answers
        them one bus frame at a time. Unconfirmed - if it matters, measure it
        rather than trusting this paragraph.

        _carry_forward_home_leave_mode() keeps the reading available on every
        following poll instead of it reverting to unknown.
        """
        await self.async_queue_command({AirconCommands.HomeLeaveModeStatusRequest: True})

    async def async_set_home_leave_mode(
        self, cooling: HomeLeaveModeSetting, heating: HomeLeaveModeSetting
    ) -> None:
        """Write new HomeLeaveMode thresholds/airflow (Tag 248, sub-codes
        27-32). Verified live (05.08.2026) - written values round-tripped
        exactly through a subsequent read, see todo.md."""
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
        try:
            await self.set_airco(params)
        except (AirconApiError, KeyError, TypeError, ValueError):
            # Already logged in set_airco(). This runs as a detached task
            # (nothing awaits it), so without this the re-raised error becomes
            # an orphaned "Task exception was never retrieved" with zero
            # HA-visible feedback that the command never reached the unit.
            # Still notify below so entities pick up self.available if the
            # same failure already flipped it.
            pass
        # Immediately push the (possibly unchanged, on failure) state to all
        # entities instead of leaving them to wait for the next poll (up to
        # MIN_TIME_BETWEEN_UPDATES later).
        self.async_set_updated_data(self._airco)

    def _set_availability(self, available: bool):
        """Mark the device available, or unavailable once it has missed
        self._availability_failure_limit polls in a row."""
        if available:
            self._consecutive_failures = 0
            self._available = True
            return

        self._consecutive_failures += 1
        if self._consecutive_failures >= self._availability_failure_limit:
            self._consecutive_failures = 0
            self._available = False

    def set_available(self, available: bool):
        """Set available status"""
        self._set_availability(available)

    @property
    def device_info(self) -> DeviceInfo:
        """Return a device description for device registry."""
        return {
            "sw_version": self._firmware,
            "identifiers": {(DOMAIN, self.airco_id)},
            "manufacturer": "Mitsubishi (WF-RAC)",
            # "model": self.airco.ModelNr,
            "name": self.device_name,
        }

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
    def service_data_enabled(self) -> bool:
        """Return whether the (local, opt-in) service data request is enabled"""
        return self._service_data_enabled

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
    def create_swing_mode_select(self) -> bool:
        """Create swing mode select"""
        return self._create_swing_mode_select

    @property
    def connection_method(self) -> str | None:
        """Return the discovered/persisted communication method (http/https), if known."""
        return self._api.method

    async def _async_update_data(self):
        """Update data via library."""
        try:
            async with asyncio.timeout(POLL_TIMEOUT.total_seconds()):
                await asyncio.gather(*[self.update()])
        except asyncio.TimeoutError as error:
            # Spelled out because str(TimeoutError()) is empty: the
            # coordinator's own message would otherwise read "Error fetching
            # <name> data:" and stop there, which says nothing at all.
            raise UpdateFailed(
                f"[{self.device_name}] did not answer within "
                f"{POLL_TIMEOUT.total_seconds():.0f}s"
            ) from error
        except Exception as error:
            raise UpdateFailed(error) from error
