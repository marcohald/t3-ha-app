# Home Assistant REST API

Base: `http(s)://<host>:8123/api/...`
Header on every request: `Authorization: Bearer <long-lived token>`.

`ha_ws.py rest <path>` performs read-only GETs; `ha_ws.py render` uses the one
POST endpoint that is genuinely useful for reading.

The path is normalised, so `states`, `/states`, `/api/states` and `/api/`
all address the same endpoint — everything that does not already start with
`/api/` gets that prefix. Without this, `/states` went out unprefixed and came
back as an unexplained `404 Not Found` while `states` worked.

## Endpoints

| Method + path | Returns |
| --- | --- |
| `GET /api/` | `{"message": "API running."}` -- liveness check without auth |
| `GET /api/config` | version, location, timezone, units, `components` |
| `GET /api/states` | every state (large; ~0.5 MB here) |
| `GET /api/states/<entity_id>` | one state; `404` with `{"message": "Entity not found."}` if absent |
| `POST /api/template` | renders Jinja; body `{"template": "..."}` |
| `GET /api/history/period/<iso>` | history; `?end_time=&filter_entity_id=` -- the only endpoint with **spelled-out** keys |
| `GET /api/logbook/<iso>` | logbook entries |
| `GET /api/error_log` | plain-text log -- **often 404 on HA OS** (no `api` panel access); use the log file instead |
| `GET /api/calendars/<entity_id>` | calendar events |
| `POST /api/services/<domain>/<service>` | calls a service (mutating) |
| `POST /api/states/<entity_id>` | sets a state (does not change the device) |

`GET /api/states/<entity_id>` is the cheapest health probe:
no WebSocket, no state dump, and a missing entity is an unambiguous 404.

## Templates: two transports, different shapes

```bash
ha_ws.py render --template '{{ states("sensor.x") | float(0) | round(2) }}'
```

- `--transport ws` (default) uses the WebSocket `render_template`
  **subscription**: the result ACK is empty and the value arrives in a following
  `event` frame. `HAConnection.cmd_subscribe()` handles that, then unsubscribes.
- `--transport rest` uses `POST /api/template`, a single synchronous reply. It
  is the simpler option and works without any WebSocket -- and its errors are
  better: a broken template gets an HTTP 400 with the Jinja message, whereas
  the WebSocket variant sends no event at all and only looks like a timeout.
  `ha_ws.py render` falls back to REST automatically in exactly that case.

Either way you get the rendered text, not a state object. That is the point:
templates let you ask a *question* of live data ("what is the sum of these three
sensors right now?") without creating a sensor.

`POST /api/template` is a POST, which is confusing for a read. It has no side
effects; `ha_ws.py` allows it from the `render` subcommand and refuses it
through `rest` without `--allow-write`, to keep the guard honest.

Templates are the right tool when you need a *derived* value once -- sum a few
sensors, format a date, resolve a state. For repeated use, a template sensor in
YAML is better, but it needs a reload/restart and an `availability` guard.

## When to prefer which API

| Need | Use |
| --- | --- |
| One or two states | REST `GET /api/states/<id>` |
| Many states, one connection | WebSocket `get_states` |
| Registries (entities, devices, areas) | WebSocket only -- no REST equivalent |
| Dashboard config | WebSocket `lovelace/config` |
| Long-term statistics | WebSocket `recorder/statistics_during_period` |
| History | WebSocket `history/history_during_period` (dict per entity) or REST `/api/history/period` (list of lists) |
| Template evaluation | REST `POST /api/template` |
| Service call | WebSocket `call_service` or REST `POST /api/services/...` |

## Error shapes

REST returns HTTP status codes with a JSON body:

- `401` -- token missing, malformed or revoked. Message: `{"message": "Invalid access token or password"}`.
- `404` -- wrong path or unknown entity.
- `400` -- bad JSON body or unrenderable template.
- `405` -- wrong method.

WebSocket returns `{"success": false, "error": {"code", "message"}}` inside a
`200`-equivalent frame. A helper that only looks at the HTTP status misses every
WebSocket failure, and vice versa. `ha_ws.py` raises `HAError` for the
WebSocket case and `ConnectError` for the REST case, both printed with the
failing command name.

## Long-lived tokens

Created in the UI under profile -> *Long-lived access tokens*. They are
instance-wide and do not expire; treat one like a password.

Never commit one. `ha_ws.py` reads, in order: `--token-file`,
`$HA_TOKEN_FILE`, `$HA_TOKEN`, then `/tmp/ha.tok` **only if it already
exists**. Errors and output are run through a redactor that masks anything
looking like a JWT or a long hex string, the token this process actually
resolved, userinfo in a URL (`user:se@cret@host` included — a password may
contain `@`), and a credential in a well-known query parameter such as
`?access_token=...`. So neither an exception traceback nor `ping`'s URL echo
can leak the token into a log.
