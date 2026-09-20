"""Holds the requirements files to the pywfrac version the manifest ships.

manifest.json is what Home Assistant installs at runtime; the requirements
files are what CI installs before running the tests. When they drift apart the
suite is green against a library nobody gets - which is what happened between
0.1.4 and 0.1.5, unnoticed until a release was already out.
"""

import json
from pathlib import Path
import re

import custom_components.mitsubishi_wf_rac as component

COMPONENT = Path(component.__file__).parent
REPO = COMPONENT.parent.parent
MANIFEST = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))

REQUIREMENTS = ("requirements-dev.txt", "requirements-floor.txt")


def manifest_pin() -> str:
    """The single pywfrac requirement of the manifest, `pywfrac==x.y.z`."""
    pins = [r for r in MANIFEST["requirements"] if r.split("=")[0].strip() == "pywfrac"]
    assert len(pins) == 1, f"expected one pywfrac requirement, got {pins}"
    assert re.fullmatch(r"pywfrac==\S+", pins[0]), (
        f"{pins[0]}: a range would let the tests run against whatever the day offers"
    )
    return pins[0]


def pins_in(text: str) -> list[str]:
    """Every pywfrac requirement line of a pip requirements file."""
    return [
        line.strip()
        for line in text.splitlines()
        if not line.lstrip().startswith("#") and re.match(r"\s*pywfrac\b", line)
    ]


def test_manifest_pins_pywfrac_exactly():
    assert manifest_pin()


def test_requirements_files_pin_the_manifest_version():
    """Both CI environments install the library the manifest ships."""
    for name in REQUIREMENTS:
        assert pins_in((REPO / name).read_text(encoding="utf-8")) == [manifest_pin()], (
            name
        )
