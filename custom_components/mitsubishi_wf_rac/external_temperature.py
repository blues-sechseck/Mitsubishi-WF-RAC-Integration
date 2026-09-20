"""The external room-temperature override, and everything it owns.

The unit has no concept of an override: byte 5 of a frame carries whatever
room temperature the controller is working with, and the unit regulates on the
last value it was given. Keeping our side of that straight is four pieces of
state and a handful of rules about when they change, all of which used to live
in the coordinator among the polling. They are gathered here because they
belong to each other and to nothing else.

The coordinator keeps what needs the wire: sending a frame, deciding whether a
status request is allowed, reading the unit back. This class decides what the
next frame should carry.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

from pywfrac.parser import SERVICE_DATA_INDOOR_COIL_RAW

from .const import (
    CONF_EXTERNAL_TEMPERATURE_SOURCE,
    CONF_OVERSHOOT_COOL,
    CONF_OVERSHOOT_DRY,
    CONF_OVERSHOOT_HEAT,
    OPERATION_MODE_COOL,
    OPERATION_MODE_DRY,
    OPERATION_MODE_HEAT,
)

if TYPE_CHECKING:
    from .coordinator import Device


class ExternalTemperatureFeed:
    """What the unit should be told the room temperature is.

    Owned by one Device, and reaching back into it for the three things only
    the coordinator has: the config entry's options, the listener registry the
    carrier subscription lives in, and the operation-data request the value
    rides out on.
    """

    def __init__(self, device: Device) -> None:
        """Bind the feed to its coordinator."""
        self._device = device
        self._override: float | None = None
        # The byte-5 values recent frames actually carried. Two, not one: a
        # frame carrying a new value goes out before the unit reports it back,
        # so during that one cycle the previous value is still the one the
        # unit is regulating on. Comparing against only the newest would make
        # applied - and with it the indoor offset - flip off and on again on
        # every value a source sensor feeds in.
        self._written: deque[int] = deque(maxlen=2)
        self._carrier: Callable[[], None] | None = None
        # Set when an override goes away after a frame has carried it: the
        # unit holds the last value it was given until a frame carries the
        # sentinel instead, so clearing on our side is only half of letting
        # go. Cleared again by the frame that does it - see note_frame().
        self._release_pending = False

    @property
    def override(self) -> float | None:
        """Return the integration-side external temperature override, if any.

        This is tracked by the integration rather than read back from the unit,
        because the wire byte reports the temperature the controller is working
        with regardless of its source and provides no flag for whether that
        value originated from an external override.
        """
        return self._override

    def set_override(self, value: float | None) -> None:
        """Set the integration-side override state.

        Used by the climate entity when restoring persisted state and whenever
        the configured source reports. Arming asks for the operation-data frame
        the value rides on (see _sync_carrier), since the poll that would
        otherwise schedule one runs before the entities exist - but that frame
        writes no setting of its own, so nothing here commands the unit. Which
        is exactly why the value counts as unapplied until a frame has carried
        it: it says what we intend to send, not what the unit currently
        regulates on.

        Nothing here says the unit has been told - see applied, which reads
        that off the wire.

        Clearing is not symmetrical with arming. Byte 5 has no set-bit, so the
        unit keeps regulating on the last value we sent it until a frame
        carries the sentinel - and the carrier that frame rides on is dropped
        by this very call. A clear therefore leaves a release outstanding
        whenever a frame really did carry a value, and the carrier stays up
        until one has carried the sentinel (#218 follow-up).
        """
        if value is None:
            if self._written:
                self._release_pending = True
            self._written.clear()
        else:
            # Arming again is its own release: whatever goes out next carries
            # the new value, and the unit was never handed back in between.
            self._release_pending = False
        self._override = value
        self._sync_carrier()

    def request_release(self) -> None:
        """Ask for a frame that hands the unit back to its own sensor.

        For the case this coordinator cannot see for itself: the frames that
        carried a value went out before a reload, so _written is empty here and
        nothing in this object knows the unit is still being fed. The climate
        entity knows, from its restored state - see
        AircoClimate.async_added_to_hass().
        """
        if self._override is not None:
            return
        self._release_pending = True
        self._sync_carrier()

    def release_carrier(self) -> None:
        """Drop the operation-data subscription the override holds, if any."""
        if self._carrier is not None:
            self._carrier()
            self._carrier = None

    def _sync_carrier(self) -> None:
        """Hold an operation-data subscription while an override is armed.

        The subscription also covers the one frame that hands the unit back
        afterwards, which is why a pending release holds it up just as an
        armed value does.

        The override needs a frame to ride on, and the operation-data request
        is the one frame that goes out on its own without writing anything
        else. Rather than making that the user's problem - enable a diagnostic
        sensor or the feature quietly does nothing - the override subscribes
        like any other consumer of that request, and the coordinator starts
        asking for the same reason it does for an enabled sensor.

        Costs what an enabled operation-data sensor costs: one extra request
        per poll cycle, holding the unit's write lock for part of it.
        """
        if self._override is not None or self._release_pending:
            needs_request = self._release_pending
            if self._carrier is None:
                needs_request = True
                self._carrier = self._device.async_add_listener(
                    lambda: None, context=SERVICE_DATA_INDOOR_COIL_RAW
                )
            if needs_request:
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
                self._device.maybe_request_service_data()
            return
        self.release_carrier()

    def note_frame(self, written: int | None) -> None:
        """Record what byte 5 of a frame that just went out really carried.

        Only for what the frame really carried: a command sent while the unit
        is off writes the sentinel, not the override. A sentinel with a
        release outstanding settles it - the unit is back on its own sensor
        and the carrier has nothing left to carry. Any frame will do, a
        command the user sent as well as the operation-data request asked for
        on purpose.
        """
        if written is None:
            self._written.clear()
            if self._release_pending and self._override is None:
                self._release_pending = False
                self.release_carrier()
        else:
            self._written.append(written)

    def corrected(self, temperature: float | None, operation_mode: int) -> float | None:
        """Bend the room temperature we hand the unit by its overshoot.

        The unit's thermostat band sits below the setting in cooling (measured
        across four units: it keeps calling for cooling until roughly half a
        kelvin under it, see issue #218). Telling it the room is that much
        colder than it is moves its stop point to where the room actually
        reaches the setting - and unlike the setpoint, which the unit rounds
        to whole degrees, this lever has the protocol's 0.25 K resolution.

        Heating is the mirror image, and zero - the default - changes nothing.

        Dry has a correction of its own rather than sharing the cooling one.
        It cools too, so the sign matches, but its airflow and its thermostat
        band are not the cooling ones: the band is wider and, on the one unit
        measured, centred on the setting, which is why its field opens on zero
        where cooling opens on the figure four units needed. Auto is left
        uncorrected: which direction it is running in is CoolHotJudge, a value
        some units never report.
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
        value = self._device.options.get(key, 0.0)
        return float(value) if isinstance(value, (int, float)) else 0.0

    @property
    def room_value(self) -> float | None:
        """The room temperature we handed the unit, or None while it regulates.

        Regulating here means on its own sensor.

        Whoever supplies a room temperature has said what "the room" means for
        this unit, so that is what the climate entity shows for as long as the
        unit is actually using it. What comes back from the unit is not it: an
        overshoot correction hands it a value that is deliberately not the
        room, and the echo is that corrected value. Deciding this per
        overshoot - as this did until the reading was found to move by the
        correction when an unrelated option changed - makes the displayed room
        temperature depend on a setting that has nothing to do with it, and
        every automation comparing it against a threshold inherits that
        silently.

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
        if self._override is None or self._device.airco is None:
            return None
        source = self._device.options.get(CONF_EXTERNAL_TEMPERATURE_SOURCE)
        if isinstance(source, str) and source:
            return self._override
        if not self.applied:
            return None
        return self._override

    @property
    def applied(self) -> bool:
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
        airco = self._device.airco
        if self._override is None or airco is None:
            return False
        raw = airco.ControllerRoomTempRaw
        return raw is not None and raw in self._written
