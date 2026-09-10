"""Config flow WF-RAC"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import Any
from uuid import uuid4

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries, exceptions
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import (
    CONF_BASE,
    CONF_DEVICE_ID,
    CONF_FORCE_UPDATE,
    CONF_HOST,
    CONF_PORT,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow, section
from homeassistant.helpers import entity_registry as er, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    AC_CERT_FILENAME,
    DEFAULT_PORT,
    CONF_OVERSHOOT_COOL,
    CONF_OVERSHOOT_DRY,
    CONF_OVERSHOOT_HEAT,
    OVERSHOOT_MAX,
    CONF_AIRCO_ID,
    CONF_AVAILABILITY_RETRY_LIMIT,
    CONF_FIRMWARE_UPDATE_CHECK,
    CONF_EXTERNAL_TEMPERATURE_SOURCE,
    CONF_OPERATOR_ID,
    CONF_INDOOR_OFFSET,
    CONF_OUTDOOR_OFFSET,
    CONF_TARGET_OFFSET,
    CONF_TARGET_OFFSET_COOL,
    CONF_TARGET_OFFSET_HEAT,
    DOMAIN,
)
from .coordinator import AVAILABILITY_FAILURE_LIMIT_MIN
from pywfrac import Repository, WfRacError

_LOGGER = logging.getLogger(__name__)

# Form-only keys: sections group the fields in the dialog, they are not
# options themselves and never reach entry.options - see async_step_init.
SECTION_INDOOR_TEMPERATURE_SOURCE = "indoor_temperature_source"
SECTION_SETPOINT_OFFSETS = "setpoint_offsets"
SECTION_SENSOR_OFFSETS = "sensor_offsets"


class WfRacConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    # Has to match the highest version async_migrate_entry produces. Home
    # Assistant skips migration entirely once entry.version equals this, so a
    # new step that is not reflected here never runs.
    VERSION = 7
    DOMAIN = DOMAIN
    # Annotated, not assigned: a dict here would be shared by every flow.
    _discovery_info: dict[str, Any]

    def is_matching(self, other_flow: "WfRacConfigFlow") -> bool:
        """Return True if two flows are attempting to configure the same device."""
        # Compare based on unique IDs if available, otherwise compare context data
        if self.unique_id and other_flow.unique_id:
            return self.unique_id == other_flow.unique_id
        # For flows without unique IDs, consider them non-matching
        return False

    def _find_entry_matching(
        self, key: str, matches: Callable[[Any], bool]
    ) -> config_entries.ConfigEntry | None:
        """Returns the first entry where matches(entry.data[key]) returns True"""
        for entry in self._async_current_entries():
            if key in entry.data and matches(entry.data[key]):
                return entry
        return None

    async def _async_register_airco(
            self,
            hass: HomeAssistant,
            data: dict[str, Any],
            exclude_entry_id: str | None = None,
            allow_port_fallback: bool = False,
            expected_airco_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate the user input allows us to connect, and register with the airco device.

        allow_port_fallback belongs to discovery only: a port the module
        announced may be wrong, a port a person typed is their decision.

        expected_airco_id aborts before the registration request when some
        other unit answers - registering takes one of the few account slots
        the module has, and a reconfigure that lands on the wrong address has
        no business spending one.
        """
        if len(data[CONF_HOST]) < 3:
            raise InvalidHost

        if not data.get(CONF_FORCE_UPDATE):
            # Is this hostname or IP address already configured on a *different*
            # entry? During reconfigure, the entry being edited already owns
            # this host among its own options, so it must not flag itself.
            existing_entry = self._find_entry_matching(
                CONF_HOST, lambda h: h == data[CONF_HOST]
            )
            if existing_entry and existing_entry.entry_id != exclude_entry_id:
                raise HostAlreadyConfigured(error_name=existing_entry.title)

        repository = Repository(
            async_get_clientsession(hass),
            data[CONF_HOST],
            data[CONF_PORT],
            data[CONF_OPERATOR_ID],
            data[CONF_DEVICE_ID],
            cert_path=hass.config.path(AC_CERT_FILENAME),
        )

        try:
            airco_id = await repository.get_airco_id()
        except (
            WfRacError,
            KeyError,
            TypeError,
            ValueError,
            AttributeError,
            OSError,
        ) as query_failed:
            # A discovery announcement has been seen carrying a port the module
            # does not serve. The port is fixed in the firmware and not
            # user-settable, so rather than failing on a value the device
            # cannot have meant, try the one it always listens on. Only the
            # announced value is second-guessed - a port the user typed is
            # taken at face value.
            if not allow_port_fallback or data[CONF_PORT] == DEFAULT_PORT:
                raise CannotConnect(reason=str(query_failed)) from query_failed
            _LOGGER.warning(
                "No answer on announced port %s, retrying on %s. Please report "
                "this with the discovery details - the announced port is "
                "supposed to be %s on every firmware branch",
                data[CONF_PORT],
                DEFAULT_PORT,
                DEFAULT_PORT,
            )
            repository = Repository(
                async_get_clientsession(hass),
                data[CONF_HOST],
                DEFAULT_PORT,
                data[CONF_OPERATOR_ID],
                data[CONF_DEVICE_ID],
                cert_path=hass.config.path(AC_CERT_FILENAME),
            )
            try:
                airco_id = await repository.get_airco_id()
            except (
                WfRacError,
                KeyError,
                TypeError,
                ValueError,
                AttributeError,
                OSError,
            ) as retry_failed:
                raise CannotConnect(reason=str(retry_failed)) from retry_failed
            data[CONF_PORT] = DEFAULT_PORT

        data[CONF_AIRCO_ID] = airco_id
        if not airco_id:
            raise CannotConnect(reason="unknown reason")
        if (
            expected_airco_id is not None
            and airco_id.lower() != expected_airco_id.lower()
        ):
            raise AbortFlow("wrong_device")

        _LOGGER.debug("Registering this controller on airco [%s]", airco_id)
        try:
            result = await repository.update_account_info(
                airco_id, hass.config.time_zone
            )
        except (
            WfRacError,
            KeyError,
            TypeError,
            ValueError,
            AttributeError,
            OSError,
        ) as register_failed:
            # Everything a wrong address can answer with counts as not
            # reaching the unit: a body that is not JSON (ValueError), one
            # that is JSON but not an object (AttributeError), or a TLS
            # handshake that never got that far (OSError).
            raise CannotConnect(reason=str(register_failed)) from register_failed
        if not result:
            raise CannotConnect(reason="no answer to the registration request")
        try:
            registration_result = int(result["result"])
        except (KeyError, TypeError, ValueError) as unreadable:
            raise CannotConnect(reason="unreadable registration answer") from unreadable
        if registration_result == 2:
            raise TooManyDevicesRegistered

        return data

    async def _async_fetch_operator_id(self) -> str:
        """Fetch UUID operator id if exists otherwise create it"""
        entry = self._find_entry_matching(CONF_OPERATOR_ID, bool)
        if entry:
            return str(entry.data[CONF_OPERATOR_ID])
        return f"hassio-{str(uuid4())[7:]}"

    async def _async_fetch_device_id(self) -> str:
        """Fetch unique device id if exists otherwise create it"""
        entry = self._find_entry_matching(CONF_DEVICE_ID, bool)
        if entry:
            return str(entry.data[CONF_DEVICE_ID])
        return f"homeassistant-device-{uuid4().hex[21:]}"

    async def _async_create_common(
            self,
            step_id: str,
            data_schema: vol.Schema,
            user_input: dict[str, Any] | None = None,
            description_placeholders: dict[str, str] | None = None,
            allow_port_fallback: bool = False,
    ) -> ConfigFlowResult:
        """Create a new entry"""
        errors: dict[str, str] = {}
        description_placeholders = description_placeholders or {}

        if user_input:
            description_placeholders["error_name"] = ""
            try:
                user_input[CONF_OPERATOR_ID] = await self._async_fetch_operator_id()
                user_input[CONF_DEVICE_ID] = await self._async_fetch_device_id()

                info = await self._async_register_airco(
                    self.hass, user_input, allow_port_fallback=allow_port_fallback
                )

                # The airco id is the unit's own identity, and the one
                # zeroconf keys on: the module announces itself as
                # <mac>.local and the airco id is that same MAC. Registering
                # it here is what lets a discovery recognise a manually added
                # entry later - and it aborts a unit reached at a second
                # address, which would otherwise become a second entry whose
                # entities collide with the first one's. Lower case on both
                # sides: discovery reads it from the announced hostname and
                # every other path from the airconId the unit reports.
                await self.async_set_unique_id(info[CONF_AIRCO_ID].lower())
                self._abort_if_unique_id_configured()

                data_input = user_input.copy()
                # Form-only: it decides whether a duplicate host is accepted
                # while adding, and means nothing to a stored entry.
                data_input.pop(CONF_FORCE_UPDATE, None)
                options_input = {
                    CONF_AVAILABILITY_RETRY_LIMIT: AVAILABILITY_FAILURE_LIMIT_MIN,
                    CONF_FIRMWARE_UPDATE_CHECK: False,
                }

                # Named after the unit rather than asked for: config flows do
                # not collect entry names, and renaming is Home Assistant's
                # own. The last four characters of the airco id are enough to
                # tell two units apart and to match one against the label on
                # the module, while the whole id stays out of the device name
                # and the entity ids built from it.
                return self.async_create_entry(
                    title=f"WF-RAC {info[CONF_AIRCO_ID][-4:]}",
                    data=data_input,
                    options=options_input,
                )
            except KnownError as error:
                _LOGGER.error("create failed")
                errors, placeholders = error.get_errors_and_placeholders(
                    data_schema.schema
                )
                for key, value in placeholders.items():
                    if isinstance(value, dict):
                        description_placeholders[key] = str(value)
                    else:
                        description_placeholders[key] = value
            except AbortFlow:
                # An abort is the flow working as intended - already
                # configured, already in progress - not an unexpected error.
                raise
            except Exception:  # pylint: disable=broad-except
                # Intentionally broad: this is the outermost boundary of the config
                # flow step, so any bug here should show the user a graceful
                # "unexpected_error" instead of crashing the flow.
                _LOGGER.error("Unexpected exception", exc_info=True)
                errors[CONF_BASE] = "unexpected_error"

        # If there is no user input or there were errors, show the form again, including any errors
        # that were found with the input.
        return self.async_show_form(
            step_id=step_id,
            data_schema=data_schema,
            errors=errors,
            description_placeholders=description_placeholders,
        )

    @staticmethod
    def _field(
        user_input: dict[str, Any] | None,
        name: str,
        which: Callable[..., Any],
        default: Any = None,
    ) -> Any:
        """Helper for creating schema fields"""
        value = user_input.get(name, default) if user_input else default
        description = None
        if value is not None:
            description = {"suggested_value": value}
        return which(name, description=description)

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle adding device discovered by zeroconf."""

        description_placeholders = {
            "id": self._discovery_info[CONF_AIRCO_ID],
            "host": self._discovery_info[CONF_HOST],
            "port": self._discovery_info[CONF_PORT],
        }

        if user_input:
            user_input[CONF_HOST] = self._discovery_info[CONF_HOST]
            user_input.setdefault(CONF_PORT, self._discovery_info[CONF_PORT])

        field = partial(self._field, user_input)
        data_schema = vol.Schema(
            {
                field(
                    CONF_PORT, vol.Optional, self._discovery_info[CONF_PORT]
                ): cv.port,
            }
        )

        return await self._async_create_common(
            step_id="discovery_confirm",
            data_schema=data_schema,
            user_input=user_input,
            description_placeholders=description_placeholders,
            allow_port_fallback=True,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
            config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return WfRacOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle adding device manually."""

        field = partial(self._field, user_input)
        data_schema = vol.Schema(
            {
                field(CONF_HOST, vol.Required): cv.string,
                field(CONF_PORT, vol.Optional, DEFAULT_PORT): cv.port,
                field(CONF_FORCE_UPDATE, vol.Optional, False): cv.boolean,
            }
        )

        return await self._async_create_common(
            step_id="user", data_schema=data_schema, user_input=user_input
        )

    async def async_step_reconfigure(
            self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle changing an existing entry's connection details (host/port)."""
        reconfigure_entry = self._get_reconfigure_entry()
        current = {
            CONF_HOST: reconfigure_entry.data[CONF_HOST],
            CONF_PORT: reconfigure_entry.data[CONF_PORT],
        }

        field = partial(self._field, user_input or current)
        data_schema = vol.Schema(
            {
                field(CONF_HOST, vol.Required): cv.string,
                field(CONF_PORT, vol.Optional, DEFAULT_PORT): cv.port,
            }
        )

        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}

        if user_input:
            try:
                data = dict(user_input)
                data[CONF_OPERATOR_ID] = reconfigure_entry.data[CONF_OPERATOR_ID]
                data[CONF_DEVICE_ID] = reconfigure_entry.data[CONF_DEVICE_ID]

                # The address may change, the unit behind it may not: every
                # entity's unique id is built from the airco id, so following
                # a typo to the next unit renames them all, orphans the
                # originals, and leaves discovery able to place neither.
                await self._async_register_airco(
                    self.hass,
                    data,
                    exclude_entry_id=reconfigure_entry.entry_id,
                    expected_airco_id=reconfigure_entry.data[CONF_AIRCO_ID],
                )

                new_data = {**reconfigure_entry.data, **data}

                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data=new_data,
                )
            except KnownError as error:
                errors, placeholders = error.get_errors_and_placeholders(
                    data_schema.schema
                )
                description_placeholders.update(
                    {k: str(v) for k, v in placeholders.items()}
                )
            except AbortFlow:
                # An abort is the flow working as intended - already
                # configured, already in progress - not an unexpected error.
                raise
            except Exception:  # pylint: disable=broad-except
                # Same outermost boundary as _async_create_common: a bug here
                # should surface as "unexpected_error", not crash the flow.
                _LOGGER.error("Unexpected exception", exc_info=True)
                errors[CONF_BASE] = "unexpected_error"

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=data_schema,
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_zeroconf(
            self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle zeroconf discovery."""

        local_name = discovery_info.hostname.rstrip(".")
        node_name = local_name.removesuffix(".local")
        host = discovery_info.host
        port = discovery_info.port

        _LOGGER.debug(
            "zeroconf discovery: hostname=%r, host=%r, port=%r",
            discovery_info.hostname,
            discovery_info.host,
            discovery_info.port,
        )

        # Lower case on both sides: this id comes from the announced
        # hostname while every other path takes it from the airconId the unit
        # reports, and a difference in case would leave discovery unable to
        # recognise an entry it had matched on before.
        await self.async_set_unique_id(node_name.lower())
        # The address only. A module that moved gets followed; its port is
        # what setup was configured with, and a rediscovery announcing a
        # different one would take a working entry offline.
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        info = {CONF_HOST: host, CONF_PORT: port}

        existing_entry = self._find_entry_matching(CONF_HOST, lambda h: h == host)
        if existing_entry:
            _LOGGER.debug("already configured!")
            return self.async_abort(reason="already_configured")

        info[CONF_AIRCO_ID] = node_name
        self._discovery_info = info

        return await self.async_step_discovery_confirm()

class WfRacOptionsFlowHandler(config_entries.OptionsFlowWithReload):
    """Base class for options handling.

    OptionsFlowWithReload rather than OptionsFlow: every option here is read
    once while the device is built (see create_device_from_entry), so a change
    only takes effect after a reload. An update listener must not be combined
    with the flow's own reloading methods - the two reload the entry twice and
    race each other.
    """

    def _own_entity_ids(self) -> list[str]:
        """Entity IDs this entry owns - the ones a source must never be."""
        registry = er.async_get(self.hass)
        return sorted(
            entry.entity_id
            for entry in er.async_entries_for_config_entry(
                registry, self.config_entry.entry_id
            )
        )

    @property
    def _source_configured(self) -> bool:
        """Whether a temperature source entity is picked for this entry."""
        return bool(self.config_entry.options.get(CONF_EXTERNAL_TEMPERATURE_SOURCE))

    def _rendered_option_keys(self) -> set[str]:
        """The option keys this form shows for the current configuration.

        Deliberately derived from the saved options - the same input the
        schema is built from - rather than recorded while building it: the
        save path needs to know what the form could not have collected, and
        answering that from state carried between the two halves is one
        forgotten assignment away from silently dropping settings.
        """
        keys = {
            CONF_AVAILABILITY_RETRY_LIMIT,
            CONF_FIRMWARE_UPDATE_CHECK,
            CONF_EXTERNAL_TEMPERATURE_SOURCE,
            CONF_TARGET_OFFSET,
            CONF_TARGET_OFFSET_COOL,
            CONF_TARGET_OFFSET_HEAT,
            CONF_INDOOR_OFFSET,
            CONF_OUTDOOR_OFFSET,
        }
        if self._source_configured:
            keys |= {CONF_OVERSHOOT_COOL, CONF_OVERSHOOT_DRY, CONF_OVERSHOOT_HEAT}
        return keys

    async def async_step_init(
            self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            # Sections hand their fields back nested under the section key,
            # while everything that reads an option reads it flat off
            # entry.options - so the shape is flattened straight back out and
            # the stored options stay exactly what they have always been.
            data: dict[str, Any] = {}
            for key, value in user_input.items():
                if isinstance(value, dict):
                    data.update(value)
                else:
                    data[key] = value
            # A field the form did not show cannot be collected from it, and
            # async_create_entry replaces the options wholesale rather than
            # merging - so an unrendered value has to be carried over by hand
            # or it is dropped. That is how the overshoot figures survive
            # removing the source that hides them. A field that was shown and
            # left empty is meant to be empty and is not carried over.
            for key, value in self.config_entry.options.items():
                if key not in self._rendered_option_keys():
                    data.setdefault(key, value)
            return self.async_create_entry(title="", data=data)

        options = self.config_entry.options

        def degrees(limit: float, step: float) -> selector.NumberSelector:
            """A correction in degrees, as a number box.

            A bare float leaves the step to the browser, which is a whole
            degree - and every field in this form is a correction that is read
            in fractions of one. The step is what the value can still change
            downstream, so each caller passes its own. Off-grid values already
            stored keep loading either way: the selector holds the range, not
            the step.
            """
            return selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=-limit,
                    max=limit,
                    step=step,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement=UnitOfTemperature.CELSIUS,
                )
            )

        # Half a degree, because that is the setpoint's own resolution - and
        # most units round that up to the next whole one anyway (see the
        # field's description), so a finer step here would promise a precision
        # the setpoint does not have.
        offset_range_validator = degrees(5.0, 0.5)
        # Negative allowed, though overshooting is what everyone has measured
        # so far: a unit that stops short of the setting instead needs the
        # correction the other way, and there is no reason to make that
        # impossible before anyone has looked.
        # A quarter degree is where this one stops mattering: the room
        # temperature byte it corrects is round(T * 4) + 61, and the source
        # value entering that sum has already been snapped to the same grid
        # (see AircoClimate._external_temperature_from_source_state), so a
        # finer correction is rounded away for every reading rather than only
        # for some.
        overshoot_validator = degrees(OVERSHOOT_MAX, 0.25)

        source_fields: dict[Any, Any] = {
            # Keep this optional without a default: an omitted source must
            # stay omitted, rather than becoming a falsey value that
            # unnecessarily changes the saved options shape.
            vol.Optional(
                CONF_EXTERNAL_TEMPERATURE_SOURCE,
                description={
                    "suggested_value": options.get(CONF_EXTERNAL_TEMPERATURE_SOURCE)
                },
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class="temperature",
                    # This integration's own temperature sensors report the
                    # injected value back while an override is armed, so
                    # picking one would feed the override into itself and walk
                    # it away from the room half a kelvin per poll.
                    # EntitySelector rejects an excluded entity on submit, not
                    # just in the picker, so this is the enforcement and not
                    # only a convenience.
                    exclude_entities=self._own_entity_ids(),
                )
            ),
        }
        # Tied to the source picker on purpose, and not because the correction
        # needs one: _corrected_external_temperature() bends whatever value is
        # armed, including one set from the action. A source entity is what
        # makes the corrections worth offering - it is watched, so a reading
        # that goes unavailable or unusable clears the override and hands the
        # unit back to its own sensor, while a value armed from the action
        # stays until something clears it. Showing the fields without a source
        # would recommend the arrangement that has no fallback (#218). They
        # appear once a source is picked and saved - a form cannot rebuild
        # itself while it is open.
        if self._source_configured:
            source_fields.update(
                {
                    # Cooling starts at the figure four units have measured
                    # (0.6-1.3 K past the setting, three of them 1.0-1.2), so
                    # the field opens on a number that is roughly right
                    # instead of on one that is certainly wrong. It is a
                    # pre-fill and nothing more: the correction applies once
                    # the form is saved, and _resolve_overshoot still reads 0
                    # until then, so nobody's regulation moves without them
                    # seeing the value first. Heating has looked symmetric
                    # around the setting wherever it has been measured, so
                    # there is no figure to offer - and dry opens on zero for
                    # the opposite reason: nobody has measured it at all, and a
                    # pre-filled guess there would move real regulation on the
                    # strength of one (#218).
                    vol.Optional(
                        key, default=options.get(key, suggested)
                    ): overshoot_validator
                    for key, suggested in (
                        (CONF_OVERSHOOT_COOL, 1.0),
                        (CONF_OVERSHOOT_DRY, 0.0),
                        (CONF_OVERSHOOT_HEAT, 0.0),
                    )
                }
            )

        setpoint_fields: dict[Any, Any] = {
            vol.Optional(
                CONF_TARGET_OFFSET,
                default=options.get(CONF_TARGET_OFFSET, 0.0),
            ): offset_range_validator,
        }
        # target_offset_cool/heat are optional per-mode overrides that must
        # stay "unset" (None) unless the user explicitly fills them in - a
        # default= here would coerce a blank field to 0.0 and defeat the
        # fallback-to-target_offset resolution in climate.py. suggested_value
        # (not default=) pre-fills the displayed value without forcing one
        # when absent.
        setpoint_fields.update(
            {
                vol.Optional(
                    key,
                    description={"suggested_value": options.get(key)},
                ): vol.Any(None, offset_range_validator)
                for key in (CONF_TARGET_OFFSET_COOL, CONF_TARGET_OFFSET_HEAT)
            }
        )

        # A tenth here, unlike the fields above: these two correct a reading on
        # its way to being displayed, so nothing rounds them off afterwards.
        sensor_offset_validator = degrees(15.0, 0.1)
        sensor_fields: dict[Any, Any] = {
            vol.Optional(
                key,
                default=options.get(key, 0.0),
            ): sensor_offset_validator
            for key in (CONF_INDOOR_OFFSET, CONF_OUTDOOR_OFFSET)
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    # Floor, not a free number: values below the minimum were
                    # the reason this option kept needing correcting in
                    # migrations. Raising it stays available for weak links.
                    vol.Required(
                        CONF_AVAILABILITY_RETRY_LIMIT,
                        default=options.get(
                            CONF_AVAILABILITY_RETRY_LIMIT, AVAILABILITY_FAILURE_LIMIT_MIN
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=AVAILABILITY_FAILURE_LIMIT_MIN)),
                    vol.Required(
                        CONF_FIRMWARE_UPDATE_CHECK,
                        default=options.get(CONF_FIRMWARE_UPDATE_CHECK, False),
                    ): bool,
                    vol.Required(SECTION_INDOOR_TEMPERATURE_SOURCE): section(
                        vol.Schema(source_fields), {"collapsed": False}
                    ),
                    # Collapsed once a source is in use: the overshoot above is
                    # the right lever then, and these two stack with it if both
                    # are set. Still shown, because they keep working - what
                    # they correct is the unit's own sensor bias, which is out
                    # of the loop while the unit regulates on a supplied value.
                    vol.Required(SECTION_SETPOINT_OFFSETS): section(
                        vol.Schema(setpoint_fields),
                        {"collapsed": self._source_configured},
                    ),
                    vol.Required(SECTION_SENSOR_OFFSETS): section(
                        vol.Schema(sensor_fields), {"collapsed": True}
                    ),
                },
            ),
        )


# pylint: disable=too-few-public-methods


class KnownError(exceptions.HomeAssistantError):
    """Base class for errors known to this config flow.

    [error_name] is the value passed to [errors] in async_show_form, which should match a key
    under "errors" in strings.json

    [applies_to_field] is the name of the field name that contains the error (for
    async_show_form); if the field doesn't exist in the form CONF_BASE will be used instead.
    """

    error_name = "unknown_error"
    applies_to_field = CONF_BASE

    def __init__(self, *args: object, **kwargs: str) -> None:
        super().__init__(*args)
        self._extra_info = kwargs

    def get_errors_and_placeholders(
        self, schema: Any
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Return dicts of errors and description_placeholders, for adding to async_show_form"""
        key = self.applies_to_field
        # An error only shows if its key is in the form; anything else falls
        # back to CONF_BASE.
        if key not in {k.schema for k in schema}:
            key = CONF_BASE
        return ({key: self.error_name}, self._extra_info or {})


class CannotConnect(KnownError):
    """Error to indicate we cannot connect."""

    error_name = "cannot_connect"


class InvalidHost(KnownError):
    """Error to indicate there is an invalid hostname."""

    error_name = "invalid_host"
    applies_to_field = CONF_HOST


class HostAlreadyConfigured(KnownError):
    """Error to indicate there is an duplicate hostname."""

    error_name = "host_already_configured"
    applies_to_field = CONF_HOST


class TooManyDevicesRegistered(KnownError):
    """Error to indicate that there are too many devices registered"""

    error_name = "too_many_devices_registered"
    applies_to_field = CONF_BASE
