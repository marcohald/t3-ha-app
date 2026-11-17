#!/usr/bin/env python3
"""ha_dashboard.py -- create, inspect, patch and back up Lovelace dashboards.

Works with **storage mode** dashboards (the ones the UI edits).  A YAML-mode
dashboard lives in ``configuration.yaml`` and must be edited as YAML instead.

Safety
------
* ``show``, ``list``, ``cards``, ``entity-refs``, ``diff``, ``backup`` and
  ``validate`` are read-only.
* ``create``, ``save``, ``delete`` and ``patch`` need ``--allow-write``.
* ``patch`` is a **dry run** unless ``--apply`` is also given, and
  ``--backup-dir`` writes a timestamped copy of the current config before the
  first save.  Restoring is a plain file copy, so keep the backups.

Typical workflow::

    ha_dashboard.py list
    ha_dashboard.py export --url-path my-dash --out dash.json   # read-only
    ha_dashboard.py patch --url-path my-dash --swap old_sensor=new_sensor --dry-run
    ha_dashboard.py patch --url-path my-dash --swap old_sensor=new_sensor --allow-write --apply
    ha_dashboard.py create --url-path my-dash --title "My Dash" --allow-write

Entity swaps
------------
``--swap`` accepts ``old=new`` pairs and rewrites every occurrence in the whole
config tree: the ``entity`` key of a card, plain strings inside ``entities``
lists, and entity references inside templates.  ``--rename`` sets a display
``name`` for a swapped entity so a card does not keep the old label.
``--add`` appends entity rows to a card matched by ``--card-title`` (or by
``--card-index``).

url_path rule
-------------
``lovelace/dashboards/create`` rejects a ``url_path`` without a hyphen with
``invalid_format`` -- ``my_dash`` fails, ``my-dash`` works.  This script checks
that up front and tells you before Home Assistant does.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as dt
import difflib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ha_ws import (  # noqa: E402  (the path fixup above is required under -I)
    HAConnection,
    HAError,
    UsageError,
    WriteRefused,
    ConnectError,
    add_common_args,
    _apply_common_defaults,
    as_list,
    collect_entity_ids,
    dashboard_label,
    fetch_config,
    iter_cards,
    iter_view_cards,
    quiet_broken_pipe,
    redact,
)

#: The state machine, for resolving service/perform_action values.  Imported
#: from ``ha_verify`` rather than reimplemented: a second copy of the
#: service-vs-entity rule is how the two commands drifted apart before.
from ha_verify import fetch_states  # noqa: E402

#: url_path must contain a hyphen; Home Assistant answers 'invalid_format'.
_URL_WORD = r"[a-z0-9]+"
#: Home Assistant requires **at least one hyphen** in a url_path; the ``+`` is
#: not cosmetic.  With ``*`` a one-word slug like ``mydash`` passed here and was
#: then rejected by the API with 'invalid_format: Url path needs to contain a
#: hyphen (-)' -- a local check that lets the bad value through is worse than
#: none, because it moves the failure somewhere harder to read.
URL_PATH_RE = re.compile(rf"^{_URL_WORD}(?:-{_URL_WORD})+$")

#: Keys whose value is a service name *or* a script entity.  ``apply_swaps``
#: used to skip them wholesale, so ``--swap script.old=script.new`` left the
#: reference in place on a card that calls the script.
SERVICE_SWAP_KEYS = frozenset({"service", "perform_action"})


def valid_url_path(url_path: str) -> bool:
    """True when the url_path passes Home Assistant's own validation."""
    return bool(URL_PATH_RE.fullmatch(url_path or ""))


def _usage_error(message: str) -> None:
    """Fail with exit 2 for a bad argument or an impossible request.

    Kept as the local spelling of :class:`UsageError` because it is called on
    the way *into* subcommands, where raising through ``main()`` keeps one
    place that formats the message.  Passing the text without a leading
    ``error:`` -- ``main()`` adds it, and a doubled prefix is how it looked
    before this was unified.
    """
    raise UsageError(message)


def _load_json_file(path: str):
    """Read a dashboard JSON file, unwrapping a ``.storage`` container.

    Raises :class:`SystemExit` with a readable message on a missing or broken
    file.  Every subcommand that takes ``--file`` used to let
    ``FileNotFoundError`` / ``JSONDecodeError`` escape as a traceback, because
    neither was in ``main()``'s ``except`` clause.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except OSError as exc:
        _usage_error(f"cannot read {path}: {exc}")
    except json.JSONDecodeError as exc:
        _usage_error(f"{path} is not valid JSON: {exc}")
    except UnicodeDecodeError as exc:
        # A binary file dies inside ``json.load`` with a UnicodeDecodeError,
        # which is neither OSError nor JSONDecodeError: it escaped to main()'s
        # operation-error handler and exited 1, while every other unreadable
        # ``--file`` exits 2.  Measured with ``--file /bin/ls``.
        _usage_error(f"{path} is not a UTF-8 text file: {exc}")
    if isinstance(stored, dict):
        # ``stored.get("data", {}).get("config", stored)`` only falls back when
        # the *key* is missing: a ``{"data": null}`` file handed ``None`` to
        # the second ``.get`` and died with an AttributeError traceback, which
        # README.md and SKILL.md promise never happens for an unreadable file.
        data = stored.get("data")
        if isinstance(data, dict):
            return data.get("config", stored)
    return stored


def check_url_path(url_path: str) -> None:
    if not valid_url_path(url_path):
        # Exit 2 (usage error), as CHANGELOG documents: this is a bad argument,
        # not a failed operation.  `raise SystemExit(str)` would have exited 1.
        raise UsageError(
            f"url_path {url_path!r} is invalid. Home Assistant requires "
            "lowercase words joined by hyphens (e.g. 'my-dash'); a value like "
            "'my_dash' or 'mydash' fails with 'invalid_format'."
        )


#: Card types that render a list of entities.  ``--add`` needs one of these:
#: appending an ``entities`` key to a ``heading`` or ``tile`` card produces a
#: config Home Assistant ignores, while the CLI reports ``added: 1``.
#:
#: Measured on the reference install over all 12 dashboards: only these three
#: types carry an ``entities`` list.  ``grid``/``*-stack`` hold ``cards``,
#: ``tile``/``button``/``gauge`` hold a single ``entity`` -- listing them here
#: would have let ``--add`` write a key they ignore.
CARD_TYPES_WITH_ENTITY_LIST = frozenset(
    {"entities", "history-graph", "statistics-graph"}
)


# --------------------------------------------------------------------------
# tree helpers
# --------------------------------------------------------------------------


def walk(node, visitor, path=()):
    """Call ``visitor(node, path)`` for every dict/list node in the tree."""
    visitor(node, path)
    if isinstance(node, dict):
        for key, value in node.items():
            walk(value, visitor, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            walk(value, visitor, path + (index,))
    return node


def iter_card_containers(node):
    """Yield every card dict, including cards that hold a single ``entity``.

    The old version only yielded dicts with an ``entities`` *list*.  A sections
    dashboard made of tile/heading cards has no such list -- on a real install
    ``show`` counted 21 cards and ``cards`` found 0, so ``patch --card-index``
    could not address any of them.  ``iter_cards`` (shared, order-stable) covers
    both layouts; nested stack/grid cards are included.
    """
    yield from iter_cards(node if isinstance(node, dict) else {"views": [node]})


def entity_row_id(item):
    """The entity id of one ``entities`` list entry, whatever shape it is.

    The list holds plain strings or ``{"entity": ...}`` objects -- but a
    hand-written config can also hold a number, ``null`` or a nested list, and
    ``i if isinstance(i, str) else i.get("entity")`` crashed on those with
    ``AttributeError: 'int' object has no attribute 'get'``.
    """
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        value = item.get("entity")
        return value if isinstance(value, str) else None
    return None


def entity_row_ids(entities) -> list:
    """The entity ids of an ``entities`` value, tolerating a non-list."""
    return [eid for item in as_list(entities) if (eid := entity_row_id(item))]


def parse_pairs(values, *, what: str = "old=new") -> dict:
    """Parse ``old=new`` command line pairs.

    ``what`` names the expected shape in the error message: the same function
    serves ``--swap old=new`` (two entity ids) and ``--rename entity=label``
    (an entity and a display name), and telling a --rename caller that both
    sides must be entity ids was wrong.
    """
    pairs = {}
    for item in values or []:
        if "=" not in item:
            _usage_error(f"expected {what}, got {item!r}")
        key, _, value = item.partition("=")
        key, value = key.strip(), value.strip()
        # An empty side is not a small typo, it is an unbounded edit: with
        # ``old == ""`` the word-boundary regex in ``_swap_in_text`` matches
        # at every punctuation mark, so ``--swap =b`` rewrote 99 places in
        # one dashboard and still exited 0.  ``--swap id=`` swapped an entity
        # for an empty string.  Both are impossible requests.
        if not key or not value:
            _usage_error(
                f"{item!r} has an empty "
                f"{'left' if not key else 'right'} side -- expected {what}")
        pairs[key] = value
    return pairs


def _swap_in_text(text: str, swaps: dict, stats: dict, hits: set) -> str:
    """Replace whole entity ids inside free text / templates.

    A plain ``str.replace`` rewrote prefixes too: swapping ``sensor.pv`` turned
    ``{{ states('sensor.pv_total') }}`` into ``sensor.solar_total``, a name that
    does not exist.  An entity id ends where a character that may not appear in
    one begins, so a trailing boundary is enough on the left and ``(?!\w)`` is
    enough on the right (``\b`` alone would not allow the ``_`` in ``_total``
    inside the match, which is what we want, but ``\b`` after ``pv`` does match
    before ``_`` -- hence the explicit lookahead).
    """
    for old, new in swaps.items():
        pattern = re.compile(r"(?<![\w.])" + re.escape(old) + r"(?![\w])")
        replaced, count = pattern.subn(new, text)
        if count:
            text = replaced
            stats["swaps"] += count
            hits.add(old)
    return text


def apply_swaps(node, swaps: dict, renames: dict, stats: dict, hits: set):
    """Rewrite entity references in place.

    Replaced are: the ``entity`` key of a card, bare strings inside an
    ``entities`` list, and entity ids appearing in a plain string (e.g. a
    markdown template).  Nothing else is touched, so the diff stays minimal.

    *hits* collects the swap keys that actually matched something.  ``swaps: 0``
    used to be the only signal, and it does not say *which* of several
    ``--swap`` pairs missed -- a typo in one of five looked like a clean run.
    """
    if isinstance(node, dict):
        entity = node.get("entity")
        if isinstance(entity, str) and entity in swaps:
            node["entity"] = swaps[entity]
            stats["swaps"] += 1
            hits.add(entity)
            new_name = renames.get(swaps[entity])
            if new_name and node.get("type") != "markdown":
                node["name"] = new_name
                stats["renames"] += 1
        for key, value in list(node.items()):
            if key == "entities" and isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, str) and item in swaps:
                        new_id = swaps[item]
                        hits.add(item)
                        stats["swaps"] += 1
                        new_name = renames.get(new_id)
                        if new_name and node.get("type") != "markdown":
                            # A bare string has no slot for a display name, so
                            # ``--rename`` was accepted and then dropped for
                            # every pure-string list -- a documented feature
                            # that did nothing.  Upgrading this one row to an
                            # option object is the smallest shape that can
                            # carry the name, and it is valid Lovelace.
                            value[index] = {"entity": new_id, "name": new_name}
                            stats["renames"] += 1
                        else:
                            value[index] = new_id
                    else:
                        apply_swaps(item, swaps, renames, stats, hits)
            elif key in SERVICE_SWAP_KEYS and isinstance(value, str):
                # ``service: script.x`` is an entity reference and must be
                # swappable; ``service: button.press`` is not an entity, and it
                # only lands here when the caller named it explicitly in
                # ``--swap``, which is a deliberate request.
                node[key] = _swap_in_text(value, swaps, stats, hits)
            elif isinstance(value, str) and key not in (
                    "entity", "name", "title", "service", "url", "url_path"):
                node[key] = _swap_in_text(value, swaps, stats, hits)
            else:
                apply_swaps(value, swaps, renames, stats, hits)
    elif isinstance(node, list):
        # Bare entity ids in a list are replaced wherever the list sits, not
        # only under an ``entities`` key: ``target: {entity_id: [...]}`` is a
        # real Lovelace shape and used to keep the old id.  The ``entities``
        # branch above stays because it also walks option objects.
        for index, item in enumerate(node):
            if isinstance(item, str) and item in swaps:
                node[index] = swaps[item]
                stats["swaps"] += 1
                hits.add(item)
            else:
                apply_swaps(item, swaps, renames, stats, hits)
    return node


def promote_mixed_entity_lists(node, stats: dict):
    """Turn bare strings into ``{"entity": ...}`` **only in mixed lists**.

    Lovelace accepts a list of plain strings and a list of option objects.  A
    list mixing both is valid too, but then adding a display name per row is
    impossible, so a patch that adds rows upgrades a mixed list.  Pure-string
    lists are left alone to keep the diff small.
    """
    if isinstance(node, dict):
        entities = node.get("entities")
        if isinstance(entities, list):
            strings = [i for i in entities if isinstance(i, str)]
            dicts = [i for i in entities if isinstance(i, dict)]
            if strings and dicts:
                node["entities"] = [
                    {"entity": i} if isinstance(i, str) else i for i in entities
                ]
                stats["promoted"] += len(strings)
        for value in node.values():
            promote_mixed_entity_lists(value, stats)
    elif isinstance(node, list):
        for item in node:
            promote_mixed_entity_lists(item, stats)
    return node


def _fmt_path(parts) -> str:
    """``("entities", 2, "entity")`` -> ``entities[2].entity``."""
    out = ""
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else (
            f".{part}" if out else str(part))
    return out


def _short(value) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False)
    return text if len(text) <= 80 else text[:77] + "..."


def config_changes(old, new, path=()) -> list:
    """Leaf-level differences between two configs as ``{path, old, new}``.

    Lists are compared by index; surplus entries show up as added (``old`` is
    ``None``) or removed (``new`` is ``None``).  ``path`` is a list of keys and
    indices from the config root.
    """
    changes: list = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in list(old) + [k for k in new if k not in old]:
            if key not in new:
                changes.append({"path": list(path) + [key],
                                "old": old[key], "new": None})
            elif key not in old:
                changes.append({"path": list(path) + [key],
                                "old": None, "new": new[key]})
            else:
                changes += config_changes(old[key], new[key], path + (key,))
    elif isinstance(old, list) and isinstance(new, list):
        for index in range(max(len(old), len(new))):
            if index >= len(new):
                changes.append({"path": list(path) + [index],
                                "old": old[index], "new": None})
            elif index >= len(old):
                changes.append({"path": list(path) + [index],
                                "old": None, "new": new[index]})
            else:
                changes += config_changes(old[index], new[index],
                                          path + (index,))
    elif old != new:
        changes.append({"path": list(path), "old": old, "new": new})
    return changes


def describe_change(config, change) -> str:
    """``view details cards[1]: entity a -> b`` for one :func:`config_changes` row.

    The context is the view (its ``path``, else ``title``, else ``#index``) and
    the first card on the way down, as ``cards[i]`` or
    ``sections[s].cards[i]``; the rest of the path is relative to that card.
    """
    parts = list(change["path"])
    where = ""
    if len(parts) >= 2 and parts[0] == "views" and isinstance(parts[1], int):
        views = as_list(config.get("views")) if isinstance(config, dict) else []
        view = views[parts[1]] if parts[1] < len(views) else {}
        name = (view.get("path") or view.get("title") or f"#{parts[1]}") \
            if isinstance(view, dict) else f"#{parts[1]}"
        rest = parts[2:]
        ctx: list = []
        if rest[:1] == ["sections"] and len(rest) >= 2:
            ctx += rest[:2]
            rest = rest[2:]
        if rest[:1] == ["cards"] and len(rest) >= 2:
            ctx += rest[:2]
            rest = rest[2:]
        where = f"view {name}" + (f" {_fmt_path(ctx)}" if ctx else "")
        parts = rest
    rel = _fmt_path(parts)
    if change["old"] is None:
        what = f"added {_short(change['new'])}"
    elif change["new"] is None:
        what = f"removed {_short(change['old'])}"
    else:
        what = f"{_short(change['old'])} -> {_short(change['new'])}"
    head = f"{where}: " if where else ""
    return f"{head}{rel + ' ' if rel else ''}{what}"


def config_diff(old, new, label: str) -> str:
    """Unified diff of two configs, rendered as indented JSON."""
    def lines(cfg):
        return json.dumps(cfg, ensure_ascii=False, indent=2).splitlines()
    return "\n".join(difflib.unified_diff(
        lines(old), lines(new), f"{label} (before)", f"{label} (after)",
        lineterm="", n=3))


def find_card(config, *, title=None, index=None):
    """Find a card by its title or by its index in ``iter_cards`` order.

    The index is the one ``cards`` prints, so ``patch --card-index N`` addresses
    exactly the card the user saw in that listing.
    """
    for position, card in enumerate(iter_cards(config)):
        if index is not None and position == index:
            return card
        if title is not None and card.get("title") == title:
            return card
    return None


# --------------------------------------------------------------------------
# subcommands: read-only
# --------------------------------------------------------------------------


async def cmd_list(ha, args):
    dashboards = await ha.cmd(type="lovelace/dashboards/list")
    rows = sorted(
        (
            {
                "url_path": d.get("url_path"),
                "title": d.get("title"),
                "mode": d.get("mode"),
                "icon": d.get("icon"),
                "show_in_sidebar": d.get("show_in_sidebar"),
                "require_admin": d.get("require_admin"),
            }
            for d in dashboards
        ),
        key=lambda r: r["url_path"] or "",
    )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for row in rows:
        flag = "" if valid_url_path(row["url_path"] or "") else "  <-- odd url_path"
        path_str = str(row["url_path"] or "")
        print(f"  {path_str:<30} {row['title']!r:<28} mode={row['mode']}"
              f" sidebar={row['show_in_sidebar']}{flag}")
    print(f"\n{len(rows)} dashboards")
    return 0


async def cmd_show(ha, args):
    config = await fetch_config(ha, args.url_path)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
        if args.json:
            print(json.dumps({"written": args.out, "url_path": args.url_path},
                             ensure_ascii=False, indent=2))
        else:
            print(f"written {args.out}")
        return 0
    if args.json:
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return 0
    print(f"title: {config.get('title')!r}")
    # ``config.get("views") or []`` and ``view.get(...)`` assumed a shaped
    # config.  ``ha_verify.py`` was hardened against hand-written files in
    # round 8 and this reader was not: ``{"views": ["kaputt"]}`` crashed with
    # an AttributeError traceback instead of saying which view is odd.
    for index, view in enumerate(as_list(config.get("views"))):
        if not isinstance(view, dict):
            print(f"view[{index}] is not an object, "
                  f"got {type(view).__name__}: {view!r}")
            continue
        # Counted with the *shared* iterator so this number matches `cards` and
        # the indices `patch --card-index` uses.  Counting only the top-level
        # containers reported 3 cards where `cards` listed 36 (grid cards nest).
        cards = list(iter_view_cards(view))
        sections = as_list(view.get("sections"))
        print(f"view[{index}] path={view.get('path')!r} title={view.get('title')!r} "
              f"icon={view.get('icon')} cards={len(cards)} sections={len(sections)}")
        for card_index, card in enumerate(cards):
            print(f"    card[{card_index}] type={card.get('type')} "
                  f"title={card.get('title')!r} "
                  f"entities={len(as_list(card.get('entities')))}")
    return 0


async def cmd_export(ha, args):
    config = await fetch_config(ha, args.url_path)
    text = json.dumps(config, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        if args.json:
            print(json.dumps(
                {"written": args.out, "bytes": len(text),
                 "url_path": args.url_path},
                ensure_ascii=False, indent=2))
        else:
            print(f"written {args.out} ({len(text)} bytes)")
    else:
        print(text)
    return 0


async def cmd_entity_refs(ha, args):
    config = await fetch_config(ha, args.url_path)
    # Resolve service/perform_action values against the instance, exactly as
    # ``ha_verify.py dashboard-url`` does.  Without *known_ids* the helper
    # cannot tell ``script.party_qr_code_aktualisieren`` from ``button.press``,
    # so it dropped both and this command reported 8 references where the
    # dashboard has 10 -- while the sibling command reported 10 for the same
    # dashboard.  The extra get_states round is the price of agreeing.
    have = await fetch_states(ha)
    references = sorted(collect_entity_ids(config, known_ids=set(have)))
    result = {"url_path": args.url_path, "count": len(references),
              "entities": references}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for entity_id in references:
            print(entity_id)
        print(f"\n{len(references)} entity references")
    return 0


async def cmd_cards(ha, args):
    config = await fetch_config(ha, args.url_path)
    rows = []
    for container in iter_card_containers(config):
        entities = entity_row_ids(container.get("entities"))
        # tile/gauge/statistic/button cards carry one ``entity`` instead of a
        # list; they used to print "(0 entities)" with the id only in the name.
        single = container.get("entity")
        if isinstance(single, str) and single not in entities:
            entities = [single] + entities
        rows.append({
            "type": container.get("type"),
            "title": container.get("title"),
            "entity": container.get("entity"),
            "count": len(entities),
            "entities": entities,
        })
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for index, row in enumerate(rows):
        name = row["title"] or row["entity"] or ""
        card_type = str(row["type"] or "?")
        print(f"[{index}] {card_type:<18} {name!r} ({row['count']} entities)")
        for entity_id in row["entities"]:
            print(f"        {entity_id}")
    return 0


def _validate_roots(view, index: int):
    """Yield ``(path, cards)`` for each place a view keeps its cards."""
    cards = as_list(view.get("cards"))
    if cards:
        yield f"view[{index}].cards", cards
    for section_index, section in enumerate(as_list(view.get("sections"))):
        if isinstance(section, dict):
            yield (f"view[{index}].sections[{section_index}].cards",
                   as_list(section.get("cards")))


def _iter_cards_with_path(path: str, cards):
    """Yield ``(path, card)`` for a card list and everything nested under it.

    The path is a location a reader can follow in the JSON, e.g.
    ``view[0].cards[2].cards[0]``.
    """
    for index, card in enumerate(as_list(cards)):
        yield f"{path}[{index}]", card
        if isinstance(card, dict):
            # ``or []`` let a truthy non-list (``"cards": 1``) through and
            # enumerate() died with a TypeError traceback.
            yield from _iter_cards_with_path(
                f"{path}[{index}].cards", as_list(card.get("cards")))


async def cmd_validate(ha, args):
    """Structural checks.  ``--file`` needs no Home Assistant at all."""
    if args.file:
        config = _load_json_file(args.file)
        label = args.file
    else:
        config = await fetch_config(ha, args.url_path)
        label = dashboard_label(args.url_path)

    problems = []
    if not isinstance(config, dict):
        # An array or a scalar in the file used to crash on ``config.get``.
        print(f"error: {label}: the dashboard config must be a JSON object, "
              f"got {type(config).__name__}", file=sys.stderr)
        return 2
    views = as_list(config.get("views"))
    if not views:
        problems.append("no views defined")
    for index, view in enumerate(views):
        if not isinstance(view, dict):
            # A config is hand-editable JSON: ``"views": ["text"]`` is a
            # realistic typo and used to crash on ``view.get``.
            problems.append(f"view[{index}] is not an object, got "
                            f"{type(view).__name__}: {view!r}")
            continue
        sections = as_list(view.get("sections"))
        if not as_list(view.get("cards")) and not sections:
            problems.append(f"view[{index}] has neither cards nor sections")
        # ``as_list`` silently drops a non-list, and the section walk below
        # skipped non-object sections: ``"sections": ["text"]`` and
        # ``{"cards": 1}`` in a section validated as "no structural problems".
        for key in ("cards", "sections"):
            raw = view.get(key)
            if raw is not None and not isinstance(raw, list):
                problems.append(f"view[{index}].{key} is not a list, got "
                                f"{type(raw).__name__}: {raw!r} (it is ignored)")
        for section_index, section in enumerate(sections):
            where = f"view[{index}].sections[{section_index}]"
            if not isinstance(section, dict):
                problems.append(f"{where} is not an object, got "
                                f"{type(section).__name__}: {section!r}")
                continue
            raw = section.get("cards")
            if raw is not None and not isinstance(raw, list):
                problems.append(f"{where}.cards is not a list, got "
                                f"{type(raw).__name__}: {raw!r} (it is ignored)")
        # Walk the *nested* cards too.  A grid/stack card holds its cards under
        # ``cards``, and the flat pass here missed them entirely: a grid with a
        # typeless card inside validated as "no structural problems found"
        # with exit 0.  The path names the real location, so the message can be
        # followed in the JSON.
        containers = []
        for root, cards in _validate_roots(view, index):
            containers.extend(_iter_cards_with_path(root, cards))
        # ``"cards": 1`` is silently ignored rather than fatal, so report it:
        # it is a mistake the author wants to know about, and the previous
        # behaviour was a TypeError traceback.
        for card_path, card in containers:
            raw = card.get("cards") if isinstance(card, dict) else None
            if raw is not None and not isinstance(raw, list):
                problems.append(
                    f"{card_path}.cards is not a list, got "
                    f"{type(raw).__name__}: {raw!r} (it is ignored)")
        for where, card in containers:
            if not isinstance(card, dict) or not card.get("type"):
                problems.append(f"{where}: a card has no type")
                continue
            card_type = str(card.get("type"))
            if (isinstance(card.get("entities"), list)
                    and card_type not in CARD_TYPES_WITH_ENTITY_LIST):
                # `patch --add` on such a card used to report success while the
                # frontend ignored the list.  Flag it at validation time too.
                problems.append(
                    f"{where}: a {card_type!r} card has an 'entities' list, "
                    f"which this card type ignores")
    # A missing top-level ``title`` is **not** a problem in storage mode: the
    # title of a dashboard lives in the dashboard registry entry, not in its
    # config blob.  Measured on a working install: 7 of 12 dashboards have
    # ``title: None`` here and a perfectly good title in the registry.

    if args.json:
        print(json.dumps({"dashboard": label, "problems": problems},
                         ensure_ascii=False, indent=2))
    else:
        print(f"== {label} ==")
        if problems:
            for problem in problems:
                print(f"   *** {problem}")
        else:
            print("   no structural problems found")
    return 1 if problems else 0


async def cmd_diff(ha, args):
    """Compare a local JSON file with the live dashboard config."""
    local = _load_json_file(args.file)
    # The only --file reader that skipped this: a valid JSON list produced
    # "file references 0 entities" and exit 1, while validate/save/save-check
    # reject the same file with exit 2.
    if not isinstance(local, dict):
        _usage_error(
            f"{args.file}: the dashboard config must be a JSON object, got "
            f"{type(local).__name__}")
    live = await fetch_config(ha, args.url_path)
    local_text = json.dumps(local, ensure_ascii=False, indent=2, sort_keys=True)
    live_text = json.dumps(live, ensure_ascii=False, indent=2, sort_keys=True)
    identical = local_text == live_text
    # Both sides resolve against the instance, for the same reason as
    # ``entity-refs``: a ``service: script.x`` reference has to be seen on
    # whichever side it sits, otherwise the one-sided diff that this command
    # exists to produce reports the two sides as equal.
    known_ids = set(await fetch_states(ha))
    local_ids = collect_entity_ids(local, known_ids=known_ids)
    live_ids = collect_entity_ids(live, known_ids=known_ids)
    only_live = sorted(live_ids - local_ids)
    only_local = sorted(local_ids - live_ids)

    if args.json:
        # --json must emit *only* JSON: printing the prose first made the whole
        # output unparseable, which defeats the point of the flag.
        print(json.dumps(
            {
                "identical": identical,
                "live_entities": len(live_ids),
                "file_entities": len(local_ids),
                "only_live": only_live,
                "only_local": only_local,
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0 if identical else 1

    if identical:
        print("identical")
        return 0
    print(f"different: live references {len(live_ids)} entities, "
          f"file references {len(local_ids)}")
    for entity_id in only_live:
        print(f"  only in live: {entity_id}")
    for entity_id in only_local:
        print(f"  only in file: {entity_id}")
    return 1


async def cmd_backup(ha, args):
    config = await fetch_config(ha, args.url_path)
    directory = args.out_dir or getattr(args, "backup_dir", None) or "."
    os.makedirs(directory, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = args.url_path or "default"
    path = os.path.join(directory, f"lovelace.{stem}.{stamp}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
    if args.json:
        # --json means *only* JSON, in every subcommand: printing prose here
        # made `backup --json` unparseable and contradicted the promise.
        print(json.dumps({"backup": path, "url_path": args.url_path},
                         ensure_ascii=False, indent=2))
    else:
        print(f"backup written: {path}")
    return 0


# --------------------------------------------------------------------------
# subcommands: mutating
# --------------------------------------------------------------------------


async def cmd_create(ha, args):
    # check_url_path() also runs here so a direct call cannot bypass it, but
    # main() already rejects a bad url_path before connecting -- SKILL.md and
    # references/dashboards.md promise a *local* check, and an unreachable
    # instance must not be able to mask a malformed path.
    check_url_path(args.url_path)
    dashboards = await ha.cmd(type="lovelace/dashboards/list")
    existing = {d.get("url_path") for d in dashboards}
    if args.url_path in existing:
        print(f"dashboard {args.url_path!r} already exists -- not creating. "
              "Use 'save' to write its config.")
        return 0
    await ha.cmd(
        type="lovelace/dashboards/create",
        url_path=args.url_path,
        title=args.title or args.url_path,
        icon=args.icon,
        show_in_sidebar=not args.hidden,
        require_admin=args.require_admin,
    )
    print(f"created {args.url_path!r} (title={args.title or args.url_path!r})")
    return 0


async def cmd_save(ha, args):
    if args.file:
        config = _load_json_file(args.file)
    elif args.config:
        # A malformed --config is a bad argument, so it belongs on exit 2 like
        # --units/--post-body; the bare JSONDecodeError fell through as a
        # ValueError and exited 1.
        try:
            config = json.loads(args.config)
        except json.JSONDecodeError as exc:
            _usage_error(f"--config must be JSON: {exc}")
    else:
        _usage_error("give --file <json> or --config '<json>'")
    if not isinstance(config, dict):
        _usage_error("the config must be a JSON object")
    await _backup(ha, args, args.url_path)
    await ha.cmd(type="lovelace/config/save", url_path=args.url_path, config=config)
    print(f"saved config to {args.url_path!r}")
    return 0


async def resolve_dashboard_id(ha, url_path: str) -> str:
    """Return the registry ``id`` for a ``url_path``.

    ``lovelace/dashboards/update`` and ``.../delete`` address a dashboard by its
    registry id (``dashboard_licht``), **not** by its ``url_path``
    (``dashboard-licht``).  Sending ``url_path`` there fails with
    ``invalid_format: not a valid option at 'url_path'`` on every call, which
    made ``delete`` impossible to use -- the one destructive subcommand that
    exists.  The two fields differ for all 12 dashboards on the reference
    install, so this lookup is not cosmetic.
    """
    dashboards = await ha.cmd(type="lovelace/dashboards/list")
    for entry in dashboards:
        if entry.get("url_path") == url_path:
            identifier = entry.get("id")
            if identifier:
                return identifier
    known = ", ".join(sorted(
        str(entry.get("url_path")) for entry in dashboards)) or "none"
    raise HAError(
        "not_found",
        f"no dashboard with url_path {url_path!r}. Known dashboards: {known}",
        command="lovelace/dashboards/list",
    )


async def cmd_delete(ha, args):
    if not args.yes:
        _usage_error(
            "refusing to delete without --yes. Deleting a dashboard "
            "removes it for every user of this instance."
        )
    # Resolve the id *before* the backup: a failing lookup must not leave a
    # backup (and a success-looking message) behind for a delete that never
    # happened.
    dashboard_id = await resolve_dashboard_id(ha, args.url_path)
    await _backup(ha, args, args.url_path)
    await ha.cmd(type="lovelace/dashboards/delete", dashboard_id=dashboard_id)
    print(f"deleted {args.url_path!r} (backup kept)")
    return 0


async def cmd_patch(ha, args):
    swaps = parse_pairs(args.swap, what="old=new (two entity ids)")
    renames = parse_pairs(args.rename,
                          what="entity=label (an entity id and a name)")
    if not swaps and not args.add:
        # --rename is deliberately not enough on its own: it renames a swap
        # target or an entity being added, so without either it would be a
        # no-op reported as success.
        _usage_error(
            "nothing to do -- give --swap or --add (--rename only names a "
            "swap target or an entity you --add)")
    if args.add and args.card_title is not None and args.card_index is not None:
        # ``find_card`` walks the cards once and takes whichever selector
        # matches *first*, so two contradicting selectors resolved silently to
        # one of them -- and it was not necessarily the one the caller meant.
        _usage_error(
            "--card-title and --card-index are alternatives, not a pair: "
            "a card that matches one but not the other has no defined "
            "answer. Pick one (use 'cards' to see both)."
        )
    if swaps and not args.add and (
            args.card_title is not None or args.card_index is not None):
        # ``--card-title``/``--card-index`` select the card ``--add`` appends
        # to.  On the ``--swap``-only path they were parsed and then never
        # read, so a caller who passed both believed the replacement was
        # scoped to one card while it ran across the whole dashboard
        # (measured: identical output with and without the index).  Refusing
        # is honest; silently scoping a replacement that people expect to be
        # dashboard-wide would be worse.  With ``--add`` present the selector
        # does its documented job and is accepted.
        _usage_error(
            "--card-title/--card-index only select the card for --add. "
            "--swap rewrites every card of the dashboard; drop the selector "
            "or use --add."
        )

    local = bool(args.file)
    if local:
        # Offline copy, like ``validate --file``: nothing is fetched or saved.
        if args.apply:
            _usage_error("--file patches a local copy and never saves to "
                         "Home Assistant; drop --apply and use --out")
        config = _load_json_file(args.file)
        if not isinstance(config, dict):
            _usage_error(f"{args.file}: the dashboard config must be a JSON "
                         f"object, got {type(config).__name__}")
        label = args.file
    else:
        if not args.url_path:
            _usage_error("give --url-path (live dashboard) or --file "
                         "(local copy)")
        config = await ha.cmd(type="lovelace/config", url_path=args.url_path)
        label = dashboard_label(args.url_path)
    original = copy.deepcopy(config)
    before = json.dumps(config, ensure_ascii=False, sort_keys=True)
    stats = {"swaps": 0, "renames": 0, "added": 0, "promoted": 0}
    hits: set = set()
    #: --add ids that were already present, so their --rename is not reported
    #: as applied.  Filled in by the --add loop below.
    skipped: set = set()
    apply_swaps(config, swaps, renames, stats, hits)
    # A --swap whose old id appears nowhere in the config is the fail-open
    # shape: the run prints "changed: False" and exits 0, which reads as "the
    # dashboard is already patched".  Name the ids that matched nothing and
    # exit 1 -- the caller asked for a replacement that did not happen.
    unmatched = sorted(set(swaps) - hits)
    # A --rename only takes effect on a swap *target* that was actually
    # rewritten, or on an entity `--add` actually appended.  That set is only
    # known after the --add loop below, so the rename half is computed there.

    if args.add:
        # ``--add`` needs a card that renders an entity list.  A heading or tile
        # card silently ignores an ``entities`` key, so writing one there made
        # the CLI report success for a change Home Assistant would never show.
        card = find_card(config, title=args.card_title, index=args.card_index)
        if card is None:
            _usage_error(
                "no card matched --card-title/--card-index; "
                "use 'cards' to list them"
            )
        card_type = str(card.get("type") or "")
        has_list = isinstance(card.get("entities"), list)
        if not has_list and card_type not in CARD_TYPES_WITH_ENTITY_LIST:
            hint = ""
            if card.get("entity"):
                hint = (f" (it holds the single entity "
                        f"{card.get('entity')!r}; swap it with '--swap' instead)")
            _usage_error(
                f"card {card_type or '(no type)'!r} cannot hold an entity "
                f"list -- it would ignore the added entities{hint}. Pick a card "
                "that shows an entity list."
            )
        if not has_list:
            card["entities"] = []
        # entity_row_id, not ``i.get("entity")``: a hand-written list can hold
        # a number or null, and the old comprehension died on it.
        existing = {row for row in entity_row_ids(card["entities"]) if row}
        for entity_id in args.add:
            if entity_id in existing:
                # Skipped, so a --rename aimed at it never ran.  Recording the
                # id lets the unmatched check below report it instead of the
                # command printing ``renames: 0`` and exiting 0.
                skipped.add(entity_id)
                continue
            row = {"entity": entity_id}
            name = renames.get(entity_id)
            if name:
                row["name"] = name
                # Counted like every other applied name: without this the
                # summary said ``renames: 0`` while the row carried the name.
                stats["renames"] += 1
            card["entities"].append(row)
            stats["added"] += 1

    # Promote *after* the rows were appended: promoting first left a list with
    # plain strings next to the new {"entity": ..., "name": ...} object, which
    # is exactly the mixed shape this function exists to avoid.
    promote_mixed_entity_lists(config, stats)

    # The rename half of the unmatched check, now that the --add loop knows
    # which ids it really appended.  An id that was already in the card was
    # skipped, so a --rename aimed at it did not run and must be reported.
    rename_targets = {swaps[old] for old in hits}
    rename_targets |= set(args.add or []) - skipped
    unmatched += [f"{key} (--rename)" for key in sorted(
        set(renames) - rename_targets)]

    after = json.dumps(config, ensure_ascii=False, sort_keys=True)
    changed = before != after

    changes = config_changes(original, config) if changed else []
    # --file without --out: the patched config goes to stdout, so the report
    # moves to stderr.  --json stays a single JSON document on stdout.
    to_stdout = local and not args.out and not args.json
    report = sys.stderr if to_stdout else sys.stdout

    if args.json:
        # --json output must stay parseable: the two prose lines below made
        # `patch --json` a JSONDecodeError.  The unmatched list goes on stderr
        # as well, so a caller reading only the exit code still sees why.
        payload = {"url_path": args.url_path, "changed": changed,
                   "dry_run": bool(local or not changed or args.dry_run
                                   or not args.apply),
                   "unmatched_swaps": unmatched,
                   "changes": changes,
                   **stats}
        if local:
            payload["file"] = args.file
            if not args.out:
                payload["config"] = config
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if unmatched:
            print(f"error: no entity reference matches: {', '.join(unmatched)}",
                  file=sys.stderr)
    else:
        if changes:
            print(f"== changes ({len(changes)}) ==", file=report)
            for change in changes:
                print(f"  {describe_change(original, change)}", file=report)
            print(config_diff(original, config, label), file=report)
            print(file=report)
        print("patch stats:", json.dumps(stats), file=report)
        print("changed:", changed, file=report)
        if unmatched:
            print(f"error: no entity reference matches: {', '.join(unmatched)}",
                  file=sys.stderr)

    if unmatched:
        # Nothing is saved, not even with --apply: a patch that silently drops
        # one of several --swap pairs writes a half-changed dashboard and then
        # reports success, and the caller has no way to notice.
        return 1

    def write_out():
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                json.dump(config, handle, ensure_ascii=False, indent=2)
        elif to_stdout:
            print(json.dumps(config, ensure_ascii=False, indent=2))

    if not changed:
        # ``--out`` still has to be written: the documented workflow is
        # ``patch --out patched.json`` then ``validate --file patched.json``,
        # and returning here left the caller with a missing file and exit 2 on
        # the next step.  An unchanged patch writes the config as it stands.
        write_out()
        if args.out and not args.json:
            print(f"nothing to change; wrote {args.out} unchanged")
        return 0
    if local or args.dry_run or not args.apply:
        if not args.json and not local:
            print("DRY RUN -- nothing saved. Add --allow-write --apply to save.")
        write_out()
        if args.out and not args.json:
            print(f"patched config written to {args.out} for inspection")
        return 0

    await _backup(ha, args, args.url_path)
    await ha.cmd(type="lovelace/config/save", url_path=args.url_path, config=config)
    if not args.json:
        print(f"saved patched config to {args.url_path!r}")
    return 0


async def _backup(ha, args, url_path: str) -> str | None:
    """Write a timestamped copy of the current config before a save.

    A failing backup is a warning, never a hard error -- but it is printed, so
    a silent save without a safety net cannot happen unnoticed.
    """
    directory = getattr(args, "backup_dir", None)
    if not directory:
        # The docstring promises that a save without a safety net cannot happen
        # unnoticed.  Returning silently broke that promise.
        print("warning: no --backup-dir given -- saving without a backup",
              file=sys.stderr)
        return None
    try:
        current = await ha.cmd(type="lovelace/config", url_path=url_path)
    except HAError as exc:
        print(f"warning: no backup written ({exc})", file=sys.stderr)
        return None
    try:
        os.makedirs(directory, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(directory, f"lovelace.{url_path}.{stamp}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(current, handle, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"warning: could not write backup to {directory}: {exc}",
              file=sys.stderr)
        return None
    print(f"backup: {path}")
    return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ha_dashboard.py",
        description="Lovelace storage-mode dashboards: inspect, patch, create, "
        "back up. Mutating subcommands need --allow-write.",
    )
    add_common_args(parser)
    parser.add_argument("--backup-dir", default=None,
                        help="directory for timestamped pre-save backups")
    sub = parser.add_subparsers(dest="subcommand")
    parent = argparse.ArgumentParser(add_help=False)
    add_common_args(parent, suppress=True)
    parent.add_argument("--backup-dir", default=argparse.SUPPRESS)

    def new(name, help_text):
        return sub.add_parser(name, help=help_text, parents=[parent])

    p = new("list", "list dashboards")
    p.set_defaults(func=cmd_list)

    p = new("show", "show a dashboard's structure")
    p.add_argument("url_path_pos", nargs="?", default=None,
                   metavar="URL_PATH", help="same as --url-path")
    p.add_argument("--url-path", default=None,
                 help="dashboard url_path; omit for the default dashboard")
    p.add_argument("--out", default=None, help="dump the raw config to a file")
    p.set_defaults(func=cmd_show)

    p = new("export", "dump the raw config as JSON")
    p.add_argument("url_path_pos", nargs="?", default=None,
                   metavar="URL_PATH", help="same as --url-path")
    p.add_argument("--url-path", default=None,
                 help="dashboard url_path; omit for the default dashboard")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_export)

    p = new("entity-refs", "list every entity the dashboard references")
    p.add_argument("url_path_pos", nargs="?", default=None,
                   metavar="URL_PATH", help="same as --url-path")
    p.add_argument("--url-path", default=None,
                 help="dashboard url_path; omit for the default dashboard")
    p.set_defaults(func=cmd_entity_refs)

    p = new("cards", "list the cards and their entities")
    p.add_argument("url_path_pos", nargs="?", default=None,
                   metavar="URL_PATH", help="same as --url-path")
    p.add_argument("--url-path", default=None,
                 help="dashboard url_path; omit for the default dashboard")
    p.set_defaults(func=cmd_cards)

    p = new("validate", "structural checks, offline with --file")
    p.add_argument("--url-path", default=None)
    p.add_argument("--file", default=None)
    p.set_defaults(func=cmd_validate)

    p = new("diff", "compare a local JSON file with the live config")
    p.add_argument("--url-path", default=None,
                 help="dashboard url_path; omit for the default dashboard")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_diff)

    p = new("backup", "write a timestamped copy of the live config")
    p.add_argument("--url-path", default=None,
                 help="dashboard url_path; omit for the default dashboard")
    p.add_argument("--out-dir", default=None)
    p.set_defaults(func=cmd_backup)

    p = new("create", "create a storage-mode dashboard  [--allow-write]")
    p.add_argument("--url-path", required=True,
                   help="lowercase, hyphenated (e.g. my-dash)")
    p.add_argument("--title", default=None)
    p.add_argument("--icon", default="mdi:view-dashboard")
    p.add_argument("--hidden", action="store_true", help="not in the sidebar")
    p.add_argument("--require-admin", action="store_true")
    p.set_defaults(func=cmd_create)

    p = new("save", "overwrite a dashboard's config  [--allow-write]")
    p.add_argument("--url-path", required=True)
    p.add_argument("--file", default=None, help="JSON file")
    p.add_argument("--config", default=None, help="inline JSON")
    p.set_defaults(func=cmd_save)

    p = new("delete", "delete a dashboard  [--allow-write --yes]")
    p.add_argument("--url-path", required=True)
    p.add_argument("--yes", action="store_true", help="confirm the deletion")
    p.set_defaults(func=cmd_delete)

    p = new("patch", "swap entities / append rows  [--allow-write --apply]")
    p.add_argument("--url-path", default=None,
                   help="live dashboard (omit when using --file)")
    p.add_argument("--file", default=None,
                   help="patch a local JSON copy without Home Assistant; "
                        "result to --out or stdout (report on stderr)")
    p.add_argument("--swap", action="append", default=[],
                   metavar="OLD=NEW", help="entity reference to replace")
    p.add_argument("--rename", action="append", default=[],
                   metavar="ENTITY=Label", help="display name for an entity")
    p.add_argument("--add", action="append", default=[],
                   metavar="ENTITY", help="append this entity to a card")
    p.add_argument("--card-title", default=None,
                   help="card to add to, matched by title")
    p.add_argument("--card-index", type=int, default=None,
                   help="card to add to, by index from 'cards'")
    p.add_argument("--dry-run", action="store_true",
                   help="explicit dry run (default unless --apply)")
    p.add_argument("--apply", action="store_true",
                   help="actually save (also needs --allow-write)")
    p.add_argument("--out", default=None,
                   help="write the patched config here instead of saving")
    p.set_defaults(func=cmd_patch)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _apply_common_defaults(args)
    if not hasattr(args, "backup_dir"):
        args.backup_dir = None
    pos = getattr(args, "url_path_pos", None)
    if pos:
        if getattr(args, "url_path", None) and args.url_path != pos:
            print(f"error: two different dashboards given: {pos!r} and "
                  f"--url-path {args.url_path!r}", file=sys.stderr)
            return 2
        args.url_path = pos
    if not getattr(args, "func", None):
        parser.print_help()
        return 2

    mutating = args.func in (cmd_create, cmd_save, cmd_delete, cmd_patch)

    def precheck() -> None:
        """Refuse locally, before any connection is opened.

        create's url_path rule is a *local* check (SKILL.md and
        references/dashboards.md say so); running it after connecting meant an
        unreachable instance masked a malformed path, and the exit code was 1
        instead of 2.  It sits inside the ``try`` because these raise
        :class:`UsageError`, and every usage error is formatted in one place.
        """
        if args.func is cmd_create:
            check_url_path(args.url_path)
        # ``patch`` is a dry run unless --apply, so it is not refused wholesale
        # -- but ``patch --apply`` without --allow-write used to fetch the
        # config, print a misleading "saving without a backup" warning for a
        # save that never happens, and only then be refused by the connection
        # guard.  Refuse the *actual* write up front, like the others.
        writes = mutating and (args.func is not cmd_patch or args.apply)
        if writes and not args.allow_write:
            # Same wording as the connection-level guard, so the two ways of
            # refusing a write read as one behaviour.
            raise UsageError("this subcommand changes the instance; "
                             "add --allow-write")

    async def run():
        precheck()
        # `validate --file` is advertised as offline: it must not require a
        # reachable instance, so it runs without opening a WebSocket.
        if args.func is cmd_validate and args.file:
            return await cmd_validate(None, args)
        if args.func is cmd_patch and args.file:
            return await cmd_patch(None, args)
        async with HAConnection(
            url=args.url, token_file=args.token_file, ws_url=args.ws_url,
            allow_write=args.allow_write, timeout=args.timeout,
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
    sys.exit(main())
