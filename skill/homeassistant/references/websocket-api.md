# Home Assistant WebSocket API

Endpoint: `ws://<host>:8123/api/websocket` (or `wss://` behind TLS).
`ha_ws.py` derives it from `HA_URL` unless `--ws-url` / `$HA_WS_URL` overrides it.

## Handshake

Three frames, in order:

1. Server sends `{"type": "auth_required", "ha_version": "..."}`.
2. Client sends `{"type": "auth", "access_token": "<long-lived token>"}`.
3. Server answers `{"type": "auth_ok", "ha_version": "..."}` or
   `{"type": "auth_invalid", "message": "..."}`.

An invalid token gives `auth_invalid` and the socket is closed. It is **not** an
HTTP 401, so a generic HTTP client shows it as a protocol error.

After auth, every command carries a client-chosen integer `id`:

```json
{"id": 1, "type": "get_states"}
```

The reply is matched by `id` **and** `type == "result"`:

```json
{"id": 1, "type": "result", "success": true, "result": [...]}
{"id": 1, "type": "result", "success": false,
 "error": {"code": "unknown_command", "message": "Unknown command."}}
```

Other `type` values arrive on the same socket: `event` (subscriptions),
`pong`, and unsolicited messages. A client that assumes "next frame is my
answer" breaks as soon as a subscription is active -- always loop until the
matching `id` shows up.

## Commands worth knowing

Read-only, safe to call on a production instance:

| Command | Returns |
| --- | --- |
| `get_config` | version, timezone, unit system, component list |
| `get_states` | every state (~1 MB in a mid-size install) |
| `get_services` | every service, grouped by domain |
| `get_panels` | the configured panels |
| `config/entity_registry/list` | entity entries incl. `disabled_by`, `hidden_by`, `device_id` |
| `config/device_registry/list` | device entries incl. `identifiers`, `via_device_id` |
| `config/area_registry/list` | areas (keyed `area_id`, **not** `id`) |
| `config/label_registry/list`, `config/floor_registry/list` | labels, floors |
| `lovelace/dashboards/list` | storage-mode dashboards |
| `lovelace/config` (`url_path`) | one dashboard's config |
| `history/history_during_period` | state history |
| `recorder/list_statistic_ids` | statistic ids incl. `has_sum`, `unit_class` |
| `recorder/statistics_during_period` | long-term statistics |
| `recorder/get_statistics_metadata` | unit and `has_mean` per statistic |
| `search/related` | what references an entity |
| `energy/get_prefs` | the Energy dashboard's configured sources and stats |
| `trace/list`, `trace/get` | automation traces |

Mutating, needs `--allow-write`:

`call_service`, `config/entity_registry/update`, `config/entity_registry/remove`,
`config/device_registry/update`, `config/area_registry/*`,
`lovelace/dashboards/create|update|delete`, `lovelace/config/save`,
`lovelace/config/delete`, `recorder/purge`, `recorder/adjust_sum_statistics`,
`homeassistant/restart`, `homeassistant/stop`.

`ha_ws.py raw --command '{"type":"..."}'` sends anything; the read-only guard
in `ha_ws.py` classifies by name and refuses unknown commands.

## Pitfall 1: history rows use short keys

`history/history_during_period` over the **WebSocket** always returns compact
keys, for both values of `minimal_response` (verified on HA 2026.11: `true` and
`false` both give `['s', 'a', 'lu']`):

```json
{"s": "23.5", "lu": 1794741377.03, "a": {"unit_of_measurement": "kWh"}}
```

- `s` is the state; the long key `state` **never** appears here.
- `lu` is `last_updated` as a float epoch in **seconds**.
- `a` carries attributes, present on the first row and whenever they change;
  absent with `no_attributes: true`.
- `c` appears only when `last_changed != last_updated`.

The spelled-out keys (`entity_id`, `state`, `attributes`, `last_changed`,
`last_updated`) come from the **REST** endpoint
`GET /api/history/period/<timestamp>`, which returns a list of lists instead of
a dict. Do not assume the WebSocket variant honours `minimal_response`; it does
not change the key shape.

`ha_verify.py history` reads `s` first and falls back to `state`, so it handles
both transports.

The WebSocket return value is a **dict keyed by entity_id**:

```json
{"sensor.x": [ ... ], "sensor.y": [ ... ]}
```

An entity that does not exist simply gets no key -- not an error, not an empty
list. Code that does `result[0]` fails on a typo'd entity id and looks like a
permissions problem.

## Pitfall 2: millisecond timestamps

`recorder/statistics_during_period` returns `start` and `end` as integer
**milliseconds** since the epoch (`1793487600000`). `datetime.fromtimestamp`
on that number raises `ValueError: year 58567 is out of range`. Divide by 1000
before converting, and accept both units defensively:

```python
def epoch_to_datetime(value):
    number = float(value)
    if number > 1e11:      # milliseconds
        number /= 1000.0
    return datetime.fromtimestamp(number, tz=timezone.utc)
```

`history/history_during_period` uses **seconds** in the same install. The two
APIs disagree; do not share a converter without the `1e11` guard.

## Pitfall 3: `render_template` is a subscription, not a query

`render_template` over WebSocket is a **subscribe** command. The first frame is
an empty ACK and the rendered text arrives in a separate `event` frame with the
same `id`:

```json
{"id": 1, "type": "result", "success": true, "result": null}
{"id": 1, "type": "event",
 "event": {"result": 2, "listeners": {"all": false, "entities": [], "domains": [], "time": false}}}
```

A client that reads only the `result` frame sees `null` and wrongly concludes
the command is broken. Home Assistant is not at fault -- the event frame was
discarded. `HAConnection.cmd()` skips `event` frames by design (they belong to
subscriptions), so it returns `null` here; `HAConnection.cmd_subscribe()` waits
for the event, unsubscribes, and returns the payload.

`ha_ws.py render` uses that path by default and prints the rendered text:

```bash
ha_ws.py render --template '{{ 1 + 1 }}'                  # 2, via WebSocket
ha_ws.py render --template '{{ 1 + 1 }}' --transport rest # 2, via REST
```

`--transport rest` uses `POST /api/template`, a plain synchronous reply that
needs no WebSocket at all. Prefer it behind a proxy that forwards HTTP but not
the WS upgrade, and for one-shot reads where a subscription adds nothing.

After a subscription the socket keeps streaming that template's updates until
it is unsubscribed. `cmd_subscribe()` always sends `unsubscribe_events` (in a
`finally`, so a timeout cannot leak the subscription either), and it waits for
both the ACK and the event instead of assuming the ACK comes first.

**A template that raises sends no event at all.** The ACK arrives, then silence:
no error frame, no event. The only symptom is the timeout, which is why
`ha_ws.py render` catches that case and re-asks `POST /api/template`, whose
HTTP 400 carries the actual Jinja error (`ZeroDivisionError: division by zero`).
If you write your own client, do the same -- "timeout" is the wrong message for
a broken template.

## Message size

`get_states` and large history requests exceed the default `websockets`
max message size (1 MiB) and the connection dies with `1009 message too big`.
`ha_ws.py` connects with `max_size=64 MiB`. Keep that when writing new clients.

## Timeouts

A command with no reply hangs forever on a healthy-looking socket. Wrap
`recv()` in `asyncio.wait_for`; the scripts use `--timeout` (default 30 s) for
both connect and every receive.
