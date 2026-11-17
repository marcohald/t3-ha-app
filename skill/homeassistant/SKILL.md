---
name: homeassistant
description: >-
  Use when inspecting, verifying, or changing a running Home Assistant (HA)
  instance via its WebSocket or REST APIs: entities, states, registries,
  automations, scripts, Lovelace dashboards, long-term statistics, history,
  templates, energy dashboards, and integrations such as MQTT/EMQX, Shelly or
  Zigbee. Ships read-only-first scripts and pitfall references. Skip for pure
  YAML or Jinja syntax questions with no instance involved, and for generic
  Linux, Docker or network tasks.
metadata:
  version: "1"
  tool_count: 4
---

# Home Assistant

Work against a running Home Assistant over its own APIs instead of guessing at
YAML. The scripts in `scripts/` talk WebSocket and REST, print a stable format,
and refuse anything that could break a production instance unless you ask twice.

## Before anything else

1. **Find out what you are pointed at.** `HA_URL` and `HA_TOKEN` come from the
   environment; if they are unset, the scripts fall back to
   `http://172.30.32.1:8123` and `/tmp/ha.tok` (HA OS Supervisor layout).
   Run `ha_ws.py ping` first -- it prints the version and confirms auth without
   changing anything.
2. **Read before you write.** Ask what the instance already contains
   (`ha_entities.py find`, `ha_dashboard.py list`, `ha_verify.py health`) before
   proposing a change. Most Home Assistant mistakes come from editing blind.
3. **Never restart or reconfigure a production instance to prove a point.**
   Every script is read-only by default and refuses mutating commands without
   `--allow-write`. Keep it that way unless the user asked for the change.

## Choosing a script

| Task | Command |
| --- | --- |
| Connect, check version and auth | `scripts/ha_ws.py ping` |
| Read states / a single state | `scripts/ha_ws.py states --filter <text> --compact` |
| Render a Jinja template with live data | `scripts/ha_ws.py render --template '{{ ... }}'` |
| Any other WebSocket command | `scripts/ha_ws.py raw --command '{"type":"get_panels"}'` |
| Any REST path | `scripts/ha_ws.py rest /api/states/sensor.x` |
| What feeds the Energy dashboard? | `scripts/ha_ws.py raw --command '{"type":"energy/get_prefs"}'` |
| Are my Energy sources healthy? (state, statistics, daily sums, gaps, jumps, flicker) | `scripts/ha_verify.py energy-check --days 3` |
| Which integrations failed to set up? | `scripts/ha_entities.py integrations --only-problems` |
| Which areas exist, and their ids? | `scripts/ha_entities.py areas` |
| Who is this entity, which device? | `scripts/ha_entities.py show sensor.x` |
| Find entities by name, id, or device | `scripts/ha_entities.py find growatt` |
| Which devices are offline? | `scripts/ha_verify.py health --by-device` |
| Where is entity X used? / before renaming | `scripts/ha_entities.py refs sensor.x`, `rename sensor.x --entity-id-new sensor.y` (no `--allow-write` = preview) |
| What is inside a group? | `scripts/ha_entities.py show light.group` (lists members) |
| Disabled / orphaned / duplicate entities (`duplicates` recommends keep/delete candidates, deletes nothing) | `scripts/ha_entities.py disabled`, `orphans`, `duplicates` |
| Do my entities exist and are they healthy? | `scripts/ha_verify.py states a b c`, `health --filter x` |
| Long-term statistics and their start | `scripts/ha_verify.py stats-range <id...>`, `stats <id...>` |
| History around an event | `scripts/ha_verify.py history sensor.x --hours 6` |
| Which dashboards are broken? (missing/unavailable per dashboard, dead ones flagged) | `scripts/ha_verify.py dashboards --sort problems` |
| Which entities does a dashboard use? | `scripts/ha_dashboard.py entity-refs --url-path x` |
| Cards of a dashboard (works for sections) | `scripts/ha_dashboard.py cards --url-path x` |
| Patch a dashboard (swap entities); shows changed cards plus a unified diff | `scripts/ha_dashboard.py patch --url-path x --swap old=new --dry-run`, offline: `patch --file cfg.json --swap old=new --out new.json` |
| Delete a dashboard | `scripts/ha_dashboard.py delete --url-path x --yes --allow-write` |

An entity inherits its device's area; `area-assign` is only needed when the
entity should differ from its device. A rename never updates the Energy
preferences -- `refs` points that out; change them by hand in the UI.

All four scripts take `--url`, `--token-file`, `--timeout`, `--json` and
`--allow-write`, in any position (`--json` before or after the subcommand both
work). Run any of them with `--help` for the full surface. `ha_verify.py`
accepts `--allow-write` but never writes.

`ha_verify.py` and `ha_entities.py` mask e-mail addresses in all output. Raw
output from `ha_ws.py` (`states`, `raw`, `rest`, `history`) and dashboard
configs from `ha_dashboard.py` is printed verbatim and can contain personal
data such as account e-mails in friendly names -- do not paste it unreviewed
into reports.

Token precedence in all four: `--token-file` > `$HA_TOKEN_FILE` > `$HA_TOKEN` >
`/tmp/ha.tok` (only when it already exists). An explicitly named file wins over
an exported token, so a stale `HA_TOKEN` in your shell cannot shadow the file
you passed.

## Safety rules that matter

- **`--allow-write` is not a formality.** The guard is **fail-closed**: only
  commands on the known read-only whitelist pass. Everything else -- including
  unknown commands such as `fire_event` -- is refused, plus
  `config/entity_registry/update`, `lovelace/config/save`,
  `recorder/adjust_sum_statistics` and `recorder/purge`. A refused command is a
  local error, not an API error: your instance was never contacted.
- **Statistics are the crown jewels.** `recorder/purge` and
  `recorder/adjust_sum_statistics` destroy history that cannot be recreated.
  There is no backup for a deleted statistic. When a user wants to "fix" an
  energy figure, read it first and show them the numbers.
- **Dashboard edits: dry run, diff, then apply.** `patch` is a dry run unless
  `--apply` is given, and `--backup-dir` keeps a timestamped copy of the live
  config. Export the config before a manual `save`.
- **Exit codes mean something.** A command exits 1 when its subject does not
  exist or a check fails (`ha_verify.py health`, `ha_entities.py show`,
  `ha_dashboard.py show`, `ha_verify.py history`), so it can gate a shell
  condition. A pattern that matches nothing is also exit 1 -- a typo in a
  `--filter`/`--pattern` must not read as a clean instance. Exit 2 is a usage
  error -- a refused write, a malformed `url_path`, a missing file, bad JSON in
  `--units`/`--post-body`, JSON of the wrong shape, a `--command` without a
  `type`, an empty side in `--swap old=new` -- and never a Home Assistant
  answer. All four CLIs agree on this.
- **`call` in `ha_entities.py` runs services.** A service call can switch a
  relay or move a cover. Only run it when the user explicitly asked for that
  exact action on that exact entity.
- **Restarting is a last resort.** See `references/yaml-config.md`: a full
  restart is needed for new YAML platforms, but it also resets every
  `input_number` that has `initial:` set, and it disconnects MQTT-based devices
  until they reconnect.

## Pitfalls that cost the most time

Read the matching reference before debugging; each one is a real failure that
looked like something else.

1. `history/history_during_period` over WebSocket always returns compact keys
   `s` and `lu` (**not** `state` and `last_changed`), regardless of
   `minimal_response`. Code that reads `row["state"]` gets a `KeyError` that
   looks like missing data. See `references/websocket-api.md`.
2. `recorder/statistics_during_period` returns `start` in **milliseconds**.
   Dividing by nothing, or treating it as seconds, produces
   `year 58567 is out of range`. See `references/statistics.md`.
3. `lovelace/dashboards/create` rejects a `url_path` without a hyphen with
   `invalid_format`. `my_dash` fails, `my-dash` works. The scripts check this
   locally before opening a connection, so the error is immediate. See
   `references/dashboards.md`.
4. New YAML platforms (`utility_meter`, `input_number`, `template`) are **not**
   picked up by `reload_all`. A restart is required -- with the side effects
   above. See `references/yaml-config.md`.
5. `input_number` with `initial: 0` snaps back to 0 on **every** restart, which
   silently moves a baseline date. Omit `initial`. See `references/yaml-config.md`.
6. Template sensors without an `availability` guard report a **wrong 0** when
   their source is missing, instead of `unavailable`. That 0 flows into
   statistics and dashboards. See `references/yaml-config.md`.
7. Entity ids from an older config keep occupying ids; the new sensor appears as
   `..._2` and dashboards silently point at the wrong one. Check with
   `ha_entities.py duplicates`.
8. A dashboard's `title` lives in the **dashboard registry entry**, not in its
   config blob -- 7 of 12 dashboards here have `title: None` in the config and a
   proper title in the registry. Do not treat it as a missing title.
9. `view.cards` is empty on a sections-style view; the cards live in
   `view.sections[].cards`. Code that reads only `cards` reports such a view as
   empty. Both scripts (`ha_dashboard.py show`, `ha_verify.py dashboard-url`)
   handle sections.
10. `unknown` is the **normal** state of a `button`, `event` or `scene` that was
    never used -- not a fault. `unavailable` is a fault in every domain. Treat
    the two differently or every dashboard with a button looks broken.
11. `lovelace/dashboards/update` and `.../delete` address a dashboard by its
    registry **`id`** (`dashboard_licht`), while `create` and
    `lovelace/config` use the **`url_path`** (`dashboard-licht`). Both differ
    for every dashboard on a storage-mode install. `ha_dashboard.py delete`
    resolves the id for you. See `references/dashboards.md`.
12. A `heading`, `tile` or `button` card **ignores** an `entities` key; only
    `entities`, `history-graph` and `statistics-graph` render an entity list.
    `patch --add` refuses the others for that reason. See
    `references/dashboards.md`.
13. On Python 3.10, `datetime.fromisoformat("2026-11-15T12:00:00Z")` fails --
    the `Z` suffix only arrived in 3.11. Since that is exactly the format Home
    Assistant prints in every timestamp it returns, the scripts normalise a
    trailing `Z` to `+00:00` themselves. Do the same in your own code.
14. `--rename` on a card whose `entities` list holds **plain strings** needs a
    shape to put the name in. `patch` rewrites that one row into
    `{"entity": ..., "name": ...}`; the old code replaced the string and
    dropped the name. Valid Lovelace, slightly larger diff.
15. A disabled entity has no state **by design**, so a dashboard that
    references one has a card that cannot render -- `--with-registry` is what
    lets `ha_verify.py dashboard` tell it from a deleted entity. Do not read
    "no state" as "fine".

## Reading a health report

`ha_verify.py health` counts `unavailable` everywhere and `unknown` only in
domains where it means something is wrong. `--include-unused` adds the
never-triggered button/event/scene entities back, and `--json` reports their
number as `unused_not_counted` so a clean exit code is not a silent one.

A `--filter` that matches no entity at all exits 1 and says so. "0 entities
are unknown/unavailable" reads like a clean bill of health, so a typo in the
filter would otherwise look like a healthy instance. `--json` carries
`filter_matched` for the same reason. Like every sibling filter, it matches the
entity id **and** the friendly name -- matching the id alone hid
`button.…_calibrate_zero` ("CO2 Calibrate Zero") from `--filter "Calibrate
Zero"`, which then reported "matched nothing" while a broken entity carried the
search term in its name.

The same trap is closed in the sibling listings: `ha_ws.py states --filter`,
`entity-ids --pattern`, `entities find`, `duplicates --pattern`, `disabled
--pattern` and `areas --pattern` all exit 1 when the pattern matches nothing,
and 0 when it matches something but reports no problem. `ha_verify.py
dashboard` counts a disabled entity as a finding (its config cannot render it),
and `ha_verify.py dashboards` exits 1 if any dashboard's config could not be
read at all.

Those patterns match the **whole** entity id, domain included: `duplicates
--pattern switch` and `--pattern switch.` both list the `switch.*` groups.
Matching only the object id silently skipped every domain-scoped search.

## Working with an energy dashboard

Long-term statistics, not raw power registers, are the source of truth for
efficiency. Read `references/statistics.md` and
`references/environment-notes.md` before recomputing any efficiency figure: a
naive integration of `charging_power` yields a 130 % round-trip efficiency,
which is physically impossible. Sanity-check `sensor.*_kwh` counters that the
Energy dashboard already uses instead.

## Reference map

| File | Covers |
| --- | --- |
| `references/websocket-api.md` | Auth, command list, `s`/`lu`, result envelope, error codes |
| `references/rest-api.md` | REST endpoints, templates, which to prefer over WebSocket |
| `references/dashboards.md` | Storage mode, url_path rule, sections views, patching |
| `references/statistics.md` | Long-term statistics vs. history, change/sum/mean, ms timestamps |
| `references/yaml-config.md` | Reload vs. restart, input_number persistence, availability |
| `references/troubleshooting.md` | Symptom to cause table |
| `references/environment-notes.md` | Concrete Growatt / EMQX / Shelly findings from a real install |
