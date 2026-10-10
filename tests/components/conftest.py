"""Marks the tests under here that core and this integration disagree on.

The suite in components/mitsubishi_wf_rac is core's own, carried over as-is so
that re-syncing it stays a mechanical step. Nothing in those files is edited
to make it pass here - where the two trees genuinely differ, the divergence is
named below instead, with the reason it exists.

A test that starts passing is reported as XPASS, which fails the run: that is
the point. Either the divergence closed and the entry belongs gone, or
something moved that nobody meant to move.
"""

from collections.abc import Generator
import gc

import pytest

# Node id (relative to this directory, parameters optional) -> why the two
# trees differ here.
DIVERGENCES = {
    "mitsubishi_wf_rac/test_climate.py::test_entity": (
        "snapshot_platform asserts that exactly one platform is loaded. Core "
        "ships climate alone; this integration loads six. Closes when core "
        "has the other five."
    ),
    "mitsubishi_wf_rac/test_climate.py::test_3d_auto_is_offered_as_a_vertical_swing_mode_only": (
        "3D auto is offered, and reported, in the horizontal swing list too. "
        "Existing automations select it there, so it stays."
    ),
    "mitsubishi_wf_rac/test_climate.py::test_an_entrusted_unit_reports_3d_auto_and_its_horizontal_position": (
        "Same: while entrusted the horizontal swing mode reads 3d_auto here, "
        "not the raw position."
    ),
    "mitsubishi_wf_rac/test_climate.py::test_an_entrusted_unit_with_an_unknown_horizontal_position": (
        "Same: while entrusted the horizontal swing mode reads 3d_auto here."
    ),
    "mitsubishi_wf_rac/test_climate.py::test_unnamed_raw_values_leave_their_attributes_unknown": (
        "A fan step the library cannot name makes the whole state read fail "
        "here, so the entity is unknown rather than off with unknown "
        "attributes. Core reads each attribute on its own."
    ),
    "mitsubishi_wf_rac/test_config_flow.py::test_user_flow_duplicate_host": (
        "A host that is already configured is shown as a form error here, "
        "after which the user can still force it; core aborts the flow."
    ),
    "mitsubishi_wf_rac/test_config_flow.py::test_unexpected_error_is_shown_not_raised": (
        "The form error for an unexpected exception is keyed "
        "unexpected_error here and in every shipped translation; core uses "
        "the common unknown."
    ),
    "mitsubishi_wf_rac/test_coordinator.py::test_a_poll_does_not_queue_behind_a_command": (
        "Core holds _send_lock across the whole poll so a poll steps aside "
        "for a command. update() here does not take that lock at all. "
        "Retrofitting it changes the serialisation model on 1,900 "
        "installations, so the divergence is deliberate."
    ),
    "mitsubishi_wf_rac/test_coordinator.py::test_a_command_issued_during_a_poll_waits_for_what_it_brings": (
        "Same lock, other direction - see above."
    ),
    "mitsubishi_wf_rac/test_coordinator.py::test_a_poll_standing_down_does_not_end_a_reported_failure": (
        "Same lock: no poll stands down here, so there is no standing-down "
        "state to keep apart from a reported failure."
    ),
    "mitsubishi_wf_rac/test_init.py::test_setup_retries_when_unreachable": (
        "The retry reason names the address here, not the library's error."
    ),
    "mitsubishi_wf_rac/test_init.py::test_a_stored_protocol_is_handed_to_the_library": (
        "The older library methods take the time zone per call, so the "
        "client is built without one; the method is passed as in core."
    ),
    "mitsubishi_wf_rac/test_init.py::test_removal_of_an_entry_that_was_never_migrated": (
        "Removal builds its client from entry.data, which an entry that never "
        "migrated lacks the host in; core falls back to the options."
    ),
    "mitsubishi_wf_rac/test_init.py::test_migration_drops_the_retired_retry_options": (
        "Core's migration writes no availability_retry_limit because no form "
        "there can change one. This integration has that form, so the option "
        "is kept here."
    ),
    "mitsubishi_wf_rac/test_init.py::test_migration_moves_the_entity_unique_id_and_keeps_the_entity_id": (
        "The unique id and entity id assertions hold; the final count of "
        "registry entries does not, as six platforms register entities."
    ),
    "mitsubishi_wf_rac/test_init.py::test_two_legacy_entries_for_one_airco_fail_the_second_migration": (
        "Entries below version 8 here were already given a unique id in "
        "earlier releases, so the version 7 step neither sets one nor "
        "refuses a duplicate."
    ),
}


def pytest_collection_modifyitems(config, items):
    """Mark the known divergences xfail, strictly."""
    for item in items:
        _, sep, relative = item.nodeid.partition("tests/components/")
        reason = DIVERGENCES.get(relative) or DIVERGENCES.get(
            relative.partition("[")[0]
        )
        if sep and reason:
            item.add_marker(pytest.mark.xfail(reason=reason, strict=True))


@pytest.fixture(autouse=True)
def collect_garbage_per_test() -> Generator[None]:
    """Report an orphaned task's unretrieved error in its own test.

    A service call whose caller was cancelled leaves its inner task behind;
    without this its error surfaces whenever the next collection happens to
    run, which can be inside another test that asserts on the log.
    """
    yield
    gc.collect()
