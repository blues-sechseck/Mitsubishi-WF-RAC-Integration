"""Fixtures for the Mitsubishi WF-RAC integration."""

from collections.abc import Generator
from dataclasses import replace
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    load_json_object_fixture,
)
from pywfrac import (
    Aircon,
    AirconCommands,
    AirconStat,
    AirconStatus,
    FirmwareInfo,
    RacParser,
    Repository,
    WfRacAccountTableFullError,
    WfRacRegistrationError,
)

from custom_components.mitsubishi_wf_rac.const import DOMAIN
from custom_components.mitsubishi_wf_rac.coordinator import Device
from homeassistant.core import HomeAssistant

from . import AIRCO_ID, ENTRY_DATA

# --- Not part of core's suite -------------------------------------------------
# Core's tests drive pywfrac's async_* methods. This integration still calls the
# older ones (get_aircon_stats, send_airco_command, update_account_info,
# del_account_info), so the mock answers those by delegating to the async_*
# mock core's tests configure. Everything below exists only for that.

_TOKENS: dict[str, Aircon] = {}


def _token(aircon: Aircon) -> str:
    """Stand in for the base64 of a state, which the parser below hands back."""
    token = f"aircon-{len(_TOKENS)}"
    _TOKENS[token] = replace(aircon)
    return token


class _TokenParser(RacParser):
    """Return the state a test set up instead of decoding bytes for it."""

    def translate_bytes(self, raw: str, *args: Any, **kwargs: Any) -> Aircon:
        if (aircon := _TOKENS.get(raw)) is not None:
            return replace(aircon)
        return super().translate_bytes(raw, *args, **kwargs)


def _recording_build_command(repository: MagicMock):
    """Keep the parameters of the command being built, as core's mock sees them."""
    original = Device._build_command  # noqa: SLF001

    def build(self: Device, params: dict[AirconCommands, Any]) -> AirconStat:
        repository.pending_params = dict(params)
        return original(self, params)

    return build


def _wire_legacy_api(repository: MagicMock, aircon_stat: dict[str, Any]) -> None:
    repository.pending_params = {}
    table_full: list[Exception] = []

    async def get_aircon_stats(airco_id=None, raw=False):
        status = await repository.async_get_status(airco_id)
        return {
            **aircon_stat,
            "airconStat": _token(status.aircon),
            "expires": status.expires,
        }

    async def send_airco_command(airco_id, command, *, timestamp_offset=0):
        base = repository.async_get_status.return_value.aircon
        if set(repository.pending_params) == {
            AirconCommands.HomeLeaveModeStatusRequest
        }:
            # Setup reads Home Leave once so away heating can follow it; core
            # has no such frame, and counting it would shift every send count.
            return _token(base)
        try:
            aircon = await repository.async_send_command(
                airco_id, base, repository.pending_params
            )
        except WfRacAccountTableFullError as ex:
            # Core's library registers and retries inside the call; here the
            # integration does, so the same refusal comes back as result 2 of
            # the write and then of the registration.
            table_full.append(ex)
            raise WfRacRegistrationError("result 2") from ex
        return _token(aircon)

    async def update_account_info(airco_id, time_zone):
        if table_full:
            table_full.clear()
            return {"result": 2}
        try:
            await repository.async_register(airco_id, time_zone)
        except WfRacAccountTableFullError:
            return {"result": 2}
        return {"result": 0}

    async def del_account_info(airco_id):
        return {"result": 0 if await repository.async_unregister(airco_id) else 1}

    repository.get_aircon_stats.side_effect = get_aircon_stats
    repository.send_airco_command.side_effect = send_airco_command
    repository.update_account_info.side_effect = update_account_info
    repository.del_account_info.side_effect = del_account_info


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Override async_setup_entry."""
    with patch(
        "custom_components.mitsubishi_wf_rac.async_setup_entry",
        return_value=True,
    ) as mock_setup_entry:
        yield mock_setup_entry


@pytest.fixture
def aircon_stat() -> dict[str, Any]:
    """Return one getAirconStat answer, as the module sends it."""
    return load_json_object_fixture("aircon_stat.json")


@pytest.fixture
def aircon_fields() -> dict[str, Any]:
    """Fields to change on the captured state; override with parametrize."""
    return {}


@pytest.fixture
def aircon(aircon_stat: dict[str, Any], aircon_fields: dict[str, Any]) -> Aircon:
    """Return the state the unit reports: off, cooling, set to 22 degrees."""
    captured = RacParser().translate_bytes(aircon_stat["airconStat"])
    return replace(captured, **aircon_fields)


@pytest.fixture
def no_consolidation_window() -> Generator[None]:
    """Send commands right away, since the frozen clock never ends the window."""
    with patch(
        "custom_components.mitsubishi_wf_rac.coordinator.UPDATE_CONSOLIDATION_PERIOD",
        timedelta(0),
    ):
        yield


@pytest.fixture
def repository_class(
    aircon: Aircon, aircon_stat: dict[str, Any]
) -> Generator[MagicMock]:
    """Patch pywfrac's Repository where the integration builds one.

    The fake unit applies the commands it is sent, so later polls follow them.
    """
    status = AirconStatus(
        aircon=aircon,
        firmware=FirmwareInfo.from_contents(aircon_stat),
        expires=aircon_stat["expires"],
    )

    parser = RacParser()

    async def send_command(
        airco_id: str, base: Aircon, params: dict[AirconCommands, Any]
    ) -> Aircon:
        # A refused write makes the library re-encode from the unit's fresh state.
        if repository.fresh_state is not None:
            base = repository.fresh_state
            repository.fresh_state = None
        stat = AirconStat.from_aircon(base)
        for key, value in params.items():
            setattr(stat, key, value)
        status.aircon = parser.translate_bytes(parser.to_base64(stat))
        return status.aircon

    repository = create_autospec(Repository, instance=True)
    repository.get_airco_id.return_value = AIRCO_ID
    repository.async_unregister.return_value = True
    repository.async_get_status.return_value = status
    repository.async_send_command.side_effect = send_command
    repository.method = "https"
    # Set a state here to make the next command start from it, not from base.
    repository.fresh_state = None

    _wire_legacy_api(repository, aircon_stat)

    cls = MagicMock(return_value=repository)
    with (
        patch("custom_components.mitsubishi_wf_rac.coordinator.Repository", cls),
        patch("custom_components.mitsubishi_wf_rac.config_flow.Repository", cls),
        patch(
            "custom_components.mitsubishi_wf_rac.coordinator.RacParser", _TokenParser
        ),
        patch.object(Device, "_build_command", _recording_build_command(repository)),
    ):
        yield cls


@pytest.fixture
def mock_repository(repository_class: MagicMock) -> MagicMock:
    """Return the library client every part of the integration shares."""
    return repository_class.return_value


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry at the current version."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Living room",
        data=ENTRY_DATA,
        unique_id=AIRCO_ID,
        version=8,
        minor_version=2,
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    mock_repository: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> MockConfigEntry:
    """Set up the integration with a reachable airco."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
