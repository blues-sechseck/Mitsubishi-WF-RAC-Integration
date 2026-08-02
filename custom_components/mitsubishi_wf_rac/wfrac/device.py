"""Device module"""
import asyncio
from datetime import timedelta
from typing import Any
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .rac_parser import RacParser
from .repository import AirconApiError, Repository
from .models.aircon import Aircon, AirconStat

from ..const import DOMAIN, MIN_TIME_BETWEEN_UPDATES

_LOGGER = logging.getLogger(__name__)

# Commands issued within this window of each other (from any entity) are
# coalesced into a single set_airco() call instead of being sent as separate
# requests. The unit expects a full state block per request, so two
# near-simultaneous separate commands can otherwise overwrite each other
# instead of merging (e.g. a fan-speed change followed shortly by a
# temperature change loses the fan change).
UPDATE_CONSOLIDATION_PERIOD = timedelta(milliseconds=500)


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
            availability_retry: bool,
            availability_retry_limit: int,
            create_swing_mode_select: bool,
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
        self._availability_retry = availability_retry
        self._availability_retry_count = 0
        self._availability_retry_limit = availability_retry_limit
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
            # evicted. add_account() is self-contained and swallows its own errors.
            await self.add_account()
            return

        try:
            self._connected_accounts = int(response["numOfAccount"])
            self._airco = self._parser.translate_bytes(response["airconStat"])
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

    async def set_airco(self, params: dict[str, Any]) -> None:
        """Method to send airco command"""
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
                self._airco = self._parser.translate_bytes(response)
            except (AirconApiError, KeyError, TypeError, ValueError) as ex:
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
        """Set availability after retry count"""
        if available:
            self._availability_retry_count = 0
            self._available = True
            return

        if not self._availability_retry:
            self._available = False
            return

        self._availability_retry_count += 1
        if self._availability_retry_count >= self._availability_retry_limit:
            self._availability_retry_count = 0
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
            # Match the underlying HTTP request timeout (30s). The WF-RAC adapter
            # is slow/flaky and frequently answers in 10-20s; a tighter coordinator
            # timeout here would cancel slow-but-valid polls and mark the entity
            # unavailable even though the unit was about to respond.
            async with asyncio.timeout(30):
                await asyncio.gather(*[self.update()])
        except Exception as error:
            raise UpdateFailed(error) from error
