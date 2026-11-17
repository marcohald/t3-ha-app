# Lovelace dashboards

A dashboard is either **storage mode** (edited in the UI, stored in
`/config/.storage/lovelace.<url_path>`) or **YAML mode** (defined in
`configuration.yaml`, `mode: yaml`). The WebSocket API only manages storage-mode
dashboards; a YAML dashboard must be edited as YAML and then reloaded.

`ha_dashboard.py` handles storage mode. Check the mode first:
`ha_dashboard.py list` prints it per dashboard.

## The url_path rule

`lovelace/dashboards/create` validates `url_path` and rejects anything that is
not lowercase words joined by hyphens:

```
my-dash     ok
my_dash     invalid_format
mydash      invalid_format
MyDash      invalid_format
```

The failure is a WebSocket `success: false` with
`error.code == "invalid_format"`, which looks like a schema problem elsewhere.
`ha_dashboard.py create` checks the pattern locally and explains the rule
before contacting the instance.

The `title` is free text and may contain spaces and umlauts. `url_path` is what
appears in the URL (`/my-dash/0`); the dashboard's `title` is only a label.

## Data model

```json
{
  "title": "Batterie",
  "views": [
    {
      "title": "Übersicht",
      "path": "uebersicht",
      "icon": "mdi:battery-sync",
      "cards": [ ... ]
    }
  ]
}
```

Views may also use `sections` (a list of column containers) instead of `cards`;
both are valid and a view normally has one or the other.

Common card types:

| `type` | Purpose | Entity keys |
| --- | --- | --- |
| `entities` | plain list | `entities: [id, {entity, name}, ...]` |
| `tile` | one prominent entity with features | `entity`, `features: [{type: "trend-graph", entity}]` |
| `gauge` | needle gauge | `entity`, `min`, `max`, `severity` |
| `statistic` | a statistic over a period | `entity`, `stat_type: change`, `period: {calendar: {period: day}}` |
| `statistics-graph` | bar/line chart of statistics | `entities`, `days_to_show`, `stat_types: [change]` |
| `grid` | nested layout | `columns`, `square`, `cards: [...]` |
| `heading` | section header | `heading`, `icon` |
| `markdown` | text | `text` |
| `history-graph` | state history chart | `entities`, `hours_to_show` |

`statistic` and `statistics-graph` read **long-term statistics**, so their
entity must have a `state_class` and statistics recorded. An `input_number` will
not work there -- it has no statistics. See `statistics.md`.

## Inspecting a dashboard without the UI

```bash
ha_dashboard.py list
ha_dashboard.py show --url-path my-dash          # structural overview
ha_dashboard.py cards --url-path my-dash         # every card and its entities
ha_dashboard.py entity-refs --url-path my-dash   # flat list of referenced entities
ha_dashboard.py export --url-path my-dash --out dash.json
ha_dashboard.py validate --file dash.json        # offline, no instance needed
```

`entity-refs` and `validate` are the pair to run after any rename: a dashboard
does not warn about a missing entity, it renders an empty card. The card simply
disappears, which users report as "the dashboard is broken".

Cross-check against the state machine:

```bash
ha_verify.py dashboard-url --url-path my-dash
```

## Patching: dry run first

```bash
# 1. what would change?
ha_dashboard.py patch --url-path my-dash \
    --swap switch.old_socket=switch.new_socket \
    --rename switch.new_socket="Nexa AC socket" \
    --dry-run

# 2. inspect the result as a file
ha_dashboard.py patch ... --dry-run --out patched.json
ha_dashboard.py validate --file patched.json

# 3. write it, with a backup
ha_dashboard.py --backup-dir ~/backups/dashboards patch --url-path my-dash \
    --swap switch.old_socket=switch.new_socket --allow-write --apply
```

`patch` rewrites three places: the `entity` key of a card, bare strings inside
an `entities` list, and entity ids embedded in text (markdown templates). It
leaves the rest of the tree byte-identical, so the saved diff stays reviewable.

Two selector rules that were the source of silent no-ops:

- `--card-title` / `--card-index` choose the card `--add` appends to. They are
  alternatives, not a pair, and they are **refused with `--swap`**: a swap
  rewrites every card of the dashboard, so accepting a selector that the swap
  path never reads made a dashboard-wide replacement look scoped to one card.
- `--rename` needs somewhere to put the name. On a pure-string `entities` list
  the affected row is rewritten to `{"entity": ..., "name": ...}` (valid
  Lovelace); on a card with a single `entity` the `name` key is set. Naming an
  id that is neither a swap target nor an entity `--add` actually appended is
  reported and exits 1 -- including an `--add` of an id the card already
  holds, which is skipped and would otherwise swallow its `--rename`.
- Both halves of `old=new` must be non-empty. An empty left side is an
  unbounded edit: the word-boundary match then fires at every punctuation
  mark, so `--swap =b` rewrote 99 places in one dashboard and still exited 0.
  An empty right side swapped an entity for `""`.

`--out FILE` is written even when nothing changed (`changed: False`), so the
`patch --out` → `validate --file` workflow always has a file to check.

`patch` also exits 1 and writes nothing when a `--swap` old id appears nowhere
in the config. `swaps: 0` alone was not enough: with five pairs, one typo
produced a half-changed dashboard and a success-looking exit.

`--add ENTITY --card-title "..."` appends a row to a card. If the target card's
`entities` list mixes plain strings with option objects, the list is normalised
to objects so a per-row `name` is possible; a pure-string list is left as is.

## Backups are not optional

`--backup-dir DIR` writes `lovelace.<url_path>.<timestamp>.json` before the
first save. Restoring is a file copy back through
`ha_dashboard.py save --url-path X --file <backup>`. Keep them outside the
dashboard's own directory.

A `save` replaces the whole config. There is no merge and no undo in the UI
beyond the browser's own history. Export before any manual `save`.

## Traps

- **Entity ids drift.** A renamed device does not rename its entities; a
  re-added integration creates `_2` ids. Dashboards keep pointing at the old
  id and the card silently empties. `ha_entities.py duplicates` finds these.
- **`switch.klima` style legacy ids.** After replacing hardware, the old entity
  may linger for months. Swap every reference deliberately rather than adding
  the new one beside it.
- **`stat_type` naming.** It is `stat_type` on a `statistic` card and
  `stat_types` (a list) on a `statistics-graph` card. Mixing them gives an
  empty chart with no error.
- **Restarting is not needed** for a storage dashboard save -- the UI picks it
  up immediately. YAML dashboards need a reload.
- **`lovelace/config/save` needs the whole config.** Sending a partial object
  deletes everything you left out.
- **`id` and `url_path` are different fields.** `create` and `lovelace/config`
  address a dashboard by `url_path` (`dashboard-licht`); `dashboards/update`
  and `dashboards/delete` address it by the registry `id`
  (`dashboard_licht`). Sending the wrong one fails with
  `invalid_format: not a valid option at 'url_path'`. Both differ for every
  dashboard on a storage-mode install -- measured across all 12 here.
  `ha_dashboard.py delete` resolves the id itself via
  `lovelace/dashboards/list` and prints the known `url_path`s when the lookup
  misses.
- **Only three card types render an `entities` list:** `entities`,
  `history-graph`, `statistics-graph`. A `heading`, `tile` or `button` silently
  ignores an `entities` key you add to it, so a patch reports success on a card
  that never shows the entity. `patch --add` refuses those card types for that
  reason, and `validate` flags an `entities` list on one.

## Views: `cards` is not the only place cards live

Modern dashboards use the **sections** view type, and those keep their cards
one level deeper:

```jsonc
{
  "views": [
    { "title": "Overview", "sections": [ { "type": "grid", "cards": [ ... ] } ] }
  ]
}
```

Measured on a real install: **22 of 38 views had `sections` and an empty
`cards`**. A tool that only reads `view.cards` reports "0 cards" for those views
and finds no entities in them either, so `entity-refs` silently misses almost
everything. Both scripts here walk `cards` *and* `sections[].cards`.

## The default dashboard

The default dashboard (`/lovelace`) has **no `url_path`**. Omitting `url_path`
from `lovelace/config` is how you address it. In the usual "auto-generated"
mode there is no stored config at all and Home Assistant answers
`config_not_found`; in storage or YAML mode it returns real content. The
read-only subcommands of `ha_dashboard.py` therefore take `--url-path` as
*optional* -- without it they inspect the default dashboard, and a
`config_not_found` is explained rather than shown as a bare error code.

## A dashboard's title is not in its config

`title` in the config blob is optional and usually absent; the title shown in
the sidebar comes from the dashboard **registry entry** (`lovelace/dashboards/list`).
7 of 12 dashboards on the reference install had `title: None` in the config and
a valid title in the registry. Treating a missing config title as a structural
problem produces a false alarm on perfectly healthy dashboards.
