"""Plans + checkpoints for long delegated tasks (Fase 2/3).

A plan is the visible, pausable, steerable shape of one long task
delegated to a bot:

* a plan has an ordered list of STEPS, each pending | running |
  done | failed;
* the plan itself is active | paused | done | failed;
* a human can PAUSE mid-flight (the running step goes back to
  pending, so it can be resumed) and STEER mid-flight (insert or
  replace the upcoming steps + record the human direction, with
  who gave it).

Built on what already exists — it does not duplicate it:

* every step maps to one Kanban card (office/kanban.py): the card
  status follows the step status (pending->todo, running->doing,
  done->done, failed->blocked). Cards are created through
  ``KanbanStore`` only; this module never writes ``data/kanban.json``
  itself.
* a step completed with a note attaches that note as ``summary``
  evidence on its card, so the Kanban verdict rule (done without
  evidence is ``unproven``) keeps working unchanged.
* duty (office/duty.py) and presence (office/presence.py) are
  untouched: a plan's running step is what puts a card in ``doing``,
  which is what duty already reads as "desk".

Routine integration (routines/scheduler.py): a routine run creates
work as a Kanban card; ``plan_for_routine_run`` wraps that card in
a plan so the background run is visible, pausable and steerable
from the deck. See plans/server-wiring.patch.md for the wiring.

100% original code for this build. No model is called here, no
credential is ever accepted or stored (BYO invariant: the server
never holds a key), and nothing is sent, published or merged —
a plan only describes work; approvals still gate the real actions.

Persistence: one JSON file (``data/plans.json`` by default, or
``GROKBOT_PLANS_FILE``), atomic writes under a lock — the same
pattern as office/kanban.py and routines/scheduler.py. A corrupt
file loads as empty, never crashes the server.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PLANS_FILE = os.path.join(_REPO_ROOT, "data", "plans.json")

STEP_STATUSES = ("pending", "running", "done", "failed")
PLAN_STATUSES = ("active", "paused", "done", "failed")

#: Step status -> Kanban card status. The card follows the step.
STEP_TO_CARD_STATUS = {
    "pending": "todo",
    "running": "doing",
    "done": "done",
    "failed": "blocked",
}

ORIGIN_KINDS = ("manual", "meeting", "agent", "mail")

MAX_TITLE_CHARS = 256
MAX_STEP_TITLE_CHARS = 256
MAX_DIRECTION_CHARS = 1024
MAX_BY_CHARS = 128
MAX_NOTE_CHARS = 4096
MAX_STEPS = 50


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        _validate_text(ref, "origin.ref", MAX_TITLE_CHARS)
    result = {"kind": kind}
    if ref is not None:
        result["ref"] = ref
    return result


def _validate_step_titles(steps) -> list:
    """Normalize a steps argument into a list of titles.

    Accepts a list of plain title strings, or a list of
    ``{"title": ...}`` dicts. Raises ValueError on an empty list,
    too many steps, or a bad title.
    """
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a non-empty list")
    if len(steps) > MAX_STEPS:
        raise ValueError("a plan has at most %d steps" % MAX_STEPS)
    titles = []
    for step in steps:
        if isinstance(step, dict):
            title = step.get("title")
        else:
            title = step
        titles.append(_validate_text(title, "step title",
                                     MAX_STEP_TITLE_CHARS))
    return titles


def plan_summary(plan: dict) -> dict:
    """Progress summary for one plan dict (pure function).

    ``percent_done`` is the share of steps done; ``current_step_id``
    is the running step, or the first pending step when none is
    running, or None when the plan has no open step left.
    """
    steps = plan.get("steps") or []
    total = len(steps)
    counts = {status: 0 for status in STEP_STATUSES}
    current_step_id = None
    first_pending_id = None
    for step in steps:
        status = step.get("status")
        if status in counts:
            counts[status] += 1
        if status == "running" and current_step_id is None:
            current_step_id = step.get("id")
        if status == "pending" and first_pending_id is None:
            first_pending_id = step.get("id")
    if current_step_id is None:
        current_step_id = first_pending_id
    return {
        "id": plan.get("id"),
        "title": plan.get("title"),
        "status": plan.get("status"),
        "assignee": plan.get("assignee"),
        "total": total,
        "done": counts["done"],
        "failed": counts["failed"],
        "running": counts["running"],
        "pending": counts["pending"],
        "percent_done": (round(100 * counts["done"] / total)
                         if total else 0),
        "current_step_id": current_step_id,
        "steering_count": len(plan.get("steering") or []),
    }


class PlanStore:
    """Plans + steps + steering log, persisted as one JSON file.

    Thread-safe. Takes an optional ``KanbanStore`` so tests (and
    the server) can point both stores at the same temp/real files;
    when omitted, a default ``KanbanStore`` is created lazily.
    """

    def __init__(self, path=None, kanban=None):
        self.path = path or os.environ.get(
            "GROKBOT_PLANS_FILE", DEFAULT_PLANS_FILE)
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._kanban = kanban
        self._lock = threading.Lock()
        self._data = self._load()

    @property
    def kanban(self):
        """The Kanban store steps map onto (created lazily)."""
        if self._kanban is None:
            from office.kanban import KanbanStore
            self._kanban = KanbanStore()
        return self._kanban

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {"plans": [], "next_id": 1}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (isinstance(data, dict)
                    and isinstance(data.get("plans"), list)
                    and isinstance(data.get("next_id"), int)):
                return data
        except (OSError, ValueError):
            pass
        return {"plans": [], "next_id": 1}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _find_locked(self, plan_id):
        for plan in self._data["plans"]:
            if plan["id"] == plan_id:
                return plan
        return None

    def _find_step(self, plan, step_id):
        for step in plan["steps"]:
            if step["id"] == step_id:
                return step
        return None

    def _sync_card(self, plan, step):
        """Move the step's Kanban card to follow the step status.

        A missing card (or a card store that fails) never breaks
        the plan transition: the plan is the record of intent, the
        card is its mirror. Failures are recorded on the step
        instead of raising.
        """
        card_id = step.get("card_id")
        if not card_id:
            return
        target = STEP_TO_CARD_STATUS[step["status"]]
        try:
            updated = self.kanban.move_card(card_id, target)
            if updated is None:
                step["card_sync"] = "card missing: %s" % card_id
            else:
                step.pop("card_sync", None)
        except Exception as e:  # card store must not break plans
            step["card_sync"] = "card sync failed: %s" % e

    def _new_step(self, title, index) -> dict:
        now = _utcnow()
        return {
            "id": "step-%d" % index,
            "title": title,
            "status": "pending",
            "card_id": None,
            "note": "",
            "created_at": now,
            "updated_at": now,
        }

    def _create_step_cards(self, plan, steps):
        """Create one Kanban card per step (origin = plan origin,
        ref = the plan id) and record each card id on its step."""
        for step in steps:
            card = self.kanban.create_card(
                step["title"],
                body=("Plan %s — step %s. Status kartu mengikuti "
                      "status langkah rencana." % (plan["id"], step["id"])),
                assignee=plan.get("assignee"),
                origin={"kind": plan["origin"]["kind"],
                        "ref": plan["id"]})
            step["card_id"] = card["id"]

    # -- CRUD --------------------------------------------------------
    def create_plan(self, title, steps, assignee=None, origin=None) -> dict:
        """Create a plan in ``active`` with all steps ``pending``.

        ``steps`` is a list of titles (or ``{"title": ...}`` dicts).
        One Kanban card is created per step. Raises ValueError on
        bad input.
        """
        title = _validate_text(title, "title", MAX_TITLE_CHARS)
        titles = _validate_step_titles(steps)
        if assignee is not None:
            assignee = _validate_text(assignee, "assignee",
                                      MAX_TITLE_CHARS)
        origin = _validate_origin(origin)
        with self._lock:
            plan_id = "plan-%d" % self._data["next_id"]
            self._data["next_id"] += 1
            now = _utcnow()
            plan = {
                "id": plan_id,
                "title": title,
                "assignee": assignee,
                "origin": origin,
                "status": "active",
                "steps": [self._new_step(t, i + 1)
                          for i, t in enumerate(titles)],
                "steering": [],
                "created_at": now,
                "updated_at": now,
                "paused_at": None,
                "completed_at": None,
            }
            self._data["plans"].append(plan)
            self._save()
            self._create_step_cards(plan, plan["steps"])
            plan["updated_at"] = _utcnow()
            self._save()
            return copy.deepcopy(plan)

    def get_plan(self, plan_id) -> dict | None:
        with self._lock:
            plan = self._find_locked(plan_id)
            return copy.deepcopy(plan) if plan is not None else None

    def list_plans(self, status=None, assignee=None) -> list:
        with self._lock:
            plans = self._data["plans"]
            if status is not None:
                if status not in PLAN_STATUSES:
                    raise ValueError(
                        "status must be one of %s" % (list(PLAN_STATUSES),))
                plans = [p for p in plans if p["status"] == status]
            if assignee is not None:
                plans = [p for p in plans if p.get("assignee") == assignee]
            return copy.deepcopy(plans)

    def summary(self, plan_id) -> dict | None:
        """Progress summary for a plan id, or None if unknown."""
        plan = self.get_plan(plan_id)
        if plan is None:
            return None
        return plan_summary(plan)

    # -- step transitions --------------------------------------------
    def start_step(self, plan_id, step_id) -> dict:
        """pending -> running. Raises KeyError for an unknown plan
        or step, ValueError when the plan is not active, the step
        is not pending, or another step is already running (a plan
        runs one step at a time, in order of attention)."""
        with self._lock:
            plan = self._find_locked(plan_id)
            if plan is None:
                raise KeyError("unknown plan %r" % plan_id)
            if plan["status"] != "active":
                raise ValueError(
                    "plan %s is %s, not active — resume it first"
                    % (plan_id, plan["status"]))
            step = self._find_step(plan, step_id)
            if step is None:
                raise KeyError("unknown step %r in plan %s"
                               % (step_id, plan_id))
            if step["status"] != "pending":
                raise ValueError(
                    "step %s is %s, not pending" % (step_id, step["status"]))
            running = [s for s in plan["steps"]
                       if s["status"] == "running"]
            if running:
                raise ValueError(
                    "step %s is already running in plan %s"
                    % (running[0]["id"], plan_id))
            step["status"] = "running"
            step["updated_at"] = _utcnow()
            plan["updated_at"] = step["updated_at"]
            self._sync_card(plan, step)
            self._save()
            return copy.deepcopy(plan)

    def complete_step(self, plan_id, step_id, note=None) -> dict:
        """running -> done. A note is kept on the step and attached
        as ``summary`` evidence to the step's Kanban card. When the
        last open step completes, the plan becomes ``done``."""
        if note is not None:
            note = _validate_text(note, "note", MAX_NOTE_CHARS,
                                  required=False) or ""
        with self._lock:
            plan = self._find_locked(plan_id)
            if plan is None:
                raise KeyError("unknown plan %r" % plan_id)
            step = self._find_step(plan, step_id)
            if step is None:
                raise KeyError("unknown step %r in plan %s"
                               % (step_id, plan_id))
            if step["status"] != "running":
                raise ValueError(
                    "step %s is %s, not running" % (step_id, step["status"]))
            step["status"] = "done"
            if note:
                step["note"] = note
            step["updated_at"] = _utcnow()
            plan["updated_at"] = step["updated_at"]
            self._sync_card(plan, step)
            if note and step.get("card_id"):
                try:
                    self.kanban.add_evidence(
                        step["card_id"], "summary",
                        "Langkah selesai: %s" % step["title"],
                        body=note, by=plan.get("assignee") or "")
                except Exception:
                    pass
            if all(s["status"] == "done" for s in plan["steps"]):
                plan["status"] = "done"
                plan["completed_at"] = plan["updated_at"]
            self._save()
            return copy.deepcopy(plan)

    def fail_step(self, plan_id, step_id, note=None) -> dict:
        """running -> failed. The plan becomes ``failed``: a failed
        step marks the whole plan, so the deck shows the failure
        instead of silently moving on. A failed plan is reopened
        only by an explicit steer that adds new steps (see
        ``steer``). A note is kept on the step and attached as
        ``summary`` evidence to the step's Kanban card."""
        if note is not None:
            note = _validate_text(note, "note", MAX_NOTE_CHARS,
                                  required=False) or ""
        with self._lock:
            plan = self._find_locked(plan_id)
            if plan is None:
                raise KeyError("unknown plan %r" % plan_id)
            step = self._find_step(plan, step_id)
            if step is None:
                raise KeyError("unknown step %r in plan %s"
                               % (step_id, plan_id))
            if step["status"] != "running":
                raise ValueError(
                    "step %s is %s, not running" % (step_id, step["status"]))
            step["status"] = "failed"
            if note:
                step["note"] = note
            step["updated_at"] = _utcnow()
            plan["updated_at"] = step["updated_at"]
            plan["status"] = "failed"
            self._sync_card(plan, step)
            if note and step.get("card_id"):
                try:
                    self.kanban.add_evidence(
                        step["card_id"], "summary",
                        "Langkah gagal: %s" % step["title"],
                        body=note, by=plan.get("assignee") or "")
                except Exception:
                    pass
            self._save()
            return copy.deepcopy(plan)

    # -- pause / resume ----------------------------------------------
    def pause_plan(self, plan_id) -> dict:
        """active -> paused. The running step (if any) goes back to
        ``pending`` — and its card back to ``todo`` — so the work
        can be resumed from the same step later. Raises KeyError
        for an unknown plan, ValueError when not active."""
        with self._lock:
            plan = self._find_locked(plan_id)
            if plan is None:
                raise KeyError("unknown plan %r" % plan_id)
            if plan["status"] != "active":
                raise ValueError(
                    "plan %s is %s, not active" % (plan_id, plan["status"]))
            now = _utcnow()
            for step in plan["steps"]:
                if step["status"] == "running":
                    step["status"] = "pending"
                    step["updated_at"] = now
                    self._sync_card(plan, step)
            plan["status"] = "paused"
            plan["paused_at"] = now
            plan["updated_at"] = now
            self._save()
            return copy.deepcopy(plan)

    def resume_plan(self, plan_id) -> dict:
        """paused -> active. Steps stay as they were (the paused
        running step is ``pending`` again and is restarted with
        ``start_step``). Raises KeyError / ValueError as above."""
        with self._lock:
            plan = self._find_locked(plan_id)
            if plan is None:
                raise KeyError("unknown plan %r" % plan_id)
            if plan["status"] != "paused":
                raise ValueError(
                    "plan %s is %s, not paused" % (plan_id, plan["status"]))
            plan["status"] = "active"
            plan["paused_at"] = None
            plan["updated_at"] = _utcnow()
            self._save()
            return copy.deepcopy(plan)

    # -- steer ---------------------------------------------------------
    def steer(self, plan_id, direction, by, after_step_id=None,
              new_steps=None, replace=None) -> dict:
        """Change direction mid-flight and record it.

        ``direction`` is the human's instruction (required),
        ``by`` is who gave it (a name/handle, required). Either:

        * ``new_steps`` — titles inserted AFTER ``after_step_id``
          (default: after the running step, else at the end), each
          with its own fresh Kanban card; or
        * ``replace`` — ``{step_id: new_title}`` retitling upcoming
          (``pending``) steps; or both.

        The steering entry is appended to ``plan["steering"]`` with
        the inserted/replaced step ids, so the deck can show the
        whole history of human redirections. A ``failed`` plan that
        gains new steps reopens to ``active`` (the failed step stays
        failed as history). A ``done`` plan cannot be steered —
        start a new plan instead.

        Raises KeyError for an unknown plan/step, ValueError on bad
        input or a done plan.
        """
        direction = _validate_text(direction, "direction",
                                   MAX_DIRECTION_CHARS)
        by = _validate_text(by, "by", MAX_BY_CHARS)
        new_titles = (_validate_step_titles(new_steps)
                      if new_steps is not None else [])
        replace_map = {}
        if replace is not None:
            if not isinstance(replace, dict) or not replace:
                raise ValueError("replace must be a non-empty object")
            for step_id, title in replace.items():
                replace_map[step_id] = _validate_text(
                    title, "replacement title", MAX_STEP_TITLE_CHARS)
        if not new_titles and not replace_map:
            raise ValueError(
                "steer needs new_steps to add or replace to change")
        with self._lock:
            plan = self._find_locked(plan_id)
            if plan is None:
                raise KeyError("unknown plan %r" % plan_id)
            if plan["status"] == "done":
                raise ValueError(
                    "plan %s is done — start a new plan instead"
                    % plan_id)
            # Validate the anchor + replaced steps before changing
            # anything: a steer is one atomic redirection.
            anchor_index = None
            if after_step_id is not None:
                anchor = self._find_step(plan, after_step_id)
                if anchor is None:
                    raise KeyError("unknown step %r in plan %s"
                                   % (after_step_id, plan_id))
                anchor_index = plan["steps"].index(anchor)
            replaced_ids = []
            for step_id in replace_map:
                step = self._find_step(plan, step_id)
                if step is None:
                    raise KeyError("unknown step %r in plan %s"
                                   % (step_id, plan_id))
                if step["status"] != "pending":
                    raise ValueError(
                        "only a pending step can be replaced "
                        "(step %s is %s)"
                        % (step_id, step["status"]))
                replaced_ids.append(step_id)
            now = _utcnow()
            for step_id in replaced_ids:
                step = self._find_step(plan, step_id)
                step["title"] = replace_map[step_id]
                step["updated_at"] = now
                if step.get("card_id"):
                    # Keep the card title in step with the step:
                    # cards have no rename op, so the new title is
                    # recorded as evidence on the card instead.
                    try:
                        self.kanban.add_evidence(
                            step["card_id"], "summary",
                            "Langkah diubah lewat steer: %s"
                            % replace_map[step_id],
                            body="Arahan %s: %s" % (by, direction),
                            by=by)
                    except Exception:
                        pass
            inserted_ids = []
            if new_titles:
                if anchor_index is not None:
                    insert_at = anchor_index + 1
                else:
                    running = [i for i, s in enumerate(plan["steps"])
                               if s["status"] == "running"]
                    insert_at = (running[0] + 1 if running
                                 else len(plan["steps"]))
                next_index = max(
                    (int(s["id"].split("-")[1]) for s in plan["steps"]),
                    default=0) + 1
                fresh = [self._new_step(t, next_index + i)
                         for i, t in enumerate(new_titles)]
                plan["steps"][insert_at:insert_at] = fresh
                self._create_step_cards(plan, fresh)
                inserted_ids = [s["id"] for s in fresh]
            plan["steering"].append({
                "by": by,
                "direction": direction,
                "ts": now,
                "after_step_id": after_step_id,
                "inserted_step_ids": inserted_ids,
                "replaced_step_ids": replaced_ids,
            })
            if plan["status"] == "failed" and inserted_ids:
                plan["status"] = "active"
            plan["updated_at"] = now
            self._save()
            return copy.deepcopy(plan)


def plan_for_routine_run(routine, card, plan_store=None, kanban=None,
                         steps=None) -> dict:
    """Wrap one routine run in a plan (scheduler integration point).

    ``routine`` is the routine record and ``card`` the Kanban card
    ``RoutineStore.run_routine`` just created. The plan's steps
    default to: do the card's work, attach evidence to the card,
    hand it to a human for review — callers pass explicit
    ``steps`` for anything more specific. The plan origin is
    ``agent`` with the routine id as ref, so the deck can trace
    the plan back to the routine that fired it.

    This helper is called BY the wiring (see
    plans/server-wiring.patch.md); routines/scheduler.py itself
    stays unchanged.
    """
    if not isinstance(routine, dict) or not routine.get("id"):
        raise ValueError("routine must be a record with an id")
    if not isinstance(card, dict) or not card.get("id"):
        raise ValueError("card must be a record with an id")
    store = plan_store if plan_store is not None else PlanStore(
        kanban=kanban)
    title = routine.get("title") or ("Routine: %s" % routine.get("pack", ""))
    default_steps = [
        "Laksanakan: %s" % title,
        "Lampirkan bukti ke kartu %s" % card["id"],
        "Serahkan ke review manusia",
    ]
    return store.create_plan(
        title, steps or default_steps,
        assignee=routine.get("character"),
        origin={"kind": "agent", "ref": routine["id"]})
