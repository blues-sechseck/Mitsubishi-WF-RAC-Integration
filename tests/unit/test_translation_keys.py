"""Guards strings.json against drifting away from the code and from en.json.

strings.json is the source Home Assistant's own tooling reads: hassfest
validates against it, and new translations are generated from it. Nothing at
runtime reads it - the UI is served from translations/, so a key that is
missing here still renders correctly and the gap stays invisible until someone
adds a language.

English is therefore not written twice: translations/en.json is generated from
strings.json by scripts/build_translations.py, and the first test below is what
holds the generated file to its source.
"""

import json
from pathlib import Path
import re

import custom_components.mitsubishi_wf_rac as component
from scripts.build_translations import build

COMPONENT = Path(component.__file__).parent
STRINGS = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
ENGLISH = json.loads((COMPONENT / "translations/en.json").read_text(encoding="utf-8"))


def test_english_translation_is_what_strings_generates():
    """The two English files said the same thing twice until three of the
    strings quietly stopped agreeing - an abort message, the reconfigure
    message and the host label. Generated now, so only strings.json is edited.
    """
    assert build() == ENGLISH


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


def test_every_setup_field_carries_a_description():
    """A form field is a label and a description; the label alone leaves the
    user guessing at what a host, a port or a duplicate-IP override is for.
    The quality scale asks for this explicitly under `config-flow`, and it is
    the part hassfest cannot see - it only checks that a config flow exists.
    """
    for section in ("config", "options"):
        for step, body in STRINGS[section]["step"].items():
            fields = set(body.get("data", {}))
            described = set(body.get("data_description", {}))
            assert fields <= described, f"{section}.{step}: {fields - described}"


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
        config_flow.CONF_OVERSHOOT_DRY,
        config_flow.CONF_OVERSHOOT_HEAT,
        config_flow.CONF_TARGET_OFFSET,
        config_flow.CONF_TARGET_OFFSET_COOL,
        config_flow.CONF_TARGET_OFFSET_HEAT,
        config_flow.CONF_INDOOR_OFFSET,
        config_flow.CONF_OUTDOOR_OFFSET,
    }


def test_every_error_the_flow_raises_has_a_message():
    """A KnownError's error_name is what async_show_form puts in `errors`, and
    the UI looks it up under config.error. A name with no entry renders as the
    raw key - which is how `invalid_host` reached users unnoticed: nothing else
    fails, the form just shows a word nobody wrote.
    """
    from custom_components.mitsubishi_wf_rac import config_flow

    raised = {
        cls.error_name
        for cls in vars(config_flow).values()
        if isinstance(cls, type)
        and issubclass(cls, config_flow.KnownError)
        and cls is not config_flow.KnownError
    }

    assert raised <= set(STRINGS["config"]["error"]), raised - set(
        STRINGS["config"]["error"]
    )


def test_a_section_describes_every_field_it_shows():
    """The guard above covers the top-level step fields; the sections carry
    their own data/data_description pair and were missed by it.
    """
    for section, group in STRINGS["options"]["step"]["init"]["sections"].items():
        fields = set(group.get("data", {}))
        described = set(group.get("data_description", {}))
        assert fields <= described, f"{section}: {fields - described}"


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
        sections = (
            body.get("options", {}).get("step", {}).get("init", {}).get("sections", {})
        )
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


def test_every_action_is_named_in_strings():
    """Home Assistant serves action names and descriptions from translations/,
    and falls back to services.yaml only where a translation is missing. The
    file has no language variants, so anything left in it reaches every user
    in English - which is how all six actions stood untranslated. Core's own
    test suite enforces the same thing through its check_translations fixture.
    """
    import yaml

    services = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))
    for name, body in services.items():
        described = STRINGS["services"][name]
        assert described["name"] and described["description"], name
        for field in body.get("fields") or {}:
            assert described["fields"][field]["name"], f"{name}.{field}"


def test_every_translated_action_still_exists():
    """A renamed or dropped action leaves its text behind, where it reads as a
    working action in every language file that carries it.
    """
    import yaml

    services = set(
        yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))
    )
    for path in (COMPONENT / "translations").glob("*.json"):
        body = json.loads(path.read_text(encoding="utf-8"))
        assert set(body.get("services", {})) <= services, path.stem


def test_a_translated_action_option_list_matches_the_selector():
    """An option the selector offers but the language does not name renders as
    its raw value - `left_left` where a label belongs.
    """
    english = ENGLISH["selector"]
    for path in (COMPONENT / "translations").glob("*.json"):
        if path.stem == "en":
            continue
        body = json.loads(path.read_text(encoding="utf-8"))
        for key, group in body.get("selector", {}).items():
            expected = set(english[key]["options"])
            assert set(group.get("options", {})) == expected, f"{path.stem}: {key}"
