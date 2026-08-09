"""Local API for sending and receiving to and from WF-RAC module"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import ssl
import time
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from aiohttp import ClientConnectionError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)
# log http requests/responses to separate logger, to allow easily turning on/off from
# configuration.yaml
_HTTP_LOG = _LOGGER.getChild("http")

# ensure that we don't overwhelm the aircon unit by waiting at least
# this long between successive requests
MIN_TIME_BETWEEN_REQUESTS = timedelta(seconds=1)

# Ceiling for a single request. The adapter is slow and frequently answers in
# 10-20s, so this cannot be tightened much - but it must leave room for a
# second attempt inside the same coordinator poll, because discovery tries one
# protocol and then the other. Device.POLL_TIMEOUT is derived from it; see the
# note there for what happens when the two are equal.
REQUEST_TIMEOUT = timedelta(seconds=25)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT.total_seconds())


def _create_permissive_ssl_context() -> ssl.SSLContext:
    """Build a permissive SSL context for units without a known certificate.

    Some WF-RAC modules' embedded HTTPS stacks only support legacy TLS
    versions/cipher suites that Python's security-hardened defaults reject
    outright - observed as `SSLV3_ALERT_HANDSHAKE_FAILURE` at the TLS
    handshake step itself, before certificate validation is even reached (so
    plain `ssl=False`, which only disables verification, doesn't help).
    Lowering OpenSSL's security level and allowing older TLS versions
    accommodates that legacy stack; this is only used for units without a
    trusted cert on file, so verification is off anyway.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1
    context.set_ciphers("DEFAULT:@SECLEVEL=1")
    return context


class AirconApiError(HomeAssistantError):
    """Raised when the aircon API returns an error"""


class AirconCommandError(AirconApiError):
    """Raised when the unit answered but refused the command itself.

    Distinct from a transport failure: the module is reachable and the
    protocol in use is the right one, it just declined this request (e.g.
    HTTP 501 for an optional command). Callers use that difference to decide
    whether the connection state is worth invalidating.
    """


class AirconConnectionError(AirconApiError):
    """Raised when the unit could not be reached at all.

    The counterpart to AirconCommandError: nothing answered, so neither the
    request nor the account it was sent under can be judged from it. These
    modules restart their WiFi periodically on their own, so single
    occurrences are expected rather than a fault.
    """


class Repository:
    """Simple Api class to send and get Aircon information"""

    api_version = "1.0"

    def __init__(  # pylint: disable=too-many-arguments
        self,
        hass: HomeAssistant,
        hostname: str,
        port: int,
        operator_id: str,
        device_id: str,
        method: str | None = None,
    ) -> None:
        self._hass = hass
        self._hostname = hostname
        self._port = port
        self._operator_id = operator_id
        self._device_id = device_id
        self._session = async_get_clientsession(hass)
        self._next_request_after = datetime.now()
        # Previously-discovered communication method (http/https), if the caller
        # persisted one from a prior run - skips rediscovery below.
        self._method: str | None = method if method in ("http", "https") else None
        self._ssl_context: ssl.SSLContext | None = None
        # Serializes _post() calls so the min-time-between-requests throttle and
        # the method-discovery/reset logic below can't interleave across
        # concurrent callers (a plain timestamp check allowed a race where two
        # requests both see the wait as satisfied and fire back-to-back).
        self._request_lock = asyncio.Lock()

    @property
    def method(self) -> str | None:
        """Return the discovered/persisted communication method (http/https), if known."""
        return self._method

    async def _get_ssl_context(self) -> ssl.SSLContext:
        """Create (once) and cache the SSL context for HTTPS communication.

        A certificate file can be stored in the HA configuration directory by
        running this command while in that directory:
        openssl s_client -connect <<AC_IP_ADDRESS>>:51443 -showcerts </dev/null 2>/dev/null \
            | openssl x509 -outform PEM > ac_cert.pem
        """
        if self._ssl_context is None:
            cert_path = self._hass.config.path("ac_cert.pem")
            cert_exists = await self._hass.async_add_executor_job(
                os.path.isfile, cert_path
            )

            if cert_exists:
                _LOGGER.debug("Certificate file found, creating secure SSL context")
                partial_func = functools.partial(
                    ssl.create_default_context, cafile=cert_path
                )
                ssl_context = await self._hass.async_add_executor_job(partial_func)
                ssl_context.check_hostname = False
            else:
                _LOGGER.debug(
                    "Certificate file not found, falling back to a permissive SSL "
                    "context (older WF-RAC modules' embedded HTTPS stacks often "
                    "only support legacy TLS versions/ciphers)"
                )
                ssl_context = await self._hass.async_add_executor_job(
                    _create_permissive_ssl_context
                )
            self._ssl_context = ssl_context
        return self._ssl_context

    async def _post(
        self, command: str, contents: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        async def _execute_request(protocol: str) -> dict[str, Any]:
            """Executes a single POST request and returns the JSON response."""
            url = f"{protocol}://{self._hostname}:{self._port}/beaver/command/{command}"
            request_kwargs: dict[str, Any] = {
                "json": data,
                "timeout": _REQUEST_TIMEOUT,
            }
            if protocol == "https":
                request_kwargs["ssl"] = await self._get_ssl_context()

            _HTTP_LOG.debug("POST %s -> %r", url, data)
            try:
                async with self._session.post(url, **request_kwargs) as resp:
                    # Read the raw body ourselves (instead of resp.json()) so we
                    # can log it - and parse it - regardless of the declared
                    # Content-Type or HTTP status. Some modules send a valid
                    # JSON body with an incorrect Content-Type (e.g. text/plain),
                    # and error responses may carry a useful JSON body too.
                    body = await resp.text()
                    _HTTP_LOG.debug(
                        "<- %s status=%s content_type=%r body=%r",
                        url,
                        resp.status,
                        resp.content_type,
                        body,
                    )
                    if resp.status >= 400:
                        raise AirconCommandError(
                            f"Aircon returned HTTP {resp.status} for {command!r}: {body}"
                        )
                    return json.loads(body)
            except (ClientConnectionError, asyncio.TimeoutError) as ex:
                raise AirconConnectionError(f"Aircon returned error: {ex}") from ex

        data = {
            "apiVer": self.api_version,
            "command": command,
            "deviceId": self._device_id,  # is unique device ID (on android it is called android_id)
            "operatorId": self._operator_id,  # is generated UUID
            "timestamp": round(time.time()),
        }
        if contents is not None:
            data["contents"] = contents

        # ensure only one request is talking to the device at a time
        async with self._request_lock:
            wait_for = (self._next_request_after - datetime.now()).total_seconds()
            if wait_for > 0:
                _LOGGER.debug("Waiting for %rs until we can send a request", wait_for)
                await asyncio.sleep(wait_for)

            # If we already know how to communicate with the unit, proceed
            if self._method in ("http", "https"):
                try:
                    json_response = await _execute_request(self._method)
                except AirconCommandError:
                    # The unit answered, so the stored method is still the
                    # right one - it just refused this particular command.
                    # Discarding the method here would cost every later
                    # request an extra discovery round trip for nothing.
                    raise
                except AirconApiError:
                    # The unit may have rebooted, changed protocol, or the
                    # persisted method from a previous run may simply be stale -
                    # reset so the next request rediscovers instead of wedging
                    # itself permanently against a method that no longer works.
                    _LOGGER.info(
                        "Request with stored method %r failed; "
                        "resetting so the next request rediscovers",
                        self._method,
                    )
                    self._method = None
                    raise

            # If we haven't yet determined if https is required, find out
            else:
                _LOGGER.debug("No stored method; attempting discovery...")
                try:
                    # Deliberately falls back on any error, command errors
                    # included: an HTTPS-only module can answer a plaintext
                    # request with a status code rather than dropping the
                    # connection, and that still means "try the other one".
                    json_response = await _execute_request("http")
                    _LOGGER.info("Discovered working communication method: HTTP")
                    # Store the required communication method
                    self._method = "http"
                except AirconApiError:
                    _LOGGER.debug("HTTP failed, trying HTTPS")
                    json_response = await _execute_request("https")
                    _LOGGER.info("Discovered working communication method: HTTPS")
                    # Store the required communication method
                    self._method = "https"

            self._next_request_after = datetime.now() + MIN_TIME_BETWEEN_REQUESTS

        _HTTP_LOG.debug(
            "Got response from %r: %r",
            self._hostname,
            json_response,
        )
        return json_response

    async def get_info(self) -> dict:
        """Simple command to get aircon details"""
        return (await self._post("getDeviceInfo"))["contents"]

    async def get_airco_id(self) -> str:
        """Simple command to get aircon ID"""
        return (await self.get_info())["airconId"]

    async def update_account_info(
        self, airco_id: str, time_zone: str
    ) -> dict[str, Any]:
        """Update the account info on the airco (sets to operator id of the device)"""
        contents = {
            "accountId": self._operator_id,
            "airconId": airco_id,
            "remote": 0,
            "timezone": time_zone,
        }
        return await self._post("updateAccountInfo", contents)

    async def del_account_info(self, airco_id: str) -> dict:
        """delete the account info on the airco"""
        contents = {"accountId": self._operator_id, "airconId": airco_id}
        return await self._post("deleteAccountInfo", contents)

    async def get_aircon_stats(self, airco_id: str | None = None, raw=False) -> dict:
        """Get the Aricon Stats from the Airco

        Sends the airconId in the request body. The official Smart M-Air app and
        every other reverse-engineered client (homebridge-mhi-wfrac,
        mqtt2mhi-wf-rac, ioBroker.woso_mitsu_aircon_rac) include it here; the
        value itself is ignored by the module but its presence is required by
        some firmware revisions, which otherwise reject getAirconStat with
        HTTP 400 / result:2. Older firmware tolerated the field being absent,
        which is why omitting it worked until now. Kept optional so callers
        without an airconId (none in this integration) still work.
        """
        contents = {"airconId": airco_id} if airco_id is not None else None
        result = await self._post("getAirconStat", contents)
        return result if raw else result["contents"]

    async def send_airco_command(self, airco_id: str, command: str) -> str:
        """send command to the Airco"""
        contents = {"airconId": airco_id, "airconStat": command}
        result = await self._post("setAirconStat", contents)
        return result["contents"]["airconStat"]
