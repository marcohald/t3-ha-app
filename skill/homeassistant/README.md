# homeassistant -- a Claude Code skill

A reusable toolkit for working with a live Home Assistant instance: inspecting
entities and registries, reading long-term statistics and history, managing
Lovelace dashboards, and avoiding the pitfalls that silently produce wrong
numbers (or a restart you did not want).

Scripts talk to Home Assistant's own WebSocket and REST APIs. They are
**read-only by default**; anything that could change a production instance is
refused locally until you pass `--allow-write`.

## Installation

The repository *is* the skill directory, so cloning it puts the skill in place.
Clone it wherever your harness looks for user-level skills.

```bash
# user-level: available in every project (this is what the harness loads)
git clone https://github.com/marcohald/ha-agent-skill.git \
    ~/.claude/skills/homeassistant

# or, for one project only
git clone https://github.com/marcohald/ha-agent-skill.git \
    .claude/skills/homeassistant
```

The directory name (`homeassistant`) becomes the skill name, and it must match
the `name:` field in `SKILL.md`. On a machine where `$HOME` is not the usual
place, use that instead of `~`:

```bash
git clone https://github.com/marcohald/ha-agent-skill.git \
    "$HOME/.claude/skills/homeassistant"
```

Verify the layout:

```
~/.claude/skills/homeassistant/
    SKILL.md
    scripts/{ha_ws.py,ha_verify.py,ha_dashboard.py,ha_entities.py}
    references/*.md
```

Claude Code discovers skills in `~/.claude/skills/<name>/SKILL.md` and in a
project's `.claude/skills/<name>/SKILL.md`. A new session picks it up; there is
nothing to register.

### Requirements

- Python 3.10 or newer.
- The `websockets` package: `python3 -m pip install websockets`
  (`ha_ws.py rest` and `ha_ws.py render --transport rest` work without it, since
  they use `urllib` only; only the WebSocket commands need the package).
- A long-lived access token from your Home Assistant profile page.

### Credentials

Nothing is hard-coded. The scripts resolve credentials like this:

| Setting | Order of precedence |
| --- | --- |
| URL | `--url`, `$HA_URL`, then `http://172.30.32.1:8123` |
| WebSocket URL | `--ws-url`, `$HA_WS_URL`, then derived from the URL |
| Token | `--token-file`, `$HA_TOKEN_FILE`, `$HA_TOKEN`, then `/tmp/ha.tok` **if it already exists** |
```bash
export HA_URL="http://homeassistant.local:8123"
export HA_TOKEN="<your long-lived token>"
```

`/tmp/ha.tok` is a convenience for Home Assistant OS, where an interactive shell
already has one. The scripts never create it and never write a token anywhere.
Output and error messages pass through a redactor that masks anything that looks
like a token.

## Quick start

```bash
cd ~/.claude/skills/homeassistant/scripts

# does it work?
python3 -I ha_ws.py ping

# what is in this instance?
python3 -I ha_ws.py config
python3 -I ha_entities.py stats
python3 -I ha_dashboard.py list

# what is broken right now?
python3 -I ha_verify.py health
```

## The four scripts

| Script | Read-only subcommands | Mutating (needs `--allow-write`) |
| --- | --- | --- |
| `ha_ws.py` | `ping`, `config`, `states`, `render`, `raw`, `rest`, `commands` | any `raw` command not on the read-only list |
| `ha_entities.py` | `find`, `show`, `disabled`, `orphans`, `duplicates`, `stats`, `areas`, `integrations` | `enable`, `disable`, `rename`, `device-rename`, `area-assign`, `call` |
| `ha_verify.py` | `states`, `health`, `stats`, `stats-range`, `history`, `dashboard-url`, `dashboard`, `dashboards`, `entity-ids` | -- (never writes) |
| `ha_dashboard.py` | `list`, `show`, `export`, `entity-refs`, `cards`, `validate`, `diff`, `backup` | `create`, `save`, `delete`, `patch` |

Every script supports `--url`, `--ws-url`, `--token-file`, `--timeout`, `--json`
and `--allow-write` in any position, plus `--help`.

`ha_dashboard.py`'s read-only subcommands take `--url-path` as *optional*: omit
it to inspect the default dashboard (`/lovelace`). Section-style views, where the
cards sit in `view.sections[].cards`, are handled everywhere — `show`, `cards`,
`validate` and `patch` all see the same cards, so the index `cards` prints is the
index `patch --card-index` uses.

`validate --file` needs no Home Assistant at all: it parses the JSON locally and
never opens a connection. A file that is missing, unreadable or not a JSON
object is reported as a one-line error instead of a traceback.

`ha_verify.py health` is domain aware: `unavailable` always counts as a problem,
`unknown` only where it means something (`button`, `event` and `scene` are
`unknown` until they are used once, which is normal). `--include-unused` counts
them anyway. A `--filter` matching no entity at all exits 1 rather than
reporting a clean zero.

`ha_verify.py dashboard` exits 1 for a dashboard that references a missing
entity, one in a bad state, **or** one whose registry entry is disabled (a
disabled entity has no state by design, so it needs `--with-registry` to be
told apart from a deleted one). A config whose views yield no entity reference
at all says so instead of declaring everything fine.

`ha_verify.py dashboards` exits 1 if any dashboard's config could not be read —
the overview is a health check, so an unreadable dashboard is a finding and not
a footnote.

`ha_verify.py` returns exit code 1 when it finds a problem, and so does any
command whose subject does not exist (`ha_entities.py show sensor.typo`,
`ha_dashboard.py show --url-path does-not-exist`, `ha_verify.py history
sensor.typo`). That makes every check usable in a shell condition, and `--json`
reports the same verdict as the text mode.

The same holds for a filter that matches nothing: `health --filter`,
`entity-ids --pattern`, and `find` / `duplicates` / `disabled` / `areas` with a
pattern all exit 1 and name what they did not find, and so does `ha_ws.py
states --filter`. All of them match across entity id *and* friendly name, so
`health --filter "Calibrate Zero"` finds
`button.schlafzimmer_co2_calibrate_zero`. Zero hits is a real answer, a typo is
not, and only the second one is worth a non-zero exit — so a filter with hits
and no problem still exits 0, and a call without `--filter`/`--pattern` is
unchanged.

Every one of those patterns is matched against the **full entity id including
its domain**, so `duplicates --pattern switch` finds the `switch.*` groups and
`--pattern switch.` finds them too. Matching only the object id made the
domain form miss all of them while a `sensor.*_switch_type` counted as a hit —
seven real groups dropped, exit 0.

`ha_ws.py rest` accepts a path with or without the `/api/` prefix
(`states`, `/states`, `/api/states`); anything not already under `/api/` gets
it added.

Exit code **2** is reserved for a usage error -- an impossible request or a bad
argument, including a refused write without `--allow-write`. It is never a
Home Assistant answer, so a shell condition can tell the two apart. All four
CLIs behave the same way here. That covers `--units` holding valid JSON of the
wrong shape (a string or a list, where a unit map belongs), `--command` without
a `type` string, and an empty side in `--swap old=new`.

Time arguments (`--start`, `--end`, `--since`) accept ISO 8601 with a trailing
`Z` (`2026-11-15T12:00:00Z`), a plain date (`2026-11-15`), `now`, or a span
such as `90d` or `6h`. The `Z` form works on Python 3.10 as well, where
`datetime.fromisoformat` does not know it yet.

## Examples

### Which entity is this, and which device is behind it?

```bash
python3 -I ha_entities.py find growatt
python3 -I ha_entities.py show sensor.growatt_batterie_geladene_kwh
```

### Are my long-term statistics still there?
```bash
python3 -I ha_verify.py stats-range sensor.growatt_batterie_geladene_kwh --since 90d
python3 -I ha_verify.py stats sensor.growatt_batterie_geladene_kwh --start 7d --types change,sum
```

### Which entities does a dashboard reference?

```bash
python3 -I ha_dashboard.py entity-refs --url-path batterie-uebersicht
python3 -I ha_verify.py dashboard-url --url-path batterie-uebersicht
```

### Swap a retired entity in a dashboard, safely

```bash
# 1. dry run -- nothing is written
python3 -I ha_dashboard.py patch --url-path my-dash \
    --swap switch.klima=switch.growatt_steckdose \
    --rename switch.growatt_steckdose="Nexa AC socket" \
    --dry-run --out patched.json

# 2. inspect and validate the result locally
python3 -I ha_dashboard.py validate --file patched.json

# 3. apply, keeping a backup of the live config
python3 -I ha_dashboard.py --backup-dir ~/ha-backups patch --url-path my-dash \
    --swap switch.klima=switch.growatt_steckdose --allow-write --apply
```

`--swap` rewrites every card of the dashboard, so `--card-title` and
`--card-index` (which select the card `--add` appends to) are refused with it
instead of being silently ignored. A `--swap` whose old id appears nowhere, and
a `--rename` whose target never turns up, both name themselves and exit 1: a
patch with one typo among five swaps used to write a half-changed dashboard and
report success.

### Evaluate a template against live data

```bash
# WebSocket subscription (default): the rendered value arrives in an event frame
python3 -I ha_ws.py render --template '{{ states("sensor.x") | float(0) | round(2) }}'

# REST fallback: POST /api/template, no WebSocket needed
python3 -I ha_ws.py render --template '{{ 1 + 1 }}' --transport rest
```

### Which integration failed, and why?

```bash
python3 -I ha_entities.py integrations --only-problems
```

### Which areas exist, and what is in them?

```bash
python3 -I ha_entities.py areas                 # ids, floors, device/entity count
python3 -I ha_entities.py areas --pattern keller
```

### What does the Energy dashboard actually use?

```bash
python3 -I ha_ws.py raw --command '{"type":"energy/get_prefs"}'
```

### Enable entities an integration disabled by default

```bash
python3 -I ha_entities.py disabled --pattern shellyplug     # inspect
python3 -I ha_entities.py enable sensor.x sensor.y --allow-write
```

## Safety model

- **Read-only by default, fail-closed.** `HAConnection.cmd()` checks every
  WebSocket command against a whitelist of known read-only commands. Anything
  not on it -- a known mutating command or one this client has never seen -- is
  refused unless the connection was opened with `--allow-write`. The refusal
  happens locally; the instance is never contacted with that command.
- **`ha_verify.py` can never write.** It opens connections with
  `allow_write=False`, fixed. It exits 1 when it finds a problem, so it works in
  a shell condition.
- **`patch` is a dry run** unless `--apply` is given, and `--backup-dir` writes
  a timestamped copy of the live config before saving.
- **`delete` needs `--yes`** in addition to `--allow-write`. It takes the
  `url_path` and resolves the registry `id` itself: `lovelace/dashboards/delete`
  addresses a dashboard by `id` (`dashboard_licht`), and the two differ for
  every dashboard here.
- **No secrets in the repository.** Credentials come only from the environment
  or a file you name. See "Credentials" above.

Destructive Home Assistant operations this toolkit can reach, and why they need
care: `recorder/purge` and `recorder/adjust_sum_statistics` destroy history that
cannot be recreated; `homeassistant/restart` resets every `input_number` that
has `initial:` set and drops MQTT device connections; `lovelace/config/save`
replaces a dashboard's entire config.

## Tests

```
python3 -m unittest discover -s tests -v          # offline, no Home Assistant needed
HA_LIVE=1 python3 -m unittest discover -s tests   # additionally: read-only smoke tests
```

Details in `tests/README.md`.

## Repository layout

```
SKILL.md                 skill manifest: description, workflow, pitfall index
README.md                this file
CHANGELOG.md             condensed review history (details: git log, prefix 'Runde N:')
tests/                   unittest regression suite (see tests/README.md)
scripts/
  ha_ws.py               WebSocket + REST core, CLI, read-only guard
  ha_verify.py           read-only health checks (entities, statistics, dashboards)
  ha_dashboard.py        Lovelace storage-mode dashboards: inspect, patch, back up
  ha_entities.py         registries: find, inspect, enable/disable, rename
references/
  websocket-api.md       auth, commands, s/lu keys, ms timestamps, null templates
  rest-api.md            endpoints, template rendering, error shapes
  dashboards.md          storage mode, url_path rule, card types, patching
  statistics.md          statistics vs history, state_class, integration pitfalls
  yaml-config.md         reload vs restart, input_number persistence, availability
  troubleshooting.md     symptom -> cause -> check
  environment-notes.md   concrete Growatt / Shelly / EMQX findings
```

## Repository

<https://github.com/marcohald/ha-agent-skill> (private)

## Licence

Personal toolkit, no warranty. Read `references/troubleshooting.md` before
changing anything on a production instance.
