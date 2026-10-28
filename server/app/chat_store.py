"""Chat persistence for the Grok Bot own-build app shell (Phase 3).

Chat history lives in its OWN JSON file (default ``data/chat.json``)
and NEVER in ``data/memory.db``. Memory stays in Phase 1's SQLite
store; chat is a separate concern with a separate file, so a chat
export can never be confused with the memory database.

Zero-dependency, Python standard library only. Writes are atomic
(temp file + replace) under a lock, so the threaded server cannot
interleave two saves.
"""

import json
import os
import threading
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CHAT_FILE = os.path.join(_REPO_ROOT, "data", "chat.json")

MEETING_BUDGET = 40


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChatStore:
    """Rooms + messages + meeting decisions, persisted as JSON."""

    def __init__(self, path=None):
        self.path = path or os.environ.get("GROKBOT_CHAT_FILE", DEFAULT_CHAT_FILE)
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return {"rooms": {}, "messages": []}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "rooms" in data and "messages" in data:
                # 0.10.9: normalize types — a malformed file (e.g.
                # "messages": {} written by hand) used to load
                # fine and then crash add_message() with
                # AttributeError: 'dict' object has no attribute
                # 'append', silently dropping every persist.
                if not isinstance(data["rooms"], dict):
                    data["rooms"] = {}
                if not isinstance(data["messages"], list):
                    data["messages"] = []
                return data
        except (OSError, ValueError):
            pass
        return {"rooms": {}, "messages": []}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def ensure_default_rooms(self, character_ids):
        """Create the two starter rooms on first run; never overwrite."""
        with self._lock:
            if not self._data["rooms"]:
                self._data["rooms"] = {
                    "general": {
                        "id": "general", "name": "General chat",
                        "mode": "chat", "members": list(character_ids),
                        "decisions": [], "disputed": [], "stopped": False,
                    },
                    "meeting-room": {
                        "id": "meeting-room", "name": "Meeting room",
                        "mode": "meeting", "members": list(character_ids),
                        "decisions": [], "disputed": [], "stopped": False,
                    },
                }
                self._save()
            return self._data["rooms"]

    def rooms(self):
        with self._lock:
            rooms = []
            for room in self._data["rooms"].values():
                used = sum(1 for m in self._data["messages"]
                           if m["room"] == room["id"])
                r = dict(room)
                r["message_count"] = used
                if room.get("mode") == "meeting":
                    r["budget"] = MEETING_BUDGET
                    r["budget_remaining"] = max(0, MEETING_BUDGET - used)
                    r["next_speaker"] = self._next_speaker(room)
                rooms.append(r)
            return rooms

    def _next_speaker(self, room):
        members = room.get("members") or []
        if not members:
            return None
        last = None
        for m in reversed(self._data["messages"]):
            if m["room"] == room["id"] and m["role"] == "assistant":
                last = m.get("character")
                break
        if last is None or last not in members:
            return members[0]
        return members[(members.index(last) + 1) % len(members)]

    def get_room(self, room_id):
        with self._lock:
            return self._data["rooms"].get(room_id)

    def purge_character(self, char_id):
        """Remove a deleted bot's traces from chat state.

        Deletes every message where character == char_id (all rooms)
        and strips char_id from every room's members list. Returns
        the number of messages removed. Used by DELETE
        /api/characters/:id so a recreated bot with the same id
        starts with a clean chat instead of resurfacing old data.
        """
        with self._lock:
            before = len(self._data["messages"])
            self._data["messages"] = [
                m for m in self._data["messages"]
                if m.get("character") != char_id
            ]
            removed = before - len(self._data["messages"])
            for room in self._data["rooms"].values():
                members = room.get("members") or []
                if char_id in members:
                    room["members"] = [m for m in members if m != char_id]
            if removed:
                self._save()
            else:
                # Members may have changed even with no messages.
                self._save()
            return removed

    def delete_room(self, room_id):
        """Delete a room and all its messages.

        Returns True on success. Raises KeyError for an unknown id
        and ValueError for the two default rooms (general,
        meeting-room), which can never be deleted.
        """
        if room_id in ("general", "meeting-room"):
            raise ValueError("default room %s cannot be deleted" % room_id)
        with self._lock:
            if room_id not in self._data["rooms"]:
                raise KeyError(room_id)
            del self._data["rooms"][room_id]
            self._data["messages"] = [
                m for m in self._data["messages"] if m["room"] != room_id
            ]
            self._save()
            return True

    def create_room(self, name, members, mode="chat"):
        with self._lock:
            room_id = name.strip().lower().replace(" ", "-") or "room"
            base, n = room_id, 2
            while room_id in self._data["rooms"]:
                room_id = "%s-%d" % (base, n)
                n += 1
            room = {"id": room_id, "name": name.strip(), "mode": mode,
                    "members": list(members), "decisions": [],
                    "disputed": [], "stopped": False}
            self._data["rooms"][room_id] = room
            self._save()
            return room

    def add_message(self, room, character, role, text, presence=None,
                    reply_to=None):
        with self._lock:
            msg = {"id": len(self._data["messages"]) + 1, "room": room,
                   "character": character, "role": role, "text": text,
                   "created_at": _utcnow(),
                   "presence": presence or [],
                   "reply_to": reply_to}
            self._data["messages"].append(msg)
            room_obj = self._data["rooms"].get(room)
            if room_obj is not None and role == "user":
                room_obj["stopped"] = False
            self._save()
            return msg

    def messages(self, room):
        with self._lock:
            return [m for m in self._data["messages"] if m["room"] == room]

    def add_decision(self, room, decision, disputed=""):
        with self._lock:
            room_obj = self._data["rooms"].get(room)
            if room_obj is None:
                return None
            entry = {"decision": decision, "disputed": disputed,
                     "created_at": _utcnow()}
            room_obj["decisions"].append(entry)
            if disputed:
                room_obj["disputed"].append(
                    {"point": disputed, "created_at": _utcnow()})
            self._save()
            return entry

    def mark_stopped(self, room, report):
        with self._lock:
            room_obj = self._data["rooms"].get(room)
            if room_obj is not None:
                room_obj["stopped"] = True
            msg = {"id": len(self._data["messages"]) + 1, "room": room,
                   "character": None, "role": "system", "text": report,
                   "created_at": _utcnow(),
                   "presence": [{"type": "stopped",
                                 "note": report}]}
            self._data["messages"].append(msg)
            self._save()
            return msg
