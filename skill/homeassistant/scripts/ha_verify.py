#!/usr/bin/env python3
"""ha_verify.py -- verify that entities, statistics and dashboards are healthy.

Everything here is **read-only**.  It answers the questions you ask after
changing a configuration:

* Do all the entities I expect actually exist, and do any show ``unknown`` or
  ``unavailable``?
* Do the statistics still have data, and where does the series start?
* Does my dashboard only reference entities that exist?
* Do my long-term statistics survive a restart / a config change?

Examples::

    # states of a fixed list
    ha_verify.py states sensor.a sensor.b input_number.nullpunkt

    # everything that mentions 'growatt' and is broken
    ha_verify.py health --filter growatt

    # how far back do the long-term statistics go?
    ha_verify.py stats-range --since 90d sensor.a sensor.b

    # daily change/sum series for a period
    ha_verify.py stats sensor.a --start 2026-11-01 --period day --types change,sum

    # all entities referenced by a dashboard, checked against the state machine
    ha_verify.py dashboard-url --url-path batterie-uebersicht

    # dashboards stored on disk as .storage/lovelace.<name> (needs file access)
    ha_verify.py dashboard --file /config/.storage/lovelace.my_dash

    # history around an event (minimal_response safe)
    ha_verify.py history sensor.x --hours 6

Exit code is 0 when everything checked out, 1 when a problem was found, so it
can be used in a shell test or CI-style check.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ha_ws import (  # noqa: E402  (the path fixup above is required under -I)
    HAConnection,
    HAError,
    UsageError,
    WriteRefused,
    ConnectError,
    add_common_args,
    _apply_common_defaults,
    collect_entity_ids as _collect_entity_ids,
    as_list,
    iter_view_cards,
    mask_emails,
    install_email_mask,
    dashboard_label,
    emit,
    fetch_config,
    quiet_broken_pipe,
    redact,
)


#: The previous local regex scanned every string and treated service names and
#: URLs as entity ids ('button.press', 'music.aalen', 'party_qr.png'), which
#: failed healthy dashboards.  The shared implementation understands the
#: structure of a Lovelace config and lives in ``ha_ws``, so this script and
#: ``ha_dashboard.py`` cannot drift apart.
collect_entity_ids = _collect_entity_ids

#: ``none`` is **not** here: it is a legitimate enum option, not an error.
#: Measured on the reference install: ``sensor.pixel_10_pro_charger_type`` has
#: ``options: [ac, usb, wireless, dock, none]`` and reports ``none`` while the
#: charger is idle.  Counting it as broken made ``states`` fail a healthy
#: entity -- and because ``health`` filtered on ``unknown``/``unavailable``
#: only, the two commands contradicted each other about the same entity.
BAD_STATES = {"unknown", "unavailable", ""}

#: ``unknown`` is the **normal** state of these domains before first use: a
#: button that was never pressed, an event that never fired, a scene that was
#: never activated.  Reporting them as broken made every dashboard with a
#: button fail.  ``unavailable`` stays a problem everywhere -- that means the
#: integration is gone, not that the entity is merely unused.
STATELESS_DOMAINS = frozenset({"button", "event", "scene"})


def is_bad_state(entity_id: str, value: str) -> bool:
    """True when *value* is a problem for *entity_id* (domain aware)."""
    lowered = str(value).lower()
    if lowered not in BAD_STATES:
        return False
    if lowered == "unknown" and entity_id.split(".", 1)[0] in STATELESS_DOMAINS:
        return False
    return True


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def parse_when(value: str) -> dt.datetime:
    """Accept ISO 8601, a date, ``now``, or a relative span such as ``90d``.

    A trailing ``Z`` is accepted: it is the ISO spelling of UTC, Home Assistant
    prints it in every timestamp it hands out (``last_changed``,
    ``sun_next_rising``), and ``datetime.fromisoformat`` only learned it in
    Python 3.11.  On the documented minimum, 3.10, ``--start 2026-11-15T12:00:00Z``
    died with "Invalid isoformat string" -- so the one timestamp format the
    API shows you was the one the flag rejected.
    """
    if value is None:
        return None
    text = value.strip()
    if text.lower() == "now":
        # The --end help text advertises 'now' as the default; without this the
        # literal string reached fromisoformat() and died with
        # "Invalid isoformat string: 'now'".
        return dt.datetime.now(dt.timezone.utc)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhdw])", text)
    if match:
        amount = float(match.group(1))
        unit = match.group(2)
        seconds = {
            "s": 1,
            "m": 60,
            "h": 3600,
            "d": 86400,
            "w": 604800,
        }[unit]
        # A span large enough to overflow ``datetime`` raised a bare
        # OverflowError traceback (``--start 1000000d``).  It is the same
        # impossible argument as a malformed one, so it leaves as a usage
        # error too.
        try:
            return dt.datetime.now(dt.timezone.utc) - dt.timedelta(
                seconds=amount * seconds
            )
        except OverflowError as exc:
            raise UsageError(
                f"{value!r} reaches further back than datetime can represent "
                f"-- use a smaller span (days, hours) or an ISO date"
            ) from exc
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        # ``fromisoformat`` rejects 2026-02-30 in the date branch but the
        # timestamp branch turns the same mistake into a UsageError; here the
        # ValueError escaped to main() and exited 1 for the same typo.
        try:
            return dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc)
        except ValueError as exc:
            raise UsageError(
                f"{value!r} is not a valid date -- got: {exc}") from exc
    # 'Z' is UTC, not an unknown zone, and 3.10 cannot parse it.  Only the
    # trailing form is rewritten, so a literal 'Z' inside a value is untouched.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise UsageError(
            f"{value!r} is not a timestamp. Use ISO 8601 "
            f"(2026-11-15T12:00:00Z), a date (2026-11-15), 'now', or a span "
            f"like 90d/6h -- got: {exc}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def iso(when: dt.datetime | None) -> str | None:
    """Format a datetime as ISO-8601 UTC, tolerating ``None``.

    ``epoch_to_datetime`` returns ``None`` for a missing timestamp, and
    ``recorder/statistics_during_period`` rows do omit keys now and then, so
    this must not assume a datetime.
    """
    return when.astimezone(dt.timezone.utc).isoformat() if when else None


def epoch_to_datetime(value) -> dt.datetime | None:
    """Home Assistant reports statistic timestamps in **milliseconds**.

    Treating them as seconds yields ``year 58567 is out of range``; this helper
    is the single place that converts, and it accepts both units defensively.
    """
    if value is None:
        return None
    number = float(value)
    if number > 1e11:  # milliseconds
        number /= 1000.0
    return dt.datetime.fromtimestamp(number, tz=dt.timezone.utc)


async def fetch_states(ha: HAConnection) -> dict:
    states = await ha.cmd(type="get_states")
    return {s["entity_id"]: s for s in states}


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------


async def cmd_states(ha, args):
    have = await fetch_states(ha)
    rows, problems = [], []
    for entity_id in args.entity_ids:
        state = have.get(entity_id)
        if state is None:
            rows.append({"entity_id": entity_id, "state": None, "problem": "missing"})
            problems.append(entity_id)
            continue
        attributes = state.get("attributes") or {}
        bad = is_bad_state(entity_id, state["state"])
        rows.append({
            "entity_id": entity_id,
            "state": state["state"],
            "unit": attributes.get("unit_of_measurement"),
            "friendly_name": attributes.get("friendly_name"),
            "device_class": attributes.get("device_class"),
            "state_class": attributes.get("state_class"),
            "last_changed": state.get("last_changed"),
            "problem": "bad state" if bad else None,
        })
        if bad:
            problems.append(entity_id)

    if args.json:
        print(json.dumps({"entities": rows, "problems": problems},
                         ensure_ascii=False, indent=2))
    else:
        width = max((len(r["entity_id"]) for r in rows), default=10)
        for row in rows:
            if row["state"] is None:
                print(f"  *** MISSING  {row['entity_id']}")
                continue
            unit = f" {row['unit']}" if row.get("unit") else ""
            flag = "  <-- " + row["problem"] if row.get("problem") else ""
            print(f"  {row['entity_id']:<{width}}  {row['state']}{unit}{flag}")
        print(f"\n{len(rows)} entities, {len(problems)} problems")
    return 1 if problems else 0


#: Rule for ``health --by-device``: a device counts as OFFLINE when *every*
#: enabled, visible, non-diagnostic ("primary") entity is unavailable.  If only
#: some are, it is PARTIAL.  ``unknown`` never counts as a fault, and unused
#: button/event/scene entities (``unknown`` by design) are ignored for the rule.
def _device_label(device: dict) -> str:
    return mask_emails(device.get("name_by_user") or device.get("name")
                       or "(unnamed)")


async def _health_by_device(ha, args, have, broken, unused, unknown_filter,
                            filter_matched) -> int:
    entities = await ha.cmd(type="config/entity_registry/list")
    devices = {d["id"]: d for d in await ha.cmd(type="config/device_registry/list")}
    try:
        entries = {e["entry_id"]: e for e in await ha.cmd(type="config_entries/get")}
    except HAError:
        entries = {}
    by_entity = {e["entity_id"]: e for e in entities}
    unavailable = {r["entity_id"] for r in broken if r["state"] == "unavailable"}
    per_device: dict = {}
    for entry in entities:
        dev_id = entry.get("device_id")
        if not dev_id or dev_id not in devices:
            continue
        if entry.get("disabled_by"):
            continue
        slot = per_device.setdefault(dev_id, {"primary": [], "secondary": []})
        primary = not entry.get("entity_category") and not entry.get("hidden_by")
        slot["primary" if primary else "secondary"].append(entry["entity_id"])
    groups, partial_n = [], 0
    for dev_id, slot in per_device.items():
        pool = slot["primary"] or slot["secondary"]
        # button/event/scene that are merely "unknown" are unused, not alive
        pool = [e for e in pool if e in unavailable
                or not (e.split(".", 1)[0] in STATELESS_DOMAINS
                        and str(have.get(e, {}).get("state")).lower() == "unknown")] or pool
        down = [e for e in pool if e in unavailable]
        all_down = [e for e in slot["primary"] + slot["secondary"]
                    if e in unavailable]
        if not all_down:
            continue
        status = "offline" if len(down) == len(pool) else "partial"
        device = devices[dev_id]
        platforms = sorted({(by_entity[e].get("platform") or "?")
                            for e in slot["primary"] + slot["secondary"]})
        loaded = [entries[c]["state"] for c in device.get("config_entries") or []
                  if c in entries]
        groups.append({
            "device_id": dev_id, "device": _device_label(device),
            "primary_unavailable": len([e for e in slot["primary"]
                                        if e in unavailable]),
            "primary_total": len(slot["primary"]),
            "model": device.get("model"), "status": status,
            "unavailable": len(all_down),
            "total": len(slot["primary"]) + len(slot["secondary"]),
            "integration": ",".join(platforms),
            "entry_state": ",".join(sorted(set(loaded))) or None,
            "entities": sorted(all_down),
        })
        partial_n += status == "partial"
    groups.sort(key=lambda g: (g["status"] != "offline", g["integration"],
                               g["device"].lower()))
    # Same name on several devices (8x "MS6586_EU"): the short id (first 8
    # characters of the device_id) tells them apart in the text output.
    name_count = Counter(g["device"] for g in groups)
    for g in groups:
        g["short_id"] = g["device_id"][:8]
        g["name_ambiguous"] = name_count[g["device"]] > 1
    deviceless = sorted(
        r["entity_id"] for r in broken
        if r["state"] == "unavailable"
        and not (by_entity.get(r["entity_id"]) or {}).get("device_id"))
    needle = (args.filter or "").lower()
    if needle:
        groups = [g for g in groups if needle in g["device"].lower()
                  or needle in g["integration"].lower()
                  or any(needle in e for e in g["entities"])]
    offline_n = sum(1 for g in groups if g["status"] == "offline")
    if args.json:
        print(json.dumps(
            {"offline_devices": offline_n, "partial_devices": partial_n,
             "devices": groups, "without_device": deviceless,
             "unused_not_counted": len(unused), "filter": args.filter,
             "filter_matched": filter_matched,
             "rule": "offline = all primary entities unavailable"},
            ensure_ascii=False, indent=2))
    else:
        print(f"{offline_n} devices offline, {len(groups) - offline_n} partial; "
              f"{len(deviceless)} unavailable entities without a device")
        print("rule: offline = all primary (non-diagnostic, visible) entities "
              "unavailable; partial = some; 'unknown' is not counted")
        print("columns: prim = unavailable/total among primary entities (the "
              "offline rule, unused button/event/scene ignored there); all = "
              "unavailable/total of every entity")
        print(f"  {'status':<8} {'prim':<6} {'all':<6} {'device':<40} "
              f"{'model':<24} {'integration':<18} entry")
        for g in groups:
            prim = f"{g['primary_unavailable']}/{g['primary_total']}"
            allc = f"{g['unavailable']}/{g['total']}"
            label = g["device"][:40]
            if g["name_ambiguous"]:
                label = f"{g['device'][:30]} [{g['short_id']}]"
            print(f"  {g['status']:<8} {prim:<6} {allc:<6} "
                  f"{label:<40} {str(g['model'] or '-')[:24]:<24} "
                  f"{g['integration'][:18]:<18} {g['entry_state'] or '-'}")
        if deviceless:
            print("== without device ==")
            for eid in deviceless:
                print(f"  {eid}")
        if unknown_filter:
            print(f"  *** no entity id or friendly name contains "
                  f"{args.filter!r} -- the filter matched nothing")
    if unknown_filter:
        return 1
    return 1 if (groups or deviceless) else 0


async def cmd_health(ha, args):
    """Summarise unknown/unavailable entities, optionally filtered.

    Domain aware: an untouched ``button``/``event``/``scene`` is ``unknown`` by
    design and is not a health problem.  Use ``--include-unused`` to see them
    anyway.
    """
    have = await fetch_states(ha)
    needle = (args.filter or "").lower()

    def _matches(entity_id: str, state: dict) -> bool:
        """Id *or* friendly name, like ``entity-ids`` and ``states --filter``.

        Matching only the id made ``--filter "Calibrate Zero"`` report "the
        filter matched nothing" while ``button.…_calibrate_zero`` was sitting
        there unavailable under the friendly name "Calibrate Zero".
        """
        if not needle:
            return True
        if needle in entity_id.lower():
            return True
        attributes = state.get("attributes") or {}
        return needle in str(attributes.get("friendly_name", "")).lower()

    # A filter that matches **nothing at all** is almost always a typo, and the
    # answer "0 entities are unknown/unavailable" reads exactly like a clean
    # bill of health.  `history` got the same guard in round 5; this is the
    # second command with the same fail-open shape.  Counted against the full
    # state list, not against the broken ones: a filter that matches 98 healthy
    # entities and no broken one is a real answer.
    filter_matched = sum(1 for entity_id, state in have.items()
                         if _matches(entity_id, state))
    broken, unused = [], []
    for entity_id, state in have.items():
        value = str(state.get("state", ""))
        # One shared set decides what is worth looking at, and one shared
        # function decides whether it is broken.  The hand-written copy that
        # stood here read only unknown/unavailable, so it silently ignored
        # ``""`` and disagreed with ``states`` about the same entity.
        if value.lower() not in BAD_STATES:
            continue
        if not _matches(entity_id, state):
            continue
        attributes = state.get("attributes") or {}
        row = {
            "entity_id": entity_id,
            "state": value,
            "friendly_name": attributes.get("friendly_name"),
            "last_changed": state.get("last_changed"),
        }
        if is_bad_state(entity_id, value) or args.include_unused:
            broken.append(row)
        else:
            unused.append(row)
    broken.sort(key=lambda r: (r["state"], r["entity_id"]))
    unknown_filter = bool(needle) and filter_matched == 0
    if getattr(args, "by_device", False):
        return await _health_by_device(ha, args, have, broken, unused,
                                       unknown_filter, filter_matched)
    if args.json:
        print(json.dumps(
            {"count": len(broken),
             "entities": broken,
             "unused_not_counted": len(unused),
             "filter": args.filter,
             "filter_matched": filter_matched},
            ensure_ascii=False, indent=2))
    else:
        print(f"{len(broken)} entities are unknown/unavailable"
              + (f" (filter {args.filter!r})" if args.filter else ""))
        for row in broken:
            print(f"  {row['state']:<12} {row['entity_id']:<58} "
                  f"{row['friendly_name'] or ''}")
        if unused and not args.include_unused:
            print(f"  ({len(unused)} unused button/event/scene entities are "
                  f"'unknown' by design; --include-unused to list them)")
        if unknown_filter:
            print(f"  *** no entity id or friendly name contains "
                  f"{args.filter!r} -- the filter matched nothing, which is "
                  f"not the same as a clean result.")
    if unknown_filter:
        # Exit 1: nothing was checked.  A green exit for a filter that matches
        # no entity at all is the fail-open shape -- it reads as "all healthy"
        # when the real answer is "you asked about something that is not here".
        return 1
    # A health check that always exits 0 is useless in a shell condition.
    return 1 if broken else 0


async def cmd_stats(ha, args):
    start = parse_when(args.start or "7d")
    end = parse_when(args.end) or dt.datetime.now(dt.timezone.utc)
    types = [t.strip() for t in args.types.split(",") if t.strip()]
    payload = {
        "type": "recorder/statistics_during_period",
        "start_time": iso(start),
        "end_time": iso(end),
        "statistic_ids": args.statistic_ids,
        "period": args.period,
        "types": types,
    }
    if args.units:
        # A malformed --units value is a bad argument, not a Home Assistant
        # answer: UsageError keeps the exit contract (2) that README.md and
        # SKILL.md promise for "a bad argument".  It fell through as a bare
        # JSONDecodeError, which the ValueError handler turned into exit 1.
        try:
            units = json.loads(args.units)
        except json.JSONDecodeError as exc:
            raise UsageError(
                f"--units must be a JSON object of unit overrides, "
                f"e.g. '{{\"pressure\": \"kPa\"}}': {exc}") from exc
        # Valid JSON is not necessarily the right shape.  A string or list
        # went straight to Home Assistant, which answered with invalid_format
        # and exit 1 -- a Home Assistant answer where README.md promises exit
        # 2 for a bad argument.
        if not isinstance(units, dict):
            raise UsageError(
                f"--units must be a JSON object of unit overrides, e.g. "
                f"'{{\"pressure\": \"kPa\"}}': got {type(units).__name__} "
                f"{args.units!r}")
        payload["units"] = units
    result = await ha.cmd(**payload)
    # ``full`` keeps every row the recorder returned; ``out`` is the --tail
    # view of it.  The verdict below has to look at ``full``: ``--tail 0`` is
    # a display choice, not "this statistic has no data", and reading the
    # trimmed list made ``--tail 0 --json`` exit 1.
    full = {}
    for statistic_id, rows in (result or {}).items():
        full[statistic_id] = [
            {
                **{k: v for k, v in row.items() if k not in ("start", "end")},
                "start": iso(epoch_to_datetime(row.get("start"))),
                "start_ms": row.get("start"),
                "end": iso(epoch_to_datetime(row.get("end"))),
            }
            for row in rows
        ]
    # Iterate requested ids, not result keys, so an entity with no statistics
    # appears as an empty list rather than missing from the JSON object.
    out = {
        sid: (
            (full.get(sid) or [])[-args.tail:]
            if args.tail > 0 else []
        )
        for sid in args.statistic_ids
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        # Asking about three entities and getting statistics for two is a
        # finding, not a success.  The old ``0 if out else 1`` looked only at
        # whether *anything* came back, so it exited 0 where the text mode
        # exited 1 -- the same check gave two different answers.
        return 1 if any(not full.get(sid) for sid in args.statistic_ids) else 0

    missing = 0
    for statistic_id in args.statistic_ids:
        rows = full.get(statistic_id) or []
        print(f"== {statistic_id}  ({len(rows)} rows, period={args.period}) ==")
        if not rows:
            print("   no statistics -- the entity has no long-term statistics "
                  "(state_class set? recorder running?)")
            missing += 1
            continue
        for row in (rows[-args.tail:] if args.tail > 0 else []):
            # row["start"] can be missing: the recorder omits keys now and
            # then, and epoch_to_datetime() documents that it returns None for
            # a missing timestamp.  Slicing None raised a TypeError traceback.
            cells = [str(row.get("start") or "?")[:16]]
            for key in types:
                value = row.get(key)
                if isinstance(value, float):
                    cells.append(f"{key}={value:.4g}")
                else:
                    cells.append(f"{key}={value}")
            print("   " + "  ".join(cells))
    return 1 if missing else 0


async def cmd_stats_range(ha, args):
    """Report the first and last bucket of the long-term statistics per entity."""
    start = parse_when(args.since or "365d")
    end = dt.datetime.now(dt.timezone.utc)
    result = await ha.cmd(
        type="recorder/statistics_during_period",
        start_time=iso(start),
        end_time=iso(end),
        statistic_ids=args.statistic_ids,
        period="day",
        types=["change", "sum"],
    )
    rows_out = []
    for statistic_id in args.statistic_ids:
        rows = (result or {}).get(statistic_id) or []
        # .get() on both ends: rows omitting a key happen on this recorder, and
        # an unguarded subscript turned that into a traceback.
        first = epoch_to_datetime(rows[0].get("start")) if rows else None
        last = epoch_to_datetime(rows[-1].get("end") or rows[-1].get("start")) \
            if rows else None
        rows_out.append({
            "statistic_id": statistic_id,
            "rows": len(rows),
            "first": first.isoformat() if first else None,
            "last": last.isoformat() if last else None,
            "last_sum": rows[-1].get("sum") if rows else None,
        })
    if args.json:
        print(json.dumps(rows_out, ensure_ascii=False, indent=2))
    else:
        for row in rows_out:
            if not row["rows"]:
                print(f"  {row['statistic_id']:<58} NO STATISTICS")
                continue
            print(f"  {row['statistic_id']:<58} rows={row['rows']:<5} "
                  f"{str(row['first'] or '?')[:16]} -> "
                  f"{str(row['last'] or '?')[:16]} "
                  f"last_sum={row['last_sum']}")
    return 1 if any(not row["rows"] for row in rows_out) else 0


async def cmd_history(ha, args):
    start = parse_when(args.start or f"{args.hours}h")
    end = parse_when(args.end) or dt.datetime.now(dt.timezone.utc)
    result = await ha.cmd(
        type="history/history_during_period",
        start_time=iso(start),
        end_time=iso(end),
        entity_ids=args.entity_ids,
        minimal_response=not args.full,
        no_attributes=not args.with_attributes,
        significant_changes_only=args.significant_only,
    )
    # An unknown entity id returns {} -- indistinguishable from "no data in this
    # window".  `states` and `stats` both exit 1 for a typo, so this does too,
    # otherwise a mistyped id reads as a quiet period.  The check costs one
    # extra get_states call and is on by default; --no-exists-check opts out.
    check = not args.no_exists_check
    known_ids = ({s["entity_id"] for s in await ha.cmd(type="get_states")}
                 if check else set())
    unknown = [e for e in args.entity_ids if e not in known_ids] if check else []
    if args.json:
        # --tail applies to both modes, as in ``stats`` above.  Iterate the
        # *requested* ids, not the result keys: an entity with no history is
        # absent from the result, and a missing key would look like a different
        # answer from the text mode's ``(0 points)`` line.
        trimmed = {
            entity_id: (
                ((result or {}).get(entity_id) or [])[-args.tail:]
                if args.tail > 0 else []
            )
            for entity_id in args.entity_ids
        }
        payload = trimmed if not check else {
            "history": trimmed, "unknown_entities": unknown}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if unknown else 0

    # The WebSocket variant always uses the short keys 's' and 'lu', for both
    # values of minimal_response (verified on HA 2026.11).  The spelled-out
    # keys only come from REST /api/history/period, hence the fallback below.
    for entity_id in args.entity_ids:
        rows = (result or {}).get(entity_id) or []
        print(f"== {entity_id}  ({len(rows)} points) ==")
        if entity_id in unknown:
            print("   *** unknown entity id -- not in the state machine. "
                  "A typo looks exactly like this; check the id.")
            continue
        for row in (rows[-args.tail:] if args.tail > 0 else []):
            if "s" in row:
                stamp = epoch_to_datetime(row.get("lu"))
                print(f"   {stamp.isoformat()[:19] if stamp else '?':<20} "
                      f"{row.get('s')}")
            else:
                print(f"   {str(row.get('last_updated'))[:19]:<20} "
                      f"{row.get('state')}")
    return 1 if unknown else 0


async def cmd_dashboard_url(ha, args):
    """Check a storage-mode dashboard over the API.

    ``--url-path`` is optional: without it the **default** dashboard is checked,
    which is addressed by omitting ``url_path`` from the request (sending
    ``"url_path": null`` is not the same thing).  The default dashboard only has
    a readable config in storage or YAML mode.
    """
    pos = getattr(args, "url_path_pos", None)
    if pos and args.url_path and pos != args.url_path:
        raise UsageError(f"two different dashboards given: {pos!r} and "
                         f"--url-path {args.url_path!r}")
    args.url_path = args.url_path or pos
    config = await fetch_config(ha, args.url_path)
    return await _check_dashboard(ha, config, args,
                                  label=dashboard_label(args.url_path))


async def cmd_dashboard(ha, args):
    try:
        with open(args.file, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except OSError as exc:
        # Exit 2, not 1: SKILL.md and README.md both list "a missing file" as a
        # usage error, and ha_dashboard.py already answers 2 for it.  The two
        # halves of the toolkit disagreeing about the same failure was the
        # finding -- a shell condition could not treat them alike.
        print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        # JSONDecodeError is a ValueError, not an OSError: it escaped as a
        # traceback while the OSError branch above looked like full coverage.
        print(f"error: {args.file} is not valid JSON: {exc}", file=sys.stderr)
        return 2
    except UnicodeDecodeError as exc:
        # A binary file raises UnicodeDecodeError inside ``json.load``; it is a
        # ValueError too, so it fell past both branches above into main()'s
        # operation-error handler and exited 1 while every other unreadable
        # ''--file'' exits 2.  Measured with ``--file /bin/ls``.
        print(f"error: {args.file} is not a UTF-8 text file: {exc}",
              file=sys.stderr)
        return 2
    if not isinstance(stored, dict):
        # `[]` used to reach ``stored.get`` and die with an AttributeError
        # traceback.  Round 4 fixed this in ``ha_dashboard.py validate`` and
        # missed the same shape here.
        print(f"error: {args.file}: the dashboard config must be a JSON "
              f"object, got {type(stored).__name__}", file=sys.stderr)
        return 2
    # A .storage file wraps the dashboard in {"data": {"config": {...}}}.
    # ``stored.get("data", {})`` only falls back when the *key* is missing: a
    # ``{"data": null}`` file handed ``None`` to the second ``.get`` and died
    # with an AttributeError traceback instead of the one-line error promised
    # for an unreadable file.  The isinstance check below could not run.
    data = stored.get("data")
    config = data.get("config", stored) if isinstance(data, dict) else stored
    if not isinstance(config, dict):
        print(f"error: {args.file}: the wrapped dashboard config must be a "
              f"JSON object, got {type(config).__name__}", file=sys.stderr)
        return 2
    return await _check_dashboard(ha, config, args, label=args.file)


def _view_name(view, index: int) -> str:
    """View path, else title, else its index -- never ``None``."""
    if isinstance(view, dict):
        return str(view.get("path") or view.get("title") or f"#{index}")
    return repr(view)


def _entity_locations(config, wanted: set) -> dict:
    """entity_id -> ['view <name> card <i> (type)', ...] for the given ids."""
    found: dict = {}
    for vi, view in enumerate(as_list(config.get("views"))):
        if not isinstance(view, dict):
            continue
        name = _view_name(view, vi)
        for ci, card in enumerate(iter_view_cards(view)):
            for eid in collect_entity_ids(card, known_ids=wanted) & wanted:
                found.setdefault(eid, []).append(
                    f"view {name} card {ci} ({card.get('type') or '?'})")
    return found


async def _check_dashboard(ha, config, args, label: str) -> int:
    # The live callers hand this whatever ``lovelace/config`` returned.  A
    # fake or half-broken instance can answer ``[]`` or ``null``; every later
    # line does ``config.get(...)``, so it died with an AttributeError
    # traceback instead of the one-line error promised for an unreadable
    # config.  Checked here rather than in each caller so a new caller cannot
    # forget it.
    if not isinstance(config, dict):
        print(f"error: {label}: the dashboard config must be a JSON object, "
              f"got {type(config).__name__}", file=sys.stderr)
        return 2
    have = await fetch_states(ha)
    # ``service``/``perform_action`` hold a service name ('button.press') *or* a
    # script entity ('script.party_qr_code_aktualisieren').  Resolving against
    # what the instance actually knows keeps the phantom out without dropping
    # the two live script references that a blanket key skip lost.
    known_ids = set(have)
    referenced = sorted(collect_entity_ids(config, known_ids=known_ids))
    registry = None
    if args.with_registry:
        # ``--with-registry`` is what separates a real loss from a disabled
        # entity: a disabled entity has no state *by design*, so without the
        # registry both look identical here.
        registry = {
            e["entity_id"]: e
            for e in await ha.cmd(type="config/entity_registry/list")
        }
    missing, disabled, bad, ok = [], [], [], []
    locations = _entity_locations(config, set(referenced))
    for entity_id in referenced:
        state = have.get(entity_id)
        if state is None:
            entry = (registry or {}).get(entity_id)
            if entry is not None and entry.get("disabled_by"):
                disabled.append(entity_id)
            else:
                missing.append(entity_id)
        elif is_bad_state(entity_id, state["state"]):
            bad.append(entity_id)
        else:
            ok.append(entity_id)

    payload = {
        "dashboard": label,
        "referenced": len(referenced),
        "ok": ok,
        "bad_state": bad,
        "missing": missing,
        "disabled": disabled,
        # A hand-written config can put anything in ``views``; a non-dict
        # entry used to crash this comprehension instead of being reported.
        "views": [_view_name(v, i)
                  for i, v in enumerate(as_list(config.get("views")))],
        "details": {
            eid: {"state": (have.get(eid) or {}).get("state")
                  if have.get(eid) else
                  ("disabled" if eid in disabled else "missing"),
                  "where": locations.get(eid, [])}
            for eid in bad + missing + disabled},
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"== {label} ==")
        print(f"   views: {payload['views']}")
        print(f"   referenced entities: {len(referenced)}")
        def where(entity_id):
            spots = locations.get(entity_id) or []
            return ("  [" + "; ".join(spots[:3])
                    + (f"; +{len(spots) - 3}" if len(spots) > 3 else "") + "]"
                    if spots else "")

        for entity_id in missing:
            print(f"   *** FEHLT   {entity_id}{where(entity_id)}")
        for entity_id in disabled:
            print(f"   ! disabled  {entity_id} (no state by design; enable it "
                  f"or drop it from the dashboard){where(entity_id)}")
        for entity_id in bad:
            print(f"   ! {have[entity_id]['state'] or 'empty':<11} "
                  f"{entity_id}{where(entity_id)}")
        if missing or bad or disabled:
            # The summary line used to be printed whenever neither entity was
            # *missing* nor in a *bad state*, so a dashboard whose only problem
            # was a disabled entity printed "enable it or drop it from the
            # dashboard" and then, one line later, "all referenced entities
            # exist and have a usable state".  A disabled entity has no state
            # by design: that sentence is false for it.
            print(f"   {len(missing) + len(bad) + len(disabled)} of "
                  f"{len(referenced)} referenced entities need attention")
        elif not referenced:
            # A view list that yields no entity at all is not a clean bill of
            # health either -- it usually means the reference was written in a
            # shape collect_entity_ids does not know (or the file is the wrong
            # dashboard).  Saying "all is well" about nothing is the fail-open
            # shape this module exists to avoid.
            print("   no entity reference found in this config -- nothing was "
                  "checked")
        else:
            print("   all referenced entities exist and have a usable state")
    # 1 also for "nothing to check": a config with no recognisable entity
    # reference cannot tell "this dashboard is fine" from "I could not read
    # this shape", so a green exit would be a claim the command cannot make.
    return 1 if (missing or bad or disabled or not referenced) else 0


#: A dashboard counts as a "Leiche" (dead) when more than this share of its
#: referenced entities no longer exists (missing, not merely disabled).
CORPSE_MISSING_RATIO = 0.5


def dashboard_health(referenced, have: dict, registry: dict) -> dict:
    """Counts for one dashboard: ok / missing / unavailable-unknown / disabled.

    ``missing`` = no state and not disabled in the registry; ``disabled`` = no
    state because the registry entry is disabled (by design, not a loss);
    ``bad`` = state ``unavailable``/``unknown`` (domain aware, see
    :func:`is_bad_state`).  ``ratio`` is the share of problem entities
    (missing + bad + disabled) among the references.
    """
    missing, bad, disabled = [], [], []
    for entity_id in referenced:
        state = have.get(entity_id)
        if state is None:
            entry = registry.get(entity_id)
            (disabled if entry and entry.get("disabled_by")
             else missing).append(entity_id)
        elif is_bad_state(entity_id, state["state"]):
            bad.append(entity_id)
    total = len(referenced)
    problems = len(missing) + len(bad) + len(disabled)
    return {
        "entities_referenced": total,
        "missing": len(missing), "bad_state": len(bad),
        "disabled": len(disabled),
        "problem_ratio": round(problems / total, 3) if total else 0.0,
        "missing_ratio": round(len(missing) / total, 3) if total else 0.0,
        "corpse": bool(total and len(missing) / total > CORPSE_MISSING_RATIO),
        "missing_ids": sorted(missing),
    }


async def cmd_dashboard_list(ha, args):
    dashboards = await ha.cmd(type="lovelace/dashboards/list")
    # One state snapshot for every dashboard, so the counts in this overview
    # match what `entity-refs` and `dashboard-url` report for the same
    # dashboard.  Without it the service/perform_action values were dropped
    # here and counted there.
    have = await fetch_states(ha)
    known_ids = set(have)
    # The registry tells a disabled entity (no state by design) from a deleted
    # one, like ``dashboard-url --with-registry``.  One call for all rows.
    try:
        entries = await ha.cmd(type="config/entity_registry/list")
    except HAError:
        entries = []
    registry = {e["entity_id"]: e for e in entries or []}
    rows = []
    for dashboard in dashboards:
        url_path = dashboard.get("url_path")
        try:
            config = await ha.cmd(type="lovelace/config", url_path=url_path)
            # ``collect_entity_ids`` tolerates a non-dict and returns an empty
            # set, so an unreadable config was reported as "entities=0" with
            # exit 0 -- the same fail-open shape this overview exists to catch.
            if not isinstance(config, dict):
                raise HAError(
                    "invalid_config",
                    f"the dashboard config must be a JSON object, got "
                    f"{type(config).__name__}", command="lovelace/config")
            referenced = sorted(collect_entity_ids(config, known_ids=known_ids))
        except HAError as exc:
            rows.append({"url_path": url_path, "title": dashboard.get("title"),
                         "mode": dashboard.get("mode"), "error": str(exc)})
            continue
        rows.append({
            "url_path": url_path,
            "title": dashboard.get("title"),
            "mode": dashboard.get("mode"),
            "icon": dashboard.get("icon"),
            "require_admin": dashboard.get("require_admin"),
            "show_in_sidebar": dashboard.get("show_in_sidebar"),
            **dashboard_health(referenced, have, registry),
        })
    sort_key = getattr(args, "sort", "name")
    good = [r for r in rows if not r.get("error")]
    errors = [r for r in rows if r.get("error")]
    if sort_key == "problems":
        good.sort(key=lambda r: (-r["problem_ratio"], str(r["url_path"])))
    elif sort_key == "missing":
        good.sort(key=lambda r: (-r["missing"], str(r["url_path"])))
    elif sort_key == "entities":
        good.sort(key=lambda r: (-r["entities_referenced"], str(r["url_path"])))
    else:
        good.sort(key=lambda r: str(r["url_path"]))
    rows = good + errors
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(f"  {'dashboard':<28} {'entities':>8} {'missing':>7} "
              f"{'unavail':>7} {'disabled':>8} {'problem':>7}  title")
        for row in rows:
            path_str = str(row["url_path"] or "")
            if row.get("error"):
                print(f"  {path_str:<28} {row.get('title')!r} ERROR {row['error']}")
                continue
            flag = ("  <-- Leiche? >50 % fehlend" if row["corpse"] else "")
            print(f"  {path_str:<28} {row['entities_referenced']:>8} "
                  f"{row['missing']:>7} {row['bad_state']:>7} "
                  f"{row['disabled']:>8} {row['problem_ratio']:>7.0%}  "
                  f"{row['title']!r}{flag}")
        corpses = [r for r in rows if r.get("corpse")]
        print(f"\n{len(rows)} dashboards, {len(corpses)} mit >50 % fehlenden "
              f"Entities (Leichen-Verdacht); Details: dashboard-url <url_path> "
              f"--with-registry")
    # A dashboard whose config could not be read is a finding, not a note: the
    # text mode prints ``ERROR`` and the JSON carries ``error``, but an exit of
    # 0 told every shell condition and CI run that the overview was clean.
    broken = [row for row in rows if row.get("error")]
    if broken:
        print(f"error: {len(broken)} of {len(rows)} dashboards could not be "
              f"read: " + ", ".join(str(r["url_path"]) for r in broken),
              file=sys.stderr)
        return 1
    return 0


#: Units an energy statistic may carry in the Energy dashboard.
ENERGY_UNITS = frozenset({"Wh", "kWh", "MWh", "GWh", "mWh", "J", "kJ", "MJ",
                          "GJ", "cal", "kcal", "Mcal", "Gcal"})
GAS_UNITS = frozenset({"m³", "m3", "ft³", "ft3", "CCF", "MCF", "L", "gal",
                       "fl. oz.", "mL", "Wh", "kWh", "MWh", "GJ"})

#: A day's change above this multiple of the median day counts as a jump.
JUMP_FACTOR = 10.0
#: Unavailable transitions within 24 h from which an entity counts as flickering.
FLICKER_MIN = 3


def energy_sources(prefs: dict) -> list:
    """Flatten ``energy/get_prefs`` into ``{role, statistic_id, label}`` rows.

    Roles: solar, battery_out, battery_in, grid_import, grid_export, gas,
    water, device.  Both grid shapes are read: the current one with
    ``flow_from``/``flow_to`` lists and the older one with top-level
    ``stat_energy_from``/``stat_energy_to``.  Only energy counters are
    returned -- ``stat_rate`` power sensors are not part of the statistics.
    """
    rows: list = []

    def add(role, statistic_id, label):
        if isinstance(statistic_id, str) and statistic_id:
            rows.append({"role": role, "statistic_id": statistic_id,
                         "label": label})

    prefs = prefs if isinstance(prefs, dict) else {}
    for src in as_list(prefs.get("energy_sources")):
        if not isinstance(src, dict):
            continue
        kind = src.get("type")
        label = src.get("name") or kind
        if kind == "solar":
            add("solar", src.get("stat_energy_from"), label)
        elif kind == "battery":
            add("battery_out", src.get("stat_energy_from"), label)
            add("battery_in", src.get("stat_energy_to"), label)
        elif kind == "grid":
            add("grid_import", src.get("stat_energy_from"), label)
            add("grid_export", src.get("stat_energy_to"), label)
            for flow in as_list(src.get("flow_from")):
                if isinstance(flow, dict):
                    add("grid_import", flow.get("stat_energy_from"), label)
            for flow in as_list(src.get("flow_to")):
                if isinstance(flow, dict):
                    add("grid_export", flow.get("stat_energy_to"), label)
        elif kind in ("gas", "water"):
            add(kind, src.get("stat_energy_from"), label)
    for dev in as_list(prefs.get("device_consumption")):
        if isinstance(dev, dict):
            add("device", dev.get("stat_consumption"), dev.get("name"))
    for dev in as_list(prefs.get("device_consumption_water")):
        if isinstance(dev, dict):
            add("water", dev.get("stat_consumption"), dev.get("name"))
    seen, unique = set(), []
    for row in rows:
        key = (row["role"], row["statistic_id"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def _median(values: list) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (
        ordered[mid - 1] + ordered[mid]) / 2


def analyse_daily(rows: list, now: dt.datetime, report_days: int,
                  tz) -> tuple:
    """Daily ``change`` series -> (days, findings).

    *rows* may reach further back than *report_days* (a baseline for the
    median); only the last *report_days* are reported and checked for gaps,
    negative changes and jumps.  Today is partial and is exempt from the jump
    rule.  A gap is a missing day bucket between two present ones, or no bucket
    at all for the newest day(s).
    """
    findings: list = []
    series = []
    for row in rows:
        start = epoch_to_datetime(row.get("start"))
        if start is None:
            continue
        series.append((start.astimezone(tz), row.get("change"),
                       row.get("sum")))
    series.sort(key=lambda item: item[0])
    today = now.astimezone(tz).date()
    first_reported = today - dt.timedelta(days=report_days - 1)
    positives = [c for _, c, _ in series
                 if isinstance(c, (int, float)) and c > 0]
    median = _median(positives) if len(positives) >= 4 else None
    days = []
    prev_sum = None
    by_day = {}
    for start, change, total in series:
        day = start.date()
        by_day[day] = change
        flags = []
        if isinstance(change, (int, float)) and change < 0:
            flags.append("negative change (reset?)")
        if (isinstance(total, (int, float)) and isinstance(prev_sum, (int, float))
                and total < prev_sum - 1e-9 and "negative change (reset?)" not in flags):
            flags.append("sum went down (reset?)")
        if (median and day != today and isinstance(change, (int, float))
                and change > JUMP_FACTOR * median):
            flags.append(f"jump: {change:.4g} is > {JUMP_FACTOR:g}x the "
                         f"median {median:.4g}")
        if isinstance(total, (int, float)):
            prev_sum = total
        if day >= first_reported:
            days.append({"day": day.isoformat(), "change": change,
                         "sum": total, "flags": flags})
            findings += [f"{day.isoformat()}: {f}" for f in flags]
    present = {d["day"] for d in days}
    for offset in range(report_days):
        day = (first_reported + dt.timedelta(days=offset))
        if day.isoformat() not in present:
            findings.append(f"{day.isoformat()}: no statistics bucket (gap)")
            days.append({"day": day.isoformat(), "change": None, "sum": None,
                         "flags": ["gap"]})
    days.sort(key=lambda d: d["day"])
    return days, findings


def count_unavailable_transitions(rows: list) -> int:
    """Times the state *became* unavailable in a history list (``s`` or ``state``)."""
    count, prev = 0, None
    for row in rows or []:
        value = row.get("s", row.get("state")) if isinstance(row, dict) else None
        if value == "unavailable" and prev is not None and prev != "unavailable":
            count += 1
        if value is not None:
            prev = value
    return count


async def cmd_energy_check(ha, args):
    """Check every Energy-dashboard source against states, metadata, history.

    Read-only: ``energy/get_prefs``, ``get_states``, ``recorder/list_statistic_ids``,
    ``recorder/statistics_during_period`` (daily buckets) and
    ``history/history_during_period`` (24 h, for unavailable flicker).  Exit 1
    when anything is found -- or when no sources are configured at all.
    """
    if args.days < 1:
        raise UsageError("--days must be at least 1")
    prefs = await ha.cmd(type="energy/get_prefs")
    sources = energy_sources(prefs)
    if not sources:
        msg = "no energy sources configured in energy/get_prefs -- nothing checked"
        if args.json:
            print(json.dumps({"days": args.days, "sources": [], "findings": 0,
                              "error": msg}, ensure_ascii=False, indent=2))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1
    have = await fetch_states(ha)
    try:
        meta_rows = await ha.cmd(type="recorder/list_statistic_ids") or []
        meta = {m.get("statistic_id"): m for m in meta_rows
                if isinstance(m, dict)}
    except HAError:
        meta = None
    tz = None
    try:
        cfg = await ha.cmd(type="get_config")
        from zoneinfo import ZoneInfo
        tz = ZoneInfo((cfg or {}).get("time_zone") or "UTC")
    except Exception:  # unknown zone / no tzdata: derived from the buckets below
        tz = None
    now = dt.datetime.now(dt.timezone.utc)
    baseline_days = max(args.days, 14)
    ids = sorted({s["statistic_id"] for s in sources})
    probe_start = now - dt.timedelta(days=baseline_days + 1)
    stats = await ha.cmd(
        type="recorder/statistics_during_period",
        start_time=iso(probe_start), end_time=iso(now),
        statistic_ids=ids, period="day", types=["change", "sum"]) or {}
    if tz is None:
        # Daily buckets start at *local* midnight.  Without tzdata the fixed
        # offset is read off a bucket start (|offset| <= 12 h); a DST change
        # inside the window can shift it by an hour, which only moves labels.
        tz = dt.timezone.utc
        for rows in stats.values():
            first = epoch_to_datetime((as_list(rows) or [{}])[-1].get("start"))
            if first is not None:
                hours = first.hour + first.minute / 60
                tz = dt.timezone(dt.timedelta(
                    hours=-hours if hours <= 12 else 24 - hours))
                break
    entity_ids = [i for i in ids if i in have]
    history = {}
    if entity_ids:
        history = await ha.cmd(
            type="history/history_during_period",
            start_time=iso(now - dt.timedelta(hours=24)), end_time=iso(now),
            entity_ids=entity_ids, minimal_response=True,
            no_attributes=True, significant_changes_only=False) or {}

    results = []
    for source in sources:
        sid = source["statistic_id"]
        findings: list = []
        state = have.get(sid)
        info = {"role": source["role"], "statistic_id": sid,
                "label": mask_emails(str(source["label"] or "")),
                "state": None, "unit": None, "state_class": None,
                "device_class": None}
        if state is None:
            if ":" not in sid:  # "domain:name" ids are external statistics
                findings.append("entity has no state (missing or disabled)")
        else:
            attrs = state.get("attributes") or {}
            info.update(state=state.get("state"),
                        unit=attrs.get("unit_of_measurement"),
                        state_class=attrs.get("state_class"),
                        device_class=attrs.get("device_class"))
            if str(state.get("state")).lower() in ("unavailable", "unknown"):
                findings.append(f"state is {state.get('state')}")
            if info["state_class"] not in ("total", "total_increasing"):
                findings.append(
                    f"state_class is {info['state_class']!r}; an energy source "
                    "needs total or total_increasing")
            allowed = GAS_UNITS if source["role"] in ("gas", "water") \
                else ENERGY_UNITS
            if source["role"] == "water":
                allowed = None  # water units vary widely; not judged
            if allowed and info["unit"] not in allowed:
                findings.append(f"unit {info['unit']!r} is not a unit for "
                                f"{source['role']}")
        if meta is None:
            info["statistics_metadata"] = None
        else:
            row = meta.get(sid)
            info["statistics_metadata"] = (
                None if row is None else
                {"has_sum": row.get("has_sum"),
                 "unit": row.get("statistics_unit_of_measurement",
                                 row.get("unit_of_measurement")),
                 "source": row.get("source")})
            if row is None:
                findings.append("no statistics metadata (never recorded?)")
            elif not row.get("has_sum"):
                findings.append("statistics have no sum (wrong state_class?)")
        days, day_findings = analyse_daily(
            as_list(stats.get(sid)), now, args.days, tz)
        info["days"] = days
        findings += day_findings
        flicker = count_unavailable_transitions(history.get(sid))
        info["unavailable_transitions_24h"] = flicker
        if flicker >= args.flicker_min:
            findings.append(f"flicker: {flicker} unavailable transitions in 24 h")
        info["findings"] = findings
        results.append(info)

    total = sum(len(r["findings"]) for r in results)
    if args.json:
        print(json.dumps({"days": args.days, "timezone": str(tz),
                          "sources": results, "findings": total},
                         ensure_ascii=False, indent=2))
    else:
        print(f"== energy-check: {len(results)} sources, last {args.days} "
              f"days (tz {tz}) ==")
        for info in results:
            unit = f" {info['unit']}" if info.get("unit") else ""
            mark = "!" if info["findings"] else " "
            print(f" {mark} {info['role']:<12} {info['statistic_id']}  "
                  f"[{info['label']}]")
            print(f"       state={info['state']}{unit} "
                  f"class={info['state_class']} dev={info['device_class']} "
                  f"flicker24h={info['unavailable_transitions_24h']}")
            cells = ["  ".join(
                f"{d['day'][5:]}="
                + (f"{d['change']:.4g}" if isinstance(d["change"], (int, float))
                   else "-") for d in info["days"])]
            print(f"       days: {cells[0]}")
            for finding in info["findings"]:
                print(f"       *** {finding}")
        print(f"\n{total} findings" if total else "\nno findings")
    return 1 if total else 0


async def cmd_entity_ids(ha, args):
    """Print entity ids matching a pattern -- useful to build shell loops.

    A pattern that matches no entity at all exits 1, the same way
    ``health --filter`` does.  ``for id in $(... entity-ids --pattern x)``
    silently iterates zero times on a typo, which looks exactly like a loop
    that ran and found nothing to do.  A caller that wants the old behaviour
    can append ``|| true``.
    """
    states = await ha.cmd(type="get_states")
    needle = args.pattern.lower() if args.pattern else None
    ids = sorted(
        s["entity_id"] for s in states
        if needle is None or needle in s["entity_id"].lower()
        or needle in str((s.get("attributes") or {}).get("friendly_name", "")).lower()
    )
    emit(ids, args.json, lambda data: print("\n".join(data)))
    if needle and not ids:
        print(f"error: no entity id or name contains {args.pattern!r}",
              file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ha_verify.py",
        description="Read-only health checks for Home Assistant entities, "
        "statistics and dashboards. Exit code 1 when a problem is found.",
    )
    add_common_args(parser)
    sub = parser.add_subparsers(dest="subcommand")
    parent = argparse.ArgumentParser(add_help=False)
    add_common_args(parent, suppress=True)

    def new(name, help_text):
        return sub.add_parser(name, help=help_text, parents=[parent])

    p = new("states", "show the state of the given entities (missing = error)")
    p.add_argument("entity_ids", nargs="+")
    p.set_defaults(func=cmd_states)

    p = new("health", "list unknown/unavailable entities, optionally filtered")
    p.add_argument("--filter", default=None, help="substring of id or name")
    p.add_argument("--include-unused", action="store_true",
                   help="also count button/event/scene, whose normal state is "
                        "'unknown' until they are used once")
    p.add_argument("--by-device", action="store_true",
                   help="group unavailable entities by device; a device is "
                        "offline when all its primary entities are unavailable")
    p.set_defaults(func=cmd_health)

    p = new("stats", "daily/hourly long-term statistics for entities")
    p.add_argument("statistic_ids", nargs="+")
    p.add_argument("--start", default="7d", help="'7d', '12h' or ISO timestamp")
    p.add_argument("--end", default=None, help="'now' (default) or ISO timestamp")
    p.add_argument("--period", default="day",
                   choices=["5minute", "hour", "day", "week", "month"])
    p.add_argument("--types", default="change,sum",
                   help="comma list of change,sum,mean,min,max,state,last_reset")
    p.add_argument("--units", default=None,
                   help='JSON unit map, e.g. \'{"energy":"kWh"}\'')
    p.add_argument("--tail", type=int, default=10, help="rows to print")
    p.set_defaults(func=cmd_stats)

    p = new("stats-range", "first/last bucket per statistic (is data still there?)")
    p.add_argument("statistic_ids", nargs="+")
    p.add_argument("--since", default="365d", help="'90d' or ISO timestamp")
    p.set_defaults(func=cmd_stats_range)

    p = new("history", "state history (minimal_response safe)")
    p.add_argument("entity_ids", nargs="+")
    p.add_argument("--hours", type=float, default=6)
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--full", action="store_true",
                   help="minimal_response=False (note: the WebSocket answer "
                        "still uses the short keys s/a/lu)")
    p.add_argument("--with-attributes", action="store_true")
    p.add_argument("--significant-only", action="store_true")
    p.add_argument("--tail", type=int, default=20)
    p.add_argument("--no-exists-check", action="store_true",
                   help="do not verify the ids against the state machine "
                        "(an unknown id then returns 0 rows and exit 0)")
    p.set_defaults(func=cmd_history)

    p = new("dashboard-url", "check a storage-mode dashboard over the API")
    p.add_argument("url_path_pos", nargs="?", default=None, metavar="URL_PATH",
                   help="same as --url-path (positional form)")
    p.add_argument("--url-path", default=None,
                   help="dashboard url_path; omit for the default dashboard")
    p.add_argument("--with-registry", action="store_true",
                   help="cross-check against the entity registry too")
    p.set_defaults(func=cmd_dashboard_url)

    p = new("dashboard", "check a dashboard stored as a .storage JSON file")
    p.add_argument("--file", required=True,
                   help="e.g. /config/.storage/lovelace.my_dashboard")
    p.add_argument("--with-registry", action="store_true")
    p.set_defaults(func=cmd_dashboard)

    p = new("dashboards", "dashboard health: referenced / missing / unavailable "
                          "/ disabled per dashboard, dead ones flagged")
    p.add_argument("--sort", default="name",
                   choices=["name", "problems", "missing", "entities"],
                   help="order rows (problems = worst share first)")
    p.set_defaults(func=cmd_dashboard_list)

    p = new("energy-check", "check every Energy-dashboard source: state, "
                            "statistics, daily sums, gaps, jumps, flicker")
    p.add_argument("--days", type=int, default=3,
                   help="days of daily sums to check (default 3)")
    p.add_argument("--flicker-min", type=int, default=FLICKER_MIN,
                   help="unavailable transitions in 24 h that count as "
                        f"flicker (default {FLICKER_MIN})")
    p.set_defaults(func=cmd_energy_check)

    p = new("entity-ids", "print matching entity ids, one per line")
    p.add_argument("--pattern", default=None)
    p.set_defaults(func=cmd_entity_ids)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _apply_common_defaults(args)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2

    async def run():
        async with HAConnection(
            url=args.url, token_file=args.token_file, ws_url=args.ws_url,
            allow_write=False, timeout=args.timeout,
        ) as ha:
            return await args.func(ha, args)

    try:
        return asyncio.run(run()) or 0
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
