"""FCM token registry — one token per character / device.

Store: JSON file ``data/push_tokens.json`` (env override
``GROKBOT_PUSH_TOKENS_FILE``), atomic writes under a lock — the
same pattern as ``office/kanban.py`` and ``computers/registry.py``.
A corrupt file loads as empty.

A record is::

    {"token": "<FCM registration token>",
     "character_id": "chief-officer",
     "device_label": "Pixel 7 Pro",
     "platform": "web",
     "created_at": "2026-10-25T06:00:00+00:00",
     "last_seen": "2026-10-25T06:00:00+00:00"}

The token value itself is stored (the sender needs it). This
file is server-side only: the ``GET /api/push/tokens`` endpoint
(see ``push/server-wiring.patch.md``) must return metadata
WITHOUT token values — the deck never needs to see a token.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TOKENS_FILE = os.path.join(_REPO_ROOT, "data", "push_tokens.json")

PLATFORMS = ("web", "android", "ios", "other")

MAX_TOKEN_CHARS = 4096
MIN_TOKEN_CHARS = 10
MAX_CHARACTER_CHARS = 128
MAX_LABEL_CHARS = 128


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_text(value, name, max_chars, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % name)
    if required and not value.strip():
        raise ValueError("%s must not be empty" % name)
    if len(value) > max_chars:
        raise ValueError("%s is %d chars, limit is %d"
                         % (name, len(value), max_chars))
    return value


class PushTokenRegistry:
    """FCM registration tokens, persisted as one JSON file."""

    def __init__(self, path=None):
        self.path = path or os.environ.get(
            "GROKBOT_PUSH_TOKENS_FILE", DEFAULT_TOKENS_FILE)
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {"tokens": []}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("tokens"), list):
                return data
        except (OSError, ValueError):
            pass
        return {"tokens": []}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def register(self, token, character_id, device_label="",
                 platform="web") -> dict:
        """Add a token, or refresh an existing one. Returns the record.

        Registering the same token again (same device re-opening
        the deck) updates ``last_seen`` instead of duplicating it.
        Raises ValueError on a bad token, character, or platform.
        """
        token = _validate_text(token, "token", MAX_TOKEN_CHARS)
        if len(token) < MIN_TOKEN_CHARS:
            raise ValueError("token is too short to be a registration token")
        character_id = _validate_text(character_id, "character_id",
                                      MAX_CHARACTER_CHARS)
        device_label = _validate_text(device_label, "device_label",
                                      MAX_LABEL_CHARS, required=False) or ""
        if platform not in PLATFORMS:
            raise ValueError("platform must be one of %s" % (list(PLATFORMS),))
        now = _utcnow()
        with self._lock:
            for rec in self._data["tokens"]:
                if rec.get("token") == token:
                    rec["character_id"] = character_id
                    rec["device_label"] = device_label
                    rec["platform"] = platform
                    rec["last_seen"] = now
                    self._save()
                    return copy.deepcopy(rec)
            record = {
                "token": token,
                "character_id": character_id,
                "device_label": device_label,
                "platform": platform,
                "created_at": now,
                "last_seen": now,
            }
            self._data["tokens"].append(record)
            self._save()
            return copy.deepcopy(record)

    def unregister(self, token) -> bool:
        """Remove a token. Returns True if one was removed."""
        with self._lock:
            before = len(self._data["tokens"])
            self._data["tokens"] = [
                r for r in self._data["tokens"] if r.get("token") != token]
            if len(self._data["tokens"]) == before:
                return False
            self._save()
            return True

    def list_tokens(self, character_id=None) -> list:
        """All records (optionally for one character), oldest first."""
        with self._lock:
            records = self._data["tokens"]
            if character_id is not None:
                records = [r for r in records
                           if r.get("character_id") == character_id]
            return copy.deepcopy(records)

    def public_list(self, character_id=None) -> list:
        """Metadata only — token values stripped. For the deck API."""
        return [{k: v for k, v in rec.items() if k != "token"}
                for rec in self.list_tokens(character_id)]

    def token_values(self, character_id=None) -> list:
        """Just the token strings (what the sender needs)."""
        return [r["token"] for r in self.list_tokens(character_id)]
