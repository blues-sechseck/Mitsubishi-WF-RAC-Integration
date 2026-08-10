"""Tests for wfrac/repository.py: how _post() classifies failures and what
that classification does to the discovered communication method. The aiohttp
session is replaced with a fake - no real network involved. Needs the `hass`
fixture (Repository takes the HA client session from it), hence
tests/integration/ rather than tests/unit/.
"""

import json
from unittest.mock import patch

import pytest
from aiohttp import ClientConnectionError

from custom_components.mitsubishi_wf_rac.const import MIN_TIME_BETWEEN_UPDATES
from custom_components.mitsubishi_wf_rac.wfrac.device import POLL_TIMEOUT
from custom_components.mitsubishi_wf_rac.wfrac.repository import (
    MIN_TIME_BETWEEN_REQUESTS,
    REQUEST_TIMEOUT,
    AirconCommandError,
    AirconConnectionError,
    Repository,
)

_OK_BODY = json.dumps({"result": 0, "contents": {"airconId": "airco-id"}})


class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.content_type = "application/json"
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


class _FakeSession:
    """Answers every post with the next queued outcome, recording the URLs so
    a test can tell which protocol was attempted.
    """

    def __init__(self, outcomes) -> None:
        self._outcomes = list(outcomes)
        self.urls: list[str] = []

    def post(self, url: str, **_kwargs):
        self.urls.append(url)
        outcome = self._outcomes.pop(0) if self._outcomes else _OK_BODY
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def repository(hass):
    def _build(outcomes, method="http"):
        with patch(
            "custom_components.mitsubishi_wf_rac.wfrac.repository."
            "async_get_clientsession"
        ):
            repo = Repository(
                hass, "127.0.0.1", 51443, "operator-id", "device-id", method=method
            )
        session = _FakeSession(outcomes)
        repo._session = session
        return repo, session

    return _build


async def test_http_error_status_raises_command_error(repository):
    repo, _ = repository([_FakeResponse(501, "Not supported this command")])
    with pytest.raises(AirconCommandError):
        await repo.get_aircon_stats("airco-id")


async def test_connection_failure_raises_connection_error(repository):
    repo, _ = repository([ClientConnectionError("boom")])
    with pytest.raises(AirconConnectionError):
        await repo.get_aircon_stats("airco-id")


async def test_refused_command_keeps_the_discovered_method(repository):
    """A 501 means the unit answered - the stored method is still correct, so
    the next request must not pay for a rediscovery.
    """
    repo, session = repository(
        [_FakeResponse(501, "Not supported this command"), _FakeResponse(200, _OK_BODY)]
    )

    with pytest.raises(AirconCommandError):
        await repo.get_aircon_stats("airco-id")
    assert repo.method == "http"

    await repo.get_aircon_stats("airco-id")
    assert session.urls == [
        "http://127.0.0.1:51443/beaver/command/getAirconStat",
        "http://127.0.0.1:51443/beaver/command/getAirconStat",
    ]


async def test_unreachable_unit_resets_the_discovered_method(repository):
    """A dead transport says nothing about which protocol is right, so the
    stored one is dropped and the next request rediscovers.
    """
    repo, _ = repository([ClientConnectionError("boom")])

    with pytest.raises(AirconConnectionError):
        await repo.get_aircon_stats("airco-id")
    assert repo.method is None


async def test_discovery_falls_back_to_https_on_a_command_error(repository):
    """An HTTPS-only module can answer a plaintext request with a status code
    rather than dropping the connection; discovery still has to try HTTPS.
    """
    repo, session = repository(
        [_FakeResponse(400, "bad request"), _FakeResponse(200, _OK_BODY)], method=None
    )

    await repo.get_aircon_stats("airco-id")

    assert repo.method == "https"
    assert session.urls == [
        "http://127.0.0.1:51443/beaver/command/getAirconStat",
        "https://127.0.0.1:51443/beaver/command/getAirconStat",
    ]


# --- the two timeouts have to relate to each other -----------------------


def test_a_poll_has_room_for_both_discovery_legs():
    """Discovery tries one protocol and then the other inside a single poll.

    When the per-request and per-poll timeouts were equal, a unit that accepts
    a connection without answering it consumed the whole window on the first
    leg, so the second protocol was never reached - and a unit that only
    speaks the second one could never recover (#236).
    """
    assert POLL_TIMEOUT >= 2 * REQUEST_TIMEOUT + MIN_TIME_BETWEEN_REQUESTS


def test_a_poll_cannot_outlive_its_own_interval():
    """Otherwise a slow poll is still running when the next one is due."""
    assert POLL_TIMEOUT < MIN_TIME_BETWEEN_UPDATES
