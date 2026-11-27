"""Coalescing of the commands entities issue close together."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from pywfrac import AIRFLOW_UNKNOWN, AirconCommands, WfRacError

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import Device

_LOGGER = logging.getLogger(__name__)

# Commands issued within this window of each other (from any entity) are
# coalesced into a single set_airco() call instead of being sent as separate
# requests. The unit expects a full state block per request, so two
# near-simultaneous separate commands can otherwise overwrite each other
# instead of merging (e.g. a fan-speed change followed shortly by a
# temperature change loses the fan change).
UPDATE_CONSOLIDATION_PERIOD = timedelta(milliseconds=500)


class CommandQueue:
    """Merges the commands issued within one window into a single request."""

    def __init__(self, device: Device) -> None:
        """Bind the queue to its coordinator."""
        self._device = device
        self._consolidated_params: dict[AirconCommands, Any] = {}
        self._consolidation_task: asyncio.Task[None] | None = None
        # _consolidation_task is only the one still taking parameters.
        self._running_flushes: set[asyncio.Task[None]] = set()

    @property
    def running_flushes(self) -> set[asyncio.Task[None]]:
        """Return every flush that has not finished, taking parameters or not."""
        return self._running_flushes

    def forget_pending(self) -> None:
        """Stop taking parameters into the flush that is still waiting."""
        self._consolidation_task = None

    async def queue(self, params: dict[AirconCommands, Any]) -> None:
        """Queue an airco command, coalescing calls made close together.

        Calls within UPDATE_CONSOLIDATION_PERIOD become one set_airco(). Every
        entity uses this rather than set_airco(), so a fan change and a
        setpoint change issued together share a request instead of racing.
        """
        self._consolidated_params.update(params)
        if (flush := self._consolidation_task) is None:
            flush = self._device.hass.async_create_task(self._async_flush())
            self._consolidation_task = flush
            self._running_flushes.add(flush)
            flush.add_done_callback(self._flush_done)
        # Every caller awaits the one flush its parameters ended up in, so a
        # refusal by the unit reaches the action that caused it instead of
        # being logged into the void. Not awaited directly because the task is
        # shared: a caller giving up (a cancelled service call) must not take
        # the other callers' command down with it.
        try:
            await asyncio.wait({flush})
            flush.result()
        except asyncio.CancelledError:
            task = asyncio.current_task()
            # Our own cancellation must propagate; only the flush being
            # cancelled at shutdown is turned into an error.
            if task is not None and task.cancelling():
                raise
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_cancelled",
                translation_placeholders={"device": self._device.device_name},
            ) from None

    def _flush_done(self, flush: asyncio.Task[None]) -> None:
        self._running_flushes.discard(flush)
        # With every caller gone nobody reads the error, which asyncio would
        # then report as never retrieved.
        if not flush.cancelled():
            flush.exception()

    async def _async_flush(self) -> None:
        await asyncio.sleep(UPDATE_CONSOLIDATION_PERIOD.total_seconds())
        params = self._consolidated_params.copy()
        self._consolidated_params.clear()
        self._consolidation_task = None
        # A real, set-bit command: mark it so the next operation-data request
        # stamps honestly and renews this command's lock rather than trimming
        # it (see service_data_stamp_backdate).
        self._device.mark_command_sent()
        try:
            await self._device.set_airco(params)
        except ValueError as ex:
            if self._device.airco.AirFlow != AIRFLOW_UNKNOWN:
                raise self._command_failed(ex) from ex
            # The fan step the unit reported has no byte to send back; nothing
            # went out.
            self._device.async_update_listeners()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_unencodable",
                translation_placeholders={"device": self._device.device_name},
            ) from ex
        except (WfRacError, KeyError, TypeError) as ex:
            # Already logged in set_airco(). A failed command says nothing
            # about the poll before it, so the listeners hear the state without
            # the coordinator being declared successful. Wrapped rather than
            # re-raised - a library exception in a service call is a traceback,
            # not something the user can read - and async_queue_command()
            # awaits this task, so it lands on the action that issued it.
            raise self._command_failed(ex) from ex
        # The unit's answer reaches the entities now, not a poll later.
        self._device.async_set_updated_data(self._device.airco)

    def _command_failed(self, error: Exception) -> HomeAssistantError:
        """Tell the listeners and build the error the action reports."""
        self._device.async_update_listeners()
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={
                "device": self._device.device_name,
                "error": str(error),
            },
        )
