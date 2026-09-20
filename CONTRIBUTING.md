# Contributing

Bug reports and measurements from your own unit are as useful here as code —
this integration talks to hardware that behaves differently across firmware
branches, and much of what it knows came out of issues. If you are reporting
rather than coding, the [bug form](.github/ISSUE_TEMPLATE/bug.yml) asks for
everything I would otherwise come back for.

## Getting set up

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

Python 3.14 or newer: `pytest-homeassistant-custom-component` pins an exact
Home Assistant per release, and on older Python versions pip resolves to a
release far below the floor this integration claims.

Optional, and worth it — the hooks run the same checks CI does, before the
commit rather than after the push:

```sh
pip install pre-commit
pre-commit install
```

## Before you push

```sh
ruff check .
ruff format --check .
mypy
pytest
```

All four pass on `main`. The ruff rule set in `ruff.toml` is Home Assistant
Core's own, minus what only applies inside core itself — this integration is
also submitted to core
([core#181403](https://github.com/home-assistant/core/pull/181403)), and a file
that already lints clean there arrives without a reformatting commit on top.
The formatter leaves Markdown alone on purpose: the snippets in
[`docs/wf-rac-module-reference.md`](docs/wf-rac-module-reference.md) are
aligned in columns to be read as tables.

## Tests

Three suites, and they are not interchangeable:

- `tests/unit` and `tests/integration` — this repo's own. They reach into the
  coordinator and cover protocol paths nothing else does.
- `tests/components` — core's suite, mirrored from the core PR and run from the
  outside through `hass.states` and `hass.services`. It exists to catch drift
  between the two trees; see [its README](tests/components/README.md).

CI runs the suites twice: against the current Home Assistant, and against the
oldest one `hacs.json` claims to support. Run the second one yourself when you
touch anything that uses a recent Home Assistant helper:

```sh
pip install -r requirements-floor.txt
pytest --ignore=tests/components   # core's suite needs core's dev helpers
```

A floor nothing runs against is a number, not a promise.

## Commits and pull requests

- One commit per intent. The body says *why*, not what the diff already shows.
- Squash before merging when the branch carries intermediate states — a
  review round that ends in "actually, put that back" should not reach `main`
  as two commits.
- English in the repository: code, comments, commit messages, documentation.
- Open a pull request rather than pushing to `main`, so CI has a chance to
  disagree before the change is permanent.

## Things that need more than a green test run

**Anything that can reach a unit** gets tried on real hardware before it goes
into a release. That is what the hardware question in the pull request template
is for — "cannot reach a unit" is a perfectly good answer, and saying so saves
me from testing something that has nothing to test.

**Protocol and state-machine logic belongs in
[`pywfrac`](https://github.com/blues-sechseck/pywfrac)**, not in this
repository. Anything that builds or reads a frame lives there; what stays here
is the Home Assistant side. `coordinator.py` in particular should get smaller
over time, not larger.

**Breaking changes** — entity ids, state values, option keys — need a migration
in `async_migrate_entry` and an entry in the README's "Check your automations"
section. People have automations built on these names.

**User-visible strings** go into `strings.json`, which is the source for
English; `python scripts/build_translations.py` writes
`translations/en.json` from it, and `--check` fails if that file is stale. The
other languages are hand-written and nothing derives them, so translations from
native speakers are genuinely welcome — write your own wording for what the
option does rather than translating the English literally.
