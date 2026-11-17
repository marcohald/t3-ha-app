# YAML configuration: reload vs. restart

Editing YAML is not enough -- Home Assistant has to load it, and *how* it loads
decides whether your change takes effect and whether it costs you data.

## Reload vs. restart

| Change | `reload_all` / YAML reload | Full restart |
| --- | --- | --- |
| `automations.yaml`, `scripts.yaml`, `scenes.yaml` | works | works |
| `configuration.yaml` `template:` sensors (after first load) | works | works |
| New `template:` platform (first time) | **no** | required |
| New `input_number`, `input_boolean`, `input_select`, `input_datetime` | **no** | required |
| New `utility_meter` | **no** | required |
| New `sensor:` platform entries | **no** | required |
| New integration via UI | n/a | not needed |
| `customize`, `homeassistant:` settings | partial | required for some keys |

The rule of thumb: **reload re-renders what already exists; it does not create
new platforms.** A brand-new `utility_meter:` block that "does nothing" after a
reload is not a YAML error -- it simply was never loaded. Confirm before
debugging the YAML: a full restart makes it appear, and that is not a fix, that
is the definition.

A restart is not free. It disconnects MQTT-based devices until they reconnect,
re-runs `initial:` values (below), and briefly makes every entity
`unavailable` -- which is recorded and pollutes statistics. Do it once,
deliberately, not as a debugging loop.

## `input_number` persistence

```yaml
input_number:
  my_baseline:
    name: Baseline
    min: 0
    max: 1000
    step: 0.001
    initial: 0        # <-- do NOT set this for a persistent baseline
```

With `initial:` present, the value is reset on **every** restart. The helper
itself is persistent (`restore_state`), so without `initial:` it keeps its last
value across restarts, and only a fresh install starts at `min`.

Consequences:

- A baseline date or a "since" zero-point stored as `input_number` with
  `initial: 0` silently jumps back to 0 at every restart. Any downstream
  template sensor recomputes a wrong delta, and nothing logs a warning.
- Omitting `initial:` is the fix. Do not "fix" it with an automation that
  restores the value on startup -- that is the same bug with extra steps.
- Changing `min`/`max` after values exist does not clamp stored values; a value
  outside the new range stays until the next write.

State: `input_number.set_value` is a **mutating** service call. `ha_ws.py`
refuses it without `--allow-write` for exactly this reason.

## Template sensors need `availability`

A template sensor without an availability guard evaluates against
`states('sensor.missing') == 'unknown'`, and Jinja arithmetic turns that into a
**wrong 0** rather than an error:

```yaml
# BAD: reports 0 W when the inverter is offline
template:
  - sensor:
      - name: Inverter output
        state: "{{ states('sensor.inverter_power') | float }}"

# GOOD: reports 'unavailable'
template:
  - sensor:
      - name: Inverter output
        availability: "{{ states('sensor.inverter_power') not in ['unknown','unavailable'] }}"
        state: "{{ states('sensor.inverter_power') | float }}"
```

The wrong 0 is the dangerous part: it flows into statistics, gauges and the
energy dashboard as a *real measurement*, dragging averages down and, for a
`total_increasing` counter, producing a bogus reset.

Use `float(0)` only when 0 is genuinely the right answer for a missing source,
and say so in a comment.

## Entity id collisions

Adding a sensor whose object id already exists does not overwrite the old
entity. The registry keeps both, the new one becomes `..._2`, and anything
pointing at the old id (dashboards, automations, energy config) keeps using the
old entity -- which may be stale or dead.

Detect: `ha_entities.py duplicates --pattern <name>` lists `_2` style groups.
Fix: rename the new entity explicitly to the id you want *after* removing the
old one, then re-check `ha_verify.py dashboard-url` for every dashboard.

## Validating YAML without breaking anything

- `ha_ws.py raw --command '{"type":"validate_config"}'` -- server-side config
  check, read-only.
- Copy the YAML block into a scratch file and parse it locally with
  `python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" file.yaml`
  (`PyYAML` is available in most HA environments).
- Never test a config change by restarting a production instance to see whether
  it validates. A syntax error there leaves the instance refusing to start.

## Configuration reload over the API

`config_entries/reload` reloads **one** integration's config entry. It is
mutating and briefly makes that integration's entities unavailable. It is the
right tool after changing integration options, not a substitute for
`reload_all`.

For a supervised HA OS install, the YAML lives under `/config` (often mapped
from `/homeassistant` on the host). Editing those files from outside the
container is fine, but the change is still inert until a reload or restart --
see the table at the top.
