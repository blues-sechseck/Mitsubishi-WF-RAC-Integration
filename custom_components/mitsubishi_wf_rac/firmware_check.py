"""Cloud check for available WF-RAC wireless-module firmware updates.

HACS only: an undocumented manufacturer endpoint reached with an imitated
user agent. Not defensible in a core review, and not ported.

Queries the manufacturer's `server/getFirmware` endpoint - unauthenticated,
no account/Cognito token involved, same call the official app makes before
showing "update available". Field names come from the app's own
`ResponseGetFirmwareMapper.java`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .coordinator import Device

_LOGGER = logging.getLogger(__name__)

# The manufacturer's getFirmware endpoint is unauthenticated and cheap, but
# there's no reason to call it on every MIN_TIME_BETWEEN_UPDATES (60s) poll -
# firmware doesn't change that often. Rate-limit background checks to this
# interval instead.
FIRMWARE_CHECK_INTERVAL = timedelta(hours=24)

_FIRMWARE_API_URL = "https://spa.smartmair.com/server/getFirmware"
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
# Matches the app's own header - the endpoint doesn't otherwise pin behavior
# to it, this just avoids standing out as an unidentified client.
_USER_AGENT = "smartmair_app[1.4.009]"


async def fetch_latest_firmware(
    hass: HomeAssistant, firm_type: str
) -> dict[str, Any] | None:
    """Return {"wireless": <mFirmVer>, "mcu": <cFirmVer>} for firm_type.

    None if the request failed or the branch is unknown to the server.
    """
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            _FIRMWARE_API_URL,
            json={"contents": {"accountId": "", "firmType": firm_type}},
            headers={"User-Agent": _USER_AGENT},
            timeout=_REQUEST_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                _LOGGER.debug("getFirmware returned HTTP %s", resp.status)
                return None
            # Some deployments of this endpoint mislabel the content type
            # (see repository.py's _post() for the same issue on the local
            # API) - parse regardless of what's declared.
            body = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
        _LOGGER.debug("getFirmware request failed: %s", ex)
        return None

    if not isinstance(body, dict) or body.get("result") != 0:
        return None
    contents = body.get("contents")
    if not isinstance(contents, dict):
        return None
    return {"wireless": contents.get("mFirmVer"), "mcu": contents.get("cFirmVer")}


class FirmwareUpdateCheck:
    """Wireless-module firmware state and the background check for one unit."""

    def __init__(self, device: Device, enabled: bool) -> None:
        """Bind the check to its coordinator."""
        self._device = device
        self.enabled = enabled
        self.firm_type: str | None = None
        self.wireless_version: str | None = None
        self.latest_wireless_version: str | None = None
        self.update_available: bool | None = None
        self._last_check: datetime | None = None

    def record_local(self, firm_type: str | None, wireless_version: str | None) -> None:
        """Remember what the module reported on the latest poll."""
        self.firm_type = firm_type
        self.wireless_version = wireless_version

    def maybe_check(self) -> None:
        """Kick off a background cloud firmware check if one is due.

        Due is FIRMWARE_CHECK_INTERVAL. Fire-and-forget: the result lands
        whenever the request completes and reaches entities via
        async_set_updated_data() in _async_check() below, independent of the
        regular 60s poll cycle that triggered this check.
        """
        # Hard opt-in gate, checked first and unconditionally: this is the
        # only outbound internet call anywhere in this integration (every
        # other request stays on the local network) - users who leave the
        # option off must get zero cloud traffic, not just a less frequent one.
        if not self.enabled:
            return
        if not self.firm_type or not self.wireless_version:
            return
        now = dt_util.utcnow()
        if (
            self._last_check is not None
            and now - self._last_check < FIRMWARE_CHECK_INTERVAL
        ):
            return
        self._last_check = now
        self._device.hass.async_create_task(
            self._async_check(self.firm_type, self.wireless_version)
        )

    async def _async_check(self, firm_type: str, wireless_firmware_ver: str) -> None:
        """Compare the locally-reported wireless firmware version.

        Compared against the manufacturer's latest for this firmType.
        """
        latest = await fetch_latest_firmware(self._device.hass, firm_type)
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

        self.latest_wireless_version = latest["wireless"]
        self.update_available = update_available
        self._device.async_set_updated_data(self._device.airco)
