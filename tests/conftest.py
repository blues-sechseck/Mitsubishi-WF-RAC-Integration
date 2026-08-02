"""Global fixtures for mitsubishi_wf_rac tests."""

from pathlib import Path

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture
def hass_config_dir() -> str:
    """Point HA's config dir at the repo root, so its custom_components/
    (containing mitsubishi_wf_rac) is what gets discovered - the plugin's
    own default points at its own (empty, for us) testing_config dir.
    """
    return str(Path(__file__).parent.parent)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations in all tests."""
    yield
