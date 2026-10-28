"""Receive-only mail client for the own build (v1).

Talks to the bot-mail MCP bridge over streamable HTTP (JSON-RPC
over POST) using only the standard library. The bridge holds one
receive-only inbox per bot, keyed by a label; the label for a
character is exactly its character file stem (for example
``chief-officer``). The template character file (``example-``)
is never a mail identity — callers exclude it.

What this client returns, and what it never does
------------------------------------------------
* ``summary(label)`` never raises: every failure becomes
  ``available=False`` plus a short, sanitized error string, so
  the deck can report honestly instead of failing the page.
* Only counts leave this module for inbox state (total + unread
  markers); message bodies and subjects are never fetched here.
* The address itself is returned only as a value for the
  authenticated deck API to display; it is never logged. Error
  strings are scrubbed of anything address-shaped before use.
* There is NO send path in v1, by design: this module exposes
  no send method at all, and the server exposes no send route.
  Drafts are a server-side in-memory store only (see server.py).

Phase 2 adds ONE send path, approval-gated on the server
(``POST /api/mail/send`` — see ``docs/mail-send-bridge-spec.md``):

* ``send_via_bridge(label, to, subject, body)`` calls the
  bridge's ``mail_send`` tool (``tools/call``). The bridge does
  NOT have that tool yet: the patch to add it lives in
  ``docs/mail-send-bridge-patch.md`` and must be installed on the
  PC (``/data/mail-mcp``) first. Until then, calling this method
  returns the clean error "bridge belum punya tool kirim — pasang
  patch dulu". There is NO fallback to any other transport and
  no silent send.
* ``dry_run=True`` (tests only): records what would be sent and
  never touches the network. This is a test mode, never a
  production send path.

Protocol shapes (from the bridge's own reference server)
-------------------------------------------------------
* ``initialize`` with ``protocolVersion`` / ``capabilities`` /
  ``clientInfo``; the reply is checked only for a JSON-RPC
  ``result``.
* ``tools/call`` with ``{"name": ..., "arguments": {...}}``; the
  reply's ``result.content[0].text`` carries the tool output, or
  ``result.isError`` marks a tool-level failure.
* ``mail_my_address`` text is either "No inbox for label ..."
  (no inbox yet) or "Your email address is: <addr> (...)".
* ``mail_inbox`` text is either "... is empty ..." (zero
  messages) or "Inbox <addr> — N message(s), newest first:"
  followed by one line per message, each starting with
  ``[read]`` or ``[NEW]``.
"""

import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "http://YOUR_SERVER:20315/mcp"
DEFAULT_TIMEOUT = 10

# The template character file stem: never a mail identity.
TEMPLATE_STEM = "example-character"

_EMAIL_LIKE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ADDRESS_RE = re.compile(r"email address is:\s*(\S+)", re.IGNORECASE)
_COUNT_RE = re.compile(r"(\d+)\s+message\(s\)", re.IGNORECASE)


class MailError(Exception):
    """The mail bridge could not be reached or answered badly."""


def _scrub(text):
    """Short, log-safe error text: no address-shaped values."""
    clean = _EMAIL_LIKE.sub("[address]", str(text or ""))
    clean = " ".join(clean.split())
    return clean[:200] if clean else "mail bridge error"


def label_for(stem):
    """Mail label for a character: exactly its file stem."""
    return str(stem or "").strip().lower()


class MailClient:
    """JSON-RPC client for the receive-only mail bridge.

    ``endpoint`` is injectable (tests pass a fake client instead
    of this class; a real deployment may point it at another
    bridge URL via the GROKBOT_MAIL_ENDPOINT env var).
    ``dry_run`` is a TEST-ONLY mode: send_via_bridge records what
    would be sent and never touches the network. It is never a
    production send path.
    """

    def __init__(self, endpoint=None, timeout=DEFAULT_TIMEOUT,
                 dry_run=False):
        self.endpoint = endpoint or os.environ.get(
            "GROKBOT_MAIL_ENDPOINT", DEFAULT_ENDPOINT)
        self.timeout = timeout
        self.dry_run = bool(dry_run)
        self._next_id = 1

    # -- transport -------------------------------------------------
    def _post(self, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise MailError("mail bridge HTTP %s" % e.code)
        except urllib.error.URLError as e:
            raise MailError("mail bridge unreachable: %s"
                            % _scrub(getattr(e, "reason", e)))
        except (TimeoutError, OSError) as e:
            raise MailError("mail bridge unreachable: %s" % _scrub(e))
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise MailError("mail bridge returned non-JSON output")

    def _rpc(self, method, params=None):
        msg_id = self._next_id
        self._next_id += 1
        reply = self._post({"jsonrpc": "2.0", "id": msg_id,
                            "method": method, "params": params or {}})
        if not isinstance(reply, dict):
            raise MailError("mail bridge returned a bad reply")
        if reply.get("error"):
            raise MailError("mail bridge error: %s"
                            % _scrub(reply["error"].get("message", "")))
        if "result" not in reply:
            raise MailError("mail bridge returned no result")
        return reply["result"]

    def _call_tool(self, name, arguments):
        # A handshake first, then the tool call — the bridge
        # answers each JSON-RPC message independently.
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "grokbot-own-mail", "version": "1.0"},
        })
        result = self._rpc("tools/call",
                           {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise MailError("mail bridge returned a bad tool result")
        if result.get("isError"):
            content = result.get("content") or []
            text = content[0].get("text", "") if content else ""
            raise MailError(_scrub(text) or "mail tool failed")
        content = result.get("content") or []
        if not content or "text" not in content[0]:
            raise MailError("mail bridge returned no tool text")
        return content[0]["text"]

    # -- tool wrappers (raise MailError on failure) ----------------
    def send_via_bridge(self, label, to, subject, body):
        """Send one message through the bridge's ``mail_send`` tool.

        The server (POST /api/mail/send) already checked the
        approval before calling this; this method only dispatches.
        Returns {"provider": "bridge", "sent": True, "to", "subject",
        "label", "bridge_reply"} on success.

        The bridge does NOT have the ``mail_send`` tool yet — the
        patch lives in docs/mail-send-bridge-patch.md and must be
        installed on the PC first. When the bridge answers that the
        tool is unknown, this raises the clean MailError "bridge
        belum punya tool kirim — pasang patch dulu", with NO
        fallback to any other transport and no silent send.

        With dry_run=True (tests only) nothing is sent and no
        network is touched: the return is {"provider": "dry_run",
        "sent": False, "would_send": {...}}.
        """
        label = label_for(label)
        if not label:
            raise MailError("need a mail character (label)")
        if not isinstance(to, str) or not to.strip():
            raise MailError("need a non-empty recipient address")
        if not isinstance(subject, str) or not subject.strip():
            raise MailError("need a non-empty subject")
        if not isinstance(body, str):
            raise MailError("body must be a string")
        to = to.strip()
        subject = subject.strip()
        if self.dry_run:
            return {"provider": "dry_run", "sent": False,
                    "would_send": {"to": to, "subject": subject,
                                   "label": label},
                    "note": "dry_run: no mail was sent; this is a "
                            "record of what would be sent"}
        try:
            text = self._call_tool(
                "mail_send",
                {"label": label, "to": to, "subject": subject,
                 "body": body})
        except MailError as e:
            msg = str(e).lower()
            if "unknown tool" in msg or "mail_send" in msg:
                raise MailError("bridge belum punya tool kirim — "
                                "pasang patch dulu")
            raise
        return {"provider": "bridge", "sent": True, "to": to,
                "subject": subject, "label": label,
                "bridge_reply": text}

    def my_address(self, label):
        """Return (address_present, address|None) for a label."""
        text = self._call_tool("mail_my_address", {"label": label})
        if "No inbox for label" in text:
            return False, None
        match = _ADDRESS_RE.search(text)
        if not match:
            raise MailError("mail bridge address reply unparseable")
        return True, match.group(1)

    def inbox_counts(self, label):
        """Return (total, unread) message counts for a label."""
        text = self._call_tool("mail_inbox", {"label": label, "limit": 25})
        if "is empty" in text:
            return 0, 0
        match = _COUNT_RE.search(text)
        if not match:
            raise MailError("mail bridge inbox reply unparseable")
        total = int(match.group(1))
        # Bridge lines look like "1. [NEW] <date> | from: ...".
        unread = sum(1 for line in text.splitlines()
                     if "[NEW]" in line)
        return total, unread

    # -- deck-facing summary (never raises) ------------------------
    def summary(self, label):
        """One character's mail state, graceful on any failure.

        Returns {label, address, address_present, inbox_count,
        unread, available, error}. ``available`` is False only
        when the bridge itself could not answer; a bridge that
        answers "no inbox yet" is available with
        address_present False.
        """
        label = label_for(label)
        base = {"label": label, "address": None,
                "address_present": False, "inbox_count": 0,
                "unread": 0, "available": False, "error": None}
        try:
            present, address = self.my_address(label)
        except MailError as e:
            base["error"] = _scrub(e)
            return base
        except Exception as e:  # never raise into the deck
            base["error"] = _scrub(e)
            return base
        base["available"] = True
        if not present:
            base["error"] = "no inbox yet"
            return base
        base["address_present"] = True
        base["address"] = address
        try:
            total, unread = self.inbox_counts(label)
        except MailError as e:
            # Address known, inbox unreadable: report honestly
            # but keep the bridge marked available.
            base["error"] = _scrub(e)
            return base
        except Exception as e:
            base["error"] = _scrub(e)
            return base
        base["inbox_count"] = total
        base["unread"] = unread
        return base
