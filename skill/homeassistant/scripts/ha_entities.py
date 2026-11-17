#!/usr/bin/env python3
"""ha_entities.py -- inspect and (carefully) edit the Home Assistant registries.

Covers the entity, device, area, label and floor registries plus the state
machine.  Every subcommand is **read-only** except the ``*-set`` /
``enable`` family, which needs ``--allow-write``.

Typical read-only uses::

    ha_entities.py find plug                    # which entities mention 'plug'?
    ha_entities.py show sensor.x                # state + registry entry + device
    ha_entities.py disabled                     # entities disabled by default
    ha_entities.py orphans                      # entities/areas with no name
    ha_entities.py duplicates --pattern growatt # suspect _2 / _3 entity ids
    ha_entities.py refs sensor.x                # dashboards, energy, groups, automations
    ha_entities.py rename sensor.x --entity-id-new sensor.y   # preview only

Careful, mutating uses (all need --allow-write)::

    ha_entities.py enable sensor.x sensor.y     # disabled_by -> None
    ha_entities.py disable sensor.x
    ha_entities.py rename sensor.x --name "New" --entity-id sensor.new
    ha_entities.py device-rename <device_id> --name "New"

Credentials: see ha_ws.py (``$HA_TOKEN``, ``$HA_TOKEN_FILE``, ``--token-file``).
Nothing is printed that includes the token.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ha_ws import (  # noqa: E402  (path fixup above is required under -I)
    HAConnection,
    HAError,
    HAHttp,
    collect_entity_ids,
    dashboard_label,
    fetch_config,
    mask_emails,
    install_email_mask,
    UsageError,
    WriteRefused,
    ConnectError,
    add_common_args,
    _apply_common_defaults,
    emit,
    quiet_broken_pipe,
    redact,
)


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


async def load_registries(ha: HAConnection) -> dict:
    """Fetch everything the inspection subcommands need in one connection."""
    entities = await ha.cmd(type="config/entity_registry/list")
    devices = await ha.cmd(type="config/device_registry/list")
    areas = await ha.cmd(type="config/area_registry/list")
    try:
        states = await ha.cmd(type="get_states")
    except HAError:
        states = []
    return {
        "entities": entities,
        "devices": devices,
        "areas": areas,
        "states": states,
        "by_entity_id": {e["entity_id"]: e for e in entities},
        "by_device_id": {d["id"]: d for d in devices},
        # area entries are keyed 'area_id', device entries 'id' -- do not mix
        "by_area_id": {a["area_id"]: a for a in areas},
        "state_by_entity_id": {s["entity_id"]: s for s in states},
    }


def device_label(device: dict | None) -> str:
    if not device:
        return "-"
    name = mask_emails(device.get("name_by_user") or device.get("name")
                       or "(unnamed)")
    model = device.get("model") or "-"
    return f"{name} [{model}]"


def entity_label(entry: dict) -> str:
    return entry.get("name") or entry.get("original_name") or "(no name)"


def effective_name(entry: dict, state: dict | None, device: dict | None) -> str:
    """Name as the UI shows it: own name, else friendly_name, else device name.

    An entity without ``name``/``original_name`` (MQTT lights, for instance)
    is displayed under its state's ``friendly_name`` or its device's name;
    ``(no name)`` only when there is really nothing.
    """
    own = entry.get("name") or entry.get("original_name")
    if own:
        return own
    friendly = ((state or {}).get("attributes") or {}).get("friendly_name")
    if friendly:
        return friendly
    if device:
        return mask_emails(device.get("name_by_user") or device.get("name")
                           or "(no name)")
    return "(no name)"


def entity_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def _short(entry: dict, reg: dict) -> dict:
    device = reg["by_device_id"].get(entry.get("device_id"))
    area_id = entry.get("area_id") or (device or {}).get("area_id")
    area = reg["by_area_id"].get(area_id)
    state = reg["state_by_entity_id"].get(entry["entity_id"], {})
    return {
        "entity_id": entry["entity_id"],
        "name": effective_name(entry, state, device),
        "state": state.get("state"),
        "unit": (state.get("attributes") or {}).get("unit_of_measurement"),
        "platform": entry.get("platform"),
        "device": device_label(device),
        "device_id": entry.get("device_id"),
        "area": (area or {}).get("name"),
        "disabled_by": entry.get("disabled_by"),
        "hidden_by": entry.get("hidden_by"),
        "entity_category": entry.get("entity_category"),
        "unique_id": entry.get("unique_id"),
        # kept so callers can tell "no name anywhere" from "the device names it":
        # HA does not copy the device name into an entity that has neither
        # ``name`` nor ``original_name``.
        "has_own_name": bool(entry.get("name") or entry.get("original_name")),
    }


def print_table(rows: list[dict], columns: list[str],
                max_width: int | None = 46) -> None:
    """Print aligned columns; ``max_width=None`` never truncates a cell."""
    if not rows:
        print("(no matches)")
        return
    widths = {
        col: max(len(col), *(len(str(r.get(col) or "")) for r in rows))
        for col in columns
    }
    if max_width:
        widths = {c: min(w, max_width) for c, w in widths.items()}

    def line(values):
        cells = []
        for col, value in zip(columns, values):
            text = str(value if value is not None else "-")
            if max_width and len(text) > widths[col]:
                text = text[: widths[col] - 1] + "…"
            cells.append(text.ljust(widths[col]))
        return "  ".join(cells).rstrip()

    print(line(columns))
    print(line(["-" * widths[c] for c in columns]))
    for row in rows:
        print(line([row.get(c) for c in columns]))


def report_empty_pattern(what: str, pattern: str | None, matched) -> int:
    """Exit code for a pattern-based listing that found no match.

    A pattern that matches nothing prints an empty table and used to exit 0.
    "0 orphans / 0 duplicates / 0 disabled entities" reads as a clean bill of
    health, so a typo in the pattern looked exactly like a healthy instance --
    the same fail-open shape ``ha_verify.py health --filter`` had.  Exit 1 and
    name the pattern; a pattern-less call keeps returning 0, because there
    really was nothing there.
    """
    if not pattern or matched:
        return 0
    print(f"error: no {what} matches pattern {pattern!r} -- "
          f"nothing was checked", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------
# read-only subcommands
# --------------------------------------------------------------------------


async def cmd_find(ha, args):
    reg = await load_registries(ha)
    needle = args.pattern.lower()
    device_hits = set()
    rows = []
    for entry in reg["entities"]:
        device = reg["by_device_id"].get(entry.get("device_id"))
        blob = " ".join(
            str(x or "")
            for x in (
                entry.get("entity_id"),
                entry.get("name"),
                entry.get("original_name"),
                entry.get("unique_id"),
                entry.get("translation_key"),
                entry.get("platform"),
                (device or {}).get("name"),
                (device or {}).get("name_by_user"),
                (device or {}).get("model"),
                # *All* identifiers, flattened.  Reading only identifiers[0]
                # missed devices that carry two -- on this install three Matter
                # devices list 'deviceid_...' first and 'serial_...' second, so
                # searching by serial found nothing.
                " ".join(
                    str(value)
                    for pair in ((device or {}).get("identifiers") or [])
                    for value in (pair if isinstance(pair, (list, tuple))
                                  else [pair])
                ),
            )
        ).lower()
        if needle in blob:
            rows.append(_short(entry, reg))
            if entry.get("device_id"):
                device_hits.add(entry["device_id"])
    result = {
        "pattern": args.pattern,
        "entities": rows,
        "devices": [
            {
                "device_id": d["id"],
                "name": device_label(d),
                "manufacturer": d.get("manufacturer"),
                "model": d.get("model"),
                "identifiers": d.get("identifiers"),
            }
            for d in (reg["by_device_id"].get(i) for i in sorted(device_hits))
            if d
        ],
    }

    def render(data):
        print(f"== devices ({len(data['devices'])}) ==")
        for d in data["devices"]:
            print(f"  {d['device_id']}  {d['name']}  {d['identifiers']}")
        print(f"== entities ({len(data['entities'])}) ==")
        print_table(data["entities"],
                    ["entity_id", "name", "state", "platform", "disabled_by"])

    emit(result, args.json, render)
    # A search that found neither an entity nor a device exits 1, like
    # `show` on an unknown id: "found nothing" is a real answer and a caller
    # scripting on it needs to see it without parsing the table.
    return report_empty_pattern(
        "entity or device", args.pattern,
        rows or device_hits)


async def cmd_show(ha, args):
    reg = await load_registries(ha)
    out = {"entities": [], "missing": []}
    for entity_id in args.entity_ids:
        entry = reg["by_entity_id"].get(entity_id)
        state = reg["state_by_entity_id"].get(entity_id)
        if entry is None and state is None:
            out["missing"].append(entity_id)
            continue
        if entry:
            record = _short(entry, reg)
        else:
            attrs = (state or {}).get("attributes") or {}
            record = {
                "entity_id": entity_id,
                "name": attrs.get("friendly_name") or entity_id,
                "state": (state or {}).get("state"),
                "unit": attrs.get("unit_of_measurement"),
            }
        members = (((state or {}).get("attributes") or {}).get("entity_id"))
        if isinstance(members, list) and members:
            states = reg["state_by_entity_id"]
            record["members"] = [
                {"entity_id": m, "state": (states.get(m) or {}).get("state")}
                for m in members if isinstance(m, str)]
        if state:
            record["attributes"] = state.get("attributes")
            record["last_changed"] = state.get("last_changed")
            record["last_updated"] = state.get("last_updated")
        if entry is None:
            record["warning"] = "no registry entry (state-only entity)"
        out["entities"].append(record)

    def render(data):
        for record in data["entities"]:
            print(f"# {record['entity_id']}")
            for key in ("name", "state", "unit", "platform", "device",
                        "device_id", "area", "disabled_by", "hidden_by",
                        "entity_category", "unique_id"):
                if key in record:
                    print(f"    {key:>15}: {record[key]}")
            if record.get("members"):
                print(f"    {'members':>15}: {len(record['members'])}")
                for m in record["members"]:
                    print(f"    {'':>15}  {m['entity_id']}  "
                          f"{m['state'] if m['state'] is not None else 'MISSING'}")
            if record.get("warning"):
                print(f"    {'warning':>15}: {record['warning']}")
        for missing in data["missing"]:
            print(f"# {missing}: not found")

    emit(out, args.json, render)
    # Exit 1 when an id does not exist, matching `ha_verify.py states`: the
    # scripts are meant to be usable in a shell condition, and `show` on a
    # typo'd id is exactly the check people put in one.
    return 1 if out["missing"] else 0


async def cmd_disabled(ha, args):
    reg = await load_registries(ha)
    domain = args.domain.lower() if args.domain else None
    if domain:
        known_domains = {entity_domain(e["entity_id"]) for e in reg["entities"]} | {
            entity_domain(s["entity_id"]) for s in reg.get("states", [])
        }
        if domain not in known_domains:
            print(f"error: unknown domain {args.domain!r} -- nothing was checked",
                  file=sys.stderr)
            return 1
    rows = [_short(e, reg) for e in reg["entities"] if e.get("disabled_by")]
    if domain:
        rows = [r for r in rows if entity_domain(r["entity_id"]) == domain]
    # Count the pattern against *every* entity of that domain, not against the
    # disabled ones: "growatt exists but nothing about it is disabled" is a
    # real, clean answer, while "no entity matches growat" is a typo.  Only
    # the second one is worth a non-zero exit.
    pattern_hits = None
    if args.pattern:
        needle = args.pattern.lower()
        candidates = [e for e in reg["entities"]
                      if not domain
                      or entity_domain(e["entity_id"]) == domain]
        pattern_hits = [
            e for e in candidates
            if needle in e["entity_id"].lower()
            or needle in (entity_label(e) or "").lower()
        ]
        rows = [
            r for r in rows
            if needle in r["entity_id"].lower()
            or needle in (r.get("name") or "").lower()
        ]
    result = {"count": len(rows), "entities": rows}
    emit(result, args.json, lambda d: print_table(
        d["entities"],
        ["entity_id", "name", "platform", "device", "disabled_by"]))
    return report_empty_pattern("entity", args.pattern, pattern_hits)


async def cmd_orphans(ha, args):
    """Find registry entries that look like leftovers.

    Deliberately conservative: helpers (``input_*``, ``timer``), automations,
    scripts, scenes and YAML template entities have **no device by design**, so
    listing every device-less entry would drown the real findings.  Device-less
    entries are only reported when they belong to a domain that normally always
    has a device, and that list is printed rather than asserted.
    """
    reg = await load_registries(ha)
    state_ids = set(reg["state_by_entity_id"])
    # Domains that legitimately never have a device_id.
    deviceless_domains = {
        "automation", "script", "scene", "input_boolean", "input_number",
        "input_text", "input_select", "input_datetime", "input_button",
        "timer", "counter", "person", "zone", "sun", "persistent_notification",
        "tag", "todo", "schedule", "conversation", "assist_satellite",
        "homeassistant", "recorder", "lovelace",
    }
    # Platforms that always create a device; a device-less entry there is odd.
    device_platforms = {
        "mqtt", "zha", "zwave_js", "shelly", "esphome", "tasmota", "matter",
        "homekit_controller", "deconz", "tuya", "hue", "shelly_ble",
    }

    # ``disabled_by`` is the important filter here: Home Assistant never
    # instantiates states for a disabled entity, so ``state not in the state
    # machine`` is its normal condition, not a fault.  Without this filter the
    # list is 700 entries of noise on a mid-size install (measured 2026-11)
    # and buries the handful of entries that an offline integration left behind.
    no_state = [_short(e, reg) for e in reg["entities"]
                if e["entity_id"] not in state_ids and not e.get("disabled_by")]
    # An entity with neither ``name`` nor ``original_name`` usually inherits its
    # name from its device, so "no name" alone is not a finding.  Report only
    # the entries where there is no name *anywhere* -- entity, device or area.
    unnamed = [
        _short(e, reg) for e in reg["entities"]
        if not (e.get("name") or e.get("original_name"))
        and not (
            (reg["by_device_id"].get(e.get("device_id")) or {}).get("name_by_user")
            or (reg["by_device_id"].get(e.get("device_id")) or {}).get("name")
        )
    ]
    suspicious_device_less = [
        _short(e, reg) for e in reg["entities"]
        if not e.get("device_id")
        and entity_domain(e["entity_id"]) not in deviceless_domains
        and (e.get("platform") or "") in device_platforms
    ]
    all_device_less = [
        _short(e, reg) for e in reg["entities"] if not e.get("device_id")
    ]
    known_devices = set(reg["by_device_id"])
    dangling = [
        _short(e, reg) for e in reg["entities"]
        if e.get("device_id") and e["device_id"] not in known_devices
    ]
    result = {
        "no_state": no_state,
        "unnamed": unnamed,
        "dangling_device_reference": dangling,
        "suspicious_device_less": suspicious_device_less,
        "device_less_total": len(all_device_less),
    }


    def render(data):
        for key, title in (
            ("no_state", "registry entries without a state (integration offline?)"),
            ("unnamed", "entries without any name"),
            ("dangling_device_reference", "entries pointing at a missing device"),
            ("suspicious_device_less",
             "device-less entries on a platform that normally has devices"),
        ):
            print(f"== {title}: {len(data[key])} ==")
            print_table(data[key], ["entity_id", "name", "platform", "device"])
        print(f"== note: {data['device_less_total']} entries have no device at "
              f"all; helpers, automations and YAML entities do that by design ==")

    emit(result, args.json, render)


def _dup_member(entry: dict, reg: dict, entry_states: dict | None = None) -> dict:
    """One id of a duplicate group with platform, state and an assessment."""
    entry_state = (entry_states or {}).get(entry.get("config_entry_id"))
    state = reg["state_by_entity_id"].get(entry["entity_id"])
    value = (state or {}).get("state")
    if entry.get("disabled_by"):
        verdict = f"disabled ({entry['disabled_by']})"
    elif state is None:
        verdict = "Altlast? no state"
    elif (state.get("attributes") or {}).get("restored"):
        verdict = "Altlast? restored"
    elif value == "unavailable" and entry_state == "loaded":
        # The integration runs and only the device is offline: not a leftover.
        verdict = "offline (Integration geladen)"
    elif value == "unavailable":
        verdict = "Altlast? unavailable"
    else:
        verdict = "aktiv"
    return {"entity_id": entry["entity_id"], "platform": entry.get("platform"),
            "entry_state": entry_state,
            "state": value if state is not None else None,
            "disabled_by": entry.get("disabled_by"),
            "device_id": entry.get("device_id"), "assessment": verdict}


def recommend_duplicates(members: list) -> dict:
    """Keep/delete recommendation for one duplicate group (never acted upon).

    Rule: a member is *active* when its assessment is ``aktiv`` (has a live,
    non-restored, non-unavailable state).  Disabled members are ignored -- they
    are already out of the way.
    * exactly one active member -> keep it.  Another enabled member is a
      deletion candidate only if it has the keeper's platform, or its
      integration's config entry is not ``loaded``.  A member of another,
      loaded integration, or one whose device is merely offline, goes to
      ``manual`` instead (e.g. an Alexa entity next to a Music Assistant one);
    * several active members -> no recommendation, ``manuell pruefen``;
    * no active member -> no recommendation, ``manuell pruefen``.
    Dashboard use (``dashboards``) is carried as information only and never
    changes the verdict.
    """
    enabled = [m for m in members if not m.get("disabled_by")]
    active = [m for m in enabled if m["assessment"] == "aktiv"]
    if len(active) == 1:
        keeper = active[0]
        keep = keeper["entity_id"]
        drop, unsure = [], []
        for m in enabled:
            if m["entity_id"] == keep:
                continue
            # Same platform: the classic `_2` leftover.  Another platform is a
            # different integration, deletable only once that integration is
            # gone or failed (entry not loaded).  An offline device of a loaded
            # integration is never a leftover.
            same = m.get("platform") == keeper.get("platform")
            # Same platform but another device: two real devices, not a
            # leftover -- unless that integration is not running at all.
            other_device = (m.get("device_id") and keeper.get("device_id")
                            and m.get("device_id") != keeper.get("device_id"))
            running = m.get("entry_state") in ("loaded", None)
            if m["assessment"].startswith("offline") or (
                    (not same or other_device) and running):
                unsure.append(m["entity_id"])
            else:
                drop.append(m["entity_id"])
        text = f"behalten: {keep}" + (
            " / Kandidaten zum Loeschen: " + ", ".join(drop)
            if drop else " (nichts sicher zu loeschen)")
        if unsure:
            text += (" / manuell pruefen (andere Integration oder Geraet "
                     "offline): " + ", ".join(unsure))
        return {"verdict": "keep" if not unsure else "partial", "keep": keep,
                "delete_candidates": drop, "manual": unsure, "text": text}
    reason = ("mehrere aktive Varianten" if active
              else "keine aktive Variante")
    return {"verdict": "manual", "keep": None, "delete_candidates": [],
            "manual": [m["entity_id"] for m in enabled],
            "text": f"keine Empfehlung ({reason}) -- manuell pruefen"}


async def _dashboard_usage(ha, wanted: set) -> dict | None:
    """entity_id -> [dashboard labels] over all dashboards; ``None`` if unreadable."""
    try:
        dashboards = await ha.cmd(type="lovelace/dashboards/list") or []
    except HAError:
        return None
    usage: dict = defaultdict(list)
    for url_path in [None] + [d.get("url_path") for d in dashboards
                              if d.get("url_path")]:
        try:
            config = await fetch_config(ha, url_path)
        except HAError:
            continue
        for eid in collect_entity_ids(config, known_ids=wanted) & wanted:
            usage[eid].append(dashboard_label(url_path))
    return usage


async def cmd_duplicates(ha, args):
    """Entity ids that look like the stale-config `_2` trap.

    Many devices legitimately have numbered sub-entities (``event.button_1`` ..
    ``button_9``, ``sensor.battery_1_soc``).  A suffix alone proves nothing, so
    a group is only reported when the **un-suffixed base id also exists** --
    that is the signature of "the old config left an entity behind and the new
    one landed as `_2`".
    """
    reg = await load_registries(ha)
    try:
        entry_states = {c.get("entry_id"): c.get("state") for c in
                        (await ha.cmd(type="config_entries/get") or [])}
    except HAError:
        entry_states = {}
    suffix = re.compile(r"^(?P<base>.*?)_(?P<n>\d+)$")
    existing_ids = {e["entity_id"] for e in reg["entities"]}
    suffixed_bases = set()
    for eid in existing_ids:
        m = suffix.match(eid)
        if m:
            suffixed_bases.add(m.group("base"))

    groups: dict[tuple, list] = defaultdict(list)
    for entry in reg["entities"]:
        entity_id = entry["entity_id"]
        domain, _, object_id = entity_id.partition(".")
        match = suffix.match(object_id)
        if match:
            cand_base = match.group("base")
            full_cand = f"{domain}.{cand_base}"
            if entity_id in suffixed_bases:
                base = object_id
            elif full_cand in existing_ids:
                base = cand_base
            else:
                base = cand_base
        else:
            base = object_id
        groups[(domain, base)].append(entry)

    rows = []
    # Pattern hits are counted against every group, before the report filters,
    # so "this entity exists but has no duplicate" stays a clean 0.  Counting
    # only the reported rows made a typo look exactly like a healthy instance.
    pattern_hits = 0
    for (domain, base), entries in sorted(groups.items()):
        # Match the *full* group id, domain included: the report prints
        # ``switch.foo*``, and every sibling command (``find``, ``disabled``,
        # ``entity-ids``, ``states --filter``) matches the whole entity id.
        # Testing only the object id made ``--pattern switch`` miss all seven
        # real switch groups while still counting a sensor named
        # ``..._switch_type`` as a hit -- 7 duplicates dropped, exit 0.
        full = f"{domain}.{base}"
        matched = (not args.pattern
                   or args.pattern.lower() in full.lower())
        if matched:
            pattern_hits += 1
        if not args.all:
            # only the true `_2` trap: the base id coexists with a suffixed one
            if f"{domain}.{base}" not in existing_ids:
                continue
        if len(entries) < 2:
            continue
        if not matched:
            continue
        members = [_dup_member(e, reg, entry_states) for e in sorted(
            entries, key=lambda e: e["entity_id"])]
        rows.append({
            "group": f"{domain}.{base}*",
            "count": len(entries),
            "members": members,
            "base_exists": f"{domain}.{base}" in existing_ids,
            "entity_ids": sorted(e["entity_id"] for e in entries),
            "disabled": [e["entity_id"] for e in entries if e.get("disabled_by")],
            "devices": sorted({e.get("device_id") for e in entries
                               if e.get("device_id")}),
        })
    wanted = {m["entity_id"] for r in rows for m in r["members"]}
    usage = await _dashboard_usage(ha, wanted) if wanted else {}
    for r in rows:
        for m in r["members"]:
            m["dashboards"] = (None if usage is None
                               else sorted(usage.get(m["entity_id"], [])))
        r["recommendation"] = recommend_duplicates(r["members"])
        # Info only: the kept id is unused while a candidate is on a dashboard.
        rec = r["recommendation"]
        if usage is not None and rec["keep"] and rec["delete_candidates"]:
            used = [c for c in rec["delete_candidates"] if usage.get(c)]
            if used and not usage.get(rec["keep"]):
                rec["note"] = ("Dashboard nutzt " + ", ".join(used)
                               + " statt " + rec["keep"]
                               + " -- vor dem Loeschen Referenzen umstellen")
    result = {
        "pattern": args.pattern,
        "mode": "all numbered groups" if args.all
                else "base id coexists with a suffixed id",
        "groups": rows,
    }
    def render(d):
        print(f"({d['mode']})")
        flat = [dict(m, group=g["group"]) for g in d["groups"]
                for m in g["members"]]
        for m in flat:
            m["used_on"] = ("?" if m["dashboards"] is None
                            else str(len(m["dashboards"])))
        print_table(flat, ["group", "entity_id", "platform", "state",
                           "assessment", "used_on"], max_width=None)
        print("\n== recommendation (Regel: genau eine aktive Variante -> "
              "behalten; Kandidat nur bei gleicher Plattform oder nicht "
              "geladener Integration; sonst manuell pruefen) ==")
        for g in d["groups"]:
            rec = g["recommendation"]
            print(f"  {g['group']:<40} {rec['text']}")
            if rec.get("note"):
                print(f"  {'':<40} Hinweis: {rec['note']}")
        print("\nVor dem Loeschen eines Kandidaten: `refs <entity_id>` -- "
              "used_on zaehlt nur Dashboards, nicht Automationen/Skripte.")
        print(f"\n{len(d['groups'])} groups. 'Altlast?' is a hint, not a "
              f"verdict: nothing is deleted; check platform and device first. "
              f"used_on = number of dashboards referencing the id.")

    emit(result, args.json, render)
    return report_empty_pattern("entity group", args.pattern, pattern_hits)


# --------------------------------------------------------------------------
# refs: where is an entity used?
# --------------------------------------------------------------------------


def _find_paths(node, needle: str, path: str = "") -> list[str]:
    """Paths inside a JSON tree whose string value is or contains *needle*.

    Matches the id as a whole token, so ``sensor.a`` does not hit ``sensor.ab``.
    """
    pattern = re.compile(r"(?<![A-Za-z0-9_.])" + re.escape(needle)
                         + r"(?![A-Za-z0-9_])")
    hits: list[str] = []
    if isinstance(node, str):
        if pattern.search(node):
            hits.append(path or "$")
    elif isinstance(node, dict):
        for key, value in node.items():
            hits += _find_paths(value, needle, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            hits += _find_paths(value, needle, f"{path}[{index}]")
    return hits


def _view_label(view, index: int) -> str:
    if isinstance(view, dict):
        return str(view.get("path") or view.get("title") or f"#{index}")
    return f"#{index}"


def _dashboard_hits(config, entity_id: str) -> list[dict]:
    hits = []
    if not isinstance(config, dict):
        return hits
    views = config.get("views")
    for vi, view in enumerate(views if isinstance(views, list) else []):
        cards = []
        if isinstance(view, dict):
            for key in ("cards",):
                if isinstance(view.get(key), list):
                    cards += [(f"cards[{i}]", c) for i, c in enumerate(view[key])]
            for si, section in enumerate(view.get("sections") or []):
                if isinstance(section, dict) and isinstance(
                        section.get("cards"), list):
                    cards += [(f"sections[{si}].cards[{i}]", c)
                              for i, c in enumerate(section["cards"])]
            badges = _find_paths(view.get("badges"), entity_id)
            if badges:
                hits.append({"view": _view_label(view, vi), "where": "badges"})
        for where, card in cards:
            if collect_entity_ids(card, known_ids={entity_id}) & {entity_id} \
                    or _find_paths(card, entity_id):
                hits.append({
                    "view": _view_label(view, vi), "where": where,
                    "card_type": card.get("type") if isinstance(card, dict)
                    else None})
    return hits


async def collect_refs(ha, entity_id: str) -> dict:
    """All places that reference *entity_id*, read-only."""
    result: dict = {"entity_id": entity_id, "dashboards": [], "energy": [],
                    "groups": [], "automations": [], "scripts": [],
                    "scenes": [], "notes": []}
    # dashboards: every storage dashboard plus the default one
    try:
        dashboards = await ha.cmd(type="lovelace/dashboards/list") or []
    except HAError:
        dashboards = []
    paths = [None] + [d.get("url_path") for d in dashboards if d.get("url_path")]
    for url_path in paths:
        try:
            config = await fetch_config(ha, url_path)
        except HAError:
            continue  # unreadable / auto-generated default dashboard
        found = _dashboard_hits(config, entity_id)
        if found:
            result["dashboards"].append(
                {"dashboard": dashboard_label(url_path), "url_path": url_path,
                 "hits": found})
    # energy preferences
    try:
        prefs = await ha.cmd(type="energy/get_prefs")
    except HAError:
        prefs = None
    for where in _find_paths(prefs, entity_id):
        result["energy"].append(where)
    if result["energy"]:
        result["notes"].append(
            "ENERGY-PREFS: the Energy dashboard references this entity. A "
            "rename does NOT update energy/save_prefs -- adjust it by hand in "
            "Settings > Dashboards > Energy.")
    # groups: state attribute entity_id
    try:
        states = await ha.cmd(type="get_states") or []
    except HAError:
        states = []
    for st in states:
        members = (st.get("attributes") or {}).get("entity_id")
        if isinstance(members, list) and entity_id in members \
                and st["entity_id"] != entity_id:
            result["groups"].append(st["entity_id"])
    # structured relations HA knows about
    try:
        related = await ha.cmd(type="search/related", item_type="entity",
                               item_id=entity_id) or {}
    except HAError:
        related = {}
    for key, bucket in (("automation", "automations"), ("script", "scripts"),
                        ("scene", "scenes"), ("group", "groups")):
        for other in related.get(key) or []:
            if other != entity_id and other not in result[bucket]:
                result[bucket].append(other)
    # config text scan: finds templates and conditions search/related misses
    result["config_scan"] = await _scan_configs(ha, states, entity_id, result)
    if result["config_scan"] == "unavailable":
        result["notes"].append(
            "automation/script configs were not scanned (REST not reachable); "
            "only search/related results are listed.")
    result["notes"].append(
        "Not scannable read-only: YAML files (templates, packages, "
        "configuration.yaml) and UI template helpers -- grep /config for the id.")
    for key in ("groups", "automations", "scripts", "scenes"):
        result[key] = sorted(result[key])
    result["total"] = (sum(len(d["hits"]) for d in result["dashboards"])
                       + len(result["energy"]) + len(result["groups"])
                       + len(result["automations"]) + len(result["scripts"])
                       + len(result["scenes"]))
    return result


async def _scan_configs(ha, states, entity_id: str, result: dict) -> str:
    """Read automation/script configs over REST GET and look for the id."""
    base = getattr(ha, "base_url", None)
    token = getattr(ha, "_token", None)
    if not base or not token:
        return "unavailable"
    http = HAHttp(url=base, token=token, timeout=getattr(ha, "timeout", 30.0))
    jobs = []
    for st in states:
        domain, _, obj = st["entity_id"].partition(".")
        if domain == "automation":
            ident = (st.get("attributes") or {}).get("id")
            if ident:
                jobs.append((st["entity_id"], "automations",
                             f"/api/config/automation/config/{ident}"))
        elif domain == "script":
            jobs.append((st["entity_id"], "scripts",
                         f"/api/config/script/config/{obj}"))
    scanned = 0
    for eid, bucket, path in jobs:
        try:
            config = await asyncio.to_thread(http.request, path)
        except (ConnectError, HAError, OSError):
            continue  # YAML-defined entries have no editable config
        scanned += 1
        if _find_paths(config, entity_id) and eid not in result[bucket]:
            result[bucket].append(eid)
    return f"{scanned}/{len(jobs)}"


def render_refs(data: dict) -> None:
    print(f"== references to {data['entity_id']} ({data['total']}) ==")
    print(f"dashboards: {sum(len(d['hits']) for d in data['dashboards'])} hits "
          f"in {len(data['dashboards'])} dashboard(s)")
    for dash in data["dashboards"]:
        for hit in dash["hits"]:
            kind = f" ({hit['card_type']})" if hit.get("card_type") else ""
            print(f"  {dash['dashboard']}  view {hit['view']}  "
                  f"{hit['where']}{kind}")
    print(f"energy prefs: {len(data['energy'])}")
    for where in data["energy"]:
        print(f"  {where}")
    for key in ("groups", "automations", "scripts", "scenes"):
        print(f"{key}: {len(data[key])}")
        for other in data[key]:
            print(f"  {other}")
    scan = data.get("config_scan")
    if scan and scan != "unavailable":
        print(f"(automation/script configs scanned: {scan})")
    for note in data["notes"]:
        print(f"! {note}")


async def cmd_refs(ha, args):
    """Every place that references an entity (read-only).

    Covers all Lovelace dashboards (default included), the Energy preferences,
    group members, automations, scripts and scenes.  Exit 1 when the id is
    unknown to both registry and state machine.
    """
    try:
        entry = await ha.cmd(type="config/entity_registry/get",
                             entity_id=args.entity_id)
    except HAError:
        entry = None
    try:
        states = await ha.cmd(type="get_states") or []
    except HAError:
        states = []
    known = entry is not None or any(
        s["entity_id"] == args.entity_id for s in states)
    data = await collect_refs(ha, args.entity_id)
    data["known"] = known
    emit(data, args.json, render_refs)
    if not known:
        print(f"error: {args.entity_id} is unknown (no registry entry, no "
              f"state)", file=sys.stderr)
        return 1
    return 0


async def cmd_areas(ha, args):
    """List the areas, their ids, and how much lives in each.

    The area registry is keyed by ``area_id`` (a slug), while automations and
    service calls expect that id -- not the display name.  Without this listing
    the only way to learn an id was to let ``area-assign`` fail on an ambiguous
    name, which is a poor way to discover an API.
    """
    reg = await load_registries(ha)
    floors = {}
    try:
        floors = {
            f["floor_id"]: f.get("name")
            for f in await ha.cmd(type="config/floor_registry/list")
        }
    except HAError:
        pass  # floor registry is optional and not present everywhere
    rows = []
    for area in sorted(reg["areas"], key=lambda a: (a.get("name") or "").lower()):
        area_id = area["area_id"]
        devices = [d for d in reg["devices"] if d.get("area_id") == area_id]
        # The *effective* area decides, the same way ``_short`` computes it: an
        # entity's own ``area_id`` overrides its device's.  Checking both
        # independently counted an entity in two areas at once -- once for its
        # explicit area and once for its device's, which it had overridden.
        entities = [
            e for e in reg["entities"]
            if (e.get("area_id")
                or (reg["by_device_id"].get(e.get("device_id")) or {}
                    ).get("area_id")) == area_id
        ]
        rows.append({
            "area_id": area_id,
            "name": area.get("name"),
            "floor": floors.get(area.get("floor_id")),
            "devices": len(devices),
            "entities": len(entities),
            "aliases": ", ".join(area.get("aliases") or []),
        })
    if args.pattern:
        needle = args.pattern.lower()
        rows = [r for r in rows
                if needle in (r["area_id"] or "").lower()
                or needle in (r["name"] or "").lower()]
    emit(rows, args.json, lambda data: (
        print_table(data, ["area_id", "name", "floor", "devices", "entities"]),
        print(f"\n{len(data)} areas"),
    ))
    # ``--pattern keller`` with no such area printed "0 areas" and exited 0,
    # which is indistinguishable from an instance that has no areas at all.
    return report_empty_pattern("area", args.pattern, rows)


async def cmd_stats(ha, args):
    reg = await load_registries(ha)
    by_domain = Counter(entity_domain(e["entity_id"]) for e in reg["entities"])
    by_platform = Counter(e.get("platform") or "?" for e in reg["entities"])
    result = {
        "entities": len(reg["entities"]),
        "devices": len(reg["devices"]),
        "areas": len(reg["areas"]),
        "states": len(reg["states"]),
        "disabled": sum(1 for e in reg["entities"] if e.get("disabled_by")),
        "hidden": sum(1 for e in reg["entities"] if e.get("hidden_by")),
        "by_domain": dict(by_domain.most_common()),
        "by_platform": dict(by_platform.most_common(20)),
    }
    emit(result, args.json, lambda d: (
        print(f"entities {d['entities']}  devices {d['devices']}  "
              f"areas {d['areas']}  states {d['states']}"),
        print(f"disabled {d['disabled']}  hidden {d['hidden']}"),
        print("top domains:", ", ".join(
            f"{k}={v}" for k, v in list(d["by_domain"].items())[:12])),
    ))


def _entry_reason(entry: dict) -> str | None:
    """Why an entry is not loaded: reason, error key, else state and source."""
    if entry.get("state") == "loaded":
        return entry.get("reason")
    parts = entry.get("reason") or entry.get("error_reason_translation_key")
    if parts:
        return parts
    source = entry.get("source")
    return (f"{entry.get('state')} (source={source}, no reason reported)"
            if source else f"{entry.get('state')} (no reason reported)")


async def cmd_integrations(ha, args):
    """List config entries and flag the ones that failed to set up.

    ``setup_retry`` and ``setup_error`` are the states that explain a whole
    family of entities going ``unavailable`` -- the counterpart to
    ``ha_verify.py health``, which only shows the symptom.
    """
    entries = await ha.cmd(type="config_entries/get")
    try:
        registry = await ha.cmd(type="config/entity_registry/list") or []
    except HAError:
        registry = []
    per_entry = Counter(e.get("config_entry_id") for e in registry
                        if e.get("config_entry_id"))
    rows = []
    for entry in entries:
        state = entry.get("state")
        if args.only_problems and state == "loaded":
            continue
        if args.domain and entry.get("domain") != args.domain:
            continue
        rows.append({
            "domain": entry.get("domain"),
            # titles are often the account name, i.e. an e-mail address
            "title": mask_emails(entry.get("title")),
            "state": state,
            "reason": mask_emails(_entry_reason(entry)),
            "entities": per_entry.get(entry.get("entry_id"), 0),
            "entry_id": entry.get("entry_id"),
            "disabled_by": entry.get("disabled_by"),
            "supports_options": entry.get("supports_options"),
            "error": entry.get("error_reason_translation_key"),
        })
    order = {"setup_error": 0, "setup_retry": 1, "not_loaded": 2, "loaded": 3}
    rows.sort(key=lambda r: (order.get(r["state"], 9), r["domain"] or ""))
    counts: dict = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    result = {"counts": counts, "entries": rows}

    def render(data):
        print("states:", ", ".join(f"{k}={v}" for k, v in sorted(
            data["counts"].items(), key=lambda kv: order.get(kv[0], 9))))
        print_table(data["entries"],
                    ["domain", "title", "state", "reason", "entities",
                     "disabled_by"])

    emit(result, args.json, render)


# --------------------------------------------------------------------------
# mutating subcommands (--allow-write)
# --------------------------------------------------------------------------


async def cmd_enable(ha, args):
    value = False if args.enable else "user"
    changed = []
    for entity_id in args.entity_ids:
        result = await ha.cmd(
            type="config/entity_registry/update",
            entity_id=entity_id,
            disabled_by=None if args.enable else "user",
        )
        entry = (result or {}).get("entity_entry", {})
        record = {
            "entity_id": entity_id,
            "disabled_by": entry.get("disabled_by"),
            "ok": entry.get("disabled_by") is None if args.enable
                  else entry.get("disabled_by") is not None,
        }
        changed.append(record)
        print(f"{entity_id:<52} disabled_by={record['disabled_by']!r} "
              f"{'ok' if record['ok'] else 'UNCHANGED'}")
    _ = value
    return {"changed": changed}


async def cmd_rename(ha, args):
    """Rename an entity.  Without --allow-write: a preview, nothing changes.

    The preview lists what would change, whether the target id is free, and
    every reference found by ``refs`` -- a rename updates the registry only;
    dashboards, automations, scripts and the Energy preferences keep the old id.
    """
    if args.name is None and not args.entity_id_new:
        raise UsageError("nothing to rename: give --name and/or --entity-id-new")
    if not getattr(ha, "allow_write", True):
        await _rename_preview(ha, args)
        raise WriteRefused(
            "rename was NOT applied (preview only). Re-run with --allow-write "
            "to apply it.")
    if args.entity_id_new:
        refs = await collect_refs(ha, args.entity_id)
        if refs["energy"]:
            print("! ENERGY-PREFS reference this entity and are not updated "
                  "by the rename; adjust them by hand.", file=sys.stderr)
        if refs["total"]:
            print(f"! {refs['total']} reference(s) keep the old id "
                  f"(see: ha_entities.py refs {args.entity_id})",
                  file=sys.stderr)
    payload = {"type": "config/entity_registry/update", "entity_id": args.entity_id}
    if args.name is not None:
        payload["name"] = None if args.name == "-" else args.name
    if args.entity_id_new:
        payload["new_entity_id"] = args.entity_id_new
    result = await ha.cmd(**payload)
    entry = (result or {}).get("entity_entry", {})
    print(f"{args.entity_id} -> {entry.get('entity_id')} name={entry.get('name')!r}")
    return entry


async def _rename_preview(ha, args) -> None:
    try:
        states = await ha.cmd(type="get_states") or []
    except HAError:
        states = []
    taken = {s["entity_id"] for s in states}
    try:
        registry = await ha.cmd(type="config/entity_registry/list") or []
    except HAError:
        registry = []
    taken |= {e["entity_id"] for e in registry}
    print(f"== rename preview: {args.entity_id} (nothing is changed) ==")
    if args.entity_id not in taken:
        print(f"! {args.entity_id} does not exist")
    if args.entity_id_new:
        print(f"  entity_id: {args.entity_id} -> {args.entity_id_new}")
        if args.entity_id_new in taken:
            print(f"! CONFLICT: {args.entity_id_new} already exists")
        if entity_domain(args.entity_id_new) != entity_domain(args.entity_id):
            print("! the domain differs; Home Assistant rejects that")
    if args.name is not None:
        print(f"  name: {'(cleared)' if args.name == '-' else args.name!r}")
    if args.entity_id_new:
        print("  statistics: HA migrates statistics_meta to the new id; "
              "history stays continuous (verify with ha_verify.py stats-range)")
        data = await collect_refs(ha, args.entity_id)
        print("  references that keep the OLD id after the rename:")
        render_refs(data)
        if data["energy"]:
            print("! Energy preferences must be adjusted manually "
                  "(not implemented here).")


async def cmd_device_rename(ha, args):
    result = await ha.cmd(
        type="config/device_registry/update",
        device_id=args.device_id,
        name_by_user=None if args.name == "-" else args.name,
    )
    print(f"{args.device_id} -> name_by_user={result.get('name_by_user')!r}")
    return result


async def cmd_area_assign(ha, args):
    """Assign an entity to an area.

    ``--area-id`` (preferred) or ``--area`` resolve an area by id, by name, or
    by a case-insensitive name match.  Passing a display name straight through
    as an ``area_id`` is a silent failure: the registry stores the internal id
    (a slug like ``wohnzimmer``), not the label.
    """
    area_id = None
    if args.area is not None and args.area != "-":
        if args.area_id:
            raise UsageError("use --area or --area-id, not both")
        areas = await ha.cmd(type="config/area_registry/list")
        by_id = {a["area_id"]: a for a in areas}
        if args.area in by_id:
            area_id = args.area
        else:
            matches = [a for a in areas
                       if (a.get("name") or "").lower() == args.area.lower()]
            if not matches:
                matches = [a for a in areas
                           if args.area.lower() in (a.get("name") or "").lower()]
            if len(matches) != 1:
                known = ", ".join(
                    f"{a['area_id']} ({a.get('name')})" for a in areas
                ) or "none"
                raise UsageError(
                    f"area {args.area!r} matched {len(matches)} areas. "
                    f"Known areas: {known}"
                )
            area_id = matches[0]["area_id"]
    elif args.area_id:
        area_id = args.area_id
    result = await ha.cmd(
        type="config/entity_registry/update",
        entity_id=args.entity_id,
        area_id=area_id,
    )
    entry = (result or {}).get("entity_entry", {})
    print(f"{args.entity_id} -> area_id={entry.get('area_id')!r}")
    return entry


async def cmd_call(ha, args):
    """Call a service. Refused unless --allow-write; use with care."""
    data = None
    if args.data:
        import json

        # Same contract as --units/--post-body/--config: a malformed JSON
        # argument is a usage error (exit 2).  The bare JSONDecodeError fell
        # through main()'s ValueError handler and exited 1.
        try:
            data = json.loads(args.data)
        except json.JSONDecodeError as exc:
            raise UsageError(f"--data must be JSON: {exc}") from exc
    domain, _, service = args.service.partition(".")
    result = await ha.cmd(
        type="call_service", domain=domain, service=service,
        service_data=data, return_response=args.return_response,
    )
    emit(result, args.json or True)
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ha_entities.py",
        description="Inspect (and carefully edit) Home Assistant registries. "
        "Read-only unless --allow-write is given.",
        epilog="For an entity that has no registry entry but does have a state, "
        "'show' still prints the state and flags the missing entry.",
    )
    add_common_args(parser)
    sub = parser.add_subparsers(dest="subcommand")
    parent = argparse.ArgumentParser(add_help=False)
    add_common_args(parent, suppress=True)

    def new(name, help_text):
        return sub.add_parser(name, help=help_text, parents=[parent])

    p = new("find", "search entities/devices by substring")
    p.add_argument("pattern")
    p.set_defaults(func=cmd_find)

    p = new("show", "state, registry entry and device for one or more entities")
    p.add_argument("entity_ids", nargs="+")
    p.set_defaults(func=cmd_show)

    p = new("disabled", "entities the integration disabled (or that you disabled)")
    p.add_argument("--domain", default=None, help="e.g. sensor")
    p.add_argument("--pattern", default=None, help="substring of id or name")
    p.set_defaults(func=cmd_disabled)

    p = new("orphans", "entries without state, name, or a live device")
    p.set_defaults(func=cmd_orphans)

    p = new("duplicates", "entities that differ only by a _2/_3 suffix, with "
                          "platform, state and an assessment per id")
    p.add_argument("--pattern", default=None, help="only groups matching this")
    p.add_argument("--all", action="store_true",
                   help="list every numbered group, not just the _2 trap "
                        "(base id exists next to a suffixed id)")
    p.set_defaults(func=cmd_duplicates)

    p = new("areas", "list areas with ids, floors and how much lives in each")
    p.add_argument("--pattern", default=None, help="substring of id or name")
    p.set_defaults(func=cmd_areas)

    p = new("stats", "counts per domain and platform")
    p.set_defaults(func=cmd_stats)

    p = new("refs", "everywhere an entity is used: dashboards, energy, groups, "
                    "automations, scripts")
    p.add_argument("entity_id")
    p.set_defaults(func=cmd_refs)

    p = new("integrations", "config entries, especially setup_error/setup_retry")
    p.add_argument("--domain", default=None, help="e.g. mqtt")
    p.add_argument("--only-problems", action="store_true",
                   help="hide entries in state 'loaded'")
    p.set_defaults(func=cmd_integrations)

    p = new("enable", "enable disabled entities  [--allow-write]")
    p.add_argument("entity_ids", nargs="+")
    p.set_defaults(func=cmd_enable, enable=True)

    p = new("disable", "disable entities  [--allow-write]")
    p.add_argument("entity_ids", nargs="+")
    p.set_defaults(func=cmd_enable, enable=False)

    p = new("rename", "rename an entity; without --allow-write it prints a "
                      "preview (changes + refs) and exits 2  [--allow-write]")
    p.description = (
        "Without --allow-write this prints a PREVIEW (what changes, whether "
        "the target id is free, every reference from 'refs') and exits 2; "
        "nothing is changed. Energy preferences are never updated by a "
        "rename and must be adjusted manually.")
    p.add_argument("entity_id")
    p.add_argument("--name", default=None, help="new name, '-' clears it")
    p.add_argument("--entity-id-new", default=None,
                   help="new entity_id, e.g. sensor.growatt_plug_power")
    p.set_defaults(func=cmd_rename)

    p = new("device-rename", "set name_by_user on a device  [--allow-write]")
    p.add_argument("device_id")
    p.add_argument("--name", required=True, help="new name, '-' clears it")
    p.set_defaults(func=cmd_device_rename)

    p = new("area-assign", "assign an entity to an area  [--allow-write]")
    p.add_argument("entity_id")
    p.add_argument("--area", default=None,
                   help="area by id or display name; '-' clears it")
    p.add_argument("--area-id", default=None,
                   help="area id verbatim (skips name resolution)")
    p.set_defaults(func=cmd_area_assign)

    p = new("call", "call a service  [--allow-write]")
    p.add_argument("service", help="domain.service, e.g. light.turn_on")
    p.add_argument("--data", default=None, help="JSON service_data")
    p.add_argument("--return-response", action="store_true",
                   help="ask HA to return the service response")
    p.set_defaults(func=cmd_call)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _apply_common_defaults(args)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2

    async def run() -> int:
        async with HAConnection(
            url=args.url, token_file=args.token_file, ws_url=args.ws_url,
            allow_write=args.allow_write, timeout=args.timeout,
        ) as ha:
            result = args.func(ha, args)
            if asyncio.iscoroutine(result):
                result = await result
            return result if isinstance(result, int) else 0

    try:
        return asyncio.run(run())
    except (WriteRefused, UsageError) as exc:
        # A refused write or an impossible request is a *usage* error: exit 2,
        # so a shell condition can tell "you asked wrong" from "it did not
        # work".  WriteRefused is caught before PermissionError on purpose --
        # it inherits from both and this branch has to win.
        print(f"error: {redact(str(exc))}", file=sys.stderr)
        return getattr(exc, "exit_code", 2)
    except (ConnectError, PermissionError, HAError, ValueError, OSError) as exc:
        print(f"error: {redact(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    quiet_broken_pipe()
    install_email_mask()
    sys.exit(main())
