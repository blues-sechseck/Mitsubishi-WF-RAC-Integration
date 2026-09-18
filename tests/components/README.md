# Core's own test suite, run against this integration

The files under `mitsubishi_wf_rac/` are the tests from
[home-assistant/core#181403](https://github.com/home-assistant/core/pull/181403),
carried over unchanged apart from the imports. They are not a replacement for
`tests/unit` and `tests/integration` — those reach into the coordinator and
cover protocol paths core's bronze cut does not contain at all. These are a
second layer, from the outside: `Repository` is patched, the entry is set up
through `hass.config_entries.async_setup()`, and every assertion is made on
`hass.states` or through `hass.services.async_call()`.

What that buys is drift detection. Core and this integration ship the same
behaviour from two trees, and core's suite is the only thing that can check
that claim without putting both files side by side. The first run already
found one: core's migration had stopped writing `availability_retry_limit`,
which is right there and wrong here.

## Re-syncing from core

Three steps, and nothing else should be needed:

1. Copy `tests/components/mitsubishi_wf_rac/` out of the core tree.
2. Rewrite the imports:
   ```
   homeassistant.components.mitsubishi_wf_rac -> custom_components.mitsubishi_wf_rac
   from tests.common import                   -> from pytest_homeassistant_custom_component.common import
   from tests.typing import                   -> from pytest_homeassistant_custom_component.typing import
   ```
3. Drop the `DOMAIN` argument from `load_json_object_fixture`.
   `pytest-homeassistant-custom-component` resolves a fixture path relative to
   the calling module; core resolves it relative to the `tests/` package. With
   no integration given, both land on `fixtures/` next to the test.

The directory has to keep its `components/mitsubishi_wf_rac` shape for that
resolution to work.

If a test fails after a re-sync, it is a finding, not a porting error. Where
the two trees genuinely disagree, the divergence is named in `conftest.py` and
marked `xfail(strict=True)` — a divergence that closes shows up as XPASS and
fails the run, which is how it gets noticed.

## Why this does not run on the floor

Core writes its tests against `dev`; this integration promises the version in
`hacs.json`. They already use test helpers that do not exist there — on HA
2026.4, `DeviceRegistry.async_get_device_by_identifier` accounts for four
failures on its own. Raising the floor for a test helper would cost 1,900
installations their upgrade, so this suite runs in the `current` job only. The
floor stays guarded by `tests/unit` and `tests/integration`, which run in both.
