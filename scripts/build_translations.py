"""Write translations/en.json out of strings.json.

Home Assistant only ever reads translations/<lang>.json at runtime: the single
path helpers/translation.py opens is `integration.file_path / "translations" /
<lang>.json`, gated on `integration.has_translations`. Nothing there looks at
strings.json - that file exists for hassfest and as the source new translations
are generated from. So the integration ships the English text twice, and the
two copies quietly stopped agreeing (an abort message, a reconfigure message
and the host label had drifted apart before this script was written).

English is now written once, in strings.json, and this resolves its
`[%key:...%]` references into the file the UI actually serves. The other
languages stay hand-written - nothing derives them.

References into `common::` resolve against the *installed* Home Assistant's own
strings.json, so the output depends on which version is installed. That is why
the guard in tests/unit/test_translation_keys.py regenerates the file instead
of comparing it against a stored copy.

    python scripts/build_translations.py            # write the file
    python scripts/build_translations.py --check    # fail if it is out of date
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any

import homeassistant

REPO = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = REPO / "custom_components" / "mitsubishi_wf_rac"
STRINGS = COMPONENT / "strings.json"
ENGLISH = COMPONENT / "translations" / "en.json"

DOMAIN = "mitsubishi_wf_rac"

_REFERENCE = re.compile(r"\[%key:([^\]]+)%\]")
# A reference that resolves to another reference is normal; a chain this long
# is a cycle.
_MAX_DEPTH = 8


def _load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _roots(strings: dict[str, Any]) -> dict[str, Any]:
    """Where a `[%key:<root>::...%]` reference is looked up."""
    common = _load(pathlib.Path(homeassistant.__file__).parent / "strings.json")
    return {"common": common["common"], "component": {DOMAIN: strings}}


def _lookup(reference: str, roots: dict[str, Any]) -> Any:
    node: Any = roots
    for part in reference.split("::"):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"unresolvable reference: [%key:{reference}%]")
        node = node[part]
    return node


def _resolve(node: Any, roots: dict[str, Any], depth: int = 0) -> Any:
    if isinstance(node, dict):
        return {key: _resolve(value, roots, depth) for key, value in node.items()}
    if not isinstance(node, str) or "%key:" not in node:
        return node
    if depth >= _MAX_DEPTH:
        raise RecursionError(f"reference chain does not terminate: {node}")
    match = _REFERENCE.fullmatch(node)
    if match is None:
        # Home Assistant resolves whole values only. A reference embedded in a
        # sentence would survive into the file and be shown to the user raw.
        raise ValueError(f"reference is not the whole value: {node}")
    return _resolve(_lookup(match.group(1), roots), roots, depth + 1)


def build() -> dict[str, Any]:
    """strings.json with every reference resolved - the English UI text."""
    strings = _load(STRINGS)
    return _resolve(strings, _roots(strings))


def _serialised() -> str:
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Write or check translations/en.json; return a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if the file is out of date",
    )
    args = parser.parse_args(argv)

    generated = _serialised()
    if args.check:
        if ENGLISH.read_text(encoding="utf-8") != generated:
            print(
                f"{ENGLISH.relative_to(REPO)} is out of date - "
                f"run python {pathlib.Path(__file__).relative_to(REPO)}",
                file=sys.stderr,
            )
            return 1
        return 0

    ENGLISH.write_text(generated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
