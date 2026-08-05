"""Unit tests for wfrac/error_codes.py - see fehlercodes-selfdiagnose.md for
the sourcing/confidence notes behind these values."""

from custom_components.mitsubishi_wf_rac.wfrac.error_codes import describe_error_code


def test_describe_error_code_high_confidence():
    assert describe_error_code("E37") == "Außenwärmetauscher-Sensor-Fehler"


def test_describe_error_code_medium_confidence_notes_the_caveat():
    description = describe_error_code("E9")
    assert description is not None
    assert "SRR" in description


def test_describe_error_code_no_error_returns_none():
    assert describe_error_code("00") is None


def test_describe_error_code_maintenance_code_returns_none():
    # M<n> codes have no documented table at all (see fehlercodes-selfdiagnose.md).
    assert describe_error_code("M01") is None


def test_describe_error_code_undocumented_e_number_returns_none():
    # Deliberately no guessed text for E-numbers outside the two tables.
    assert describe_error_code("E2") is None
