"""The blueprints shipped in blueprints/ are checked the way HA loads them.

They are not part of the integration download - HACS installs
custom_components/ and nothing else - but a broken one costs whoever imports it
an error with no obvious owner, and the person who contributed it cannot see
this repo's CI.
"""

from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mitsubishi_wf_rac.const import DOMAIN
from homeassistant.components.automation.config import (
    AUTOMATION_BLUEPRINT_SCHEMA,
    PLATFORM_SCHEMA,
)
from homeassistant.components.blueprint import models
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.template import Template
from homeassistant.util.yaml import loader

BLUEPRINTS = sorted(
    (Path(__file__).parent.parent.parent / "blueprints" / "automation").rglob("*.yaml")
)
LOCKOUT = next(p for p in BLUEPRINTS if p.name.startswith("mhi-multi-split"))

HEADS = {
    "bedroom": ("cooling", "off"),
    "living_room": ("heating", "on"),
}


def _load(path: Path) -> models.Blueprint:
    return models.Blueprint(
        loader.load_yaml(str(path)),
        expected_domain="automation",
        path=str(path),
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.name)
async def test_blueprint_loads_and_its_automation_validates(path: Path) -> None:
    """Both halves: the blueprint block, and the automation it produces once
    the inputs are filled in. The second is what actually runs, and a
    substitution can be valid YAML and still not be an automation.
    """
    blueprint = _load(path)
    inputs = models.BlueprintInputs(
        blueprint,
        {
            "use_blueprint": {
                "path": path.name,
                "input": {name: [] for name in blueprint.inputs},
            }
        },
    )

    PLATFORM_SCHEMA(inputs.async_substitute())


async def test_the_lockout_blueprint_pairs_each_head_by_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The three entity lists are matched up by device, not by position.

    That is the one part of this blueprint that is not the contributor's own
    tested automation: his listed climate/judge/demand together per head, and
    turning that into three flat selectors means the pairing has to be derived.
    Feeding the lists in different orders is the case that would go unnoticed -
    it would still run, just with one head's vote read off another head's
    sensor.
    """
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    climates, judges, demands = [], [], []
    for room, (judge_state, demand_state) in HEADS.items():
        device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, room)}
        )
        for domain, suffix, state in (
            ("climate", "thermostat", "auto"),
            ("sensor", "cool_hot_judge", judge_state),
            ("binary_sensor", "compressor_demand", demand_state),
        ):
            registered = entity_registry.async_get_or_create(
                domain,
                DOMAIN,
                f"{room}-{suffix}",
                device_id=device.id,
                suggested_object_id=f"{room}_{suffix}",
            )
            hass.states.async_set(registered.entity_id, state)
            {"climate": climates, "sensor": judges, "binary_sensor": demands}[
                domain
            ].append(registered.entity_id)

    # Reversed on purpose: same heads, different order.
    variables = {
        "climate_entities": climates,
        "judge_sensors": list(reversed(judges)),
        "demand_sensors": list(reversed(demands)),
        "release_grace_minutes": 10,
    }
    fleet_template = _load(LOCKOUT).data["variables"]["fleet"]
    fleet = Template(fleet_template, hass).async_render(variables)

    votes = {row["entity"].split(".")[1]: row["vote"] for row in fleet}
    assert votes == {
        "bedroom_thermostat": "cooling",
        "living_room_thermostat": "heating",
    }
    # Both count as holding the system: the living room's demand is on, and the
    # bedroom's went off just now, which is inside the release grace. That is
    # the property the blueprint's restart behaviour rests on - everything
    # reads as calling until the grace has passed, so nothing rotates on a
    # half-populated state machine.
    assert all(row["calling"] for row in fleet)


def _render_variables(hass: HomeAssistant, seed: dict) -> dict:
    """Evaluate the blueprint's variables block in order, the way an automation
    run does, so a template can be exercised with the ones it depends on
    already filled in.
    """
    variables = dict(seed)
    for name, template in _load(LOCKOUT).data["variables"].items():
        if name in variables:
            continue
        variables[name] = Template(template, hass).async_render(variables)
    return variables


async def test_a_manual_run_with_nothing_to_resolve_says_so(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Triggering the automation by hand skips its conditions, so the guard
    branch is the only thing between a manual run and a stand-down with nothing
    to stand down. Both heads vote the same way here: no lockout exists.
    """
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    climates, judges, demands = [], [], []
    for room in HEADS:
        device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, room)}
        )
        for domain, suffix, state in (
            ("climate", "thermostat", "auto"),
            ("sensor", "cool_hot_judge", "cooling"),
            ("binary_sensor", "compressor_demand", "off"),
        ):
            registered = entity_registry.async_get_or_create(
                domain,
                DOMAIN,
                f"{room}-{suffix}",
                device_id=device.id,
                suggested_object_id=f"{room}_{suffix}",
            )
            hass.states.async_set(registered.entity_id, state)
            {"climate": climates, "sensor": judges, "binary_sensor": demands}[
                domain
            ].append(registered.entity_id)

    variables = _render_variables(
        hass,
        {
            "climate_entities": climates,
            "judge_sensors": judges,
            "demand_sensors": demands,
            "release_grace_minutes": 0,
            "cooldown_minutes": 30,
            "relax_delay": "00:04:00",
        },
    )
    assert variables["conflict"] is False

    guard = _load(LOCKOUT).data["actions"][0]["choose"][0]
    condition = guard["conditions"][0]["value_template"]
    assert Template(condition, hass).async_render(variables) is True

    message = guard["sequence"][0]["data"]["message"]
    assert "do not disagree" in Template(message, hass).async_render(variables)
