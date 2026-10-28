"""Device client for the app shell (DEVICES.md slices 2-4).

Character-centered by design: this module is the ONLY path by which
the app shell touches a device, and every action it performs is
attributed to a character (name + role) before it is reported
anywhere. The bridge's own presence log (direct CLI use, no
character) is kept separate and labelled as unattributed.

Transport: subprocess calls to the PC-side bridge CLI ONLY. This
module imports no HTTP client and makes no outbound request, so the
Phase 3 BYO-API invariant (the server never sees a credential, and
cannot forward one) is preserved: the only bytes that ever travel
are screenshots and action results, fetched fresh per request and
never stored as a log.

Shot discipline: `shot()` writes the bridge's PNG to a temporary
file, reads the bytes, and deletes the file before returning. No
screenshot is ever written into the repo, the chat file, or memory.
Text discipline: text-action details are recorded as a length class
only, never the text itself (same rule as the bridge).
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_BRIDGE_CANDIDATES = (
    os.environ.get("GROKBOT_BRIDGE_PY"),
    str(_REPO_ROOT / "devices" / "bridge.py"),
    "/data/grokbot-own/devices/bridge.py",
)


class BridgeError(Exception):
    """Bridge unavailable, or a bridge call failed."""


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _length_class(text):
    if len(text) <= 10:
        return "short"
    if len(text) <= 100:
        return "medium"
    return "long"


class BridgeClient:
    """Subprocess transport to the PC-side bridge CLI."""

    def __init__(self, bridge_py=None):
        self.bridge_py = bridge_py
        if not self.bridge_py:
            for cand in _DEFAULT_BRIDGE_CANDIDATES:
                if cand and os.path.isfile(cand):
                    self.bridge_py = cand
                    break

    def available(self):
        return bool(self.bridge_py and os.path.isfile(self.bridge_py))

    def _run_json(self, args):
        if not self.available():
            raise BridgeError("bridge CLI not found")
        proc = subprocess.run(
            [sys.executable, self.bridge_py] + args,
            capture_output=True, text=True, timeout=90)
        try:
            return json.loads(proc.stdout or "{}")
        except ValueError:
            raise BridgeError("bridge returned non-JSON output")

    def status(self):
        return self._run_json(["status"])

    def devices(self):
        return self._run_json(["devices"])

    def shot(self, kind):
        """Fresh screenshot bytes. Temp file deleted before return."""
        if kind not in ("phone", "display"):
            raise BridgeError("unknown shot kind: %r" % kind)
        if not self.available():
            raise BridgeError("bridge CLI not found")
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        # scrot refuses to overwrite an existing file (it writes to a
        # _000 suffixed sibling instead, leaving the requested path
        # empty), so remove the pre-created temp file first; the
        # bridge creates it fresh.
        os.unlink(tmp)
        try:
            res = self._run_json(["shot", kind, tmp])
            if not res.get("ok"):
                raise BridgeError(res.get("error", "shot failed"))
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def shot_meta(self, kind):
        """Fresh shot metadata only (bridge's own byte count). The
        temp file is deleted without its bytes ever being read."""
        if kind not in ("phone", "display"):
            raise BridgeError("unknown shot kind: %r" % kind)
        if not self.available():
            raise BridgeError("bridge CLI not found")
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        # Same scrot rule as shot(): the pre-created file must go
        # first, or the bridge writes a _000 sibling instead.
        os.unlink(tmp)
        try:
            res = self._run_json(["shot", kind, tmp])
            return {"ok": bool(res.get("ok")),
                    "bytes": res.get("bytes", 0),
                    "error": res.get("error"),
                    "ts": _utcnow()}
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def presence(self, n=50):
        return self._run_json(["presence", str(n)]).get("events", [])

    def phone_input(self, action, params):
        """Route one validated phone action to the bridge CLI."""
        if action == "tap":
            args = ["input", "tap", str(params["x"]), str(params["y"])]
        elif action == "swipe":
            args = ["input", "swipe", str(params["x1"]), str(params["y1"]),
                    str(params["x2"]), str(params["y2"]),
                    str(params.get("ms", 300))]
        elif action == "keyevent":
            args = ["input", "keyevent", params["key"]]
        elif action == "text":
            args = ["input", "text", params["text"]]
        elif action == "launch":
            args = ["input", "launch", params["component"]]
        else:
            raise BridgeError("unknown phone action: %r" % action)
        return self._run_json(args)

    def browser_action(self, action, params):
        """Route one validated browser action to the bridge CLI."""
        if action == "key":
            args = ["browser", "key", params["key"]]
        elif action == "click":
            args = ["browser", "click", str(params["x"]), str(params["y"])]
        elif action == "mousemove":
            args = ["browser", "mousemove", str(params["x"]),
                    str(params["y"])]
        elif action == "type":
            args = ["browser", "type", params["text"]]
        else:
            raise BridgeError("unknown browser action: %r" % action)
        return self._run_json(args)


# ---- character attribution (the character-centered contract) ----

def character_id(character):
    return Path(character.path).stem


def attribution(character, device, action, detail, ok, error=None,
                bridge_event=None):
    """One attributed device action. Text is never recorded: a text
    detail is reduced to a length class before this record exists."""
    if action in ("text", "type") and isinstance(detail, dict) \
            and "text" in detail:
        detail = {"length_class": _length_class(detail["text"])}
    return {
        "ts": _utcnow(),
        "character_id": character_id(character),
        "character_name": character.name,
        "character_role": character.role,
        "device": device,
        "action": action,
        "detail": detail,
        "ok": ok,
        "error": error,
        "bridge_event": bridge_event,
    }


def vision_turn(character, room, shots, model_path):
    """One character's turn with fresh screenshots attached. The
    record holds shot METADATA only (ok/bytes/timestamp) — image
    bytes are served transiently to the browser and never stored
    here, in chat, or in memory."""
    return {
        "ts": _utcnow(),
        "character_id": character_id(character),
        "character_name": character.name,
        "character_role": character.role,
        "room": room,
        "shots": shots,
        "model_path": model_path,
    }


def merge_feed(attributions, bridge_events):
    """Devices feed: fully-attributed app actions first, then the
    bridge's raw log, explicitly labelled unattributed (direct CLI
    use, no character). Nothing unattributed is ever presented as
    character activity."""
    attributed = sorted(attributions, key=lambda e: e["ts"], reverse=True)
    raw = sorted(bridge_events, key=lambda e: e.get("ts", ""),
                 reverse=True)
    for e in raw:
        e.setdefault("character_name", None)
        e["attributed"] = False
    for e in attributed:
        e["attributed"] = True
    return {"attributed": attributed, "bridge_raw": raw}
