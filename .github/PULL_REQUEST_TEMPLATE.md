## What this changes, and why

<!-- The why is the part that is hard to reconstruct later. One or two
sentences are plenty. Link the issue or discussion if there is one. -->

## Tested on hardware

<!-- Which unit and which firmware, or "not tested on hardware" - that is a
fine answer for a change that cannot reach a unit. The module's firmware
versions are in the diagnostics download. -->

- Model:
- Firmware (`firmType`, `mcu`, `wireless`):

## Checklist

- [ ] `ruff check .`, `ruff format --check .`, `mypy` and `pytest` pass locally
- [ ] Tests cover the change, or it is one that cannot be tested
- [ ] User-visible strings went into `strings.json`, with
      `scripts/build_translations.py` re-run for `translations/en.json`
- [ ] Breaking change? Then it migrates in `async_migrate_entry` and says so
      in the README's "Check your automations" section
