"""Guards against the #219 regression: a WfRacEntity subclass that doesn't
override _update_state(). WfRacEntity._handle_coordinator_update() calls
self._update_state() unconditionally on every coordinator update; a missing
override raises AttributeError, which the base class's except clause treats
as an update failure and reports it through Device.set_available(False) -
not just for the one entity missing it.
"""

from custom_components.mitsubishi_wf_rac import (
    binary_sensor,  # noqa: F401
    button,  # noqa: F401
    climate,  # noqa: F401
    number,  # noqa: F401
    select,  # noqa: F401
    sensor,  # noqa: F401
    update,  # noqa: F401
)
from custom_components.mitsubishi_wf_rac.entity import WfRacEntity


def test_every_entity_subclass_overrides_update_state():
    # Importing the platform modules above is what populates this - each of
    # their entity classes subclasses WfRacEntity directly.
    subclasses = WfRacEntity.__subclasses__()
    assert subclasses

    missing = [cls.__name__ for cls in subclasses if not hasattr(cls, "_update_state")]
    assert not missing, f"missing _update_state(): {missing}"
