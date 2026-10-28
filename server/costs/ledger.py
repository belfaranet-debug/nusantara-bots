"""Cost ledger — per-task usage records + daily aggregates per bot.

FinOps-lite for the own build. The server NEVER sees a
credential: the browser reads the ``usage`` object
(``prompt_tokens`` / ``completion_tokens`` / ``model``) from the
provider's own response — the call the browser already made with
its BYO credential, Bearer ONLY to the provider URL — and POSTs
just the numbers + the model name to ``/api/costs/record``.
This module stores those numbers, estimates the cost from the
configurable price table, and answers two questions for the
deck: "how much did each bot spend today?" and "may this bot
start a new task?".

Model
-----
* One record per task (one provider call): bot, task id, task
  kind, model, prompt/completion/total tokens, estimated
  ``cost_usd``, ``price_known`` (False + cost 0 when the model
  has no price — the tokens are still recorded, the cost is
  honestly zero, never invented), UTC day, timestamp.
* A record is a fact about spend that already happened — the
  provider already charged it — so ``record()`` never refuses
  on cap grounds. The cap is a PRE-FLIGHT gate: call
  ``check_cap(bot)`` before starting a task; when it says no,
  the task endpoint refuses with "cap harian tercapai".
* Cap default: unlimited (None) until the owner sets one — see
  ``costs/config.py``.
* Storage: one JSON file (``data/costs.json`` by default),
  atomic write under a lock — the same pattern as
  ``office/kanban.py`` and ``approvals/queue.py``.

Zero-dependency, Python standard library only.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from datetime import datetime, timezone

from .config import CostConfig
from .prices import ESTIMATE_NOTE, estimate_cost

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_COSTS_FILE = os.path.join(_REPO_ROOT, "data", "costs.json")

# "cap harian tercapai" — the exact message the task-start path
# returns when the gate refuses a new task.
CAP_REACHED_MESSAGE = "cap harian tercapai"

MAX_BOT_CHARS = 128
MAX_TASK_ID_CHARS = 256
MAX_TASK_KIND_CHARS = 64
MAX_MODEL_CHARS = 128


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _today_utc():
    return _utcnow().date().isoformat()


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


def _validate_tokens(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer" % name)
    if value < 0:
        raise ValueError("%s must not be negative" % name)
    return value


def _empty_aggregate():
    return {"prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "cost_usd": 0.0, "tasks": 0,
            "price_unknown_tasks": 0}


class CostLedger:
    """Usage records + daily aggregates + cap gate, one JSON file."""

    def __init__(self, path=None, config=None):
        self.path = path or os.environ.get(
            "GROKBOT_COSTS_FILE", DEFAULT_COSTS_FILE)
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.config = config if config is not None else CostConfig()
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {"records": [], "next_id": 1}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (isinstance(data, dict)
                    and isinstance(data.get("records"), list)
                    and isinstance(data.get("next_id"), int)):
                return data
        except (OSError, ValueError):
            pass
        return {"records": [], "next_id": 1}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # -- recording --------------------------------------------------
    def record(self, bot, task_id, model, prompt_tokens,
               completion_tokens, task_kind="", recorded_at=None) -> dict:
        """Record one task's provider usage. Returns the record.

        ``recorded_at`` is a datetime (UTC assumed when naive) or
        an ISO string; it defaults to now. The record's ``day``
        is the UTC date of that timestamp — the same day the cap
        and the summary aggregate on. Raises ValueError on bad
        input. Never refuses on cap grounds: this is a fact about
        spend that already happened (see module docstring).
        """
        bot = _validate_text(bot, "bot", MAX_BOT_CHARS)
        task_id = _validate_text(task_id, "task_id", MAX_TASK_ID_CHARS)
        model = _validate_text(model, "model", MAX_MODEL_CHARS)
        task_kind = _validate_text(task_kind, "task_kind",
                                   MAX_TASK_KIND_CHARS,
                                   required=False) or ""
        prompt_tokens = _validate_tokens(prompt_tokens, "prompt_tokens")
        completion_tokens = _validate_tokens(completion_tokens,
                                             "completion_tokens")
        if recorded_at is None:
            when = _utcnow()
        elif isinstance(recorded_at, datetime):
            when = recorded_at
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        elif isinstance(recorded_at, str):
            try:
                when = datetime.fromisoformat(recorded_at)
            except ValueError:
                raise ValueError("recorded_at must be an ISO timestamp")
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        else:
            raise ValueError("recorded_at must be a datetime or ISO string")
        cost, price_known = estimate_cost(
            model, prompt_tokens, completion_tokens,
            prices=self.config.prices())
        with self._lock:
            rec_id = "cost-%d" % self._data["next_id"]
            self._data["next_id"] += 1
            record = {
                "id": rec_id,
                "bot": bot,
                "task_id": task_id,
                "task_kind": task_kind,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "cost_usd": cost,
                "price_known": price_known,
                "day": when.date().isoformat(),
                "recorded_at": _iso(when),
            }
            self._data["records"].append(record)
            self._save()
            return copy.deepcopy(record)

    # -- reading ----------------------------------------------------
    def list_records(self, bot=None, day=None, limit=None) -> list:
        """Records, newest first. Filter by bot and/or UTC day."""
        with self._lock:
            records = list(self._data["records"])
        if bot is not None:
            records = [r for r in records if r["bot"] == bot]
        if day is not None:
            records = [r for r in records if r["day"] == day]
        records.sort(key=lambda r: r["recorded_at"], reverse=True)
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise ValueError("limit must be an integer")
            if limit < 0:
                raise ValueError("limit must not be negative")
            records = records[:limit]
        return copy.deepcopy(records)

    def _aggregate_locked(self, records):
        per_bot = {}
        totals = _empty_aggregate()
        most_expensive = None
        for rec in records:
            agg = per_bot.setdefault(rec["bot"], _empty_aggregate())
            for key in ("prompt_tokens", "completion_tokens",
                        "total_tokens"):
                agg[key] += rec[key]
                totals[key] += rec[key]
            agg["cost_usd"] = round(agg["cost_usd"] + rec["cost_usd"], 8)
            totals["cost_usd"] = round(
                totals["cost_usd"] + rec["cost_usd"], 8)
            agg["tasks"] += 1
            totals["tasks"] += 1
            if not rec["price_known"]:
                agg["price_unknown_tasks"] += 1
                totals["price_unknown_tasks"] += 1
            if (most_expensive is None
                    or (rec["cost_usd"], rec["total_tokens"])
                    > (most_expensive["cost_usd"],
                       most_expensive["total_tokens"])):
                most_expensive = rec
        return per_bot, totals, most_expensive

    def daily_summary(self, day=None, bot=None) -> dict:
        """Spend for one UTC day (default today), per bot + totals.

        Shape:
        {"day": "YYYY-MM-DD", "bots": {bot: aggregate, ...},
         "totals": aggregate,
         "most_expensive_task": record | None,
         "estimate_note": ESTIMATE_NOTE}
        ``bot`` narrows the summary to one bot (its aggregate is
        still under "bots", and "totals" equals it).
        """
        day = day or _today_utc()
        with self._lock:
            records = [r for r in self._data["records"]
                       if r["day"] == day]
        if bot is not None:
            records = [r for r in records if r["bot"] == bot]
        per_bot, totals, most_expensive = self._aggregate_locked(records)
        return {"day": day, "bots": per_bot, "totals": totals,
                "most_expensive_task": copy.deepcopy(most_expensive)
                if most_expensive is not None else None,
                "estimate_note": ESTIMATE_NOTE}

    # -- cap gate ---------------------------------------------------
    def spent_today(self, bot, day=None) -> float:
        """Estimated USD spent by one bot on one UTC day."""
        summary = self.daily_summary(day=day, bot=bot)
        return summary["totals"]["cost_usd"]

    def check_cap(self, bot, day=None) -> dict:
        """May this bot start a new task? The pre-flight gate.

        Returns {"bot", "day", "cap_usd" (None = unlimited),
        "spent_usd", "remaining_usd" (None when unlimited),
        "allowed": bool, "message": str}. ``allowed`` is False
        exactly when a cap is set and today's estimated spend
        has reached it (spent >= cap). The message is the clear
        refusal the task-start path returns to the deck.
        """
        if not isinstance(bot, str) or not bot.strip():
            raise ValueError("bot must be a non-empty string")
        day = day or _today_utc()
        cap = self.config.cap_for(bot)
        spent = self.spent_today(bot, day=day)
        if cap is None:
            return {"bot": bot, "day": day, "cap_usd": None,
                    "spent_usd": spent, "remaining_usd": None,
                    "allowed": True,
                    "message": "tidak ada cap harian untuk bot ini"}
        remaining = round(max(0.0, cap - spent), 8)
        allowed = spent < cap
        if allowed:
            message = ("sisa cap harian $%.4f dari $%.4f"
                       % (remaining, cap))
        else:
            message = ("%s untuk %s: sudah $%.4f dari cap harian "
                       "$%.4f" % (CAP_REACHED_MESSAGE, bot, spent, cap))
        return {"bot": bot, "day": day, "cap_usd": cap,
                "spent_usd": spent, "remaining_usd": remaining,
                "allowed": allowed, "message": message}
