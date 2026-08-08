"""Config flow WF-RAC"""

from __future__ import annotations

import logging
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
    CONF_NAME,
    CONF_PORT,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_AIRCO_ID,
    CONF_AVAILABILITY_RETRY_LIMIT,
    CONF_CREATE_SWING_MODE_SELECT,
    CONF_FIRMWARE_UPDATE_CHECK,
    CONF_OPERATOR_ID,
    CONF_INDOOR_OFFSET,
    CONF_OUTDOOR_OFFSET,
    CONF_SERVICE_DATA,
    CONF_TARGET_OFFSET,
    CONF_TARGET_OFFSET_COOL,
    CONF_TARGET_OFFSET_HEAT,
    DOMAIN,
)
from .wfrac.device import AVAILABILITY_FAILURE_LIMIT_MIN
from .wfrac.repository import AirconApiError, Repository

_LOGGER = logging.getLogger(__name__)


class WfRacConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 5
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL
    _discovery_info = {}
    DOMAIN = DOMAIN

    def is_matching(self, other_flow: "WfRacConfigFlow") -> bool:
        """Return True if two flows are attempting to configure the same device."""
        # Compare based on unique IDs if available, otherwise compare context data
        if self.unique_id and other_flow.unique_id:
            return self.unique_id == other_flow.unique_id
        # For flows without unique IDs, consider them non-matching
        return False

    def _find_entry_matching(self, key, matches):
        """Returns the first entry where matches(entry.data[key]) returns True"""
        for entry in self._async_current_entries():
            if key in entry.data and matches(entry.data[key]):
                return entry
        return None

    def _find_entry_matching_option(self, key, matches):
        """Returns the first entry where matches(entry.options[key]) returns True"""
        for entry in self._async_current_entries():
            if key in entry.options and matches(entry.options[key]):
                return entry
        return None

    async def _async_register_airco(
            self, hass: HomeAssistant, data: dict
    ) -> dict[str, Any]:
        """Validate the user input allows us to connect, and register with the airco device"""
        if len(data[CONF_HOST]) < 3:
            raise InvalidHost

        if len(data[CONF_NAME]) < 3:
            raise InvalidName

        if not data.get(CONF_FORCE_UPDATE):
            # Is this hostname or IP address already configured?
            existing_entry = self._find_entry_matching_option(
                CONF_HOST, lambda h: h == data[CONF_HOST]
            )
            if existing_entry:
                raise HostAlreadyConfigured(error_name=existing_entry.data[CONF_NAME])

        repository = Repository(
            hass,
            data[CONF_HOST],
            data[CONF_PORT],
            data[CONF_OPERATOR_ID],
            data[CONF_DEVICE_ID],
        )

        try:
            airco_id = await repository.get_airco_id()
        except (AirconApiError, KeyError, TypeError) as query_failed:
            raise CannotConnect(reason=str(query_failed)) from query_failed  # type: ignore

        data[CONF_AIRCO_ID] = airco_id
        if not airco_id:
            raise CannotConnect(reason="unknown reason")  # type: ignore

        _LOGGER.info(
            "Trying to register OperatorId[%s] on Airco[%s]",
            data[CONF_OPERATOR_ID],
            data[CONF_AIRCO_ID],
        )
        result = await repository.update_account_info(airco_id, hass.config.time_zone)
        if not result:
            raise CannotConnect
        if int(result["result"]) == 2:
            raise TooManyDevicesRegistered

        return data

    async def _async_fetch_operator_id(self):
        """Fetch UUID operator id if exists otherwise create it"""
        entry = self._find_entry_matching(CONF_OPERATOR_ID, bool)
        if entry:
            return entry.data[CONF_OPERATOR_ID]
        return f"hassio-{str(uuid4())[7:]}"

    async def _async_fetch_device_id(self):
        """Fetch unique device id if exists otherwise create it"""
        entry = self._find_entry_matching(CONF_DEVICE_ID, bool)
        if entry:
            return entry.data[CONF_DEVICE_ID]
        return f"homeassistant-device-{uuid4().hex[21:]}"

    async def _async_create_common(
            self,
            step_id: str,
            data_schema: vol.Schema,
            user_input: dict[str, Any] | None = None,
            description_placeholders: dict[str, str] | None = None,
    ):
        """Create a new entry"""
        errors = {}
        description_placeholders = description_placeholders or {}

        if user_input:
            description_placeholders["error_name"] = ""
            try:
                user_input[CONF_OPERATOR_ID] = await self._async_fetch_operator_id()
                user_input[CONF_DEVICE_ID] = await self._async_fetch_device_id()

                info = await self._async_register_airco(self.hass, user_input)

                data_input = user_input.copy()
                options_input = {
                    CONF_HOST: user_input[CONF_HOST],
                    CONF_AVAILABILITY_RETRY_LIMIT: AVAILABILITY_FAILURE_LIMIT_MIN,
                    CONF_FIRMWARE_UPDATE_CHECK: False,
                    # Off by default: each poll would otherwise carry an
                    # extra write to the unit just to piggy-back a read
                    # request on it - see wfrac/device.py's
                    # _maybe_request_service_data().
                    CONF_SERVICE_DATA: False,
                }
                data_input.pop(CONF_HOST)

                return self.async_create_entry(
                    title=info[CONF_NAME],
                    data=data_input,
                    options=options_input,
                )
            except KnownError as error:
                _LOGGER.error("create failed")
                errors, placeholders = error.get_errors_and_placeholders(
                    data_schema.schema
                )
                errors.update(errors)
                for key, value in placeholders.items():
                    if isinstance(value, dict):
                        description_placeholders[key] = str(value)
                    else:
                        description_placeholders[key] = value
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
    def _field(user_input, name, which, default=None):
        """Helper for creating schema fields"""
        value = user_input.get(name, default) if user_input else default
        description = None
        if value is not None:
            description = {"suggested_value": value}
        return which(name, description=description)

    async def async_step_discovery_confirm(self, user_input=None):
        """Handle adding device discovered by zeroconf."""

        description_placeholders = {
            "id": self._discovery_info[CONF_NAME],
            "host": self._discovery_info[CONF_HOST],
            "port": self._discovery_info[CONF_PORT],
        }

        if user_input:
            for key in [CONF_HOST, CONF_PORT]:
                user_input[key] = self._discovery_info[key]

        field = partial(self._field, user_input)
        data_schema = vol.Schema(
            {
                field(
                    CONF_NAME, vol.Required, f"Airco {self._discovery_info[CONF_NAME]}"
                ): str,
            }
        )

        return await self._async_create_common(
            step_id="discovery_confirm",
            data_schema=data_schema,
            user_input=user_input,
            description_placeholders=description_placeholders,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
            config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return WfRacOptionsFlowHandler()

    async def async_step_user(self, user_input=None):
        """Handle adding device manually."""

        field = partial(self._field, user_input)
        data_schema = vol.Schema(
            {
                field(CONF_NAME, vol.Required, "Airco unknown"): cv.string,
                field(CONF_HOST, vol.Required): cv.string,
                field(CONF_PORT, vol.Optional, 51443): cv.port,
                field(CONF_FORCE_UPDATE, vol.Optional, False): cv.boolean,
                field(CONF_CREATE_SWING_MODE_SELECT, vol.Optional, True): cv.boolean,
            }
        )

        return await self._async_create_common(
            step_id="user", data_schema=data_schema, user_input=user_input
        )

    async def async_step_zeroconf(
            self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle zeroconf discovery."""

        local_name = discovery_info.hostname.rstrip(".")
        node_name = local_name[: -len(".local")]
        host = discovery_info.host
        port = discovery_info.port

        _LOGGER.debug(
            "zeroconf discovery: hostname=%r, host=%r, port=%r",
            discovery_info.hostname,
            discovery_info.host,
            discovery_info.port,
        )

        info = {CONF_HOST: host, CONF_PORT: port}

        await self.async_set_unique_id(node_name)
        self._abort_if_unique_id_configured(updates=info)

        existing_entry = self._find_entry_matching_option(CONF_HOST, lambda h: h == host)
        if existing_entry:
            _LOGGER.debug("already configured!")
            return self.async_abort(reason="already_configured")

        info[CONF_NAME] = node_name
        self._discovery_info = info

        return await self.async_step_discovery_confirm()

    @property
    def _name(self) -> str | None:
        return self.context.get(CONF_NAME)


class WfRacOptionsFlowHandler(config_entries.OptionsFlow):
    """Base class for options handling."""

    async def async_step_init(
            self, user_input: dict[str, Any] | None = None
    ):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        offset_range_validator = vol.All(vol.Coerce(float), vol.Range(min=-5.0, max=5.0))
        # target_offset_cool/heat are optional per-mode overrides that must
        # stay "unset" (None) unless the user explicitly fills them in - a
        # default= here would coerce a blank field to 0.0 and defeat the
        # fallback-to-target_offset resolution in climate.py. suggested_value
        # (not default=) pre-fills the displayed value without forcing one
        # when absent.
        per_mode_offset_fields = {
            vol.Optional(
                key,
                description={"suggested_value": self.config_entry.options.get(key)},  # type: ignore
            ): vol.Any(None, offset_range_validator)
            for key in (CONF_TARGET_OFFSET_COOL, CONF_TARGET_OFFSET_HEAT)
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST,
                        default=self.config_entry.options.get(CONF_HOST),  # type: ignore
                    ): str,
                    # Floor, not a free number: values below the minimum were
                    # the reason this option kept needing correcting in
                    # migrations. Raising it stays available for weak links.
                    vol.Required(
                        CONF_AVAILABILITY_RETRY_LIMIT,
                        default=self.config_entry.options.get(  # type: ignore
                            CONF_AVAILABILITY_RETRY_LIMIT, AVAILABILITY_FAILURE_LIMIT_MIN
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=AVAILABILITY_FAILURE_LIMIT_MIN)),
                    vol.Required(
                        CONF_FIRMWARE_UPDATE_CHECK,
                        default=self.config_entry.options.get(CONF_FIRMWARE_UPDATE_CHECK, False),  # type: ignore
                    ): bool,
                    vol.Required(
                        CONF_SERVICE_DATA,
                        default=self.config_entry.options.get(CONF_SERVICE_DATA, False),  # type: ignore
                    ): bool,
                    vol.Optional(
                        CONF_INDOOR_OFFSET,
                        default=self.config_entry.options.get(CONF_INDOOR_OFFSET, 0.0), # type: ignore
                    ): vol.All(vol.Coerce(float), vol.Range(min=-15.0, max=15.0)),
                    vol.Optional(
                        CONF_OUTDOOR_OFFSET,
                        default=self.config_entry.options.get(CONF_OUTDOOR_OFFSET, 0.0), # type: ignore
                    ): vol.All(vol.Coerce(float), vol.Range(min=-15.0, max=15.0)),
                    vol.Optional(
                        CONF_TARGET_OFFSET,
                        default=self.config_entry.options.get(CONF_TARGET_OFFSET, 0.0), # type: ignore
                    ): offset_range_validator,
                    **per_mode_offset_fields,
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

    def __init__(self, *args: object, **kwargs: dict[str, str]) -> None:
        super().__init__(*args)
        self._extra_info = kwargs

    def get_errors_and_placeholders(self, schema):
        """Return dicts of errors and description_placeholders, for adding to async_show_form"""
        key = self.applies_to_field
        # Errors will only be displayed to the user if the key is actually in the form (or
        # CONF_BASE for a general error), so we'll check the schema (seems weird there
        # isn't a more efficient way to do this...)
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


class InvalidName(KnownError):
    """Error to indicate there is an invalid hostname."""

    error_name = "name_invalid"
    applies_to_field = CONF_NAME


class TooManyDevicesRegistered(KnownError):
    """Error to indicate that there are too many devices registered"""

    error_name = "too_many_devices_registered"
    applies_to_field = CONF_BASE
