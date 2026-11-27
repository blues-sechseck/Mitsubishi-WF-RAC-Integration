"""Home Leave mode: the unit's away thresholds and airflow (Tag 248)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pywfrac import Aircon, AirconCommands, HomeLeaveModeSetting, WfRacError

from homeassistant.exceptions import HomeAssistantError

from .const import (
    DOMAIN,
    HOME_LEAVE_SETPOINT_CEILING,
    HOME_LEAVE_TEMP_HEAT,
    STATUS_REQUEST_STOPPED,
)

if TYPE_CHECKING:
    from .coordinator import Device

_LOGGER = logging.getLogger(__name__)


class HomeLeaveMode:
    """Reading and writing the Home Leave setting of one unit."""

    def __init__(self, device: Device) -> None:
        """Bind the helper to its coordinator."""
        self._device = device

    @property
    def heat_setpoint(self) -> float:
        """Return the setpoint the away preset heats to.

        The unit's own Heating TempSetting once a Tag-248 reading is in, so
        Home Assistant and the Smart M-Air app agree. A value outside
        HOME_LEAVE_TEMP_HEAT..HOME_LEAVE_SETPOINT_CEILING would either not
        register as Home Leave or is unconfirmed on hardware, and falls back.
        """
        airco = self._device.airco
        heating = airco.HomeLeaveModeForHeating if airco else None
        if (
            heating is not None
            and HOME_LEAVE_TEMP_HEAT
            <= heating.TempSetting
            < HOME_LEAVE_SETPOINT_CEILING
        ):
            return heating.TempSetting
        return HOME_LEAVE_TEMP_HEAT

    def carry_forward(self, new_airco: Aircon) -> None:
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
        if self._device.airco is None:
            return
        if new_airco.HomeLeaveModeForCooling is None:
            new_airco.HomeLeaveModeForCooling = (
                self._device.airco.HomeLeaveModeForCooling
            )
        if new_airco.HomeLeaveModeForHeating is None:
            new_airco.HomeLeaveModeForHeating = (
                self._device.airco.HomeLeaveModeForHeating
            )

    async def request_status(self) -> None:
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

        carry_forward() keeps the reading available on every
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
        if self._device.reduced_status_block:
            raise self._device.reduced_status_error()
        if self._device.status_request_mode == STATUS_REQUEST_STOPPED:
            # This unit changes its settings whatever we put in the frame, so
            # the frame is not sent at all any more (#329).
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="status_request_not_sent",
                translation_placeholders={"device": self._device.device_name},
            )
        if (
            self._device.status_request_carries_state
            and not await self._device.async_read_before_echo()
        ):
            # Sending the state we have would be a write on this module, and
            # the caller asked for a reading.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="status_request_read_failed",
                translation_placeholders={"device": self._device.device_name},
            )
        await self._device.set_airco(
            {AirconCommands.HomeLeaveModeStatusRequest: True},
            is_status_request=True,
        )

    async def read_once(self) -> None:
        """Read Tag 248 once at setup, so away heating has its setting.

        The reading lives in memory only, and without it away heats to
        HOME_LEAVE_TEMP_HEAT rather than what the Smart M-Air app shows.
        Skipped on a module whose status request carries state or is not sent
        at all (#329): there the request is a write. Skipped as well on a unit
        with a reduced state block, which never answers it. A failure is not worth
        more than a debug line - away still works, on the fallback.
        """
        if (
            self._device.reduced_status_block
            or not self._device.airco.Capabilities.home_leave_mode
            or self._device.status_request_mode == STATUS_REQUEST_STOPPED
            or self._device.status_request_carries_state
        ):
            return
        try:
            await self._device.async_request_home_leave_mode_status()
        except (HomeAssistantError, WfRacError, KeyError, TypeError, ValueError) as ex:
            # Same set the queued-command flush catches: set_airco() raises
            # the encoder's errors as they come.
            _LOGGER.debug(
                "[%s] Home Leave mode not read at setup: %s",
                self._device.device_name,
                ex,
            )

    async def write(
        self, cooling: HomeLeaveModeSetting, heating: HomeLeaveModeSetting
    ) -> None:
        """Write new HomeLeaveMode thresholds and airflow.

        That is Tag 248, sub-codes 27-32. Written values round-trip exactly
        through a subsequent read.
        """
        await self._device.async_queue_command(
            {
                AirconCommands.HomeLeaveModeForCooling: cooling,
                AirconCommands.HomeLeaveModeForHeating: heating,
            }
        )
