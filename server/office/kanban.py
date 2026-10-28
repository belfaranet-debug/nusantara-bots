"""Kanban board + evidence model for the own build (office piece).

Adapted in SHAPE from hermes-virtual-office (MIT) — see office/SOURCE.md.
No code was copied: this is a fresh Python implementation over a local
JSON file, following the same atomic-write-under-lock pattern as
app/chat_store.py and computers/registry.py.

Model
-----
* Card statuses: todo | doing | review | blocked | done.
  Display columns: backlog(todo), doing(doing), review(review, blocked),
  done(done).
* Every card records its ORIGIN (manual / meeting / agent / mail + ref):
  the board is one shared surface, so a card must say where it came from.
* Evidence is attached to a card, never implied by the status:
  kinds summary | log | link | file.
* Verdict (pure function, same rule as the upstream evidence model):
  a card that is not done is "open"; a done card WITH evidence is
  "proven"; a done card WITHOUT evidence is "unproven". A claim of
  "done" on its own is never proof.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_KANBAN_FILE = os.path.join(_REPO_ROOT, "data", "kanban.json")

KANBAN_STATUSES = ("todo", "doing", "review", "blocked", "done")
BOARD_COLUMNS = {
    "backlog": ("todo",),
    "doing": ("doing",),
    "review": ("review", "blocked"),
    "done": ("done",),
}
EVIDENCE_KINDS = ("summary", "log", "link", "file")
ORIGIN_KINDS = ("manual", "meeting", "agent", "mail")
PRIORITIES = (1, 2, 3)

MAX_TITLE_CHARS = 256
MAX_BODY_CHARS = 4096
MAX_LABEL_CHARS = 256
MAX_REF_CHARS = 256
MAX_BY_CHARS = 128


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def verdict(card: dict) -> str:
    """Evidence verdict for one card dict: open | proven | unproven."""
    if not isinstance(card, dict):
        return "open"
    if card.get("status") != "done":
        return "open"
    evidence = card.get("evidence")
    if isinstance(evidence, list) and evidence:
        return "proven"
    return "unproven"


def _validate_text(value, name: str, max_chars: int, required: bool = True):
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


def _validate_origin(origin) -> dict:
    if origin is None:
        return {"kind": "manual"}
    if not isinstance(origin, dict):
        raise ValueError("origin must be an object")
    kind = origin.get("kind")
    if kind not in ORIGIN_KINDS:
        raise ValueError("origin.kind must be one of %s" % (list(ORIGIN_KINDS),))
    ref = origin.get("ref")
    if ref is not None:
        _validate_text(ref, "origin.ref", MAX_REF_CHARS)
    result = {"kind": kind}
    if ref is not None:
        result["ref"] = ref
    return result


class KanbanStore:
    """Cards + evidence, persisted as one JSON file. Thread-safe."""

    def __init__(self, path=None):
        self.path = path or os.environ.get(
            "GROKBOT_KANBAN_FILE", DEFAULT_KANBAN_FILE)
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {"cards": [], "next_id": 1}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (isinstance(data, dict)
                    and isinstance(data.get("cards"), list)
                    and isinstance(data.get("next_id"), int)):
                return data
        except (OSError, ValueError):
            pass
        return {"cards": [], "next_id": 1}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def create_card(self, title, body="", assignee=None, priority=2,
                    origin=None) -> dict:
        """Create a card in 'todo'. Raises ValueError on bad input."""
        title = _validate_text(title, "title", MAX_TITLE_CHARS)
        body = _validate_text(body, "body", MAX_BODY_CHARS, required=False) or ""
        if assignee is not None:
            assignee = _validate_text(assignee, "assignee", MAX_TITLE_CHARS)
        if priority not in PRIORITIES:
            raise ValueError("priority must be one of %s" % (list(PRIORITIES),))
        origin = _validate_origin(origin)
        with self._lock:
            card_id = "card-%d" % self._data["next_id"]
            self._data["next_id"] += 1
            now = _utcnow()
            card = {
                "id": card_id,
                "title": title,
                "body": body,
                "assignee": assignee,
                "status": "todo",
                "priority": priority,
                "origin": origin,
                "evidence": [],
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            self._data["cards"].append(card)
            self._save()
            return copy.deepcopy(card)

    def list_cards(self, status=None, assignee=None) -> list:
        with self._lock:
            cards = self._data["cards"]
            if status is not None:
                cards = [c for c in cards if c["status"] == status]
            if assignee is not None:
                cards = [c for c in cards if c["assignee"] == assignee]
            return copy.deepcopy(cards)

    def get_card(self, card_id) -> dict | None:
        with self._lock:
            for card in self._data["cards"]:
                if card["id"] == card_id:
                    return copy.deepcopy(card)
        return None

    def move_card(self, card_id, status) -> dict | None:
        """Set a card's status. Returns the updated card, or None if missing.

        completed_at is set when entering 'done' and cleared when leaving it.
        Raises ValueError on an unknown status.
        """
        if status not in KANBAN_STATUSES:
            raise ValueError("status must be one of %s" % (list(KANBAN_STATUSES),))
        with self._lock:
            for card in self._data["cards"]:
                if card["id"] == card_id:
                    old = card["status"]
                    card["status"] = status
                    card["updated_at"] = _utcnow()
                    if status == "done" and old != "done":
                        card["completed_at"] = card["updated_at"]
                    elif status != "done":
                        card["completed_at"] = None
                    self._save()
                    return copy.deepcopy(card)
        return None

    def add_evidence(self, card_id, kind, label, body="", by="") -> dict | None:
        """Attach one evidence item to a card. Returns the card, or None."""
        if kind not in EVIDENCE_KINDS:
            raise ValueError("evidence kind must be one of %s"
                             % (list(EVIDENCE_KINDS),))
        label = _validate_text(label, "evidence label", MAX_LABEL_CHARS)
        body = _validate_text(body, "evidence body", MAX_BODY_CHARS,
                              required=False) or ""
        by = _validate_text(by, "evidence by", MAX_BY_CHARS, required=False) or ""
        with self._lock:
            for card in self._data["cards"]:
                if card["id"] == card_id:
                    card["evidence"].append({
                        "kind": kind,
                        "label": label,
                        "body": body,
                        "by": by,
                        "ts": _utcnow(),
                    })
                    card["updated_at"] = _utcnow()
                    self._save()
                    return copy.deepcopy(card)
        return None

    def verdict_for(self, card_id) -> str | None:
        """Evidence verdict for a card id, or None if the card is missing."""
        card = self.get_card(card_id)
        if card is None:
            return None
        return verdict(card)

    def board(self) -> dict:
        """Cards grouped by display column, with counts + unproven-done.

        Each card carries its evidence verdict (open/proven/unproven)
        so the UI renders the server's verdict, never its own copy
        of the rule.
        """
        with self._lock:
            cards = copy.deepcopy(self._data["cards"])
        for card in cards:
            card["verdict"] = verdict(card)
        columns = {}
        counts = {}
        for column, statuses in BOARD_COLUMNS.items():
            members = [c for c in cards if c["status"] in statuses]
            # Highest priority first, oldest first within a priority.
            members.sort(key=lambda c: (-c["priority"], c["created_at"]))
            columns[column] = members
            counts[column] = len(members)
        unproven_done = sum(
            1 for c in cards
            if c["status"] == "done" and verdict(c) == "unproven")
        return {"columns": columns, "counts": counts,
                "unproven_done": unproven_done}
