# Blueprints

Automation blueprints for units running this integration, contributed by people
who use them on their own systems.

## What is in here

- **[MHI multi-split mode lockout resolver](automation/smdev0925/mhi-multi-split-mode-lockout.yaml)**
  by [smdev0925](https://github.com/smdev0925) — one outdoor unit serves one
  mode at a time, and MHI's AUTO latches its heat/cool choice, so a head that
  went quiet hours ago can still be locking everyone else out. This stands the
  side that just had its turn down to fan-only for a few minutes, which drops
  the latch and lets the waiting side take the system. It decides nothing about
  what a room needs: the vote is the unit's own Cool/Heat Status, and priority
  is its Compressor Demand. Running on a four-head SCM80.
  ([discussion](https://github.com/blues-sechseck/Mitsubishi-WF-RAC-Integration/discussions/328))

- **[MHI multi-split AUTO replacement](automation/smdev0925/mhi-multi-split-auto-replacement.yaml)**
  by [smdev0925](https://github.com/smdev0925) — takes MHI's AUTO off each
  indoor unit and makes the heat/cool decision outside the unit. The setpoint
  stays yours: the blueprint reads it and never writes one. The two limits are
  distances from that setpoint rather than temperatures, so one pair of values
  suits every room and the band moves when you move the setpoint. A unit cooling
  whose room falls past the Cooling Limit turns to heating; a unit heating whose
  room rises past the Heating Limit turns to cooling; everything else is left
  alone. `off`, `dry`, `auto` and `fan_only` are ignored, and dry is a known
  limitation. Optional lockout protection resolves the split this creates on a
  multi-split, using the rules of the resolver above but without needing a
  Cool/Heat Status sensor, because no unit is in AUTO and a unit's mode is its
  request. A second option, Cooling priority, deals with the indoor expansion
  valves staying open while the outdoor unit heats: a unit that changes to
  cooling takes the outdoor unit at once, even from a heating unit that is
  running, and while heating runs, every unit in cooling goes to heating
  together, so it settles, closes its louvres and stops its fan rather than
  blowing warm air into its room. With lockout protection on, the side that
  ran last keeps the outdoor unit until a waiting room leaves its band; that
  room then takes it, even while the other side runs, so neither side can keep
  it for hours. A unit changed by hand between cooling and heating does not wait
  for its band edge. A debug switch writes every decision, and
  the reason for it, to the logbook. **Use this blueprint or the resolver on a
  unit, never both — they deadlock each other.** Proven on a four-head SCM80: the
  stand-down with its ten-second fan time, the restore and the handover; the
  idle-units part of Cooling priority (before 0.10.0 it moved only rooms at or
  below their setpoint); the immediate response to a mode changed by hand; and
  one unit turning from heating to cooling at its own limit. **Cooling
  priority's stand-down of a running heating side and the forced turn for a
  room outside its band are off-line-tested only.**
  **The cooling-to-heating rule has still not been seen to fire on hardware**,
  nor has a genuine recovery from a stranded unit, nor the dry block.

## Why they live here and not in the integration

The integration reports what a unit says and sends what you ask it to. It does
not decide anything on your behalf — no thermostat logic, no inference about
what a room needs, no coordination between units. That belongs in automations,
where you can read it, trace it and change it without waiting for a release.

A blueprint is the packaged form of exactly that: someone's working automation,
with the parts you have to fill in turned into a form.

## Installing one

Blueprints are **not** part of the integration download. HACS installs
`custom_components/mitsubishi_wf_rac/` and nothing else, so a file in here never
reaches your configuration on its own. Import it yourself:

**Settings → Automations & scenes → Blueprints → Import blueprint**, then paste
the GitHub URL of the `.yaml` file.

Home Assistant fetches it, checks it, and stores it under
`blueprints/automation/<author>/` in your configuration directory. It then shows
up under "Create automation → Use a blueprint". Re-importing the same URL later
picks up changes.

## Keeping one up to date

Re-importing the same URL picks up changes, but you have to remember to do it,
and two things quietly hand you a stale copy: `raw.githubusercontent.com` caches
for five minutes, so a re-import straight after a push fetches the old file; and
Home Assistant keeps parsed blueprints in memory, so editing the file on disk by
hand changes nothing until you re-import or restart.

[Blueprints Updater](https://github.com/luuquangvu/blueprints-updater) (MIT,
install as a HACS custom repository under **Integration**) removes the chore. It
watches the `source_url` each blueprint was imported from, offers updates as
normal Home Assistant update entities, and can apply them automatically with a
backup. It needs Home Assistant 2024.12 or later — one release newer than these
blueprints require.

Either way, check what is actually running rather than what the file says. The
AUTO replacement blueprint prints its version at the front of every logbook
entry for that reason.

Each blueprint's header says which entities it needs. Several of the useful ones
are diagnostic entities that are **disabled by default** — you turn those on
under Settings → Devices & services → the device → "+N entities not shown".

## Contributing one

Send a pull request. Blueprints stay under their author's name, in the file and
in the pull request history.

What a blueprint needs before it can go in:

- **It has to run.** Say on what — how many indoor units, single-split or
  multi-split, and roughly how long it has been in service. "Untested but should
  work" is a gist, not a blueprint.
- **A header that says what it does and what it assumes**, including every
  entity it needs and whether that entity is on by default.
- **No hidden single-split assumptions.** On a multi-split several readings
  belong to the shared outdoor unit rather than to the head you asked, and
  compressor frequency is the obvious trap — it does not tell you what one
  indoor unit is doing. If a blueprint only makes sense on one architecture, its
  header should say so.
- **Helpers are the user's to create.** A blueprint cannot create an
  `input_text` or an `input_number`. If yours needs one, take it as an entity
  input and document it.
- **Write sparingly.** Every command to a unit takes a short exclusive lease on
  it, so an automation that writes on a tight loop will collide with the app and
  with this integration. Check the current state before sending a command that
  would not change anything.
