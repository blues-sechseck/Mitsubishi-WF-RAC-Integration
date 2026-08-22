"""Tests for wfrac/repository.py: how _post() classifies failures and what
that classification does to the discovered communication method. The aiohttp
session is replaced with a fake - no real network involved. Needs the `hass`
fixture (Repository takes the HA client session from it), hence
tests/integration/ rather than tests/unit/.
"""

import asyncio
import json
import ssl
from unittest.mock import patch

import pytest
from aiohttp import ClientConnectionError

from custom_components.mitsubishi_wf_rac.const import MIN_TIME_BETWEEN_UPDATES
from custom_components.mitsubishi_wf_rac.coordinator import POLL_TIMEOUT
from custom_components.mitsubishi_wf_rac.wfrac.repository import (
    MIN_TIME_BETWEEN_REQUESTS,
    REQUEST_TIMEOUT,
    AirconCommandError,
    AirconConnectionError,
    AirconRegistrationError,
    AirconWriteRefusedError,
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
    cause = ClientConnectionError("connection refused")
    repo, _ = repository([cause])
    with pytest.raises(AirconConnectionError) as error:
        await repo.get_aircon_stats("airco-id")
    assert error.value.__cause__ is cause


async def test_timeout_raises_connection_error(repository):
    cause = asyncio.TimeoutError()
    repo, _ = repository([cause])
    with pytest.raises(AirconConnectionError) as error:
        await repo.get_aircon_stats("airco-id")
    assert error.value.__cause__ is cause


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


async def test_rediscovery_tries_the_last_working_method_first(repository):
    """Recovery must not put an HTTPS unit behind an HTTP-first timeout."""
    repo, session = repository(
        [ClientConnectionError("boom"), _FakeResponse(200, _OK_BODY)],
        method="https",
    )
    repo._ssl_context = ssl.create_default_context()

    with pytest.raises(AirconConnectionError):
        await repo.get_aircon_stats("airco-id")
    assert repo.method is None

    await repo.get_aircon_stats("airco-id")
    assert repo.method == "https"
    assert session.urls == [
        "https://127.0.0.1:51443/beaver/command/getAirconStat",
        "https://127.0.0.1:51443/beaver/command/getAirconStat",
    ]


@pytest.mark.parametrize(
    ("old_method", "new_method"), (("http", "https"), ("https", "http"))
)
async def test_rediscovery_recovers_after_a_protocol_change(
    repository, old_method, new_method
):
    """The alternative remains reachable if a firmware line changes protocol."""
    repo, session = repository(
        [
            ClientConnectionError("unit offline"),
            ClientConnectionError("old protocol refused"),
            _FakeResponse(200, _OK_BODY),
        ],
        method=old_method,
    )
    repo._ssl_context = ssl.create_default_context()

    with pytest.raises(AirconConnectionError):
        await repo.get_aircon_stats("airco-id")

    await repo.get_aircon_stats("airco-id")

    assert repo.method == new_method
    assert session.urls == [
        f"{old_method}://127.0.0.1:51443/beaver/command/getAirconStat",
        f"{old_method}://127.0.0.1:51443/beaver/command/getAirconStat",
        f"{new_method}://127.0.0.1:51443/beaver/command/getAirconStat",
    ]


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


async def test_refusal_in_the_result_field_is_reported_once(repository, caplog):
    """HTTP 200 with a non-zero result is a request the unit accepted and did
    not carry out - invisible until now (#212). Reported, but not acted on:
    which firmware reports what on success is not established.
    """
    caplog.set_level("DEBUG", logger="custom_components.mitsubishi_wf_rac.wfrac.repository")
    refused = json.dumps({"result": 12, "contents": {"airconStat": "AAA="}})
    repo, _ = repository(
        [
            _FakeResponse(200, refused),
            _FakeResponse(200, refused),
            _FakeResponse(200, _OK_BODY),
            _FakeResponse(200, refused),
        ]
    )

    # The caller still gets the response: nothing about the control flow moves.
    assert await repo.get_aircon_stats("airco-id") == {"airconStat": "AAA="}
    await repo.get_aircon_stats("airco-id")

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "result 12 (refused - another client holds the write lock" in warnings[0].message

    # A success clears it, so a later refusal is worth saying again.
    await repo.get_aircon_stats("airco-id")
    await repo.get_aircon_stats("airco-id")

    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 2


async def test_send_airco_command_raises_on_registration_result_code(repository):
    """Unlike getAirconStat (asserted above), setAirconStat refusing with
    result 2 must be visible to the caller rather than swallowed -
    Device.set_airco() relies on this to re-register and retry instead of
    losing the command (#294).
    """
    refused = json.dumps({"result": 2, "contents": {"airconStat": "AAA="}})
    repo, _ = repository([_FakeResponse(200, refused)])

    with pytest.raises(AirconRegistrationError):
        await repo.send_airco_command("airco-id", "cmd")


@pytest.mark.parametrize("code", [1, 11, 12])
async def test_send_airco_command_raises_on_write_refusal_codes(repository, code):
    """1/11/12 mean the unit declined to carry the write out - usually
    another client's 60s write lock (#294). They must reach the caller as
    their own error type: waiting and retrying helps, re-registering does
    not.
    """
    refused = json.dumps({"result": code, "contents": {"airconStat": "AAA="}})
    repo, _ = repository([_FakeResponse(200, refused)])

    with pytest.raises(AirconWriteRefusedError):
        await repo.send_airco_command("airco-id", "cmd")

    # ...and not as the registration error, which would send set_airco() down
    # the pointless re-register path.
    assert not issubclass(AirconWriteRefusedError, AirconRegistrationError)


async def test_send_airco_command_does_not_raise_on_unrelated_result_code(repository):
    """Result 10 is an internal error in the unit, not something either
    recovery path can act on - just the existing logged-but-not-acted-on
    refusal.
    """
    refused = json.dumps({"result": 10, "contents": {"airconStat": "AAA="}})
    repo, _ = repository([_FakeResponse(200, refused)])

    assert await repo.send_airco_command("airco-id", "cmd") == "AAA="


async def test_unknown_result_code_is_still_reported(repository, caplog):
    repo, _ = repository([_FakeResponse(200, json.dumps({"result": 77, "contents": {}}))])

    await repo.get_aircon_stats("airco-id")

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "result 77 (meaning unknown)" in warnings[0].message
