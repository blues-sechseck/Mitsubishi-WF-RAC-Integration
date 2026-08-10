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


def test_describe_maintenance_code_uses_the_shared_number_space():
    # Errors and protective stops are numbered out of one table, so M35 is the
    # same condition as E35 at an earlier escalation stage.
    assert describe_error_code("M35") == "Schutzabschaltung: Kühl-Hochdruckschutz"


def test_describe_maintenance_code_normalises_the_zero_padding():
    # rac_parser spells M codes "M01" but E codes "E1", and both mean code 1.
    assert describe_error_code("M01") == "Schutzabschaltung: Fehler Kabel-Fernbedienung"


def test_describe_error_code_indoor_side_codes_from_the_databooks():
    assert describe_error_code("E85") == "Frostschutz aktiv"
    assert describe_error_code("E86") == "Heiz-Hochdruckschutz aktiv"


def test_describe_error_code_undocumented_e_number_returns_none():
    # Deliberately no guessed text for E-numbers outside the two tables.
    assert describe_error_code("E2") is None
