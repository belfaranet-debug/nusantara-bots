"""Computer registry — the local record of the owner's machines.

Our answer to the official app's "Sand Machine": one persistent
computer per account, provisioned and recovered on vendor
infrastructure. Ours is the opposite: a small LOCAL record of the
machines the owner already has (this PC, the phone on the device
bridge), kept in a JSON file next to chat history. The registry
records; it does not provision, execute, or reach the network.
A machine here is a label + a status + capabilities + timestamps,
nothing more — the actual work on a machine still goes through the
existing device bridge (Phase 5), which is a separate concern.

Statuses: ``active`` (in use), ``paused`` (kept, not in use),
``removed`` (terminal — a removed record is history, never
resurrected, so a removal cannot be undone by accident).

Zero-dependency, Python standard library only. Persistence is the
same atomic-JSON-under-a-lock pattern as ``chat_store.py``.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPUTERS_FILE = _REPO_ROOT / "data" / "computers.json"

STATUSES = ("active", "paused", "removed")
OWNER = "owner"  # single-owner build: every machine belongs to the owner.

MAX_LABEL_CHARS = 128
MAX_NOTES_CHARS = 1024
MAX_CAPABILITIES = 16
MAX_CAPABILITY_CHARS = 64


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_label(label):
    if not isinstance(label, str) or not label.strip():
        raise ValueError("label must be a non-empty string")
    if len(label) > MAX_LABEL_CHARS:
        raise ValueError("label is %d chars, limit is %d"
                         % (len(label), MAX_LABEL_CHARS))
    return label.strip()


def _validate_capabilities(capabilities):
    if capabilities is None:
        return []
    if not isinstance(capabilities, list):
        raise ValueError("capabilities must be a list of strings")
    if len(capabilities) > MAX_CAPABILITIES:
        raise ValueError("too many capabilities: %d, limit is %d"
                         % (len(capabilities), MAX_CAPABILITIES))
    cleaned = []
    for item in capabilities:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("each capability must be a non-empty string")
        if len(item) > MAX_CAPABILITY_CHARS:
            raise ValueError("capability %r is too long" % item)
        cleaned.append(item.strip())
    return cleaned


def _validate_notes(notes):
    if notes is None:
        return ""
    if not isinstance(notes, str):
        raise ValueError("notes must be a string")
    if len(notes) > MAX_NOTES_CHARS:
        raise ValueError("notes is %d chars, limit is %d"
                         % (len(notes), MAX_NOTES_CHARS))
    return notes


class ComputerRegistry:
    """Persistent registry of the owner's machines (JSON file)."""

    def __init__(self, path=None):
        self.path = Path(path or os.environ.get(
            "GROKBOT_COMPUTERS_FILE", str(DEFAULT_COMPUTERS_FILE)))
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        if not self.path.is_file():
            return {"machines": [], "seq": 0}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(
                    data.get("machines"), list) and isinstance(
                    data.get("seq"), int):
                return data
        except (OSError, ValueError):
            pass
        return {"machines": [], "seq": 0}

    def _save(self):
        tmp = str(self.path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def register(self, label, capabilities=None, notes=""):
        """Add a machine. Returns the record (a copy)."""
        label = _validate_label(label)
        capabilities = _validate_capabilities(capabilities)
        notes = _validate_notes(notes)
        with self._lock:
            self._data["seq"] += 1
            now = _utcnow()
            record = {
                "id": "local-%d" % self._data["seq"],
                "label": label,
                "owner": OWNER,
                "status": "active",
                "capabilities": capabilities,
                "notes": notes,
                "created_at": now,
                "last_seen": now,
            }
            self._data["machines"].append(record)
            self._save()
            return dict(record)

    def list_machines(self):
        """All records, in registration order (copies)."""
        with self._lock:
            return [dict(m) for m in self._data["machines"]]

    def get(self, machine_id):
        """One record by id, or None."""
        with self._lock:
            for m in self._data["machines"]:
                if m["id"] == machine_id:
                    return dict(m)
        return None

    def set_status(self, machine_id, status):
        """Move a machine between active/paused/removed.

        ``removed`` is terminal: a removed machine cannot change
        status again. Raises KeyError for an unknown id and
        ValueError for a bad status or a terminal transition.
        """
        if status not in STATUSES:
            raise ValueError("unknown status %r (want one of %s)"
                             % (status, ", ".join(STATUSES)))
        with self._lock:
            for m in self._data["machines"]:
                if m["id"] == machine_id:
                    if m["status"] == "removed":
                        raise ValueError(
                            "machine %s is removed (terminal)"
                            % machine_id)
                    m["status"] = status
                    m["last_seen"] = _utcnow()
                    self._save()
                    return dict(m)
        raise KeyError("unknown machine %r" % machine_id)

    def touch(self, machine_id):
        """Refresh a machine's last_seen (heartbeat). Returns the
        record. Raises KeyError for an unknown id and ValueError
        for a removed machine."""
        with self._lock:
            for m in self._data["machines"]:
                if m["id"] == machine_id:
                    if m["status"] == "removed":
                        raise ValueError(
                            "machine %s is removed (terminal)"
                            % machine_id)
                    m["last_seen"] = _utcnow()
                    self._save()
                    return dict(m)
        raise KeyError("unknown machine %r" % machine_id)
