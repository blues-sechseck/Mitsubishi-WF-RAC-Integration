"""Cloud check for available WF-RAC wireless-module firmware updates.

Queries the manufacturer's `server/getFirmware` endpoint - unauthenticated,
no account/Cognito token involved, same call the official app makes before
showing "update available". See `../../../../firmware-kompatibilitaet.md`
and `smartMAir_apk/FUNDE.md` ("OTA-Ablauf") for the reverse-engineering notes
and field names (`ResponseGetFirmwareMapper.java`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

_FIRMWARE_API_URL = "https://spa.smartmair.com/server/getFirmware"
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
# Matches the app's own header - the endpoint doesn't otherwise pin behavior
# to it, this just avoids standing out as an unidentified client.
_USER_AGENT = "smartmair_app[1.4.009]"


async def fetch_latest_firmware(hass: HomeAssistant, firm_type: str) -> dict[str, Any] | None:
    """Return {"wireless": <mFirmVer>, "mcu": <cFirmVer>} for firm_type, or
    None if the request failed or the branch is unknown to the server."""
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
