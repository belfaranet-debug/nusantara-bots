"""MCP client (Python standard library only) for the Nusantara Bots server.

Lives on the PC at ``/data/grokbot-own/app/mcp_client.py`` next to
``server.py`` and is imported lazily by the server endpoints, so the
server still starts if this module is absent (the MCP endpoints then
answer 503).

What it does, in one paragraph: read the bot's own MCP config file
(``mcp.json``, one entry per server: stdio ``{command, args, env}`` or
remote ``{type, url, headers}``), spawn each enabled server, run the
``initialize`` / ``tools/list`` / ``tools/call`` handshake over JSON-RPC
2.0, and present the union of tools to the app under stable names
``<server>_<tool>``. Servers that fail to start are reported in
``skipped`` instead of failing the whole request. Large catalogues
switch to directory mode (``search_tools`` / ``describe_tool`` /
``call_tool``); large results are trimmed, spilled to disk, and
re-readable locally through ``query_result``.

Security rules baked in here (not optional):

* A child process's stderr is discarded, never recorded -- it may
  contain credentials.
* Secret values (env / header values from the config) are never
  returned by any listing and never appear in error text; errors are
  passed through :func:`redact_text` before leaving this module.
* A child only ever receives its own entry's env, on top of a copy of
  the server environment with control-plane variables stripped.
* A call whose outcome is uncertain (timeout, transport cut after the
  request was sent) is never retried blindly: the session fails with
  :class:`McpSessionError` and the caller must not re-dispatch it.
"""

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

# ---------------------------------------------------------------------------
# Constants (limits follow the reference design's tested numbers)
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "nusantara-bots"

MAX_SERVERS = 32            # servers aggregated per request
MAX_TOOLS = 128             # tools exposed per request
CATALOG_BYTES = 1024 * 1024  # total tool-catalog JSON per request
SCHEMA_BYTES = 64 * 1024    # max inputSchema JSON per tool
DEFAULT_RESULT_BUDGET = 8000   # chars of tool result shown to the model
MAX_RESULT_BUDGET = 2_000_000
STARTUP_STDIO_S = 8.0       # handshake deadline, stdio server
STARTUP_REMOTE_S = 30.0     # handshake deadline, remote server
DEFAULT_CALL_TIMEOUT_S = 600.0  # one tools/call, 10 minutes
MAX_REMOTE_BYTES = 32 * 1024 * 1024  # max HTTP response body
STDIO_FRAME_BYTES = 32 * 1024 * 1024  # max one JSON-RPC line, stdio (0.10.11: raised from 2 MB -- rea's tools/list frame alone is 3,158,397 bytes for 207 tools; matches MausBot MAX_REMOTE_MCP_BYTES 32 MB)
MAX_LIST_PAGES = 100        # tools/list pagination cap
DIRECTORY_TOOL_MAX = 40     # > this many tools -> directory mode
DIRECTORY_CATALOG_CHARS = 100_000  # or catalog JSON larger than this
RESULT_STORE_MAX_BYTES = 64 * 1024 * 1024  # trimmed results held in memory
SPILL_MAX_AGE_S = 24 * 3600  # spilled results older than this are deleted
OUTPUT_BYTES = 50 * 1024    # cap on result text handed back per call
KILL_GRACE_S = 5.0          # SIGTERM grace before SIGKILL on close

SERVER_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
# Control-plane env prefix never passed to a child MCP server.
CONTROL_ENV_PREFIXES = ("GROKBOT_",)
RESERVED_SERVER_NAMES = {"local"}  # "local" is the built-in result store

_DIRECTORY_TOOLS = ("search_tools", "describe_tool", "call_tool")


class McpError(Exception):
    """A plain MCP failure (bad config, refused call, bad schema)."""


class McpSessionError(McpError):
    """The session failed after a request may have been dispatched.

    The outcome of the in-flight operation is UNKNOWN -- it may
    already have taken effect. Callers must not retry it.
    """


class McpAuthError(McpError):
    """The remote server refused its credentials (HTTP 401)."""


# ---------------------------------------------------------------------------
# Naming: <server>_<tool>, lowercase, sanitised, max 64 chars
# ---------------------------------------------------------------------------

def _sanitize(name):
    cleaned = re.sub(r"[^a-z0-9_]", "_", str(name).lower()).strip("_")
    return cleaned or "tool"


def display_tool_name(server, tool):
    """``<server>_<tool>``: lowercase, non ``[a-z0-9_]`` -> ``_``, <= 64."""
    return ("%s_%s" % (_sanitize(server), _sanitize(tool)))[:64]


def unique_tool_name(server, tool, used):
    """Display name with ``_2`` / ``_3`` ... suffix on collision.

    The suffix is fitted so the total stays <= 64 characters.
    """
    base = display_tool_name(server, tool)
    if base not in used:
        return base
    n = 2
    while True:
        suffix = "_%d" % n
        candidate = base[:64 - len(suffix)] + suffix
        if candidate not in used:
            return candidate
        n += 1


# ---------------------------------------------------------------------------
# Scope: allow / deny selectors  mcp:<server>:<tool>  or  mcp:<server>:*
# ---------------------------------------------------------------------------

def _selector_matches(selector, server, tool):
    if not isinstance(selector, str):
        return False
    parts = selector.split(":")
    if len(parts) != 3 or parts[0] != "mcp":
        return False
    if parts[1] != server:
        return False
    return parts[2] == "*" or parts[2] == tool


def validate_scope(scope):
    """Return an error string, or None when the scope is well formed."""
    if scope is None:
        return None
    if not isinstance(scope, dict):
        return "toolScope must be an object"
    for key in ("allow", "deny"):
        selectors = scope.get(key)
        if selectors is None:
            continue
        if not isinstance(selectors, list) or len(selectors) > 256:
            return "toolScope.%s must be a list of at most 256 selectors" % key
        for sel in selectors:
            if not isinstance(sel, str) or len(sel) > 1024:
                return "toolScope selector must be a string of at most 1024 chars"
            if any(ord(c) < 32 for c in sel):
                return "toolScope selector contains a control character"
            parts = sel.split(":")
            if len(parts) != 3 or parts[0] != "mcp" or not parts[1]:
                return "bad toolScope selector: %r" % sel
            if "*" in parts[1] or ("*" in parts[2] and parts[2] != "*"):
                return "bad toolScope selector: %r" % sel
    return None


def scope_allows(scope, server, tool):
    """True when this (server, tool) passes the scope.

    Deny always wins. An empty/missing allow list permits everything
    except what is denied.
    """
    if not scope:
        return True
    for sel in scope.get("deny") or []:
        if _selector_matches(sel, server, tool):
            return False
    allow = scope.get("allow") or []
    if not allow:
        return True
    return any(_selector_matches(sel, server, tool) for sel in allow)


def scope_allows_server(scope, server):
    """True when the scope permits at least one tool of this server."""
    if not scope:
        return True
    for sel in scope.get("deny") or []:
        if isinstance(sel, str) and sel == "mcp:%s:*" % server:
            return False
    allow = scope.get("allow") or []
    if not allow:
        return True
    return any(isinstance(sel, str) and sel.split(":")[1:2] == [server]
               for sel in allow)


# ---------------------------------------------------------------------------
# Config: load / normalise / public view / secrets
# ---------------------------------------------------------------------------

def normalize_entry(name, raw):
    """Validate one config entry. Return (entry, None) or (None, reason).

    Accepts stdio entries with or without an explicit ``"type": "stdio"``
    (an entry that carries ``type`` must not be misread as remote), and
    remote entries with ``type`` ``http`` (default) or ``sse``.
    """
    if not isinstance(raw, dict):
        return None, "entry for %s is not an object" % name
    if not SERVER_NAME_RE.match(name):
        return None, ("server name %r must match ^[a-z][a-z0-9_-]{0,31}$"
                      % name)
    if name in RESERVED_SERVER_NAMES:
        return None, "server name %r is reserved" % name
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        return None, "enabled for %s must be a boolean" % name
    budget = raw.get("resultBudget", DEFAULT_RESULT_BUDGET)
    if not isinstance(budget, int) or isinstance(budget, bool) \
            or budget < 0 or budget > MAX_RESULT_BUDGET:
        return None, ("resultBudget for %s must be an integer 0..%d"
                      % (name, MAX_RESULT_BUDGET))
    scope = raw.get("toolScope")
    scope_err = validate_scope(scope)
    if scope_err is not None:
        return None, "toolScope for %s: %s" % (name, scope_err)
    entry = {"enabled": enabled, "resultBudget": budget}
    if scope is not None:
        entry["toolScope"] = scope
    etype = raw.get("type")
    if etype in ("http", "sse"):
        url = raw.get("url") or ""
        if not isinstance(url, str) or not (
                url.startswith("http://") or url.startswith("https://")):
            return None, "url for %s must start with http:// or https://" % name
        if len(url) > 2048:
            return None, "url for %s is too long" % name
        parsed = urllib.parse.urlparse(url)
        if parsed.username or parsed.password:
            return None, ("credentials must go in a header, not in the "
                          "url for %s" % name)
        headers = raw.get("headers") or {}
        if not isinstance(headers, dict) or len(headers) > 32:
            return None, "headers for %s must be an object of at most 32" % name
        for key, value in headers.items():
            if not isinstance(key, str) or not HEADER_NAME_RE.match(key):
                return None, "bad header name for %s: %r" % (name, key)
            if not isinstance(value, str) or "\n" in value or "\r" in value:
                return None, "header %s for %s must be a single line" % (key, name)
        entry.update({"type": etype, "url": url, "headers": dict(headers)})
        return entry, None
    if etype is not None and etype != "stdio":
        return None, "unknown type for %s: %r" % (name, etype)
    command = raw.get("command") or ""
    if not isinstance(command, str) or not command.strip() \
            or len(command) > 1024:
        return None, "command for %s is required (max 1024 chars)" % name
    args = raw.get("args") or []
    if not isinstance(args, list) or len(args) > 64 \
            or any(not isinstance(a, str) for a in args):
        return None, "args for %s must be a list of at most 64 strings" % name
    env = raw.get("env") or {}
    if not isinstance(env, dict) or len(env) > 64:
        return None, "env for %s must be an object of at most 64" % name
    for key, value in env.items():
        if not isinstance(key, str) or not ENV_NAME_RE.match(key):
            return None, "bad env name for %s: %r" % (name, key)
        if not isinstance(value, str):
            return None, "env %s for %s must be a string" % (key, name)
    entry.update({"command": command, "args": list(args), "env": dict(env)})
    return entry, None


def load_bot_config(path):
    """Load a bot MCP config file. Return (config, skipped).

    ``config`` maps server name -> normalised entry (enabled ones and
    disabled ones alike; callers filter on ``enabled``). ``skipped``
    is a list of ``{"server": name, "reason": ...}`` for entries that
    failed validation -- loading never fails the whole file. A missing
    or unreadable file means "no servers configured": ({}, []).
    Keys starting with "_" are ignored (comments).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}, []
    if not isinstance(raw, dict):
        return {}, []
    config, skipped = {}, []
    for name, entry in raw.items():
        if not isinstance(name, str) or name.startswith("_"):
            continue
        normalised, reason = normalize_entry(name, entry)
        if normalised is None:
            skipped.append({"server": name, "reason": reason})
        else:
            config[name] = normalised
    return config, skipped


def public_entry(entry):
    """A listing-safe view of one entry: key names only, never values."""
    out = {"enabled": entry.get("enabled", True),
           "resultBudget": entry.get("resultBudget", DEFAULT_RESULT_BUDGET)}
    if "toolScope" in entry:
        out["toolScope"] = entry["toolScope"]
    if entry.get("type") in ("http", "sse"):
        out.update({"type": entry["type"], "url": entry["url"],
                    "headerKeys": sorted(entry.get("headers") or {})})
    else:
        out.update({"command": entry.get("command"),
                    "args": list(entry.get("args") or []),
                    "envKeys": sorted(entry.get("env") or {})})
    return out


def collect_secrets(config):
    """Secret values in a config, for redaction. Never logged.

    A value counts as a secret when it is a header value (headers
    carry auth by nature) or its env key looks like a credential
    (token / key / secret / password / auth / ...). Plain paths and
    flags are NOT secrets -- redacting them would only mangle useful
    diagnostics.
    """
    secret_key = re.compile(r"(token|secret|password|auth|credential|"
                            r"bearer|api[_-]?key|private)", re.IGNORECASE)
    secrets = []
    for entry in config.values():
        for key, value in (entry.get("env") or {}).items():
            if isinstance(value, str) and len(value) >= 4 \
                    and secret_key.search(key):
                secrets.append(value)
        for value in (entry.get("headers") or {}).values():
            if isinstance(value, str) and len(value) >= 4:
                secrets.append(value)
    secrets.sort(key=len, reverse=True)
    return secrets


def redact_text(text, secrets):
    """Replace every configured secret value in ``text`` with [redacted]."""
    if not text:
        return text
    for secret in secrets or []:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _child_env(entry_env):
    """Env for a child MCP server: control-plane vars stripped, then the
    entry's own env applied last. The child never sees operator secrets.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(CONTROL_ENV_PREFIXES)}
    env.update(entry_env or {})
    return env


def default_spill_dir():
    """Where trimmed results are spilled (0600 files, >24h cleaned)."""
    override = os.environ.get("MCP_SPILL_DIR")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".cache",
                        "nusantara-bots", "mcp-spill")


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 over newline-delimited frames
# ---------------------------------------------------------------------------

class LineChannel:
    """One JSON-RPC 2.0 peer over a line-delimited byte stream.

    ``reader`` needs ``readline(limit)``; ``writer`` needs ``write`` +
    ``flush``. A background thread dispatches responses to waiting
    requests by id. Requests coming FROM the server are answered
    ``-32601 Method not supported`` (this client serves no methods).
    A malformed frame fails the session: pending requests raise
    :class:`McpSessionError` and no blind retry may follow.
    """

    def __init__(self, reader, writer, frame_limit=STDIO_FRAME_BYTES):
        self._reader = reader
        self._writer = writer
        self._frame_limit = frame_limit
        self._next_id = 0
        self._pending = {}  # id -> [event, box]
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._failed = None
        self._closed = False
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    # -- sending ---------------------------------------------------------
    def _send(self, obj):
        raw = (json.dumps(obj) + "\n").encode("utf-8")
        with self._write_lock:
            self._writer.write(raw)
            self._writer.flush()

    def notify(self, method, params=None):
        if self._closed:
            return
        try:
            self._send({"jsonrpc": "2.0", "method": method,
                        "params": params or {}})
        except (OSError, ValueError):
            pass

    def request(self, method, params=None, timeout=DEFAULT_CALL_TIMEOUT_S):
        """Send a request and wait for its result.

        Raises McpError on a protocol error reply, McpSessionError when
        the outcome is uncertain (timeout / transport failure after the
        request was sent).
        """
        if self._closed or self._failed is not None:
            raise McpSessionError("session is closed: %s"
                                  % (self._failed or "closed"))
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
        event = threading.Event()
        box = {}
        with self._lock:
            self._pending[req_id] = [event, box]
        try:
            self._send({"jsonrpc": "2.0", "id": req_id,
                        "method": method, "params": params or {}})
        except (OSError, ValueError) as e:
            with self._lock:
                self._pending.pop(req_id, None)
            self._fail("send failed: %s" % e)
            raise McpSessionError("failed to send %s: %s" % (method, e))
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            # The operation may already be running upstream: tell the
            # server to cancel, and report the outcome as uncertain.
            self.notify("notifications/cancelled",
                        {"requestId": req_id, "reason": "timeout"})
            raise McpSessionError(
                "timeout waiting for %s (outcome may be uncertain)" % method)
        if "error" in box:
            err = box["error"]
            if isinstance(err, dict):
                raise McpError("MCP error %s: %s"
                               % (err.get("code"), err.get("message")))
            raise McpError("MCP error: %s" % (err,))
        if self._failed is not None:
            raise McpSessionError("session failed: %s" % self._failed)
        return box.get("result")

    # -- receiving -------------------------------------------------------
    def _fail(self, reason):
        with self._lock:
            if self._failed is None:
                self._failed = reason
            pending = list(self._pending.values())
            self._pending.clear()
        for event, _box in pending:
            event.set()

    def _read_loop(self):
        try:
            while not self._closed:
                line = self._reader.readline(self._frame_limit + 2)
                if not line:
                    self._fail("transport closed")
                    return
                if len(line) > self._frame_limit or (
                        len(line) == self._frame_limit + 2
                        and not line.endswith(b"\n")):
                    self._fail("frame exceeds %d bytes" % self._frame_limit)
                    return
                try:
                    frame = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._fail("invalid JSON-RPC frame")
                    return
                self._dispatch(frame)
        except (OSError, ValueError) as e:
            self._fail("read failed: %s" % e)

    def _dispatch(self, frame):
        if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
            self._fail("invalid JSON-RPC envelope")
            return
        if "method" in frame:
            if "id" in frame:
                # Server -> client request: this client serves no methods.
                try:
                    self._send({"jsonrpc": "2.0", "id": frame["id"],
                                "error": {"code": -32601,
                                          "message": "Method not supported"}})
                except (OSError, ValueError):
                    pass
            return  # notification from the server: ignore
        req_id = frame.get("id")
        if req_id is None or ("result" not in frame
                              and "error" not in frame):
            self._fail("invalid JSON-RPC response envelope")
            return
        with self._lock:
            slot = self._pending.pop(req_id, None)
        if slot is None:
            # A response nobody asked for: corrupt stream, fail closed.
            self._fail("unsolicited response id %r" % (req_id,))
            return
        event, box = slot
        if "error" in frame:
            box["error"] = frame["error"]
        else:
            box["result"] = frame.get("result")
        event.set()

    def close(self):
        """Stop waiting. Does NOT close the reader synchronously: the
        reader thread may be blocked inside ``readline()`` on the same
        buffered object, and closing it from here would deadlock
        until that read returns. The owner (e.g. StdioSession) ends
        the stream -- killing the child produces EOF, which lets the
        reader thread exit on its own (it is a daemon thread).
        """
        self._closed = True
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for event, _box in pending:
            event.set()


# ---------------------------------------------------------------------------
# Schema validation (stdlib, shallow-but-recursive)
# ---------------------------------------------------------------------------

_SIMPLE_TYPES = {
    "string": str, "integer": int, "number": (int, float),
    "boolean": bool, "array": list, "object": dict, "null": type(None),
}


def validate_schema(schema):
    """Return an error string, or None when the schema is usable.

    A tool schema must be an object schema and its JSON must fit in
    SCHEMA_BYTES. (Oversized / non-object schemas drop the tool.)
    """
    if not isinstance(schema, dict):
        return "inputSchema must be an object"
    if "type" in schema and schema["type"] != "object":
        return "inputSchema type must be object"
    try:
        size = len(json.dumps(schema))
    except (TypeError, ValueError):
        return "inputSchema is not JSON-serialisable"
    if size > SCHEMA_BYTES:
        return "inputSchema exceeds %d bytes" % SCHEMA_BYTES
    props = schema.get("properties")
    if props is not None and not isinstance(props, dict):
        return "inputSchema.properties must be an object"
    required = schema.get("required")
    if required is not None and (not isinstance(required, list)
                                 or any(not isinstance(r, str)
                                        for r in required)):
        return "inputSchema.required must be a list of strings"
    return None


def _check_value(schema, value, path, errors, depth):
    if not isinstance(schema, dict) or depth > 5:
        return
    expected = schema.get("type")
    if expected is not None:
        if isinstance(expected, list):
            ok = any(_matches_type(t, value) for t in expected)
        else:
            ok = _matches_type(expected, value)
        if not ok:
            errors.append("%s: expected %s, got %s"
                          % (path or "arguments", expected,
                             type(value).__name__))
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append("%s: value not in enum" % (path or "arguments",))
    if isinstance(value, dict):
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                _check_value(sub, value[key],
                             "%s.%s" % (path, key) if path else key,
                             errors, depth + 1)
        for key in schema.get("required") or []:
            if key not in value:
                errors.append("%s: missing required property %r"
                              % (path or "arguments", key))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _check_value(schema["items"], item, "%s[%d]" % (path, i),
                         errors, depth + 1)


def _matches_type(expected, value):
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) \
            and not isinstance(value, bool)
    wanted = _SIMPLE_TYPES.get(expected)
    if wanted is None:
        return True  # unknown type name: do not invent a rejection
    return isinstance(value, wanted)


def validate_arguments(schema, args):
    """Check ``args`` against a tool's inputSchema. Return error list."""
    if not isinstance(args, dict):
        return ["arguments must be an object"]
    errors = []
    _check_value(schema or {}, args, "", errors, 0)
    return errors


# ---------------------------------------------------------------------------
# Result store: trim + spill + local query
# ---------------------------------------------------------------------------

def describe_shape(text):
    """One-line shape summary of a tool result (for the trimmed view)."""
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        lines = text.count("\n") + 1 if text else 0
        return "text, %d characters, %d lines" % (len(text), lines)
    if isinstance(value, dict):
        keys = list(value.keys())
        shown = ", ".join(str(k) for k in keys[:8])
        more = " (+%d more)" % (len(keys) - 8) if len(keys) > 8 else ""
        return "object with keys: %s%s" % (shown, more)
    if isinstance(value, list):
        first = ""
        if value:
            item = value[0]
            first = (", first item is an object with keys: %s"
                     % ", ".join(str(k) for k in list(item.keys())[:5])) \
                if isinstance(item, dict) else \
                ", first item is %s" % type(item).__name__
        return "array of %d items%s" % (len(value), first)
    return "%s value" % type(value).__name__


def _example_expression(text):
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return "result[0:3]"
    if isinstance(value, dict) and value:
        return "%s" % next(iter(value.keys()))
    if isinstance(value, list) and value:
        return "[0]"
    return "result"


class ResultStore:
    """Holds full tool results that were trimmed for the model.

    Full text is spilled to disk (0600, cleaned when older than 24h)
    and held in memory up to RESULT_STORE_MAX_BYTES (oldest evicted
    first). :meth:`query` answers locally -- it never calls upstream.
    """

    def __init__(self, spill_dir=None, max_bytes=RESULT_STORE_MAX_BYTES):
        self.spill_dir = spill_dir or default_spill_dir()
        self.max_bytes = max_bytes
        self._items = OrderedDict()  # handle -> {text, bytes, spill}
        self._total = 0
        self._seq = 0
        try:
            os.makedirs(self.spill_dir, exist_ok=True)
        except OSError:
            pass

    def put(self, text, structured=None):
        """Store a full result. Return its handle (``r1``, ``r2`` ...)."""
        self._cleanup_spill()
        self._seq += 1
        handle = "r%d" % self._seq
        data = text.encode("utf-8")
        spill = None
        try:
            path = os.path.join(self.spill_dir, handle + ".json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            spill = path
        except OSError:
            spill = None
        self._items[handle] = {"text": text, "bytes": len(data),
                               "spill": spill, "structured": structured}
        self._total += len(data)
        # Memory eviction drops the in-memory copy only. The spill
        # file stays until the 24 h cleanup: it is the durable copy
        # query_result falls back to.
        while self._total > self.max_bytes and len(self._items) > 1:
            _old, item = self._items.popitem(last=False)
            self._total -= item["bytes"]
        return handle

    def __len__(self):
        return len(self._items)

    def get(self, handle):
        """Full stored text for a handle, or None (memory or spill)."""
        item = self._items.get(handle)
        if item is not None:
            return item["text"]
        path = os.path.join(self.spill_dir, handle + ".json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except (OSError, ValueError):
            return None

    def _cleanup_spill(self):
        try:
            now = time.time()
            for name in os.listdir(self.spill_dir):
                path = os.path.join(self.spill_dir, name)
                try:
                    if now - os.path.getmtime(path) > SPILL_MAX_AGE_S:
                        os.unlink(path)
                except OSError:
                    pass
        except OSError:
            pass

    def summarize(self, text, budget, handle):
        """The trimmed view the model sees: head + shape + handle.

        The whole summary fits inside ``budget`` characters.
        """
        total = len(text)
        shape = describe_shape(text)
        example = _example_expression(text)
        # Size the head so the whole summary fits inside budget; if
        # digit counts shift the length, trim the head by the excess.
        footer = ("\n\n[shape: " + shape + "]\n[read more with "
                  "query_result, handle " + handle
                  + ", e.g. expression " + repr(example) + "]")
        head_len = max(0, budget - len(footer) - 120)
        head = text[:head_len]
        tail = "\n..." if total > head_len else ""
        header = ("[trimmed: showing the first %d of %d characters; "
                  "full result saved as %s]\n" % (head_len, total, handle))
        summary = header + head + tail + footer
        if len(summary) > budget:
            excess = len(summary) - budget
            head = head[:max(0, len(head) - excess)]
            header = ("[trimmed: showing the first %d of %d characters; "
                      "full result saved as %s]\n"
                      % (len(head), total, handle))
            summary = header + head + tail + footer
        return summary

    def query(self, handle, expression):
        """Evaluate a dotted-path expression over a stored result.

        Supports dotted keys, ``[n]`` indexes and ``[*]`` wildcards
        (a tiny evaluator, no new dependency). Returns
        ``(value, None)`` or ``(None, error)``. Read-only; local only.
        """
        text = self.get(handle)
        if text is None:
            return None, "unknown result handle %r" % handle
        try:
            value = json.loads(text)
        except ValueError:
            value = text
        try:
            return _eval_path(value, expression), None
        except (KeyError, IndexError, TypeError, ValueError) as e:
            return None, "cannot evaluate %r: %s" % (expression, e)


def _eval_path(value, expression):
    """Dotted path + [index] + [*] over JSON-like data."""
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("empty expression")
    current = [value]
    for segment in expression.split("."):
        if not segment:
            raise ValueError("empty path segment in %r" % expression)
        # segment may be: key, key[0], key[*], [0], [*]
        match = re.fullmatch(r"([^\[\]]*)(.*)", segment)
        key, rest = match.group(1), match.group(2)
        if key:
            current = [_get_key(item, key) for item in current]
        while rest:
            bracket = re.fullmatch(r"\[([^\]]*)\](.*)", rest)
            if not bracket:
                raise ValueError("bad bracket in %r" % expression)
            inside, rest = bracket.group(1), bracket.group(2)
            if inside == "*":
                expanded = []
                for item in current:
                    if isinstance(item, list):
                        expanded.extend(item)
                    else:
                        raise TypeError("wildcard over non-array")
                current = expanded
            elif inside == "":
                raise ValueError("empty index in %r" % expression)
            else:
                try:
                    idx = int(inside)
                except ValueError:
                    raise ValueError("bad index %r" % inside)
                current = [_get_index(item, idx) for item in current]
    if len(current) == 1:
        return current[0]
    return current


def _get_key(item, key):
    if isinstance(item, dict) and key in item:
        return item[key]
    raise KeyError(key)


def _get_index(item, idx):
    if isinstance(item, list):
        return item[idx]  # IndexError propagates as evaluation error
    raise TypeError("index over non-array")


_SHARED_STORE = None


def get_shared_store(spill_dir=None):
    """The process-wide result store (handles survive across requests)."""
    global _SHARED_STORE
    if _SHARED_STORE is None:
        _SHARED_STORE = ResultStore(spill_dir=spill_dir)
    return _SHARED_STORE


# ---------------------------------------------------------------------------
# SSE parsing (shared by the HTTP transports)
# ---------------------------------------------------------------------------

def parse_sse_text(text):
    """Parse SSE text into [(event, data), ...]. Blank line ends one event.

    Multiple ``data:`` lines join with "\\n". Comment lines (``: ...``)
    and ``id:`` / ``retry:`` fields are ignored.
    """
    events = []
    event, data_lines = "message", []
    for line in text.splitlines():
        if line == "":
            if data_lines or event != "message":
                events.append((event, "\n".join(data_lines)))
            event, data_lines = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        elif line.startswith("event:"):
            event = line[6:].strip() or "message"
        # id: / retry: ignored
    if data_lines or event != "message":
        events.append((event, "\n".join(data_lines)))
    return events


# ---------------------------------------------------------------------------
# Stdio session
# ---------------------------------------------------------------------------

class StdioSession:
    """One stdio MCP server: spawn, handshake, list, call, kill.

    ``proc_factory`` is injectable for tests (defaults to
    ``subprocess.Popen``). Stderr is discarded, never recorded.
    """

    def __init__(self, entry, proc_factory=None):
        self.entry = entry
        self._proc_factory = proc_factory or subprocess.Popen
        self._proc = None
        self._channel = None

    def start(self, timeout=STARTUP_STDIO_S):
        command = [self.entry["command"]] + list(self.entry.get("args") or [])
        try:
            self._proc = self._proc_factory(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=_child_env(self.entry.get("env")),
                start_new_session=True)
        except (OSError, ValueError) as e:
            raise McpError("cannot start %r: %s" % (command[0], e))
        try:
            self._channel = LineChannel(self._proc.stdout, self._proc.stdin)
            self._channel.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": "0.10.11"},
            }, timeout=timeout)
            self._channel.notify("notifications/initialized")
        except McpError:
            self.close()
            raise
        return self

    def list_tools(self):
        tools, seen_cursors = [], set()
        cursor = None
        for _ in range(MAX_LIST_PAGES):
            params = {}
            if cursor:
                params["cursor"] = cursor
            result = self._channel.request("tools/list", params,
                                           timeout=STARTUP_STDIO_S)
            if not isinstance(result, dict) or not isinstance(
                    result.get("tools"), list):
                raise McpSessionError("bad tools/list response")
            tools.extend(result["tools"])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
            if cursor in seen_cursors:
                raise McpSessionError("tools/list cursor loop")
            seen_cursors.add(cursor)
        raise McpSessionError("tools/list exceeded %d pages" % MAX_LIST_PAGES)

    def call_tool(self, name, arguments, timeout=DEFAULT_CALL_TIMEOUT_S):
        try:
            result = self._channel.request(
                "tools/call",
                {"name": name, "arguments": arguments or {}},
                timeout=timeout)
        except McpError as e:
            if isinstance(e, McpSessionError):
                raise
            # A refused call is a tool result, not a session failure.
            return {"text": "tool error: %s" % e, "isError": True,
                    "structuredContent": None}
        return _parse_call_result(result)

    def close(self):
        channel, proc = self._channel, self._proc
        self._channel, self._proc = None, None
        if channel is not None:
            channel.close()
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, 15)  # SIGTERM to the whole tree
                except (OSError, ProcessLookupError):
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=KILL_GRACE_S)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, 9)  # SIGKILL
                    except (OSError, ProcessLookupError):
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
        except Exception:
            pass
        # The pipes are deliberately NOT closed here: the reader
        # thread may still be inside readline() on proc.stdout, and
        # closing that buffered object from this thread would block
        # until the read returns. The dead child puts the pipes at
        # EOF, the reader thread exits on its own, and the pipe
        # objects are then released with the (now unreferenced)
        # Popen/channel objects.


def _parse_call_result(result):
    """Normalise a tools/call result to {text, isError, structuredContent}."""
    if not isinstance(result, dict):
        return {"text": "tool returned a non-object result",
                "isError": True, "structuredContent": None}
    texts = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            texts.append(str(item.get("text") or ""))
        elif isinstance(item, dict):
            texts.append("[unhandled content type: %s]"
                         % item.get("type"))
    structured = result.get("structuredContent")
    if structured is not None and not texts:
        texts.append(json.dumps(structured, ensure_ascii=False))
    return {"text": "".join(texts), "isError": bool(result.get("isError")),
            "structuredContent": structured}


# ---------------------------------------------------------------------------
# HTTP session: Streamable HTTP (POST + GET SSE) + legacy SSE
# ---------------------------------------------------------------------------

class _HttpResponse:
    """Minimal response shape used by the injectable opener."""

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers  # dict-like, case-insensitive get
        self._body = body

    def read(self, size=-1):
        if size is None or size < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:size], self._body[size:]
        return data


class HttpSession:
    """One remote MCP server over HTTP.

    Streamable HTTP (``type: http``): POST the JSON-RPC request; the
    reply is a single JSON object or an SSE stream. Legacy SSE
    (``type: sse``): open a GET event stream first, take the
    ``endpoint`` event, POST requests there, read replies off the
    stream. ``opener`` is injectable for tests (defaults to
    ``urllib.request.urlopen``). Responses are capped at 32 MB.
    A 401 raises McpAuthError with a check-credentials hint, not the
    raw error.
    """

    def __init__(self, entry, opener=None):
        self.entry = entry
        self.url = entry["url"]
        self._opener = opener or urllib.request.urlopen
        self._session_id = None
        self._next_id = 0
        self._pending = {}  # id -> [event, box]
        self._lock = threading.Lock()
        self._sse_thread = None
        self._sse_endpoint = None
        self._sse_endpoint_event = threading.Event()
        self._sse_failed = None
        self._closed = False

    # -- setup -----------------------------------------------------------
    def start(self, timeout=STARTUP_REMOTE_S):
        if self.entry.get("type") == "sse":
            self._open_sse_stream(timeout)
        result = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": "0.10.11"},
        }, timeout=timeout)
        if not isinstance(result, dict):
            raise McpSessionError("bad initialize response")
        self._notify("notifications/initialized")
        return self

    def _headers(self, extra=None):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        headers.update(self.entry.get("headers") or {})
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if extra:
            headers.update(extra)
        return headers

    def _read_capped(self, response):
        chunks, total = [], 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_REMOTE_BYTES:
                raise McpSessionError("response exceeds %d bytes"
                                      % MAX_REMOTE_BYTES)
            chunks.append(chunk)
        return b"".join(chunks)

    def _do_post(self, url, payload, timeout):
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers=self._headers(), method="POST")
        try:
            response = self._opener(request, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise McpAuthError(
                    "this server refused its credentials (HTTP 401). "
                    "Check the configured header / sign-in and try again")
            raise McpError("HTTP %d from %s" % (e.code, self.url))
        except (urllib.error.URLError, OSError, socket.timeout) as e:
            raise McpError("cannot reach %s: %s" % (self.url, e))
        return response

    def _response_session_id(self, response):
        headers = getattr(response, "headers", {}) or {}
        get = getattr(headers, "get", None)
        value = get("mcp-session-id") if get else None
        if value:
            self._session_id = value

    # -- streamable HTTP -------------------------------------------------
    def _rpc_streamable(self, method, params, timeout):
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
        response = self._do_post(
            self.url, {"jsonrpc": "2.0", "id": req_id,
                       "method": method, "params": params or {}},
            timeout)
        self._response_session_id(response)
        content_type = ""
        headers = getattr(response, "headers", {}) or {}
        get = getattr(headers, "get", None)
        if get:
            content_type = get("Content-Type") or get("content-type") or ""
        raw = self._read_capped(response)
        if "text/event-stream" in content_type:
            for _event, data in parse_sse_text(raw.decode("utf-8", "replace")):
                try:
                    frame = json.loads(data)
                except ValueError:
                    continue
                if isinstance(frame, dict) and frame.get("id") == req_id:
                    return _unwrap_response(frame)
            raise McpSessionError("no matching response in SSE stream")
        try:
            frame = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise McpSessionError("non-JSON response from %s" % self.url)
        if isinstance(frame, list):
            for item in frame:
                if isinstance(item, dict) and item.get("id") == req_id:
                    return _unwrap_response(item)
            raise McpSessionError("no matching response in batch")
        return _unwrap_response(frame)

    # -- legacy SSE ------------------------------------------------------
    def _open_sse_stream(self, timeout):
        request = urllib.request.Request(
            self.url, headers={"Accept": "text/event-stream"},
            method="GET")
        try:
            response = self._opener(request, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise McpAuthError(
                    "this server refused its credentials (HTTP 401). "
                    "Check the configured header / sign-in and try again")
            raise McpError("HTTP %d from %s" % (e.code, self.url))
        except (urllib.error.URLError, OSError, socket.timeout) as e:
            raise McpError("cannot reach %s: %s" % (self.url, e))
        self._sse_thread = threading.Thread(
            target=self._sse_read_loop, args=(response,), daemon=True)
        self._sse_thread.start()
        if not self._sse_endpoint_event.wait(timeout):
            raise McpError("SSE stream gave no endpoint within %ds"
                           % timeout)
        if self._sse_failed is not None:
            raise McpError("SSE stream failed: %s" % self._sse_failed)

    def _sse_read_loop(self, response):
        buffer = ""
        try:
            while not self._closed:
                chunk = response.read(65536)
                if not chunk:
                    break
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", "replace")
                buffer += chunk
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    for event, data in parse_sse_text(block + "\n\n"):
                        self._sse_dispatch(event, data)
        except (OSError, ValueError) as e:
            self._sse_failed = str(e)
        finally:
            self._sse_endpoint_event.set()

    def _sse_dispatch(self, event, data):
        if event == "endpoint":
            self._sse_endpoint = urllib.parse.urljoin(self.url, data.strip())
            self._sse_endpoint_event.set()
            return
        try:
            frame = json.loads(data)
        except ValueError:
            return
        if not isinstance(frame, dict) or frame.get("id") is None:
            return
        with self._lock:
            slot = self._pending.pop(frame["id"], None)
        if slot is not None:
            event_obj, box = slot
            box["frame"] = frame
            event_obj.set()

    def _rpc_sse(self, method, params, timeout):
        if not self._sse_endpoint:
            raise McpSessionError("SSE endpoint not established")
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
        event_obj = threading.Event()
        box = {}
        with self._lock:
            self._pending[req_id] = [event_obj, box]
        try:
            response = self._do_post(
                self._sse_endpoint,
                {"jsonrpc": "2.0", "id": req_id,
                 "method": method, "params": params or {}}, timeout)
            self._read_capped(response)  # 202 + empty body; replies come via SSE
        except McpError:
            with self._lock:
                self._pending.pop(req_id, None)
            raise
        if not event_obj.wait(timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            raise McpSessionError(
                "timeout waiting for %s (outcome may be uncertain)" % method)
        return _unwrap_response(box["frame"])

    # -- common ----------------------------------------------------------
    def _rpc(self, method, params, timeout):
        if self.entry.get("type") == "sse":
            return self._rpc_sse(method, params, timeout)
        return self._rpc_streamable(method, params, timeout)

    def _notify(self, method, params=None):
        if self.entry.get("type") == "sse":
            if not self._sse_endpoint:
                return
            try:
                response = self._do_post(
                    self._sse_endpoint,
                    {"jsonrpc": "2.0", "method": method,
                     "params": params or {}}, timeout=10)
                self._read_capped(response)
            except McpError:
                pass
            return
        try:
            response = self._do_post(
                self.url, {"jsonrpc": "2.0", "method": method,
                           "params": params or {}}, timeout=10)
            self._read_capped(response)
        except McpError:
            pass

    def list_tools(self):
        tools, seen_cursors = [], set()
        cursor = None
        for _ in range(MAX_LIST_PAGES):
            params = {}
            if cursor:
                params["cursor"] = cursor
            result = self._rpc("tools/list", params, timeout=STARTUP_REMOTE_S)
            if not isinstance(result, dict) or not isinstance(
                    result.get("tools"), list):
                raise McpSessionError("bad tools/list response")
            tools.extend(result["tools"])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
            if cursor in seen_cursors:
                raise McpSessionError("tools/list cursor loop")
            seen_cursors.add(cursor)
        raise McpSessionError("tools/list exceeded %d pages" % MAX_LIST_PAGES)

    def call_tool(self, name, arguments, timeout=DEFAULT_CALL_TIMEOUT_S):
        try:
            result = self._rpc("tools/call",
                               {"name": name, "arguments": arguments or {}},
                               timeout=timeout)
        except McpError as e:
            if isinstance(e, (McpSessionError, McpAuthError)):
                raise
            return {"text": "tool error: %s" % e, "isError": True,
                    "structuredContent": None}
        return _parse_call_result(result)

    def close(self):
        self._closed = True
        with self._lock:
            for event_obj, _box in self._pending.values():
                event_obj.set()
            self._pending.clear()


def _unwrap_response(frame):
    if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
        raise McpSessionError("invalid JSON-RPC envelope")
    if "error" in frame:
        err = frame["error"]
        if isinstance(err, dict):
            raise McpError("MCP error %s: %s"
                           % (err.get("code"), err.get("message")))
        raise McpError("MCP error: %s" % (err,))
    if "result" not in frame:
        raise McpSessionError("invalid JSON-RPC response envelope")
    return frame["result"]


def make_session(entry, session_factory=None):
    """Build (but do not start) the right session for one entry."""
    if session_factory is not None:
        return session_factory(entry)
    if entry.get("type") in ("http", "sse"):
        return HttpSession(entry)
    return StdioSession(entry)


# ---------------------------------------------------------------------------
# Directory mode: big catalogues expose search/describe/call only
# ---------------------------------------------------------------------------

STOP_WORDS = {
    # English
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with",
    "by", "is", "are", "get", "set", "list", "show", "me", "my", "how",
    "what", "when", "do", "does", "please", "can", "you", "i",
    # Indonesian
    "yang", "dan", "atau", "di", "ke", "dari", "untuk", "dengan", "adalah",
    "tolong", "saya", "aku", "apa", "bagaimana", "berapa", "ini", "itu",
}


def _stem(word):
    """Tiny suffix stemmer (English + Indonesian), conservative on
    purpose: only strip a suffix when the remaining stem is still
    long enough to be a real word (``files`` -> ``file``, but ``makan``
    stays ``makan``)."""
    if len(word) > 4 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 3 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 4 and word.endswith(("ses", "xes", "zes", "ches",
                                        "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    if len(word) > 6 and word.endswith("kan"):
        return word[:-3]
    if len(word) > 5 and word.endswith("an"):
        return word[:-2]
    return word


def _tokens(text):
    out = []
    for raw in re.split(r"[^a-z0-9]+", (text or "").lower()):
        if not raw or raw in STOP_WORDS:
            continue
        out.append(_stem(raw))
    return out


class DirectoryCatalog:
    """Local searchable index over one server's full tool catalogue."""

    def __init__(self, server, tools):
        self.server = server
        self.tools = list(tools)
        self.by_name = {}
        for tool in self.tools:
            name = tool.get("name")
            if isinstance(name, str):
                self.by_name[name] = tool

    def search(self, query, limit=8):
        """Keyword search (stemming + stop-words), best first, max 20."""
        wanted = _tokens(query)
        if not wanted:
            return []
        limit = max(1, min(int(limit or 8), 20))
        scored = []
        for tool in self.tools:
            name_tokens = _tokens(tool.get("name"))
            desc_tokens = _tokens(tool.get("description"))
            score = 0
            for word in wanted:
                if word in name_tokens:
                    score += 3
                if word in desc_tokens:
                    score += 1
            if score:
                scored.append((score, tool))
        scored.sort(key=lambda pair: -pair[0])
        return [{"name": t.get("name"), "description": t.get("description")}
                for _score, t in scored[:limit]]

    def describe(self, name):
        """Full tool record (schema included) for one tool, or None."""
        return self.by_name.get(name)


def directory_tool_definitions(server):
    """The 3 directory tools exposed for a big-catalogue server."""
    prefix = _sanitize(server)
    return [
        {"name": "%s_search_tools" % prefix,
         "description": "Search this server's tool catalogue by keyword. "
                        "Returns matching tool names and descriptions.",
         "inputSchema": {
             "type": "object",
             "properties": {
                 "query": {"type": "string",
                           "description": "Keywords describing the tool needed"},
                 "limit": {"type": "integer",
                           "description": "Max results (default 8, max 20)"}},
             "required": ["query"]},
         "server": server, "directory": True, "original": None},
        {"name": "%s_describe_tool" % prefix,
         "description": "Show the full input schema of one tool "
                        "from this server's catalogue.",
         "inputSchema": {
             "type": "object",
             "properties": {
                 "name": {"type": "string",
                          "description": "Exact tool name from search_tools"}},
             "required": ["name"]},
         "server": server, "directory": True, "original": None},
        {"name": "%s_call_tool" % prefix,
         "description": "Call one tool from this server's catalogue "
                        "by its exact name.",
         "inputSchema": {
             "type": "object",
             "properties": {
                 "name": {"type": "string",
                          "description": "Exact tool name from search_tools"},
                 "arguments": {"type": "object",
                               "description": "Arguments for that tool"}},
             "required": ["name", "arguments"]},
         "server": server, "directory": True, "original": None},
    ]


def directory_call_target(tool_name, arguments):
    """Map a directory ``call_tool`` invocation to its real target.

    Returns (server, original_tool_name, inner_arguments) or
    (None, None, None) when the call is not a directory call.
    """
    if not isinstance(arguments, dict):
        return None, None, None
    if tool_name.endswith("_call_tool"):
        server = tool_name[:-(len("_call_tool"))]
        return server, arguments.get("name"), arguments.get("arguments") or {}
    return None, None, None


# ---------------------------------------------------------------------------
# Aggregation: config -> tool list + calls
# ---------------------------------------------------------------------------

QUERY_RESULT_TOOL = {
    "name": "query_result",
    "description": "Read part of a previously trimmed tool result by "
                   "handle. Arguments: {handle, expression} where "
                   "expression is a dotted path with [n] indexes and "
                   "[*] wildcards (e.g. \"items[0].title\"). "
                   "Answered locally; never calls the upstream server.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "handle": {"type": "string",
                       "description": "Result handle from a trimmed result"},
            "expression": {"type": "string",
                           "description": "Dotted path, e.g. \"items[0].title\""}},
        "required": ["handle", "expression"]},
    "server": "local", "directory": False, "original": None,
}


def _tool_record(server, raw, used):
    """Normalise one raw tools/list entry, or None when unusable."""
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        return None
    schema = raw.get("inputSchema") or {}
    error = validate_schema(schema)
    if error is not None:
        return None
    name = unique_tool_name(server, raw["name"], used)
    used.add(name)
    return {"name": name,
            "description": raw.get("description") or "",
            "inputSchema": schema,
            "server": server, "directory": False,
            "original": raw["name"]}


class ToolRegistry:
    """Aggregate every enabled server's tools; execute calls on demand.

    Sessions are spawned per operation and closed afterwards (no
    long-lived client cache in this first pass; a per-server cache with
    an inactivity watchdog is the follow-up if spawn-per-call proves
    slow). The result store is shared process-wide so ``query_result``
    handles survive across requests.
    """

    def __init__(self, config, scope=None, session_factory=None,
                 spill_dir=None, store=None):
        self.config = config or {}
        self.scope = scope
        self._session_factory = session_factory
        self.store = store if store is not None else get_shared_store(spill_dir)
        self.tools = []       # public definitions (see QUERY_RESULT_TOOL)
        self.skipped = []     # [{"server", "reason"}]
        self.catalogs = {}    # server -> DirectoryCatalog
        self._by_name = {}    # exposed name -> definition
        self._built = False

    # -- listing ---------------------------------------------------------
    def _list_server(self, name, entry):
        session = make_session(entry, self._session_factory)
        try:
            timeout = (STARTUP_REMOTE_S if entry.get("type") in ("http", "sse")
                       else STARTUP_STDIO_S)
            session.start(timeout=timeout)
            return session.list_tools(), None
        except (McpError, OSError) as e:
            return None, str(e)
        finally:
            try:
                session.close()
            except Exception:
                pass

    def _aggregate_server(self, name, raw_tools, used):
        """Turn one server's raw tools into public definitions.

        Returns (definitions, catalogue_or_None). Directory mode kicks
        in above 40 tools or 100 KB of catalogue JSON.
        """
        catalogue_json = json.dumps(raw_tools, ensure_ascii=False)
        if len(raw_tools) > DIRECTORY_TOOL_MAX \
                or len(catalogue_json) > DIRECTORY_CATALOG_CHARS:
            catalog = DirectoryCatalog(name, raw_tools)
            defs = directory_tool_definitions(name)
            for definition in defs:
                used.add(definition["name"])
            return defs, catalog
        defs = []
        for raw in raw_tools:
            record = _tool_record(name, raw, used)
            if record is None:
                continue
            if not scope_allows(self.scope, name, record["original"]):
                continue
            defs.append(record)
        return defs, None

    def build(self):
        """Spawn every enabled server (max 32) and aggregate its tools."""
        self.tools, self.skipped, self.catalogs = [], [], {}
        self._by_name = {}
        used = set()
        names = [n for n, e in self.config.items()
                 if e.get("enabled", True)]
        if len(names) > MAX_SERVERS:
            for extra in names[MAX_SERVERS:]:
                self.skipped.append(
                    {"server": extra, "reason": "server limit (%d) exceeded"
                     % MAX_SERVERS})
            names = names[:MAX_SERVERS]
        for name in names:
            entry = self.config[name]
            if self.scope is not None and not scope_allows_server(
                    self.scope, name):
                continue
            raw_tools, error = self._list_server(name, entry)
            if error is not None:
                self.skipped.append({"server": name, "reason": error})
                continue
            defs, catalog = self._aggregate_server(name, raw_tools, used)
            if catalog is not None:
                self.catalogs[name] = catalog
            if not defs and raw_tools:
                self.skipped.append(
                    {"server": name,
                     "reason": "no usable tools (schema or scope filtered)"})
                continue
            for definition in defs:
                self.tools.append(definition)
                self._by_name[definition["name"]] = definition
            if len(self.tools) > MAX_TOOLS:
                self.tools = self.tools[:MAX_TOOLS]
                self._by_name = {t["name"]: t for t in self.tools}
                self.skipped.append(
                    {"server": name, "reason": "tool limit (%d) exceeded"
                     % MAX_TOOLS})
                break
        if len(json.dumps(self.tools, ensure_ascii=False)) > CATALOG_BYTES:
            # Catalogue cap: keep the head, report the rest as skipped.
            kept, size = [], 0
            for tool in self.tools:
                item = len(json.dumps(tool, ensure_ascii=False))
                if size + item > CATALOG_BYTES:
                    self.skipped.append(
                        {"server": tool.get("server"),
                         "reason": "catalogue size limit (1 MB) exceeded"})
                    continue
                kept.append(tool)
                size += item
            self.tools = kept
            self._by_name = {t["name"]: t for t in kept}
        # query_result is only useful when there is something to
        # query: at least one real tool, or results already stored.
        if self.tools or len(self.store):
            self.tools.append(dict(QUERY_RESULT_TOOL))
            self._by_name[QUERY_RESULT_TOOL["name"]] = QUERY_RESULT_TOOL
        self._built = True
        return {"tools": [self._public(t) for t in self.tools],
                "skipped": self.skipped}

    @staticmethod
    def _public(definition):
        return {"name": definition["name"],
                "description": definition["description"],
                "inputSchema": definition["inputSchema"],
                "server": definition["server"],
                "directory": bool(definition.get("directory"))}

    # -- calling ---------------------------------------------------------
    def _resolve(self, tool_name):
        """Find (server, original, definition) for an exposed name.

        Uses the built map when available; otherwise walks the servers
        in config order, listing each until the name is found (same
        naming + collision order as build()).
        """
        if tool_name in self._by_name:
            definition = self._by_name[tool_name]
            return definition["server"], definition.get("original"), definition
        used = set()
        for name, entry in self.config.items():
            if not entry.get("enabled", True):
                continue
            if self.scope is not None and not scope_allows_server(
                    self.scope, name):
                continue
            raw_tools, error = self._list_server(name, entry)
            if error is not None:
                continue
            defs, catalog = self._aggregate_server(name, raw_tools, used)
            if catalog is not None:
                self.catalogs[name] = catalog
            for definition in defs:
                self._by_name[definition["name"]] = definition
                if definition["name"] == tool_name:
                    return name, definition.get("original"), definition
        return None, None, None

    def call(self, tool_name, arguments, timeout=DEFAULT_CALL_TIMEOUT_S):
        """Execute one exposed tool. Returns {ok, text, isError, handle}.

        Validation failures raise McpError (unknown tool, scope denial,
        bad arguments). Execution problems come back as
        ``ok: False`` results so the caller can decide what to do.
        """
        if not isinstance(arguments, dict):
            raise McpError("arguments must be an object")
        if tool_name == "query_result":
            return self._call_query_result(arguments)
        server, original, definition = self._resolve(tool_name)
        if definition is None:
            raise McpError("unknown tool %r" % tool_name)
        if definition.get("directory"):
            return self._call_directory(tool_name, arguments, timeout)
        if not scope_allows(self.scope, server, original):
            raise McpError("tool scope excludes %s" % tool_name)
        errors = validate_arguments(definition["inputSchema"], arguments)
        if errors:
            raise McpError("arguments do not match the tool schema: %s"
                           % "; ".join(errors))
        entry = self.config[server]
        result = self._call_upstream(entry, original, arguments, timeout)
        return self._finish(entry, result)

    def _call_directory(self, tool_name, arguments, timeout):
        if tool_name.endswith("_search_tools"):
            kind = "search_tools"
        elif tool_name.endswith("_describe_tool"):
            kind = "describe_tool"
        elif tool_name.endswith("_call_tool"):
            kind = "call_tool"
        else:
            raise McpError("not a directory tool: %r" % tool_name)
        sanitised = tool_name[:-(len(kind) + 1)]
        # The directory tool name carries the *sanitised* server name;
        # recover the real config name from it.
        server = self._unsanitize_server(sanitised)
        if server is None:
            raise McpError("unknown directory server for %r" % tool_name)
        if server not in self.catalogs:
            # Catalogue not built yet: list this server once.
            raw_tools, error = self._list_server(server, self.config[server])
            if error is not None:
                raise McpError("cannot list %s: %s" % (server, error))
            self.catalogs[server] = DirectoryCatalog(server, raw_tools)
        catalog = self.catalogs[server]
        schema_by_kind = {}
        for definition in directory_tool_definitions(server):
            for kind_name in ("search_tools", "describe_tool", "call_tool"):
                if definition["name"].endswith("_" + kind_name):
                    schema_by_kind[kind_name] = definition["inputSchema"]
        errors = validate_arguments(schema_by_kind[kind], arguments)
        if errors:
            raise McpError("arguments do not match the tool schema: %s"
                           % "; ".join(errors))
        if kind == "search_tools":
            found = catalog.search(arguments.get("query") or "",
                                   arguments.get("limit") or 8)
            return {"ok": True, "text": json.dumps(found, ensure_ascii=False),
                    "isError": False, "handle": None}
        if kind == "describe_tool":
            record = catalog.describe(arguments.get("name") or "")
            if record is None:
                return {"ok": False,
                        "text": "unknown tool %r on server %s"
                                % (arguments.get("name"), server),
                        "isError": True, "handle": None}
            return {"ok": True,
                    "text": json.dumps(record, ensure_ascii=False),
                    "isError": False, "handle": None}
        # call_tool: map to the real tool, enforce scope + schema, call.
        target, inner = arguments.get("name"), arguments.get("arguments") or {}
        if not isinstance(target, str) or not target:
            raise McpError("call_tool needs a tool name")
        if not scope_allows(self.scope, server, target):
            raise McpError("tool scope excludes %s on %s" % (target, server))
        record = catalog.describe(target)
        if record is None:
            return {"ok": False,
                    "text": "unknown tool %r on server %s" % (target, server),
                    "isError": True, "handle": None}
        errors = validate_arguments(record.get("inputSchema") or {}, inner)
        if errors:
            raise McpError("arguments do not match the tool schema: %s"
                           % "; ".join(errors))
        result = self._call_upstream(self.config[server], target, inner,
                                     timeout)
        return self._finish(self.config[server], result)

    def _unsanitize_server(self, sanitised):
        for name in self.config:
            if _sanitize(name) == sanitised:
                return name
        return None

    def _call_upstream(self, entry, tool_name, arguments, timeout):
        session = make_session(entry, self._session_factory)
        try:
            start_timeout = (STARTUP_REMOTE_S
                             if entry.get("type") in ("http", "sse")
                             else STARTUP_STDIO_S)
            session.start(timeout=start_timeout)
            return session.call_tool(tool_name, arguments, timeout=timeout)
        finally:
            try:
                session.close()
            except Exception:
                pass

    def _finish(self, entry, result):
        text = result.get("text") or ""
        budget = entry.get("resultBudget", DEFAULT_RESULT_BUDGET)
        handle = None
        if budget != 0 and len(text) > budget:
            handle = self.store.put(
                text, structured=result.get("structuredContent"))
            text = self.store.summarize(text, budget, handle)
        if len(text) > OUTPUT_BYTES:
            text = text[:OUTPUT_BYTES] + "\n[truncated to 50 KB]"
        return {"ok": not result.get("isError"), "text": text,
                "isError": bool(result.get("isError")), "handle": handle}

    def _call_query_result(self, arguments):
        handle = arguments.get("handle")
        expression = arguments.get("expression")
        if not isinstance(handle, str) or not isinstance(expression, str):
            raise McpError("query_result needs {handle, expression} strings")
        value, error = self.store.query(handle, expression)
        if error is not None:
            return {"ok": False, "text": error, "isError": True,
                    "handle": None}
        return {"ok": True,
                "text": json.dumps(value, ensure_ascii=False, default=str),
                "isError": False, "handle": None}


# ---------------------------------------------------------------------------
# Convenience entry points (used by the server endpoints)
# ---------------------------------------------------------------------------

def list_tools(config, scope=None, session_factory=None, spill_dir=None):
    """Aggregate tools for a config dict. Fail-soft: never raises."""
    try:
        registry = ToolRegistry(config, scope=scope,
                                session_factory=session_factory,
                                spill_dir=spill_dir)
        return registry.build()
    except Exception as e:  # fail-soft: chat must keep working
        return {"tools": [], "skipped": [{"server": "*",
                                          "reason": "aggregation failed: %s" % e}]}


def call_tool(config, tool_name, arguments, scope=None,
              session_factory=None, spill_dir=None,
              timeout=DEFAULT_CALL_TIMEOUT_S):
    """Execute one exposed tool. Raises McpError on validation failures."""
    registry = ToolRegistry(config, scope=scope,
                            session_factory=session_factory,
                            spill_dir=spill_dir)
    return registry.call(tool_name, arguments, timeout=timeout)


def probe_server(name, entry, session_factory=None, secrets=()):
    """Handshake + tools/list for one server.

    Returns ``{ok: True, tools: [{name, description}], total}`` or
    ``{ok: False, error}`` with secret values redacted from the error.
    """
    try:
        session = make_session(entry, session_factory)
        try:
            timeout = (STARTUP_REMOTE_S
                       if entry.get("type") in ("http", "sse")
                       else STARTUP_STDIO_S)
            session.start(timeout=timeout)
            raw_tools = session.list_tools()
        finally:
            try:
                session.close()
            except Exception:
                pass
    except McpError as e:
        return {"ok": False, "error": redact_text(str(e), secrets)}
    except Exception as e:  # never leak a traceback or secret to the UI
        return {"ok": False, "error": redact_text("probe failed: %s" % e,
                                                  secrets)}
    tools = [{"name": t.get("name"), "description": t.get("description") or ""}
             for t in raw_tools if isinstance(t, dict)
             and isinstance(t.get("name"), str)]
    return {"ok": True, "tools": tools, "total": len(tools)}
