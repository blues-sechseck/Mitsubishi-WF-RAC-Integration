"""Marks the tests under here that core and this integration disagree on.

The suite in components/mitsubishi_wf_rac is core's own, carried over as-is so
that re-syncing it stays a mechanical step. Nothing in those files is edited
to make it pass here - where the two trees genuinely differ, the divergence is
named below instead, with the reason it exists.

A test that starts passing is reported as XPASS, which fails the run: that is
the point. Either the divergence closed and the entry belongs gone, or
something moved that nobody meant to move.
"""

import pytest

# Node id (relative to this directory) -> why the two trees differ here.
DIVERGENCES = {
    "mitsubishi_wf_rac/test_climate.py::test_entity": (
        "snapshot_platform asserts that exactly one platform is loaded. Core "
        "ships climate alone; this integration loads six. Closes when core "
        "has the other five."
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
    "mitsubishi_wf_rac/test_init.py::test_migration_from_version_1": (
        "Core's migration writes no availability_retry_limit because no form "
        "there can change one. This integration has that form, so writing the "
        "minimum on migration is correct here."
    ),
}


def pytest_collection_modifyitems(config, items):
    """Mark the known divergences xfail, strictly."""
    for item in items:
        _, sep, relative = item.nodeid.partition("tests/components/")
        if sep and (reason := DIVERGENCES.get(relative)):
            item.add_marker(pytest.mark.xfail(reason=reason, strict=True))
