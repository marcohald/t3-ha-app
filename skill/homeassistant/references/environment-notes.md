# Environment notes: a real Growatt / Shelly / EMQX installation

These are findings from one concrete Home Assistant instance (HA OS,
`172.30.32.1:8123`) that produced reusable general lessons. They are recorded
because the *shape* of each problem recurs, not because the entity ids belong in
a skill. Replace the ids with your own.

## Topology

- **Growatt Nexa 2000 inverter** (data logger `0HVRE0ED25DT005M`,
  IP 192.168.200.64) talks MQTT. Entities are
  `sensor.growatt_0hvre0ed25dt005m_*` and go `unknown` when the logger drops.
- **Growatt socket = Shelly Plug M Gen3** (IP 192.168.200.40, MAC
  `0892725A753C`), sitting between grid and inverter. It measures the
  inverter's **AC output**, not a consumer: negative means feed-in. Entity
  `sensor.growatt_steckdose_leistung` keeps the sign.
- **EMQX** add-on `a0d7b954_emqx`, dashboard on `:18083`.
- The agent add-on has no `docker_api`; Docker and `emqx ctl` are reachable only
  through a separate SSH add-on, using an SSH key that lives outside this
  repository. No key material, key path or credential is stored here.

## Lesson 1: a plug measuring a generator, not a load

`Hausverbrauch = netz - plug` holds because the plug sits on the inverter's AC
side. With feed-in, `plug` is negative and the expression still works:
`Hausverbrauch = netz + abs(plug)`.

Two candidate formulas were compared against the inverter's own house-load
register over 2 911 points:

- `netz - plug` -- MAE **52 W**
- `netz` alone -- **90 W**
- double-counting variants -- **100 W**

The lesson generalises: identify which side of a device a meter sits on before
combining it with another meter. A sign that flips between consumption and
production silently corrupts every derived total, and the error is small enough
that a single day's graph still looks plausible.

Also verified: `output_power == charging_power - pv_total_power` with
MAE 13.5 W, and the plug follows `output_power`. Relations like this are worth
testing explicitly -- they confirm a sensor's meaning rather than assuming it
from the name.

## Lesson 2: which energy counters are trustworthy

For battery round-trip efficiency, the correct source is the **integral
counters**, the same ones the Energy dashboard uses:

- `sensor.growatt_batterie_geladene_kwh` (from `charging_power`)
- `sensor.growatt_batterie_entladene_kwh` (from `output_power`)

Round trip over the installation: **83.2 kWh charged / 56.0 kWh discharged
= ~67 %**.

Not trustworthy:

1. `sensor.growatt_0hvre0ed25dt005m_nexa_batterie_entladene_energie` is
   **negative** (`-124 kWh`) with `state_class: total`. Ratios built on it are
   meaningless.
2. Naively integrating `charging_power` gives **~130 %** efficiency. Two causes:
   the recorder stores only changes (a constant value is stored once, so an
   integrator that clamps intervals drops long constant phases), and an idle
   offset of about **-23 W** accumulates tens of phantom kWh over months.
3. Daily figures ranged **3 % to 204 %**; only sums over >= 30 days are stable.

General rule: prefer a counter the platform maintains over a value you compute
from a power sensor. When a computed efficiency exceeds 100 %, the arithmetic is
wrong -- not the physics.

## Lesson 3: a data logger that drops and never returns

On 2026-11-14 the logger disconnected at 17:47 and did not come back. Every
`sensor.growatt_0hvre0ed25dt005m_*` became `unknown`, and template sensors with
an `availability` guard became `unavailable` -- correctly. Sensors **without**
the guard would have reported 0 W, which is a plausible-looking lie that flows
into statistics.

Diagnose with:

```bash
ha_verify.py health --filter growatt
```

This is the routine check after any integration "goes quiet". `unavailable` for
a whole device family is a connectivity problem, not a config problem.

**`unknown` and `unavailable` are not the same thing.** A dropped logger makes
entities `unavailable`; `unknown` means HA has no value yet. For `button`,
`event` and `scene`, `unknown` is the *normal* state until the entity is used
once, so `ha_verify.py health` does not count it -- `--include-unused` lists
them anyway. On this install roughly 105 entities fall in that bucket and the
counted figure moves by tens between runs as integrations reconnect, so treat
any single number here as a snapshot, not a target.

## Lesson 4: entities the integration disables by default

The Shelly integration ships diagnostic entities disabled
(`Spannung`, `Stromstärke`, `Frequenz`, `Temperatur`, `Energieverbrauch`,
signal strength, restart-required). They must be enabled one by one:

```bash
ha_entities.py disabled --pattern shellyplug
ha_entities.py enable sensor.x sensor.y --allow-write
```

`disabled_by: "integration"` means the integration chose that, not the user.
Enabling is a registry update and is reversible; it is not a config change.

Note the naming trap: `SHELLYPLUGMG3_0892725A753C_...` in entity ids carries an
uppercase MAC fragment, while the friendly names are localised German. Searching
by either `ha_entities.py find 0892725a753c` or `find steckdose` works, because
`find` searches the id, the name and the device identifiers together.

## Lesson 5: EMQX admin password is not reset over REST

The EMQX dashboard password cannot be reset through the REST API -- that
endpoint needs a valid token, which is the thing that is lost. It is reset in
the container:

```bash
ssh -i <path-to-your-key> root@<host>
docker exec <emqx-container> /opt/emqx/bin/emqx ctl admins passwd <user> <new-password>
```

Substitute the container name, the admin user and the new password yourself;
none of them belong in a repository. The new password is typed at an interactive
shell, never passed as a literal here.

`emqx` is not on `PATH` inside the container; use the full path. Verify with
`POST /api/v5/login` (HTTP 200 plus a token). MQTT client users live in the
authenticator `password_based:built_in_database`, managed at
`/api/v5/authentication/password_based%3Abuilt_in_database/users`.

General point: an add-on's own control CLI is often the only path for
administrative recovery, and it is reachable only from a shell with Docker
access. The HA WebSocket API cannot help here.

## What belongs in a skill and what does not

Entity ids, MAC addresses and IPs above are **instance-specific**. The reusable
content is:

- the sign convention question ("which side does this meter sit on?"),
- the counter-versus-integration rule,
- the availability-guard requirement,
- the "whole device family is `unknown`" diagnosis,
- `disabled_by: integration` as a distinct state from `disabled_by: user`.

Copy the reasoning, not the ids.
