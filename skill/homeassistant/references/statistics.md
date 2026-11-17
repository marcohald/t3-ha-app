# Long-term statistics vs. history

Home Assistant keeps two different records, and confusing them is the single
most common source of wrong energy numbers.

| | History | Long-term statistics |
| --- | --- | --- |
| Stores | every state change (recorder, default 10 days) | hourly aggregates, kept indefinitely |
| Command | `history/history_during_period` | `recorder/statistics_during_period` |
| Value types | raw states | `change`, `sum`, `mean`, `min`, `max`, `state`, `last_reset` |
| Time unit | seconds (float epoch) | **milliseconds** (integer) |
| Requires | recorder | `state_class` on the entity (`measurement`, `total`, `total_increasing`) |
| Used by | history graphs, logbook | energy dashboard, `statistic` / `statistics-graph` cards |

History is for "what happened at 14:32". Statistics are for "how much this
month". A sensor with no `state_class` produces **no statistics at all** and
therefore cannot appear in an energy dashboard, no matter how correct its value
is.

## Reading statistics

```python
result = await ha.cmd(
    type="recorder/statistics_during_period",
    start_time="2026-11-01T00:00:00+00:00",
    end_time="2026-11-16T00:00:00+00:00",
    statistic_ids=["sensor.growatt_batterie_geladene_kwh"],
    period="day",                       # 5minute|hour|day|week|month
    types=["change", "sum", "mean"],
)
rows = result["sensor.growatt_batterie_geladene_kwh"]
```

A row:

```json
{"start": 1793487600000, "end": 1793574000000, "last_reset": null,
 "state": 78.261, "sum": 78.261, "change": 0.496}
```

Key facts:

- `start` and `end` are **milliseconds**. `datetime.fromtimestamp(start)`
  raises `year 58567 is out of range`. Use the guard in `websocket-api.md`.
- The reply is a dict keyed by `statistic_id`. A statistic id that does not
  exist, or has no rows in the window, produces a **missing key** or an empty
  list -- never an error. Always check for absence explicitly.
- `sum` is the cumulative total at the end of the bucket; `change` is the
  delta *within* the bucket. For an energy counter, summing `change` over a
  month reproduces the difference in `sum` -- use that as a self-check.
- Not every statistic has every type. A `measurement` (temperature, power) has
  `mean`, `min`, `max` but no `sum`; a `total_increasing` counter has `sum`
  and `change` but no `mean`. Ask for a type the statistic does not have and it
  comes back absent, not zero.
- `recorder/list_statistic_ids` reports `has_sum` and `has_mean` per id, and
  `recorder/get_statistics_metadata` reports the unit and `mean_type`. Check
  these before asserting anything about what a number means.

## `state_class` in one paragraph

- `measurement` -- instantaneous value (power, temperature). Aggregated to
  mean/min/max. **No `sum`**, so it cannot be an energy source directly.
- `total` -- monotonically increasing per meter but may reset. Needs a
  `last_reset` for rate calculation.
- `total_increasing` -- can only go up (lifetime energy). Home Assistant derives
  consumption from the difference; a counter that goes *down* is treated as a
  reset and injects a bogus spike.

A sensor whose `state_class` changed after the fact keeps the old statistics;
the recorder does not rewrite history. Expect a mixed series after such a change
and read it bucket by bucket.

## Pitfall: integrating raw power registers

`change`/`sum` on a proper integral counter is trustworthy. Reconstructing
energy by integrating a power sensor is **not**, and the error is large:

- The recorder stores only *changes*. A constant value is stored once, and an
  integrator that clamps intervals to a few minutes silently drops every long
  constant phase -- losing real energy.
- Idle sensors carry a small offset (a few watts). Over months that becomes tens
  of phantom kWh.
- The result can exceed 100 % efficiency. In one real install, naive
  integration of `charging_power` produced ~130 % round-trip efficiency, which
  is physically impossible.

Rule: integrate a power register only with a proper `integration` platform
sensor that Home Assistant itself maintains, and sanity-check the result against
the installation's own counters. If the two disagree by more than a few percent,
trust the counter.

## Sampling noise in short windows

Daily efficiency figures from one installation ranged from **3 % to 204 %**,
while the 60-day total was a stable 67 %. A battery's efficiency is not
meaningful over a single day: the charge and discharge phases do not align, and
small denominators amplify small errors.

Always aggregate over **at least 30 days** before making a claim, and quote the
window. A single day's number is noise, not a measurement.

## `recorder/adjust_sum_statistics` -- the dangerous one

This command rewrites the `sum` of an existing statistic. There is no undo. It
is the correct tool when a meter was replaced (so the old cumulative total must
be offset to keep the series continuous) and the wrong tool for "the number
looks wrong to me".

Before using it: export both the statistic's metadata and the affected rows,
record the old value, and confirm which physical event justifies the change.
`recorder/purge` is worse -- it deletes rows outright, and a purged statistic
cannot be reconstructed from history because the raw states are gone too.

Both are on the mutating list in `ha_ws.py` and need `--allow-write`.

## Verifying statistics survived a change

```bash
ha_verify.py stats-range sensor.a sensor.b --since 90d
```

prints the first and last bucket plus `rows` and `last_sum` per statistic. If
`rows` dropped to a handful after a config change, the entity lost its
`state_class`, or the statistic id changed. A rename through the **entity
registry** (`ha_entities.py rename`, or the UI) does not start a new series:
Home Assistant migrates `statistics_meta` to the new `entity_id`, so history
stays continuous under the new id. A *new* series appears when a sensor is
deleted and recreated, or when it lands under a different id (`..._2`). The
Energy preferences are **not** updated by a rename and keep the old id.

`ha_verify.py stats` prints the actual buckets for a window and flags a
statistic with no rows at all.

## Reading the Energy dashboard's own configuration

`energy/get_prefs` returns the exact statistics the Energy dashboard is built
from -- which sensor is solar, battery or grid, which is the cost source, and
the `stat_rate` power sensors. Read it before guessing at an efficiency figure:

```bash
ha_ws.py raw --command '{"type":"energy/get_prefs"}'
```

That is the ground truth for "which counter does the dashboard actually use".
If a `sensor.*_kwh` you were about to compute with does not appear here, the
dashboard is not using it either.
