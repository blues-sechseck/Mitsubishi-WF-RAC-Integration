"""Device module"""
import asyncio
from datetime import timedelta
from typing import Any
import logging

from async_timeout import timeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import Throttle

from .rac_parser import RacParser
from .repository import Repository
from .models.aircon import Aircon, AirconStat

from ..const import DOMAIN, MIN_TIME_BETWEEN_UPDATES

_LOGGER = logging.getLogger(__name__)

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
    ) -> None:
        self._api = Repository(hass, hostname, port, operator_id, device_id)
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
        self._availability_retry = availability_retry
        self._availability_retry_count = 0
        self._availability_retry_limit = availability_retry_limit
        self._create_swing_mode_select = create_swing_mode_select

        super().__init__(
            hass,
            _LOGGER,
            name=name,
            update_interval=timedelta(seconds=60),
        )

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    async def update(self):
        """Update the device information from API"""

        try:
            response = await self._api.get_aircon_stats()

            if response is None:
                self._set_availability(False)
                _LOGGER.warning("Received no data for device %s", self._airco_id)
                return
        except Exception as e:  # pylint: disable=broad-except
            self._set_availability(False)
            _LOGGER.warning(
                "Error: something went wrong updating the airco [%s] values", self.device_name,
                exc_info=e,
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
            self._firmware = f'{response["firmType"]}, mcu: {response["mcu"]["firmVer"]}, wireless: {response["wireless"]["firmVer"]}'
            self._airco = self._parser.translate_bytes(response["airconStat"])
            await self.async_refresh()
            self._set_availability(True)
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.warning("Could not parse airco data", exc_info=e)
            self._set_availability(False)

    async def delete_account(self):
        """Delete account (operator id) from the airco"""
        try:
            return await self._api.del_account_info(self._airco_id)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.warning("Could not delete account from airco %s", self._airco_id)

    async def add_account(self):
        """Add account (operator id) from the airco"""
        try:
            return await self._api.update_account_info(
                self._airco_id, self._hass.config.time_zone
            )
        except Exception:  # pylint: disable=broad-except
            _LOGGER.warning("Could not add account from airco %s", self._airco_id)

    async def set_airco(self, params: dict[str, Any]) -> None:
        """Method to send airco command"""
        _LOGGER.debug("Setting airco: %s", params)
        if self.airco is None:
            await self._hass.async_add_executor_job(self.update)

        if self._airco is None:
            raise ValueError("Airco object is empty")

        airco_stat = AirconStat(self._airco)

        for key, value in params.items():
            setattr(airco_stat, key, value)

        try:
            command = self._parser.to_base64(airco_stat)
            response = await self._api.send_airco_command(self._airco_id, command)
            self._airco = self._parser.translate_bytes(response)
            await self.async_refresh()
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.warning("Could not send airco data: %s", str(e))
            raise

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

    async def _async_update_data(self):
        """Update data via library."""
        try:
            # Match the underlying HTTP request timeout (30s). The WF-RAC adapter
            # is slow/flaky and frequently answers in 10-20s; a tighter coordinator
            # timeout here would cancel slow-but-valid polls and mark the entity
            # unavailable even though the unit was about to respond.
            async with timeout(30):
                await asyncio.gather(*[self.update()])
        except Exception as error:
            raise UpdateFailed(error) from error
