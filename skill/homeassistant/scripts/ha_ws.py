#!/usr/bin/env python3
"""ha_ws.py -- generic Home Assistant WebSocket API client (library + CLI).

This is the shared core used by the other ``ha_*.py`` scripts. Import it with::

    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # needed: -I
    from ha_ws import HAConnection, HAError, resolve_url, resolve_token

Safety model
------------
The client is **read-only by default**.  ``HAConnection.cmd()`` refuses an
unknown or known-mutating command unless the connection was opened with
``allow_write=True`` (CLI: ``--allow-write``).  This turns "did I accidentally
restart Home Assistant?" into a hard local error instead of an outage.

Credentials (placeholders only -- never commit real values)
-----------------------------------------------------------
``HA_URL``        Base URL, e.g. ``http://homeassistant.local:8123``.
                  Default: ``http://172.30.32.1:8123`` (HA OS Supervisor).
``HA_TOKEN``      Long-lived access token, e.g. ``export HA_TOKEN="$HA_TOKEN"``.
``HA_TOKEN_FILE`` File containing the token on the first line.
Fallback          ``/tmp/ha.tok`` -- used **only** if it already exists, so
                  interactive shells on a HA OS box work without exports.
``HA_WS_URL``     Overrides the WebSocket URL derived from ``HA_URL``.

The token is never echoed, logged or included in error messages.

Only the standard library plus ``websockets`` are required.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from urllib.parse import urlsplit, urlunsplit

try:
    import websockets
    WEBSOCKETS_IMPORT_ERROR: str | None = None
except ImportError:  # pragma: no cover - depends on the environment
    # Import lazily instead of exiting here: the REST-only paths (`rest`,
    # `render --transport rest`) use ``urllib`` and must keep working on a
    # machine without the package.  The error is raised when a WebSocket is
    # actually requested, which is the only point where it matters.
    websockets = None  # type: ignore[assignment]
    WEBSOCKETS_IMPORT_ERROR = (
        "the 'websockets' package is required for the WebSocket API "
        "(python3 -m pip install websockets). REST-only commands such as "
        "'rest' and 'render --transport rest' work without it."
    )

__all__ = [
    "HAError",
    "UsageError",
    "WriteRefused",
    "dashboard_label",
    "fetch_config",
    "HAConnection",
    "HAHttp",
    "ConnectError",
    "HATimeout",
    "resolve_url",
    "resolve_token",
    "ws_url_from_http",
    "is_read_only",
    "redact",
    "strip_userinfo",
    "open_connection",
    "add_common_args",
    "quiet_broken_pipe",
    "ENTITY_RE",
    "NON_ENTITY_KEYS",
    "collect_entity_ids",
    "iter_cards",
    "as_list",
]

DEFAULT_HTTP_URL = "http://172.30.32.1:8123"
FALLBACK_TOKEN_FILE = "/tmp/ha.tok"
MAX_MESSAGE_SIZE = 64 * 1024 * 1024

# --------------------------------------------------------------------------
# Read-only / mutating command classification
# --------------------------------------------------------------------------

#: WebSocket commands that are known to only read state.
READ_ONLY_COMMANDS = frozenset(
    {
        "auth/current_user",
        "config/area_registry/list",
        "config/category_registry/list",
        "config/device_registry/list",
        "config/entity_registry/get",
        "config/entity_registry/get_entries",
        "config/entity_registry/list",
        "config/entity_registry/list_for_display",
        "config/floor_registry/list",
        "config/label_registry/list",
        "config_entries/get",
        "config_entries/get_single",
        "config_entries/subentries/list",
        "energy/get_prefs",  # the Energy dashboard's source configuration
        "get_config",
        "get_panels",
        "get_services",
        "get_states",
        "history/history_during_period",
        "lovelace/config",
        "lovelace/dashboards/list",
        "lovelace/resources",
        "recorder/get_statistics_metadata",
        "recorder/info",
        "recorder/list_statistic_ids",
        "recorder/statistics_during_period",
        "render_template",  # a subscription; see cmd_subscribe()
        "search/related",
        "system_health/info",
        "todo/item/list",
        "trace/get",
        "trace/list",
        "validate_config",
        "zone/list",
    }
)

#: Explicitly known destructive commands.  Listed separately so the error
#: message can say *what* would have happened.
MUTATING_COMMANDS = frozenset(
    {
        "call_service",
        "config/area_registry/create",
        "config/area_registry/delete",
        "config/area_registry/update",
        "config/device_registry/remove_config_entry",
        "config/device_registry/update",
        "config/entity_registry/remove",
        "config/entity_registry/update",
        "config/label_registry/create",
        "config/label_registry/delete",
        "config/label_registry/update",
        "config_entries/disable",
        "config_entries/reload",
        "config_entries/subentries/add",
        "config_entries/subentries/delete",
        "homeassistant/restart",
        "homeassistant/stop",
        "input_number/set_value",
        "lovelace/config/delete",
        "lovelace/config/save",
        "lovelace/dashboards/create",
        "lovelace/dashboards/delete",
        "lovelace/dashboards/update",
        "lovelace/resources/create",
        "lovelace/resources/delete",
        "lovelace/resources/update",
        "recorder/adjust_sum_statistics",
        "recorder/purge",
        "service/call",
        "subscribe_events",  # harmless but keeps the stream busy; opt in
        "todo/item/create",
        "todo/item/update",
    }
)

#: Suffix / fragment hints.  Kept only to produce a *better error message* for
#: commands we do not know; it never decides whether a command is allowed.
_MUTATING_HINTS = re.compile(
    r"(^|/)(save|create|delete|remove|update|add|set(_[a-z_]+)?|call|"
    r"execute|reload|restart|stop|start|purge|adjust|fire)(/|$)"
)


def is_read_only(command: str) -> bool:
    """Return True only when *command* is on the known read-only whitelist.

    This is **fail-closed**: an unknown command is treated as mutating and needs
    ``--allow-write``.  A heuristic on the command name is not enough -- names
    like ``fire_event`` or ``persistent_notification/dismiss`` mutate the
    instance and contain none of the obvious verbs.
    """
    return command in READ_ONLY_COMMANDS


# --------------------------------------------------------------------------
# Entity references inside a Lovelace / dashboard config
# --------------------------------------------------------------------------

#: ``domain.object_id`` as it appears in a dashboard config.
ENTITY_RE = re.compile(r"\b([a-z][a-z0-9_]*\.([a-z0-9_]+))\b")

#: Keys whose string value is **never** an entity reference.
#:
#: Measured on a real install (2026-11): scanning every string, URLs produced
#: phantom "missing entities" -- ``hald.pw`` and ``music.aalen`` (parts of
#: ``https://music.aalen.hald.pw/...``) and ``party_qr.png``
#: (``/local/party_qr.png``).  A checker that reports them fails a healthy
#: dashboard, so the *key* decides before the text does.
NON_ENTITY_KEYS = frozenset(
    {
        "audio",
        "entity_picture",
        "heading",
        "icon",
        "icon_path",
        "image",
        "label",
        "media_content_id",
        "navigation_path",
        "source",
        "url",
        "url_path",
        "video",
    }
)

#: ``service`` / ``perform_action`` hold a service name (``button.press``) *or*
#: a script entity (``script.party_qr_code_aktualisieren``).  Skipping the key
#: entirely was wrong -- it hid two live script references on the reference
#: install.  These keys are resolved against the instance instead: the value
#: counts only when it really is an entity id, which keeps ``button.press`` out
#: without needing a hard-coded domain list.
SERVICE_KEYS = frozenset({"service", "perform_action"})

#: A path prefix such as ``/local/`` marks a file, not an entity.  There is no
#: file-suffix rule: an entity id may legitimately end in ``.md`` or
#: ``.json`` (``sensor.md``), so filtering on the suffix alone silently
#: dropped real references.  Measured over all 12 dashboards on the reference
#: install: the suffix rule changed **nothing**; the path prefix and the key
#: list were doing the whole job.

_URL_RE = re.compile(r"\S+://\S+")

#: ``scheme://user:secret@host`` -- the userinfo must never reach the terminal.
#: Applied by :func:`strip_userinfo`, which uses ``urlsplit`` instead of this
#: pattern: a password may contain ``@``, and a regex stops at the first one.


def collect_entity_ids(node, found=None, *, _key=None, known_ids=None) -> set:
    """Collect ``domain.object_id`` references from a dashboard config tree.

    Shared by ``ha_verify.py`` and ``ha_dashboard.py`` so both agree on what
    counts as a reference.  Three guards keep file names, URLs and pure service
    names out: keys in :data:`NON_ENTITY_KEYS` are not scanned, URLs are
    stripped from a string before the pattern runs, and a value under a
    :data:`SERVICE_KEYS` key is only counted when *known_ids* confirms it is a
    real entity.
    """
    if found is None:
        found = set()
    if isinstance(node, str):
        if _key in NON_ENTITY_KEYS:
            return found
        if _key in SERVICE_KEYS:
            candidate = node.strip()
            if known_ids and candidate in known_ids:
                found.add(candidate)
            return found
        text = _URL_RE.sub(" ", node)
        for match in ENTITY_RE.finditer(text):
            if match.start() > 0 and text[match.start() - 1] in "/\\":
                continue  # '/local/party_qr.png' is a path, not an entity
            found.add(match.group(1))
    elif isinstance(node, dict):
        for key, value in node.items():
            collect_entity_ids(value, found, _key=key, known_ids=known_ids)
    elif isinstance(node, list):
        for value in node:
            collect_entity_ids(value, found, _key=_key, known_ids=known_ids)
    return found


def as_list(value):
    """Return *value* as a list, whatever a hand-written config put there.

    A Lovelace config is user-editable JSON, and ``"cards": 1`` or
    ``"views": "x"`` are things a person really writes.  ``x or []`` was used
    for this and is not the same test: a truthy non-list survives it and then
    blows up in ``enumerate()`` with a TypeError traceback instead of a
    validation message.  Anything that is not a list is treated as absent.
    """
    return value if isinstance(value, list) else []


def iter_view_cards(view):
    """Yield every card of one view, in display order.

    Covers both layouts: a classic view keeps its cards in ``view.cards``, a
    **sections** view keeps them in ``view.sections[].cards``.  Nested
    stack/grid cards are included, so the count here matches ``iter_cards`` and
    the index ``ha_dashboard.py cards`` prints addresses the same card in
    ``patch --card-index``.
    """
    if not isinstance(view, dict):
        return
    for card in as_list(view.get("cards")):
        yield from _walk_cards(card)
    for section in as_list(view.get("sections")):
        if not isinstance(section, dict):
            continue
        for card in as_list(section.get("cards")):
            yield from _walk_cards(card)


def iter_cards(config):
    """Yield every card of a dashboard config, in display order."""
    if isinstance(config, dict):
        for view in as_list(config.get("views")):
            yield from iter_view_cards(view)


def _walk_cards(card):
    if isinstance(card, dict):
        yield card
        for nested in as_list(card.get("cards")):
            yield from _walk_cards(nested)


# --------------------------------------------------------------------------
# Errors and small helpers
# --------------------------------------------------------------------------


class ConnectError(RuntimeError):
    """Transport or authentication problem."""


class HATimeout(ConnectError):
    """No frame arrived within ``--timeout``.

    A subclass of :class:`ConnectError` so callers that only care about "the
    transport failed" keep working, while :meth:`HAConnection.cmd_subscribe`
    can tell a quiet subscription apart from a broken socket.
    """


class UsageError(RuntimeError):
    """The *caller* asked for something impossible.

    A bad flag, a malformed JSON argument, a refused write.  Distinct from
    :class:`HAError` (Home Assistant said no) because the two need different
    exit codes: every script exits **2** for a UsageError and **1** for a
    failed operation, so a shell condition can tell "you asked wrong" from
    "it did not work".

    ``raise SystemExit("text")`` looks like it does this and does not: a
    ``SystemExit`` carrying a string exits **1**.
    """

    #: Class attribute, not just an instance one: :class:`WriteRefused`
    #: inherits from ``PermissionError`` as well, and ``OSError.__init__`` wins
    #: the MRO, so an ``__init__`` here would never run for that subclass.
    exit_code = 2

    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


class WriteRefused(PermissionError, UsageError):
    """The read-only guard refused a command.

    Both types on purpose: existing callers that catch ``PermissionError``
    (the documented behaviour) keep working, while ``main()`` can recognise it
    as a usage error and exit 2, as SKILL.md and README.md promise.
    """


class HAError(RuntimeError):
    """Home Assistant answered with ``success: false``."""

    def __init__(self, code: str, message: str, command: str | None = None):
        self.code = code
        self.message = message
        self.command = command
        detail = f"{code}: {message}"
        if command:
            detail = f"{command} -> {detail}"
        super().__init__(detail)


_SECRET_PATTERNS = (
    re.compile(r"eyJ[A-Za-z0-9_\-\.]{20,}"),  # JWT-ish
    re.compile(r"\b[0-9a-f]{32,}\b"),  # raw hex tokens
)

#: A credential in a query string (`?access_token=...`).  ``ping`` echoed the
#: whole ``--url``, and a token that is neither ``$HA_TOKEN`` nor JWT/hex-shaped
#: matched nothing above.  Kept narrow: only well-known parameter names, so a
#: harmless ``?limit=100`` survives.  The name is kept so the URL stays
#: readable -- ``?access_token=***REDACTED***``.
#:
#: ``api_password`` is Home Assistant's own historical URL credential, and
#: ``password``/``secret`` are what people put there; they leaked unmasked.
#: The value stops at anything that ends a URL inside prose -- parentheses,
#: brackets, a quote, a comma or the final full stop -- otherwise masking
#: ``(http://h/?token=x).`` swallowed the closing paren.
_SECRET_QUERY_RE = re.compile(
    r"(?i)\b(access_token|auth_token|api_key|api_password|password|secret|token)"
    r"=[^&\s\"'()\[\]{}<>,;]+")

#: The token that was actually resolved for this process.
#:
#: ``redact`` used to compare only ``$HA_TOKEN``.  A token from ``--token-file``,
#: ``$HA_TOKEN_FILE`` or ``/tmp/ha.tok`` -- the documented default path -- was
#: masked only if it happened to look like a JWT or 32 hex characters, so a
#: perfectly ordinary long-lived token leaked in full.  ``resolve_token``
#: records what it returned here.
_RESOLVED_TOKEN: str | None = None


def _remember_token(token: str | None) -> None:
    """Record the resolved token so :func:`redact` can mask it verbatim."""
    global _RESOLVED_TOKEN
    if token:
        _RESOLVED_TOKEN = token


def _mask_userinfo(url: str) -> str:
    """Mask ``user:password@`` in one URL-shaped run of characters.

    ``urlsplit`` is not used: this runs on URL *runs inside a sentence*
    (``cannot connect to ws://user:pw@host:9999/api/websocket``), where
    ``urlsplit`` sees garbage as the scheme and an empty netloc, so it handed
    the whole sentence back unmasked.  Splitting on ``://`` and cutting the
    netloc at the first path/query/fragment character works for both a bare
    URL and a URL embedded in an error message.

    ``rpartition("@")`` rather than taking the first ``@``: a password may
    legally contain one (``user:se@cret``), and the old regex stopped there,
    leaving the tail of the password in the output.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    end = len(rest)
    for char in "/?#":
        index = rest.find(char)
        if index != -1 and index < end:
            end = index
    netloc, tail = rest[:end], rest[end:]
    if "@" not in netloc:
        return url
    return f"{scheme}://***:***@{netloc.rpartition('@')[2]}{tail}"


def strip_userinfo(url: str) -> str:
    """Replace credentials embedded in a URL with ``***:***``.

    ``--url http://user:password@host:8123`` is valid and the password was
    printed by ``ping``, by ``--json`` output and by every connect error.
    """
    if not isinstance(url, str) or "://" not in url:
        return url
    return _mask_userinfo(url)


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")


def mask_emails(text):
    """Mask e-mail addresses (``jo***@example.com`` keeps the domain).

    Config-entry titles are often the account name, i.e. an e-mail address.
    Non-strings are returned unchanged so it can be mapped over a row.
    """
    if not isinstance(text, str):
        return text
    def _mask(m):
        local, _, domain = m.group(0).rpartition("@")
        return local[:2 if len(local) > 2 else 1] + "***@" + domain
    return _EMAIL_RE.sub(_mask, text)


def redact(text: str) -> str:
    """Mask anything that looks like a credential before printing it."""
    if not isinstance(text, str):
        text = str(text)
    for token in (os.environ.get("HA_TOKEN"), _RESOLVED_TOKEN):
        if token and len(token) > 8:
            text = text.replace(token, "***REDACTED***")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("***REDACTED***", text)
    text = _SECRET_QUERY_RE.sub(r"\1=***REDACTED***", text)
    # Per whitespace-separated word: an error message is a sentence with a URL
    # in it, and masking only works if each URL run is handled on its own.
    text = " ".join(_mask_userinfo(word) for word in text.split(" "))
    return mask_emails(text)


def ws_url_from_http(base: str) -> str:
    """Derive ``ws(s)://host:port/api/websocket`` from an HTTP base URL."""
    parts = urlsplit(base)
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(
        parts.scheme
    )
    if scheme is None:
        raise ConnectError(f"unsupported URL scheme in {base!r}")
    path = parts.path.rstrip("/")
    if path.endswith("/api/websocket"):
        path = path
    elif path:
        path = path + "/api/websocket"
    else:
        path = "/api/websocket"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def resolve_url(cli_value: str | None = None) -> str:
    """HTTP base URL: ``--url`` > ``$HA_URL`` > default."""
    return cli_value or os.environ.get("HA_URL") or DEFAULT_HTTP_URL


def resolve_token(cli_file: str | None = None) -> str:
    """Return the access token.

    Precedence: ``--token-file`` > ``$HA_TOKEN_FILE`` > ``$HA_TOKEN`` >
    ``/tmp/ha.tok`` (only when that file exists).  An explicitly named file wins
    over an exported token: a stale ``$HA_TOKEN`` in the shell must not shadow
    the file the user just passed.
    """
    env_token = os.environ.get("HA_TOKEN")
    if cli_file or os.environ.get("HA_TOKEN_FILE"):
        path = cli_file or os.environ["HA_TOKEN_FILE"]
        return _read_token_file(path)
    if env_token and env_token.strip():
        token = env_token.strip()
        _remember_token(token)
        return token
    if os.path.exists(FALLBACK_TOKEN_FILE):
        return _read_token_file(FALLBACK_TOKEN_FILE)
    raise ConnectError(
        "no access token found: set $HA_TOKEN (recommended) or $HA_TOKEN_FILE, "
        f"pass --token-file, or provide {FALLBACK_TOKEN_FILE}"
    )


def _read_token_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError as exc:
        raise ConnectError(f"cannot read token file {path}: {exc}") from exc
    if not token:
        raise ConnectError(f"token file {path} is empty")
    _remember_token(token)
    return token


# --------------------------------------------------------------------------
# Dashboard access shared by ha_dashboard.py and ha_verify.py
# --------------------------------------------------------------------------


def dashboard_label(url_path: str | None) -> str:
    """Human label for a dashboard, including the default one.

    The default dashboard has no ``url_path`` at all -- it is the dashboard
    ``/lovelace``.  Naming it ``lovelace/None`` in output is confusing, so the
    scripts print ``lovelace (default)`` instead.
    """
    return f"lovelace/{url_path}" if url_path else "lovelace (default)"


async def fetch_config(ha, url_path: str | None):
    """Read a dashboard config; ``url_path=None`` means the default dashboard.

    Omitting ``url_path`` is how the API addresses the default dashboard.  It
    only has a readable config in storage or YAML mode; in "auto-generated"
    mode Home Assistant answers ``config_not_found``, which is a legitimate
    answer rather than a bug -- so it is explained instead of passed through
    as a bare error code.
    """
    payload = {"type": "lovelace/config"}
    if url_path:
        payload["url_path"] = url_path
    try:
        return await ha.cmd(**payload)
    except HAError as exc:
        if getattr(exc, "code", None) == "config_not_found":
            # Only the default dashboard can be "auto-generated"; for a named
            # path the same error means the dashboard does not exist, and
            # talking about storage mode there would be misleading.
            hint = (
                " (the default dashboard is auto-generated unless you switch "
                "it to storage mode in the UI)"
                if not url_path
                else ""
            )
            raise HAError(
                "config_not_found",
                f"{dashboard_label(url_path)} has no readable config{hint}",
                command="lovelace/config",
            ) from exc
        raise


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------


class HAConnection:
    """Async context manager around one Home Assistant WebSocket connection.

    ``async with HAConnection(allow_write=False) as ha: await ha.cmd(type="get_states")``
    """

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        token_file: str | None = None,
        ws_url: str | None = None,
        allow_write: bool = False,
        timeout: float = 30.0,
    ):
        base = resolve_url(url)
        self.base_url = base.rstrip("/")
        self.http_url = self.base_url
        self.ws_url = ws_url or os.environ.get("HA_WS_URL") or ws_url_from_http(base)
        self._token = token or resolve_token(token_file)
        # A token passed straight to the constructor must be masked too.  The
        # module documents itself as a library, and only resolve_token() used
        # to register the value -- so library callers leaked their own token
        # into any error message carrying it.
        _remember_token(self._token)
        self.allow_write = allow_write
        self.timeout = timeout
        self.ws = None
        self._id = 0
        self.ha_version: str | None = None

    # -- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> "HAConnection":
        await self.connect()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def connect(self) -> "HAConnection":
        if websockets is None:
            raise ConnectError(WEBSOCKETS_IMPORT_ERROR or "websockets missing")
        try:
            self.ws = await asyncio.wait_for(
                websockets.connect(self.ws_url, max_size=MAX_MESSAGE_SIZE),
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as ConnectError
            raise ConnectError(f"cannot connect to {self.ws_url}: {exc}") from exc

        greeting = await self._recv()
        if greeting.get("type") != "auth_required":
            raise ConnectError(f"unexpected greeting: {redact(json.dumps(greeting))}")

        await self._send({"type": "auth", "access_token": self._token})
        answer = await self._recv()
        kind = answer.get("type")
        if kind == "auth_invalid":
            raise ConnectError(
                "authentication failed (auth_invalid). Check HA_TOKEN; the token "
                "must be a long-lived access token of this instance."
            )
        if kind != "auth_ok":
            raise ConnectError(f"unexpected auth reply: {redact(json.dumps(answer))}")
        self.ha_version = answer.get("ha_version")
        return self

    async def close(self) -> None:
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
            self.ws = None

    # -- protocol ----------------------------------------------------------
    async def _recv(self) -> dict:
        """Read one frame, translating transport failures into ``ConnectError``.

        Without this, a socket timeout or a connection dropped mid-command
        surfaced as ``asyncio.TimeoutError`` / ``websockets.ConnectionClosed``
        -- neither was caught by the CLIs, so the user got a traceback instead
        of the one-line error the scripts promise.
        """
        try:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=self.timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise HATimeout(
                f"no answer from {self.ws_url} within {self.timeout:g}s"
            ) from exc
        except websockets.ConnectionClosed as exc:  # type: ignore[union-attr]
            raise ConnectError(
                f"connection to {self.ws_url} closed while waiting for a "
                f"reply: {exc}"
            ) from exc
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConnectError(
                f"non-JSON frame from {self.ws_url}: {redact(raw[:200])}"
            ) from exc
        if not isinstance(frame, dict):
            # A proxy or a wrong port can answer with valid JSON that is not a
            # protocol frame: ``[]``, ``"text"``, ``null``.  Returning it let
            # ``connect()`` call ``.get("type")`` on a list and die with
            # ``AttributeError: 'list' object has no attribute 'get'`` -- an
            # uncaught traceback where the scripts promise one line.
            raise ConnectError(
                f"non-protocol frame from {self.ws_url}: expected a JSON "
                f"object, got {type(frame).__name__}: {redact(raw[:200])}"
            )
        return frame

    async def _send(self, payload: dict) -> None:
        """Send one frame, translating a dead socket into a one-line error.

        Only ``recv`` was guarded, so a Home Assistant restart (or a TCP reset)
        between the handshake and the command produced a bare
        ``websockets.exceptions.ConnectionClosedOK`` traceback.  That exception
        is not an ``OSError``, so ``main()``'s handler never saw it -- exactly
        the traceback :meth:`_recv` exists to prevent.
        """
        try:
            await self.ws.send(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001 - surfaced as ConnectError
            raise ConnectError(
                f"cannot send '{payload.get('type')}' to {self.ws_url}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    async def cmd(self, **payload):
        """Send one command and return its ``result`` value.

        Raises :class:`HAError` when Home Assistant reports ``success: false``
        and :class:`PermissionError` when the command would write and the
        connection was opened without ``allow_write=True``.
        """
        command = payload.get("type")
        if not isinstance(command, str):
            raise ValueError("command needs a 'type'")
        if not self.allow_write and not is_read_only(command):
            hint = ""
            if command in MUTATING_COMMANDS:
                hint = " (known mutating command)"
            elif _MUTATING_HINTS.search(command):
                hint = " (looks like a write command)"
            elif command not in READ_ONLY_COMMANDS:
                hint = " (unknown command -- refused by default)"
            raise WriteRefused(
                f"refusing '{command}'{hint}: not on the read-only whitelist. "
                "Re-run with --allow-write if you really mean to mutate this "
                "Home Assistant instance."
            )
        self._id += 1
        payload["id"] = self._id
        await self._send(payload)
        while True:
            message = await self._recv()
            if message.get("type") == "event":
                continue  # subscription traffic, not our answer
            if message.get("id") != self._id:
                continue
            if message.get("type") != "result":
                continue
            if not message.get("success", False):
                error = message.get("error") or {}
                raise HAError(
                    error.get("code", "unknown"),
                    error.get("message", "no message"),
                    command=command,
                )
            return message.get("result")

    async def cmd_subscribe(self, **payload):
        """Send a subscription command and return its first event payload.

        Some commands (``render_template``) are subscriptions: the first frame
        is ``{"success": true, "result": null}`` and the actual answer arrives
        in a following ``type: "event"`` frame carrying the same ``id``.  A
        client that only reads the result frame sees ``null`` and wrongly
        concludes the command is broken.  This method waits for the event,
        unsubscribes, and returns ``event``.
        """
        command = payload.get("type")
        if not self.allow_write and not is_read_only(command):
            raise WriteRefused(f"refusing '{command}': pass --allow-write")
        self._id += 1
        command_id = self._id
        payload["id"] = command_id
        await self._send(payload)
        event = None
        acked = False
        # The ACK and the event are two separate frames, and their order is not
        # guaranteed: Home Assistant normally sends the result frame first, but
        # an immediate render can arrive before it.  Wait for *both* instead of
        # breaking on whichever shows up first.  The subscription is released in
        # ``finally`` so a timeout cannot leave it streaming on the server.
        try:
            while not (acked and event is not None):
                message = await self._recv()
                if message.get("id") != command_id:
                    continue  # another command's answer or foreign event
                kind = message.get("type")
                if kind == "result":
                    if not message.get("success", False):
                        error = message.get("error") or {}
                        raise HAError(error.get("code", "unknown"),
                                      error.get("message", "no message"),
                                      command=command)
                    acked = True
                elif kind == "event":
                    event = message.get("event")
        except HATimeout as exc:
            raise HAError(
                "timeout",
                f"no event for '{command}' within {self.timeout:g}s",
                command=command,
            ) from exc
        finally:
            # stop the subscription so the socket does not keep streaming
            if acked:
                try:
                    self._id += 1
                    await self.ws.send(json.dumps({
                        "id": self._id,
                        "type": "unsubscribe_events",
                        "subscription": command_id,
                    }))
                except Exception:  # noqa: BLE001 - best effort, socket may be gone
                    pass
        return event


class HAHttp:
    """Minimal REST client for the few endpoints the WebSocket API lacks.

    Only ``urllib`` from the standard library is used, so this also works when
    ``websockets`` is unavailable.  Read-only: :meth:`request` refuses anything
    but GET unless ``write=True`` is passed explicitly.
    """

    def __init__(self, url: str | None = None, token: str | None = None,
                 token_file: str | None = None, timeout: float = 30.0):
        self.base_url = resolve_url(url).rstrip("/")
        self._token = token or resolve_token(token_file)
        _remember_token(self._token)
        self.timeout = timeout

    def request(self, path: str, method: str = "GET", body=None, write: bool = False):
        import urllib.error
        import urllib.request

        if method != "GET" and not write:
            raise WriteRefused(
                f"refusing HTTP {method} {path}: pass --allow-write if you "
                "really mean to mutate this Home Assistant instance "
                "(note: /api/template is POST-only and is allowed from the "
                "'render' subcommand)."
            )
        if not path.startswith("/"):
            path = "/" + path
        data = None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ConnectError(
                f"HTTP {exc.code} for {path}: {redact(detail)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ConnectError(f"cannot reach {self.base_url}{path}: {exc}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # HTML is never a Home Assistant API answer.  A proxy, an ingress
            # panel or a --url that carries a path (/lovelace) redirects to the
            # login page, and printing that page with exit 0 made "the API
            # replied" indistinguishable from "someone's web UI replied".
            # /api/error_log is plain text and still passes this check.
            head = raw.lstrip()[:15].lower()
            if head.startswith("<!doctype html") or head.startswith("<html"):
                raise ConnectError(
                    f"{path} returned HTML, not JSON: the URL probably points "
                    f"at the web UI or an ingress login page rather than the "
                    f"API. Check --url ({self.base_url}).")
            return raw

    def config(self):
        return self.request("/api/config")

    def states(self):
        return self.request("/api/states")

    def state(self, entity_id: str):
        return self.request(f"/api/states/{entity_id}")

    def render_template(self, template: str):
        """Render Jinja server-side via ``POST /api/template``.

        This is the simpler of the two transports: one synchronous request and
        no WebSocket at all.  The WebSocket command ``render_template`` also
        works, but it is a *subscription* -- its ACK is empty and the value
        arrives in a following ``event`` frame (see
        :meth:`HAConnection.cmd_subscribe`), which is easy to get wrong.
        """
        return self.request(
            "/api/template", method="POST", body={"template": template}, write=True
        )

    def logbook(self, entity_id: str, start: str, end: str):
        return self.request(
            f"/api/logbook/{start}?end_time={end}&entity={entity_id}"
        )


def open_connection(args) -> HAConnection:
    """Build an :class:`HAConnection` from parsed CLI arguments."""
    return HAConnection(
        url=getattr(args, "url", None),
        token_file=getattr(args, "token_file", None),
        ws_url=getattr(args, "ws_url", None),
        allow_write=getattr(args, "allow_write", False),
        timeout=getattr(args, "timeout", 30.0),
    )


#: name -> default, used to rebuild values argparse suppressed in subparsers.
COMMON_DEFAULTS = {
    "url": None,
    "ws_url": None,
    "token_file": None,
    "allow_write": False,
    "timeout": 30.0,
    "json": False,
}


def add_common_args(parser: argparse.ArgumentParser, suppress: bool = False) -> None:
    """Add the shared --url/--token-file/--allow-write/--timeout flags.

    With ``suppress=True`` the flags carry ``default=argparse.SUPPRESS`` so that
    re-declaring them on a subparser does not clobber a value given before the
    subcommand (``ha_ws.py --url X states`` and ``ha_ws.py states --url X``
    therefore both work).
    """
    none: dict = {"default": argparse.SUPPRESS} if suppress else {}
    flag: dict = {"action": "store_true"}
    if suppress:
        flag["default"] = argparse.SUPPRESS
    parser.add_argument(
        "--url",
        help="Home Assistant base URL (default: $HA_URL or %s)" % DEFAULT_HTTP_URL,
        **(none or {"default": None}),
    )
    parser.add_argument(
        "--ws-url",
        help="Override the WebSocket URL (default: derived from --url, or $HA_WS_URL)",
        **(none or {"default": None}),
    )
    parser.add_argument(
        "--token-file",
        # Order matches resolve_token(): an explicitly named file wins, and
        # $HA_TOKEN_FILE beats a stale exported $HA_TOKEN.  The help text had
        # the last two swapped, so it described a precedence the code does not
        # implement -- the exact confusion the order exists to prevent.
        help="File containing the token (default: $HA_TOKEN_FILE, else "
        f"$HA_TOKEN, else {FALLBACK_TOKEN_FILE} if it exists)",
        **(none or {"default": None}),
    )
    parser.add_argument(
        "--allow-write",
        help="Permit WebSocket/HTTP operations that are not known read-only. "
        "Without this flag every mutating operation is refused locally.",
        **flag,
    )
    parser.add_argument(
        "--timeout", type=float, help="socket timeout in seconds",
        **(none or {"default": 30.0}),
    )
    parser.add_argument("--json", help="machine-readable output", **flag)


def _apply_common_defaults(args) -> None:
    for name, default in COMMON_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)


def emit(data, as_json: bool, text_renderer=None) -> None:
    """Print *data* as JSON or via *text_renderer*."""
    if as_json or text_renderer is None:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False))
    else:
        text_renderer(data)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


async def _cmd_ping(ha, args):
    # redact, not just strip_userinfo: --url may carry credentials *and* a
    # token in its query string (?access_token=...), which strip_userinfo
    # leaves alone.  ping printed both.
    info = {
        "url": redact(ha.http_url),
        "ws_url": redact(ha.ws_url),
        "ha_version": ha.ha_version,
        "auth": "ok",
    }
    emit(info, args.json, lambda d: print(
        f"OK  {d['ha_version']}  {d['url']}  (auth ok)"))


async def _cmd_config(ha, args):
    config = await ha.cmd(type="get_config")

    def render(cfg):
        for key in ("version", "location_name", "time_zone", "currency",
                    "country", "language", "state"):
            if key in cfg:
                print(f"{key:>14}: {cfg[key]}")
        comps = cfg.get("components") or []
        print(f"{'components':>14}: {len(comps)}")

    emit(config, args.json, render)


async def _cmd_states(ha, args):
    states = await ha.cmd(type="get_states")
    matched_all = len(states)
    if args.filter:
        needle = args.filter.lower()
        states = [
            s for s in states
            if needle in s["entity_id"].lower()
            or needle in str(
                (s.get("attributes") or {}).get("friendly_name", "")).lower()
        ]
    if args.compact:
        states = [
            {
                "entity_id": s["entity_id"],
                "state": s["state"],
                "unit": (s.get("attributes") or {}).get("unit_of_measurement"),
                "last_changed": s.get("last_changed"),
            }
            for s in states
        ]
    states = sorted(states, key=lambda s: s["entity_id"])

    def render(rows):
        for row in rows:
            if args.compact:
                unit = f" {row['unit']}" if row.get("unit") else ""
                print(f"{row['entity_id']:<60} {row['state']}{unit}")
            else:
                print(f"{row['entity_id']:<60} {row['state']}")

    emit(states, args.json, render)
    # A filter that matches nothing printed an empty list and exited 0, which
    # reads exactly like "this instance has no such entity family" -- the same
    # fail-open shape the other listings were cured of in round 9.  Exit 1 and
    # name the filter; a filter with hits, or no filter at all, stays 0.
    if args.filter and not states:
        print(f"error: no entity id or name contains {args.filter!r} "
              f"({matched_all} entities checked)", file=sys.stderr)
        return 1
    return 0


async def _cmd_render(ha, args):
    """Render a template.

    ``render_template`` over the WebSocket API is a *subscription*: the result
    frame is an empty ACK and the rendered text arrives in an ``event`` frame.
    ``ha.cmd_subscribe`` handles that.  ``--transport rest`` uses
    ``POST /api/template`` instead, which is a plain synchronous reply and needs
    no WebSocket at all (useful behind a proxy that does not forward WS).
    """
    template = args.template
    if template == "-":
        template = sys.stdin.read()

    def show(value):
        if args.json:
            print(json.dumps({"result": value}, ensure_ascii=False, indent=2))
        else:
            print(value if isinstance(value, str) else json.dumps(value))

    if args.transport == "rest":
        show(HAHttp(url=args.url, token_file=args.token_file,
                    timeout=args.timeout).render_template(template))
        return

    try:
        event = await ha.cmd_subscribe(type="render_template", template=template)
    except HAError as exc:
        # A template that raises produces **no event frame at all** -- the ACK
        # arrives, then silence, so the only symptom is the timeout.  The REST
        # endpoint reports the actual Jinja error, so ask it instead of handing
        # the user a bare "timeout".
        if getattr(exc, "code", None) != "timeout":
            raise
        rest = HAHttp(url=args.url, token_file=args.token_file,
                      timeout=args.timeout)
        try:
            show(rest.render_template(template))
        except ConnectError as rest_exc:
            raise HAError(
                "render_failed",
                "WebSocket render_template sent no event (a template that "
                f"raises never renders); REST says: {rest_exc}",
                command="render_template",
            ) from exc
        return
    if not isinstance(event, dict) or "result" not in event:
        raise HAError(
            "render_failed",
            f"unexpected render_template event: {redact(json.dumps(event))[:200]}",
            command="render_template",
        )
    show(event["result"])


async def _cmd_raw(ha, args):
    raw = args.command
    if raw == "-":
        raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(f"--command must be a JSON object: {exc}") from exc
    if not isinstance(payload, dict):
        raise UsageError("--command must be a JSON object, e.g. "
                         '\'{"type":"get_states"}\'')
    payload.pop("id", None)
    # Every one of these is the same class of mistake -- a command this CLI
    # cannot send -- so they all leave as UsageError (exit 2).  ``{}``,
    # ``{"type": 5}`` and ``[]`` used to disagree: the first two reached
    # ``cmd()`` and became a ValueError (exit 1), only ``[]`` was exit 2.
    if not isinstance(payload.get("type"), str) or not payload["type"]:
        raise UsageError(
            "--command needs a \"type\" string, e.g. '{\"type\":\"get_states\"}'")
    if "self" in payload:
        # ``ha.cmd(**payload)`` would bind this key to the method's own ``self``
        # and die with "got multiple values for argument 'self'".
        raise UsageError(
            "--command may not use the key \"self\": it collides with the "
            "client's own method argument")
    emit(await ha.cmd(**payload), True)


async def _cmd_rest(_ha, args):
    """GET an arbitrary /api/... path over REST (read-only)."""
    path = args.path or ""
    # Three spellings, one endpoint.  The old rule only prefixed a path that
    # had *no* leading slash, so ``rest states`` fetched /api/states while
    # ``rest /states`` fetched /states and returned an unexplained 404.
    if path in ("", "/", "api", "/api", "api/"):
        path = "/api/"
    elif not path.startswith("/api/"):
        # ``api/states`` (a prefix without the leading slash) is the third
        # spelling of the same endpoint; it used to become /api/api/states.
        stripped = path.lstrip("/")
        if stripped.startswith("api/"):
            stripped = stripped[4:]
        path = "/api/" + stripped
    body = None
    method = "GET"
    if args.post_body:
        method = "POST"
        # Same contract as ``raw --command``: a malformed JSON argument is a
        # usage error (exit 2), not an operation that failed against Home
        # Assistant.  It used to surface as a bare JSONDecodeError -> exit 1.
        try:
            body = json.loads(args.post_body)
        except json.JSONDecodeError as exc:
            raise UsageError(
                f"--post-body must be JSON: {exc}") from exc
    result = HAHttp(
        url=args.url, token_file=args.token_file, timeout=args.timeout
    ).request(path, method=method, body=body, write=args.allow_write)
    if isinstance(result, str) and not args.json:
        print(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def _cmd_commands(ha, args):
    """Print what this client considers read-only (useful for scripting)."""
    if getattr(args, "json", False):
        print(json.dumps(
            {
                "read_only": sorted(READ_ONLY_COMMANDS),
                "mutating": sorted(MUTATING_COMMANDS),
            },
            ensure_ascii=False,
            indent=2,
        ))
        return
    print("# read-only (allowed by default)")
    for name in sorted(READ_ONLY_COMMANDS):
        print(" ", name)
    print("# mutating (needs --allow-write)")
    for name in sorted(MUTATING_COMMANDS):
        print(" ", name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ha_ws.py",
        description="Home Assistant WebSocket API client (read-only by default).",
        epilog="Credentials come from $HA_TOKEN / $HA_TOKEN_FILE / --token-file or "
        f"{FALLBACK_TOKEN_FILE}; never hard-code a token.",
    )
    add_common_args(parser)
    sub = parser.add_subparsers(dest="subcommand")
    parent = argparse.ArgumentParser(add_help=False)
    add_common_args(parent, suppress=True)

    def new(name, help_text):
        return sub.add_parser(name, help=help_text, parents=[parent])

    p = new("ping", "connect, authenticate, print version")
    p.set_defaults(func=_cmd_ping)

    p = new("config", "get_config (version, timezone, components)")
    p.set_defaults(func=_cmd_config)

    p = new("states", "list entity states")
    p.add_argument("--filter", default=None, help="substring of entity_id / name")
    p.add_argument("--compact", action="store_true",
                   help="only entity_id, state, unit, last_changed")
    p.set_defaults(func=_cmd_states)

    p = new("render", "render a Jinja template server-side")
    p.add_argument("--template", required=True,
                   help="template text, or '-' to read stdin")
    p.add_argument("--transport", choices=["ws", "rest"], default="ws",
                   help="'ws' uses the render_template subscription (default), "
                        "'rest' uses POST /api/template (no WebSocket needed)")
    p.set_defaults(func=_cmd_render)

    p = new("raw", "send one raw WebSocket command (JSON)")
    p.add_argument("--command", required=True,
                   help='JSON object, e.g. \'{"type":"get_panels"}\'; "-" reads stdin')
    p.set_defaults(func=_cmd_raw)

    p = new("rest", "GET a REST path such as /api/states")
    p.add_argument("path", help="path, e.g. 'states' or '/api/states/sensor.x'")
    p.add_argument("--post-body", default=None,
                   help="JSON body; switches the request to POST (needs "
                        "--allow-write)")
    p.set_defaults(func=_cmd_rest)

    p = new("commands", "list known read-only / mutating commands")
    p.set_defaults(func=_cmd_commands)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _apply_common_defaults(args)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2

    async def run():
        try:
            if args.func is _cmd_commands:
                return _cmd_commands(None, args)
            # REST-only subcommands must not depend on the WebSocket port
            # being reachable (proxies often forward HTTP but not WS upgrade).
            needs_ws = not (
                args.func is _cmd_rest
                or (args.func is _cmd_render and args.transport == "rest")
            )
            if not needs_ws:
                return await args.func(None, args)
            async with open_connection(args) as ha:
                # Returned, not discarded: a subcommand's exit code (a filter
                # that matched nothing) was thrown away here, so every command
                # in this CLI exited 0 no matter what it decided.
                return await args.func(ha, args)
        except (WriteRefused, UsageError) as exc:
            # Caught before PermissionError: a refused write is a usage error
            # and belongs on exit 2, not on the same code as "Home Assistant
            # said no".  WriteRefused inherits from both, so order matters.
            print(f"error: {redact(str(exc))}", file=sys.stderr)
            return getattr(exc, "exit_code", 2)
        except (ConnectError, PermissionError, HAError, ValueError, OSError) as exc:
            print(f"error: {redact(str(exc))}", file=sys.stderr)
            return 1

    return asyncio.run(run()) or 0


def quiet_broken_pipe() -> None:
    """Make ``script.py ... | head`` exit without a traceback.

    The exit code is **141** (128 + SIGPIPE), which is the normal shell
    convention for a closed pipe -- not 0.  Called at the top of every script's
    ``__main__`` block.
    """
    try:
        import signal

        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (ImportError, AttributeError, ValueError):
        pass


class _EmailMaskingStream:
    """Text stream wrapper that masks e-mail addresses on every write."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        return self._stream.write(mask_emails(text))

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_email_mask() -> None:
    """Mask e-mail addresses in everything the script prints (stdout/stderr).

    Account names end up in device names, friendly names, ``identifiers`` and
    ``unique_id`` -- masking field by field missed some (``find`` printed the
    ``identifiers`` of a Google Drive device in clear).  Used by the inspection
    scripts whose output is never fed back into Home Assistant;
    ``ha_dashboard.py`` and ``ha_ws.py raw`` print configs verbatim instead.
    """
    if not isinstance(sys.stdout, _EmailMaskingStream):
        sys.stdout = _EmailMaskingStream(sys.stdout)
    if not isinstance(sys.stderr, _EmailMaskingStream):
        sys.stderr = _EmailMaskingStream(sys.stderr)


#: backwards-compatible alias used by earlier revisions
_quiet_broken_pipe = quiet_broken_pipe


if __name__ == "__main__":
    quiet_broken_pipe()
    sys.exit(main())
