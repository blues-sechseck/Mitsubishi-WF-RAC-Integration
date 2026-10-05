"""The blueprints shipped in blueprints/ are checked the way HA loads them.

They are not part of the integration download - HACS installs
custom_components/ and nothing else - but a broken one costs whoever imports it
an error with no obvious owner, and the person who contributed it cannot see
this repo's CI.
"""

from datetime import timedelta
from pathlib import Path

from freezegun.api import FrozenDateTimeFactory
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
# By name, and not by prefix: a second blueprint in this directory shares the
# prefix and sorts ahead of this one.
LOCKOUT = next(p for p in BLUEPRINTS if p.name == "mhi-multi-split-mode-lockout.yaml")
AUTO_REPLACEMENT = next(
    p for p in BLUEPRINTS if p.name == "mhi-multi-split-auto-replacement.yaml"
)

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


def _render_variables(hass: HomeAssistant, path: Path, seed: dict) -> dict:
    """Evaluate the blueprint's variables block in order, the way an automation
    run does, so a template can be exercised with the ones it depends on
    already filled in.
    """
    variables = dict(seed)
    for name, template in _load(path).data["variables"].items():
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
        LOCKOUT,
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


async def test_the_lockout_blueprint_survives_a_head_with_no_sensors(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """A head listed as a climate entity but with no Cool/Heat Status or
    Compressor Demand sensor on its device.

    Until 1.0.1 the lookup ended in `| first`, which gives Undefined for an
    empty list, and Undefined is not none - so the template raised, the run was
    abandoned and nothing reached the logbook. The header promises that such a
    head fails safe and silent: no vote, not calling.
    """
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    climates, judges, demands = [], [], []
    for room, with_sensors in (("bedroom", True), ("living_room", False)):
        device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, room)}
        )
        for domain, suffix, state in (
            ("climate", "thermostat", "auto"),
            ("sensor", "cool_hot_judge", "cooling"),
            ("binary_sensor", "compressor_demand", "off"),
        ):
            if domain != "climate" and not with_sensors:
                continue
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
        LOCKOUT,
        {
            "climate_entities": climates,
            "judge_sensors": judges,
            "demand_sensors": demands,
            "release_grace_minutes": 10,
            "cooldown_minutes": 20,
            "relax_delay": "00:04:00",
        },
    )

    rows = {row["entity"].split(".")[1]: row for row in variables["fleet"]}
    assert rows["living_room_thermostat"]["vote"] == "none"
    assert rows["living_room_thermostat"]["calling"] is False
    assert variables["conflict"] is False


# The AUTO replacement. Each unit is one device: a climate entity carrying the
# mode, the room temperature and the setpoint, and a Compressor Demand sensor.


def _add_unit(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    entry: MockConfigEntry,
    room: str,
    mode: str,
    temp: float,
    target: float,
    demand: str | None = "off",
) -> tuple[str, str | None]:
    """Register one unit and set its states. `demand=None` leaves the device
    without a Compressor Demand sensor.
    """
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, room)}
    )
    climate = entity_registry.async_get_or_create(
        "climate",
        DOMAIN,
        f"{room}-thermostat",
        device_id=device.id,
        suggested_object_id=f"{room}_thermostat",
    ).entity_id
    hass.states.async_set(
        climate, mode, {"current_temperature": temp, "temperature": target}
    )
    if demand is None:
        return climate, None
    sensor = entity_registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{room}-compressor_demand",
        device_id=device.id,
        suggested_object_id=f"{room}_compressor_demand",
    ).entity_id
    hass.states.async_set(sensor, demand)
    return climate, sensor


def _auto_replacement_seed(climates: list, demands: list, **inputs) -> dict:
    """Every input of the AUTO replacement, at its default unless given.
    Lockout protection is on, because every test here needs it.
    """
    return {
        "climate_entities": climates,
        "cool_limit": 2,
        "heat_limit": 2,
        "lockout_protection": True,
        "demand_sensors": [d for d in demands if d is not None],
        "release_grace_minutes": 10,
        "cooldown_minutes": 20,
        "relax_delay": {"hours": 0, "minutes": 0, "seconds": 10},
        "recover_fan": False,
        "never_recover": [],
        "follow_heat": False,
        "debug_logbook": False,
        "dwell_minutes": 20,
        "settle_minutes": 5,
    } | inputs


async def test_the_auto_replacement_survives_a_unit_with_no_demand_sensor(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The same case as the resolver test above, for the AUTO replacement: its
    lookup already avoids `| first`. The whole variables block is rendered, down
    to `decision`, so an error anywhere in it fails the test.
    """
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    office = _add_unit(
        hass, device_registry, entity_registry, entry, "office", "cool", 23, 22
    )
    lounge = _add_unit(
        hass,
        device_registry,
        entity_registry,
        entry,
        "lounge",
        "heat",
        20,
        21,
        demand=None,
    )

    variables = _render_variables(
        hass,
        AUTO_REPLACEMENT,
        _auto_replacement_seed([office[0], lounge[0]], [office[1], lounge[1]]),
    )

    rows = {row["entity"].split(".")[1]: row for row in variables["fleet"]}
    assert rows["lounge_thermostat"]["calling"] is False
    assert rows["lounge_thermostat"]["since"] == 99999
    assert isinstance(variables["decision"], str)


@pytest.mark.parametrize(
    ("lounge_temp", "expected"),
    [
        # 0.2 °C under its setpoint: not at its band edge. [HW, 0.10.6]
        (20.8, "IDLE units in both modes, but no room on the heat side"),
        # Exactly at setpoint - Cooling Limit: at the edge counts.
        (19.0, "ACT lockout: stand down office_thermostat so heat can run"),
    ],
)
async def test_a_waiting_room_takes_the_outdoor_unit_only_at_its_band_edge(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    lounge_temp: float,
    expected: str,
) -> None:
    """The office has just finished cooling, the lounge waits in heating, and
    nothing is calling. Until 0.10.5 a lounge 0.2 °C under its setpoint stood
    the office down; since 0.10.6 the lounge must reach setpoint - Cooling
    Limit, the same edge as the rules.
    """
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    office = _add_unit(
        hass, device_registry, entity_registry, entry, "office", "cool", 21, 22
    )
    lounge = _add_unit(
        hass,
        device_registry,
        entity_registry,
        entry,
        "lounge",
        "heat",
        lounge_temp,
        21,
    )
    # Both units have held their modes for longer than the dwell and the
    # cooldown. The office then ran, and stopped longer ago than the release
    # grace, so it is the side that operated last and nothing is calling.
    freezer.tick(timedelta(minutes=30))
    hass.states.async_set(office[1], "on")
    freezer.tick(timedelta(minutes=5))
    hass.states.async_set(office[1], "off")
    freezer.tick(timedelta(minutes=15))

    variables = _render_variables(
        hass,
        AUTO_REPLACEMENT,
        _auto_replacement_seed([office[0], lounge[0]], [office[1], lounge[1]]),
    )

    assert variables["last_served"] == "cool"
    assert variables["in_use"] == []
    assert " ".join(variables["decision"].split()).startswith(expected)


@pytest.mark.parametrize(
    ("lounge_demand", "direction"),
    [
        # The lounge is heating: the case Cooling priority is for.
        ("on", "heat"),
        # Nothing is calling, but the idle outdoor unit can still hold heating
        # for a master unit the blueprint cannot see. `direction != 'cool'`
        # stands the heating side down here too, on purpose - see the comment
        # above `priority_ready`.
        ("off", "none"),
    ],
)
async def test_cooling_priority_stands_the_heating_side_down(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    lounge_demand: str,
    direction: str,
) -> None:
    """The office is changed from heating to cooling, and that change is the
    trigger. Cooling takes the outdoor unit at once: no settle time, no
    cooldown, and a calling heating unit does not keep it.
    """
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    office = _add_unit(
        hass, device_registry, entity_registry, entry, "office", "heat", 24, 22
    )
    lounge = _add_unit(
        hass, device_registry, entity_registry, entry, "lounge", "heat", 20, 21
    )
    freezer.tick(timedelta(minutes=30))
    hass.states.async_set(lounge[1], lounge_demand)

    from_state = hass.states.get(office[0])
    hass.states.async_set(
        office[0], "cool", {"current_temperature": 24, "temperature": 22}
    )
    trigger = {
        "id": "mode_changed",
        "entity_id": office[0],
        "from_state": from_state,
        "to_state": hass.states.get(office[0]),
    }

    variables = _render_variables(
        hass,
        AUTO_REPLACEMENT,
        _auto_replacement_seed(
            [office[0], lounge[0]],
            [office[1], lounge[1]],
            follow_heat=True,
            trigger=trigger,
        ),
    )

    assert variables["direction"] == direction
    assert variables["priority_ready"] is True
    assert " ".join(variables["decision"].split()) == (
        "ACT cooling priority: office_thermostat changed to cooling, so "
        "lounge_thermostat stands down now"
    )
