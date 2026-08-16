"""Guards strings.json against drifting away from the code and from en.json.

strings.json is the source Home Assistant's own tooling reads: hassfest
validates against it, and new translations are generated from it. Nothing at
runtime reads it - the UI is served from translations/, so a key that is
missing here still renders correctly and the gap stays invisible until someone
adds a language.
"""

import json
import re
from pathlib import Path

import custom_components.mitsubishi_wf_rac as component

COMPONENT = Path(component.__file__).parent
STRINGS = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
ENGLISH = json.loads((COMPONENT / "translations/en.json").read_text(encoding="utf-8"))


def test_entity_keys_match_english_translation():
    """Both files carry the same English text, so they must carry the same keys."""
    for domain, entries in ENGLISH["entity"].items():
        assert set(STRINGS["entity"].get(domain, {})) == set(entries), domain


def test_exception_keys_match_english_translation():
    assert set(STRINGS["exceptions"]) == set(ENGLISH["exceptions"])


def test_issue_keys_match_english_translation():
    assert set(STRINGS["issues"]) == set(ENGLISH["issues"])


def test_raised_translation_keys_exist_in_strings():
    """Every `translation_key="..."` passed to a HomeAssistantError subclass
    or to ir.async_create_issue() must resolve somewhere - a typo here fails
    silently at runtime (HA falls back to the plain message arg, or the issue
    just never shows up) rather than raising, so nothing else would catch it.
    """
    used_keys = set()
    for path in COMPONENT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        used_keys.update(re.findall(r'translation_key="([a-z_]+)"', text))

    assert used_keys, "expected to find at least one translation_key in the source"
    assert used_keys <= set(STRINGS["exceptions"]) | set(STRINGS["issues"])


def test_step_data_keys_match_english_translation():
    for section in ("config", "options"):
        for step, body in ENGLISH[section]["step"].items():
            expected = set(body.get("data", {}))
            actual = set(STRINGS[section]["step"].get(step, {}).get("data", {}))
            assert actual == expected, f"{section}.{step}"


def test_setup_and_options_steps_have_distinct_titles():
    """The options form grew out of a copy of the setup step and kept its
    heading while the fields diverged - it now holds offsets and polling
    behaviour, none of which is connection info.
    """
    setup = STRINGS["config"]["step"]["user"]["title"]
    options = STRINGS["options"]["step"]["init"]["title"]
    assert setup != options


def test_per_mode_offsets_explain_that_blank_means_the_general_offset():
    """These two fields carry no default on purpose: blank resolves to the
    general target offset (see climate.py). Without a description the form
    gives the user no way to know that.
    """
    described = STRINGS["options"]["step"]["init"]["data_description"]
    assert "target_offset_cool" in described
    assert "target_offset_heat" in described


