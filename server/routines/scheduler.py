"""Routines — scheduled work that creates tasks, never runs a model.

A routine is a small schedule (interval / daily / cron subset) tied
to a character + a pack. When it fires, the server does exactly two
honest things:

1. creates a Kanban card (origin ``agent``, ref = the routine id)
   describing the work to do, and
2. writes a team-memory entry noting the routine fired.

That is the whole server-side execution. The real model work
happens through the browser / BYO path: a human (or a browser-side
model call) picks the card up. The server never calls a model, never
pretends it did, and never auto-sends, auto-publishes, or
auto-merges anything a routine produced — those still need a human
approval (see approvals/).

Schedules (no new dependencies — the parser is hand-written here)
-----------------------------------------------------------------
* ``{"kind": "interval", "seconds": 3600}`` — every N seconds
  (N >= 60; anything shorter is a polling loop, not a routine).
* ``{"kind": "daily", "time": "07:00"}`` — once a day at HH:MM
  (UTC; the server has no timezone config — see README.md).
* ``{"kind": "cron", "expression": "0 7 * * 1"}`` — 5 fields
  (minute hour day-of-month month day-of-week). Each field supports
  ``*``, a single number, a comma list, a range (``1-5``), and a
  step (``*/15``, ``0-30/10``). NOT supported: names (JAN, MON),
  ``L`` / ``W`` / ``#``, seconds fields, years. When both
  day-of-month and day-of-week are restricted, a run happens when
  EITHER matches (standard cron rule).

``next_run_after(schedule, now)`` is a pure function; ``tick(now)``
runs every due + enabled routine once and advances its next run.
A disabled routine never runs — ``tick`` skips it, and a direct
``run_routine`` on one raises.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ROUTINES_FILE = os.path.join(_REPO_ROOT, "data", "routines.json")

SCHEDULE_KINDS = ("interval", "daily", "cron")

MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 365 * 86400

MAX_CHARACTER_CHARS = 128
MAX_PACK_CHARS = 128
MAX_TITLE_CHARS = 256

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Built-in routine: the weekly project pulse from NEXT-LEVEL-PLAN
# §7 Fase 2 (Kanban -> draft summary, human sends it). Weekly on
# Monday at 07:00 UTC; the pack name is the use-case pack's name.
WEEKLY_PULSE_ROUTINE = {
    "character": "chief-officer",
    "pack": "weekly-pulse",
    "schedule": {"kind": "cron", "expression": "0 7 * * 1"},
    "title": "Weekly project pulse",
}


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _parse_iso(value):
    """Datetime (aware, UTC) for an ISO string, or None."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _as_dt(now):
    """Accept a datetime or an ISO string; default to now (UTC)."""
    if now is None:
        return _utcnow()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now
    parsed = _parse_iso(now)
    if parsed is None:
        raise ValueError("now must be a datetime or ISO string")
    return parsed


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


# -- cron subset ----------------------------------------------------

def _parse_cron_field(field, low, high):
    """Allowed values for one cron field, or None for ``*``.

    Supports ``*``, numbers, comma lists, ranges, and steps
    (``*/15``, ``1-30/5``). Raises ValueError on anything else —
    including names (JAN/MON), L, W, #, and out-of-range numbers.
    """
    field = str(field).strip()
    if field == "*":
        return None
    allowed = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError("empty item in cron field %r" % field)
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) <= 0:
                raise ValueError("bad cron step in %r" % part)
            step = int(step_s)
        else:
            base = part
        if base == "*":
            start, end = low, high
        elif "-" in base:
            a, b = base.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise ValueError("bad cron range in %r" % part)
            start, end = int(a), int(b)
        elif base.isdigit():
            start, end = int(base), int(base)
        else:
            raise ValueError(
                "unsupported cron field %r (only *, numbers, lists, "
                "ranges and steps are supported)" % part)
        if start < low or end > high or start > end:
            raise ValueError("cron field %r out of range %d-%d"
                             % (part, low, high))
        for value in range(start, end + 1, step):
            allowed.add(value)
    return allowed


_CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def parse_cron(expression):
    """Parse a 5-field cron expression into per-field allowed sets.

    Returns a tuple of 5 sets-or-None (None = ``*``). Day-of-week
    accepts 0-7 (both 0 and 7 mean Sunday). Raises ValueError.
    """
    if not isinstance(expression, str):
        raise ValueError("cron expression must be a string")
    fields = expression.strip().split()
    if len(fields) != 5:
        raise ValueError("cron expression needs exactly 5 fields, "
                         "got %d: %r" % (len(fields), expression))
    parsed = tuple(_parse_cron_field(f, low, high)
                   for f, (low, high) in zip(fields, _CRON_RANGES))
    minute, hour, dom, month, dow = parsed
    if dow is not None and 7 in dow:
        dow = set(dow)
        dow.discard(7)
        dow.add(0)
    return (minute, hour, dom, month, dow)


def _cron_matches(parsed, dt):
    """True when ``dt`` matches a parsed cron expression."""
    minute, hour, dom, month, dow = parsed
    if minute is not None and dt.minute not in minute:
        return False
    if hour is not None and dt.hour not in hour:
        return False
    if month is not None and dt.month not in month:
        return False
    dom_match = dom is None or dt.day in dom
    # cron weekday: Monday=0 .. Sunday=6 in Python; cron Sunday=0.
    cron_dow = (dt.weekday() + 1) % 7
    dow_match = dow is None or cron_dow in dow
    if dom is not None and dow is not None:
        return dom_match or dow_match
    return dom_match and dow_match


def validate_schedule(schedule):
    """Validate a schedule dict; returns a cleaned copy.

    Raises ValueError on a bad kind, bad interval, bad daily time,
    or bad cron expression.
    """
    if not isinstance(schedule, dict):
        raise ValueError("schedule must be an object")
    kind = schedule.get("kind")
    if kind not in SCHEDULE_KINDS:
        raise ValueError("schedule.kind must be one of %s"
                         % (list(SCHEDULE_KINDS),))
    if kind == "interval":
        seconds = schedule.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError("interval schedule needs seconds (a number)")
        seconds = int(seconds)
        if seconds < MIN_INTERVAL_SECONDS or seconds > MAX_INTERVAL_SECONDS:
            raise ValueError("interval seconds must be between %d and %d"
                             % (MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS))
        return {"kind": "interval", "seconds": seconds}
    if kind == "daily":
        time_str = schedule.get("time")
        if not isinstance(time_str, str) or not _TIME_RE.match(time_str):
            raise ValueError("daily schedule needs time as HH:MM (24h)")
        return {"kind": "daily", "time": time_str}
    expression = schedule.get("expression")
    parse_cron(expression)  # raises ValueError on a bad expression
    return {"kind": "cron", "expression": str(expression).strip()}


def next_run_after(schedule, now=None):
    """Next run strictly after ``now`` (datetime or ISO string).

    Pure function: interval adds its seconds; daily takes the next
    HH:MM today, else tomorrow; cron scans forward minute by
    minute (capped at 366 days — a cron that never matches in a
    year is rejected as unsatisfiable).
    """
    schedule = validate_schedule(schedule)
    now = _as_dt(now)
    if schedule["kind"] == "interval":
        return now + timedelta(seconds=schedule["seconds"])
    if schedule["kind"] == "daily":
        hour, minute = (int(p) for p in schedule["time"].split(":"))
        candidate = now.replace(hour=hour, minute=minute, second=0,
                                microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    parsed = parse_cron(schedule["expression"])
    candidate = (now.replace(second=0, microsecond=0)
                 + timedelta(minutes=1))
    limit = now + timedelta(days=366)
    while candidate <= limit:
        if _cron_matches(parsed, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("cron expression %r has no run in the next year"
                     % schedule["expression"])


class RoutineStore:
    """Routines, persisted as one JSON file. Thread-safe."""

    def __init__(self, path=None):
        self.path = path or os.environ.get(
            "GROKBOT_ROUTINES_FILE", DEFAULT_ROUTINES_FILE)
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {"routines": [], "next_id": 1}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (isinstance(data, dict)
                    and isinstance(data.get("routines"), list)
                    and isinstance(data.get("next_id"), int)):
                return data
        except (OSError, ValueError):
            pass
        return {"routines": [], "next_id": 1}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _find_locked(self, routine_id):
        for rec in self._data["routines"]:
            if rec["id"] == routine_id:
                return rec
        return None

    # -- CRUD --------------------------------------------------------
    def create_routine(self, character, pack, schedule, enabled=True,
                       title=None, now=None) -> dict:
        """Create a routine. Its first ``next_run`` is computed from
        the creation time, so a routine created mid-schedule does
        not fire immediately for a run it already missed."""
        character = _validate_text(character, "character",
                                   MAX_CHARACTER_CHARS)
        pack = _validate_text(pack, "pack", MAX_PACK_CHARS)
        schedule = validate_schedule(schedule)
        if title is not None:
            title = _validate_text(title, "title", MAX_TITLE_CHARS)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        now_dt = _as_dt(now)
        with self._lock:
            routine_id = "routine-%d" % self._data["next_id"]
            self._data["next_id"] += 1
            record = {
                "id": routine_id,
                "character": character,
                "pack": pack,
                "title": title or ("Routine: %s" % pack),
                "schedule": schedule,
                "enabled": enabled,
                "last_run": None,
                "next_run": _iso(next_run_after(schedule, now_dt)),
                "last_error": None,
                "created_at": _iso(now_dt),
            }
            self._data["routines"].append(record)
            self._save()
            return copy.deepcopy(record)

    def create_weekly_pulse(self, now=None) -> dict:
        """Create the built-in weekly-pulse routine (see
        WEEKLY_PULSE_ROUTINE). A convenience wrapper, not magic:
        it is one ordinary routine like any other."""
        spec = WEEKLY_PULSE_ROUTINE
        return self.create_routine(
            spec["character"], spec["pack"], spec["schedule"],
            title=spec["title"], now=now)

    def list_routines(self) -> list:
        with self._lock:
            return copy.deepcopy(self._data["routines"])

    def get_routine(self, routine_id) -> dict | None:
        with self._lock:
            rec = self._find_locked(routine_id)
            return copy.deepcopy(rec) if rec is not None else None

    def set_enabled(self, routine_id, enabled) -> dict:
        """Enable / disable a routine. Returns the updated record.

        Raises KeyError for an unknown id, ValueError for a
        non-boolean flag.
        """
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        with self._lock:
            rec = self._find_locked(routine_id)
            if rec is None:
                raise KeyError("unknown routine %r" % routine_id)
            rec["enabled"] = enabled
            self._save()
            return copy.deepcopy(rec)

    def delete_routine(self, routine_id) -> bool:
        """Delete a routine. Returns True if one was removed."""
        with self._lock:
            for i, rec in enumerate(self._data["routines"]):
                if rec["id"] == routine_id:
                    del self._data["routines"][i]
                    self._save()
                    return True
        return False

    # -- running ------------------------------------------------------
    def due_routines(self, now=None) -> list:
        """Enabled routines whose ``next_run`` is at or before
        ``now``. Disabled routines are never due."""
        now_dt = _as_dt(now)
        with self._lock:
            due = []
            for rec in self._data["routines"]:
                if not rec.get("enabled"):
                    continue
                nxt = _parse_iso(rec.get("next_run"))
                if nxt is not None and nxt <= now_dt:
                    due.append(copy.deepcopy(rec))
            return due

    def run_routine(self, routine_id, kanban=None, memory=None,
                    now=None) -> dict:
        """Run one routine now (Fase 2 execution).

        Creates a Kanban card (origin ``agent``, ref = routine id)
        and a team-memory entry noting the run. The card is the
        work item; the model work itself happens through the
        browser/BYO path, not here. Advances ``last_run`` /
        ``next_run`` from the run time.

        Raises KeyError for an unknown id, ValueError for a
        disabled routine.
        """
        now_dt = _as_dt(now)
        with self._lock:
            rec = self._find_locked(routine_id)
            if rec is None:
                raise KeyError("unknown routine %r" % routine_id)
            if not rec.get("enabled"):
                raise ValueError("routine %s is disabled" % routine_id)
            snapshot = copy.deepcopy(rec)
        if kanban is None:
            from office.kanban import KanbanStore
            kanban = KanbanStore()
        if memory is None:
            from memory.store import MemoryStore
            memory = MemoryStore()
        card = kanban.create_card(
            snapshot["title"],
            body=("Scheduled routine %s (pack %s, character %s). "
                  "Work item created by the routine; the model work "
                  "happens through the browser/BYO path, and any "
                  "send/publish/merge still needs human approval."
                  % (snapshot["id"], snapshot["pack"],
                     snapshot["character"])),
            assignee=snapshot["character"],
            origin={"kind": "agent", "ref": snapshot["id"]})
        memory_id = None
        try:
            memory_id = memory.remember(
                "team",
                "Routine %s fired: %s (pack %s). Kanban card %s created."
                % (snapshot["id"], snapshot["title"],
                   snapshot["pack"], card["id"]),
                snapshot["character"])
        except Exception:
            memory_id = None
        with self._lock:
            rec = self._find_locked(routine_id)
            if rec is not None:
                rec["last_run"] = _iso(now_dt)
                rec["next_run"] = _iso(
                    next_run_after(rec["schedule"], now_dt))
                rec["last_error"] = None
                self._save()
                updated = copy.deepcopy(rec)
            else:
                updated = snapshot
        return {"routine": updated, "card": card,
                "memory_id": memory_id}

    def tick(self, now=None, kanban=None, memory=None) -> list:
        """Run every due routine once. Called periodically by the
        server (see automation/server-wiring.patch.md).

        Returns one result per due routine: ``{"id", "ok", ...}``.
        A routine that fails is recorded with ``last_error`` and
        does not stop the others; its ``next_run`` still advances
        so a broken routine cannot spin the tick loop.
        """
        now_dt = _as_dt(now)
        results = []
        for rec in self.due_routines(now_dt):
            try:
                out = self.run_routine(rec["id"], kanban=kanban,
                                       memory=memory, now=now_dt)
                results.append({"id": rec["id"], "ok": True,
                                "card_id": out["card"]["id"],
                                "memory_id": out["memory_id"]})
            except Exception as e:
                with self._lock:
                    found = self._find_locked(rec["id"])
                    if found is not None:
                        found["last_error"] = str(e)
                        try:
                            found["next_run"] = _iso(next_run_after(
                                found["schedule"], now_dt))
                        except ValueError:
                            pass
                        self._save()
                results.append({"id": rec["id"], "ok": False,
                                "error": str(e)})
        return results
