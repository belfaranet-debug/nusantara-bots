"""Presence beacons (office piece).

A character says where it is by sending a beacon: {character_id, state,
note}. The server keeps the latest beacon per character plus a short
history, and reports last-seen + age for each character.

This is a CLAIM by the character about itself, not a server-side status:
the deck shows it as-is, with its age, so a stale beacon reads as stale
instead of looking live. Anything older than STALE_AFTER_SECONDS counts
as stale — the same 120-second window the duty machine uses for a live
chat turn.

States: active | idle | stopped.
"""

from __future__ import annotations

import copy
import threading
import time
from datetime import datetime, timezone

BEACON_STATES = ("active", "idle", "stopped")

#: A beacon older than this is stale (same window as the duty live-chat).
STALE_AFTER_SECONDS = 120

MAX_NOTE_CHARS = 256
MAX_HISTORY = 50


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


class BeaconLog:
    """Latest beacon per character + capped history. Thread-safe, in-memory."""

    def __init__(self, max_history: int = MAX_HISTORY):
        self._lock = threading.Lock()
        self._latest: dict = {}
        self._history: list = []
        self._max_history = max_history

    def record(self, character_id, state, note="", ts=None) -> dict:
        """Record one beacon. Raises ValueError on bad input.

        `ts` is an epoch-seconds float for tests; when omitted, now is used.
        """
        if not isinstance(character_id, str) or not character_id.strip():
            raise ValueError("character_id must be a non-empty string")
        if state not in BEACON_STATES:
            raise ValueError("state must be one of %s" % (list(BEACON_STATES),))
        if not isinstance(note, str):
            raise ValueError("note must be a string")
        if len(note) > MAX_NOTE_CHARS:
            raise ValueError("note is %d chars, limit is %d"
                             % (len(note), MAX_NOTE_CHARS))
        if ts is None:
            ts = time.time()
        entry = {"character_id": character_id, "state": state,
                 "note": note, "ts": float(ts)}
        with self._lock:
            self._latest[character_id] = copy.deepcopy(entry)
            self._history.append(copy.deepcopy(entry))
            del self._history[:max(0, len(self._history) - self._max_history)]
            return copy.deepcopy(entry)

    def last_seen(self, character_id) -> dict | None:
        with self._lock:
            entry = self._latest.get(character_id)
            return copy.deepcopy(entry) if entry else None

    def age_seconds(self, character_id, now=None) -> float | None:
        """Age of the latest beacon in seconds, or None when never seen."""
        entry = self.last_seen(character_id)
        if entry is None:
            return None
        if now is None:
            now = time.time()
        return max(0.0, float(now) - entry["ts"])

    def is_stale(self, character_id, now=None) -> bool:
        """True when the character has no beacon or its beacon is stale."""
        age = self.age_seconds(character_id, now=now)
        if age is None:
            return True
        return age > STALE_AFTER_SECONDS

    def history(self, limit: int = MAX_HISTORY) -> list:
        with self._lock:
            return copy.deepcopy(self._history[-limit:])

    def snapshot(self, now=None) -> dict:
        """{characters: {id: {state, note, last_seen, age_seconds}}, ...}."""
        if now is None:
            now = time.time()
        with self._lock:
            characters = {}
            for character_id, entry in self._latest.items():
                characters[character_id] = {
                    "state": entry["state"],
                    "note": entry["note"],
                    "last_seen": _iso(entry["ts"]),
                    "age_seconds": round(max(0.0, now - entry["ts"]), 1),
                }
        return {"characters": characters,
                "stale_after_seconds": STALE_AFTER_SECONDS}
