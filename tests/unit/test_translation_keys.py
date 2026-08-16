"""Guards strings.json against drifting away from the code and from en.json.

strings.json is the source Home Assistant's own tooling reads: hassfest
validates against it, and new translations are generated from it. Nothing at
runtime reads it - the UI is served from translations/, so a key that is
missing here still renders correctly and the gap stays invisible until someone
adds a language.
"""

import json
from pathlib import Path

COMPONENT = Path("custom_components/mitsubishi_wf_rac")
STRINGS = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
ENGLISH = json.loads((COMPONENT / "translations/en.json").read_text(encoding="utf-8"))


def test_entity_keys_match_english_translation():
    """Both files carry the same English text, so they must carry the same keys."""
    for domain, entries in ENGLISH["entity"].items():
        assert set(STRINGS["entity"].get(domain, {})) == set(entries), domain


def test_step_data_keys_match_english_translation():
    for section in ("config", "options"):
        for step, body in ENGLISH[section]["step"].items():
            expected = set(body.get("data", {}))
            actual = set(STRINGS[section]["step"].get(step, {}).get("data", {}))
            assert actual == expected, f"{section}.{step}"


