"""Tests for firmware_check.py: the manufacturer's getFirmware endpoint.

The endpoint is unauthenticated and outside our control, so every shape it
can answer with has to end somewhere defined - the update entity treats
None as "nothing known" and says so rather than claiming to be up to date.
"""

import aiohttp
import pytest

from custom_components.mitsubishi_wf_rac.firmware_check import (
    _FIRMWARE_API_URL,
    fetch_latest_firmware,
)
from homeassistant.core import HomeAssistant


async def test_a_good_answer_gives_both_versions(hass: HomeAssistant, aioclient_mock):
    """Both branches ship their own version, and the entity compares each."""
    aioclient_mock.post(
        _FIRMWARE_API_URL,
        json={"result": 0, "contents": {"mFirmVer": "025", "cFirmVer": "200"}},
    )

    assert await fetch_latest_firmware(hass, "WF-RAC-HTTPS") == {
        "wireless": "025",
        "mcu": "200",
    }


async def test_the_declared_content_type_is_ignored(
    hass: HomeAssistant, aioclient_mock
):
    """Deployments of this endpoint mislabel it, the local API does too."""
    aioclient_mock.post(
        _FIRMWARE_API_URL,
        text='{"result": 0, "contents": {"mFirmVer": "025", "cFirmVer": "200"}}',
        headers={"Content-Type": "text/html"},
    )

    assert await fetch_latest_firmware(hass, "WF-RAC-HTTPS") is not None


@pytest.mark.parametrize(
    "response",
    [
        pytest.param({"status": 500}, id="server_error"),
        pytest.param({"json": []}, id="not_an_object"),
        pytest.param({"json": {"result": 1}}, id="result_says_no"),
        pytest.param({"json": {"result": 0}}, id="no_contents"),
        pytest.param(
            {"json": {"result": 0, "contents": "none"}}, id="contents_not_an_object"
        ),
    ],
)
async def test_an_answer_we_cannot_use_reports_nothing(
    hass: HomeAssistant, aioclient_mock, response: dict
):
    """None means "unknown", which is the only honest answer here."""
    aioclient_mock.post(_FIRMWARE_API_URL, **response)

    assert await fetch_latest_firmware(hass, "WF-RAC-HTTPS") is None


async def test_an_unreachable_endpoint_reports_nothing(
    hass: HomeAssistant, aioclient_mock
):
    """The check is optional and off by default - it must never raise into
    the coordinator's poll."""
    aioclient_mock.post(_FIRMWARE_API_URL, exc=aiohttp.ClientError("no route"))

    assert await fetch_latest_firmware(hass, "WF-RAC-HTTPS") is None
