# Troubleshooting: symptom to cause

Start with `ha_ws.py ping`. If that fails, nothing else will work and the
problem is transport or auth, not Home Assistant's contents.

## Connection and auth

| Symptom | Cause | Fix |
| --- | --- | --- |
| `cannot connect to ws://...` | wrong host/port; HA not running | check `HA_URL`; try `ha_ws.py rest /api/` |
| `auth_invalid` | token revoked, mistyped, or from another instance | new long-lived token in the profile page |
| `no access token found` | neither `$HA_TOKEN` nor `/tmp/ha.tok` | `export HA_TOKEN=...` or `--token-file` |
| hangs with no output | command sent, reply never arrives | lower `--timeout`; the socket is up but HA is busy |
| `1009 message too big` | default 1 MiB WebSocket limit | use `ha_ws.py` (64 MiB); do not hand-roll the client |
| `404: Not Found` on `/api/error_log` | endpoint unavailable on HA OS | read the log file instead |

## Data looks wrong

| Symptom | Cause | Check |
| --- | --- | --- |
| `KeyError: 'state'` on history rows | `minimal_response` renamed keys to `s`/`lu` | `websocket-api.md` |
| `year 58567 is out of range` | statistic `start` treated as seconds | it is **milliseconds** |
| statistic missing from the reply | wrong id, or no rows in the window | both return silently -- check for absence |
| entity "does not exist" but the UI shows it | typo, or the entity is disabled | `ha_entities.py show <id>` |
| `history` shows 0 points for an entity | the id is unknown (typo) -- indistinguishable from a quiet window | `history` now exits 1 and names it; `--no-exists-check` disables that |
| `unknown_command` on a valid-looking type | command name is wrong or too new | `ha_ws.py commands` lists what this client knows |
| `render_template` returns `null` | you read only the ACK frame -- the value is in the following `event` frame | use `ha_ws.py render` (handles both), or `--transport rest` |
| state is `unknown` after a restart | source integration not back yet | `ha_verify.py health --filter <name>` |
| a `button`/`event`/`scene` is `unknown` | normal until it is used once -- not a fault | `health --include-unused` lists them too |
| `health` reports fewer entities than expected | `unknown` in button/event/scene is not counted | `--include-unused` |

## Entities

| Symptom | Cause | Check |
| --- | --- | --- |
| a sensor exists but never updates | integration disabled the entity | `ha_entities.py disabled --pattern <name>` |
| duplicate `_2` ids | entity from an older config still registered | `ha_entities.py duplicates` |
| dashboard card is empty | entity referenced no longer exists | `ha_verify.py dashboard-url --url-path X` |
| no statistics, but the state is fine | missing `state_class` | `recorder/list_statistic_ids` |
| entity has a state but no registry entry | YAML-defined, not in the registry | expected for YAML `template:` sensors |
| area shows as empty | entity has no `area_id` and no device area | `ha_entities.py show` |
| `orphans` lists hundreds of entries | disabled entities have no state by design | `orphans` filters `disabled_by`; use `disabled` to list them |
| `orphans` names 100+ entries as unnamed | the name comes from the device | only entries with no name anywhere are reported |

## Dashboards

| Symptom | Cause | Check |
| --- | --- | --- |
| `invalid_format` on create | `url_path` without a hyphen | `my-dash`, not `my_dash` |
| chart empty, no error | statistic card on a non-statistic entity | needs `state_class` + statistics |
| chart empty after a rename | the old entity id is still referenced | `ha_dashboard.py entity-refs` |
| `statistic` card blank | `stat_type` typo (singular on `statistic`) | `statistics-graph` uses `stat_types` (list) |
| whole dashboard changed unexpectedly | `save` sent a partial config | restore from `--backup-dir` |
| dashboard absent from the sidebar | `show_in_sidebar: false` | `ha_dashboard.py list` |
| a view reports `cards=0` but is not empty | sections view: cards live in `view.sections[].cards` | both scripts read `sections` too |
| `show` prints `title: None`, `validate` complains about a title | the title is in the dashboard registry, not the config blob | `ha_dashboard.py list` shows the real title |
| `config_not_found` for the default dashboard | auto-generated dashboards have no stored config | switch it to storage mode, or inspect a named dashboard |

## Configuration

| Symptom | Cause | Check |
| --- | --- | --- |
| new YAML platform ignored after reload | reload does not load new platforms | needs a restart (`yaml-config.md`) |
| `input_number` back at 0 after a restart | `initial:` set | remove `initial:` |
| template sensor reports 0 while source is offline | no `availability` guard | add the guard |
| automation ran with `unknown` values | conditions not guarded against unavailable sources | add a `not in ['unknown','unavailable']` condition |

## Statistics and energy

| Symptom | Cause | Check |
| --- | --- | --- |
| efficiency over 100 % | power register integrated naively | use the integral counters (`statistics.md`) |
| daily efficiency wild (3 %..204 %) | window too short | aggregate >= 30 days |
| history lost after a rename | a renamed entity starts a new statistic series | the old id keeps the old rows |
| energy dashboard flat | `total_increasing` counter went backwards | treat as a meter reset |
| statistic series has a step | meter replaced; `sum` not adjusted | `recorder/adjust_sum_statistics` -- with care |

## Diagnostic commands to run first

```bash
ha_ws.py ping                                  # transport + auth + version
ha_ws.py rest /api/config                      # which integrations are loaded
ha_verify.py health                            # what is unknown/unavailable
ha_entities.py stats                           # counts: entities, devices, disabled
ha_verify.py dashboards                        # which dashboards reference what
```

## When not to touch the instance

If the user's question is "why is this number wrong", the answer is usually in
the API data, not in a config change. Read first: `ha_verify.py states`,
`stats-range`, `history`. Propose a fix and let the user apply it. A production
energy install with months of statistics has no cheap recovery from
`recorder/purge`, `adjust_sum_statistics`, or a restart that resets an
`input_number` baseline.
