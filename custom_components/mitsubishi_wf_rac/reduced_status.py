"""What a unit with a reduced status block does not get.

Some units fill only the model, power/mode, fan and setpoint bytes of the
state block and leave the rest at zero (#431). Everything this integration
builds on those bytes - the operation-data channel, the injected room
temperature, the compressor flag - has nothing to work with there. This module
holds the two pieces that are not entity or coordinator behaviour: the repair
for a configured temperature source, and the registry cleanup that keeps
entities from an earlier run from lingering as permanently unknown.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from .const import DOMAIN, SERVICE_DATA_SENSOR_TYPES

_LOGGER = logging.getLogger(__name__)


def external_temperature_unsupported_issue_id(entry_id: str) -> str:
    """Repair-issue id for a temperature source on a unit that ignores it."""
    return f"external_temperature_unsupported_{entry_id}"


def report_external_temperature_unsupported(
    hass: HomeAssistant, entry_id: str, device_name: str
) -> None:
    """Raise the repair for a source the unit cannot use.

    Persistent and not fixable: the way out is removing the source in the
    options, which no confirm button can do for the user.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        external_temperature_unsupported_issue_id(entry_id),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="external_temperature_unsupported",
        translation_placeholders={"device_name": device_name},
    )


def unsupported_entity_unique_ids(airco_id_lower: str) -> dict[str, list[str]]:
    """The unique ids of the entities a reduced unit does not get, per domain.

    Built the way each entity class builds its own; the tests hold the two to
    each other.
    """
    prefix = f"{DOMAIN}-{airco_id_lower}"
    home_leave = [
        f"{prefix}-home-leave-{mode}-{slug}"
        for mode in ("cooling", "heating")
        for slug in ("temp_rule", "temp_setting")
    ]
    return {
        "sensor": [
            *(f"{prefix}-{kind}-sensor" for kind in SERVICE_DATA_SENSOR_TYPES),
            f"{prefix}-research-data",
        ],
        "switch": [f"{prefix}-silent-operation"],
        "binary_sensor": [
            f"{prefix}-compressor",
            f"{prefix}-external-temperature-active",
            f"{prefix}-defrost",
        ],
        "number": [f"{unique_id}-number" for unique_id in home_leave],
        "select": [
            f"{prefix}-home-leave-{mode}-air-flow-select"
            for mode in ("cooling", "heating")
        ],
    }


def async_remove_unsupported_entities(
    hass: HomeAssistant, entry_id: str, airco_id_lower: str
) -> None:
    """Drop the registry entries of entities a reduced unit does not get.

    Kept in one place, ahead of the platforms: they simply do not create these
    entities on such a unit, so nothing there would remove what an earlier run
    registered. Scoped to the entry, since a unique id alone does not say whose
    entity it is when two entries share an airco.
    """
    registry = er.async_get(hass)
    for domain, unique_ids in unsupported_entity_unique_ids(airco_id_lower).items():
        for unique_id in unique_ids:
            entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
            if entity_id is None:
                continue
            entity = registry.async_get(entity_id)
            if entity is None or entity.config_entry_id != entry_id:
                continue
            _LOGGER.debug("Removing %s: the unit reports no data for it", entity_id)
            registry.async_remove(entity_id)
