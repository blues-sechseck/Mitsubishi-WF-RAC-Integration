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


def test_step_section_keys_match_english_translation():
    """Fields moved into sections leave step.data empty, so the check above
    would compare two empty sets and pass while saying nothing.
    """
    for section in ("config", "options"):
        for step, body in ENGLISH[section]["step"].items():
            sections = body.get("sections", {})
            mirror = STRINGS[section]["step"].get(step, {}).get("sections", {})
            assert set(mirror) == set(sections), f"{section}.{step}"
            for name, group in sections.items():
                assert set(mirror[name].get("data", {})) == set(group.get("data", {})), name


def test_sections_cover_every_field_the_options_form_shows():
    """A field the form renders but no section names shows up unlabelled."""
    from custom_components.mitsubishi_wf_rac import config_flow

    init = ENGLISH["options"]["step"]["init"]
    labelled = set(init.get("data", {}))
    for group in init.get("sections", {}).values():
        labelled |= set(group.get("data", {}))

    handler = config_flow.WfRacOptionsFlowHandler
    rendered = config_flow.WfRacOptionsFlowHandler._rendered_option_keys
    assert handler and rendered  # imported, not just referenced
    # _rendered_option_keys() is the form's own answer to "what does this
    # dialog collect", so the labels have to cover exactly that.
    assert labelled == {
        config_flow.CONF_AVAILABILITY_RETRY_LIMIT,
        config_flow.CONF_FIRMWARE_UPDATE_CHECK,
        config_flow.CONF_EXTERNAL_TEMPERATURE_SOURCE,
        config_flow.CONF_OVERSHOOT_COOL,
        config_flow.CONF_OVERSHOOT_HEAT,
        config_flow.CONF_TARGET_OFFSET,
        config_flow.CONF_TARGET_OFFSET_COOL,
        config_flow.CONF_TARGET_OFFSET_HEAT,
        config_flow.CONF_INDOOR_OFFSET,
        config_flow.CONF_OUTDOOR_OFFSET,
    }


def test_a_translated_section_labels_every_field_in_it():
    """A section whose fields carry no label in that language renders them as
    their raw keys - `overshoot_cool` where a label belongs. Nothing else
    catches it: the file is valid JSON, hassfest is happy, and it only shows
    up to someone running Home Assistant in that language.

    Translating a section at all therefore means translating its fields.
    Leaving the whole section out stays fine - that falls back wholesale.
    """
    english = ENGLISH["options"]["step"]["init"]["sections"]
    for path in (COMPONENT / "translations").glob("*.json"):
        if path.stem == "en":
            continue
        body = json.loads(path.read_text(encoding="utf-8"))
        sections = body.get("options", {}).get("step", {}).get("init", {}).get("sections", {})
        for name, group in sections.items():
            expected = set(english[name].get("data", {}))
            assert set(group.get("data", {})) == expected, f"{path.stem}: {name}"


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
    described = _described_option_keys()
    assert "target_offset_cool" in described
    assert "target_offset_heat" in described


def test_external_temperature_source_explains_its_failsafe():
    assert "external_temperature_source" in _described_option_keys()


def _described_option_keys() -> set[str]:
    """Every option field carrying a description, wherever it now sits."""
    init = STRINGS["options"]["step"]["init"]
    described = set(init.get("data_description", {}))
    for group in init.get("sections", {}).values():
        described |= set(group.get("data_description", {}))
    return described

