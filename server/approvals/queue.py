"""Approval queue — the human review gate for external actions.

Phase 2 (NEXT-LEVEL-PLAN.md §7). Every action that touches the
outside world — send, publish, merge, deploy, delete, change
production data (the ``review.required_before`` list every pack
manifest must carry) — needs a human to approve it first. An
approval is a record in a JSON file, requested by an agent or a
character, decided by a human, and consumed exactly once by the
action it authorizes.

Shape adapted from hermes-virtual-office (MIT) approvals
(``src/lib/hermes/approvals.ts``) — see office/SOURCE.md. No code
was copied: this is a fresh Python implementation over a local
JSON file, following the same atomic-write-under-lock pattern as
``office/kanban.py`` and ``computers/registry.py``.

Model
-----
* Statuses: pending | approved | denied | expired.
  ``approved`` is terminal for the decision, but the record is
  only *spent* when ``consumed`` flips to True — one approval
  authorizes exactly one action. A second consume is refused.
* ``expires_at`` is set at request time from ``ttl_seconds``
  (default 24 h). A pending OR approved record past its expiry is
  swept to ``expired`` the next time the queue is read or acted
  on, so a dead approval never silently authorizes anything — an
  approval that expired after being approved still refuses the
  send at the gate.
* Actions are an OPEN set: any non-empty string is accepted.
  The six defaults (send, publish, merge, deploy, delete,
  change-production-data — the ``review.required_before`` list
  every pack manifest must carry) are the ones that always
  require approval; other modules may request approval for their
  own actions (skill-update, vault-release, template-import,
  routine-run, ...). The queue does not judge whether an action
  *should* need approval — it only records and gates it.
* Approval levels (Phase 2/3, 25 Okt 2026 — replaces the binary
  always-approve switch): ``ask`` / ``edits`` / ``auto`` / ``full``
  / ``custom``. Resolution: per-bot beats per-action, which beats
  the global mode; nothing set anywhere means ``ask``. An unknown
  level value fails closed to ``ask`` — never to a more permissive
  level. Levels are cumulative: ``edits`` auto-approves file-edit
  actions, ``auto`` adds the safe (read-only) actions, ``full``
  auto-approves everything — it behaves exactly like the old
  always-approve ON. ``custom`` grants nothing by itself: it
  defers to the per-bot / per-action rules, and to ``ask`` when
  neither is set. ``alwaysAllow`` is a command allowlist (e.g.
  read / web-search) that auto-approves its actions at any level.
  A record that auto-approves carries ``auto: true``,
  ``decided_by: "auto"``, ``reason`` and ``rule`` naming the rule
  that decided it (``full-access`` / ``command-allowlist`` /
  ``safe-action`` / ``file-edit``), and its history records the
  same ``rule`` — the decision log. Auto-approval never bypasses
  the gate: ``POST /api/mail/send`` still needs an approved
  approval id — the request just arrives approved.
* Delegation: a level flows down (Chief → teammate) through
  ``resolve_level(..., inherited_level=...)``, capped by the
  teammate's engine cap (``engineCaps`` in settings):
  effective = min(inherited level, engine cap). A bot can never
  raise its own level: levels change only through
  ``update_settings`` (the human settings endpoint); the approval
  request path takes no level at all — ``inherited_level`` is an
  internal parameter for delegation code only and is never read
  from an HTTP request.
* Every status change is appended to the record's ``history[]``
  (who, when, why) — the audit trail lives inside the record.
* Stale data looks stale: ``GET /api/approvals`` reports each
  item's age in seconds plus a ``stale`` flag (pending older than
  ``STALE_AFTER_SECONDS`` = 300 s, the same 5-minute bar the plan
  sets for the queue view). Health warns only when a pending item
  is older than one hour (see ``app/server.py``).

Zero-dependency, Python standard library only.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from datetime import datetime, timedelta, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_APPROVALS_FILE = os.path.join(_REPO_ROOT, "data", "approvals.json")

# The six default actions that always require approval — the
# same six every pack manifest must list under
# review.required_before (NEXT-LEVEL-PLAN §4). The queue itself
# accepts ANY non-empty action string (see request_approval);
# this tuple is the documented default set, not a closed enum.
APPROVAL_ACTIONS = ("send", "publish", "merge", "deploy", "delete",
                    "change-production-data", "vault-release")

APPROVAL_STATUSES = ("pending", "approved", "denied", "expired")

# A pending item older than this is shown as stale in the queue
# view (5 minutes, per the plan's "basi > 5 mnt" rule).
STALE_AFTER_SECONDS = 300

# Health warns on a pending item older than this (1 hour).
HEALTH_WARN_AFTER_SECONDS = 3600

DEFAULT_TTL_SECONDS = 86400  # 24 hours
MAX_TTL_SECONDS = 30 * 86400  # 30 days

MAX_TARGET_CHARS = 512
MAX_DETAIL_CHARS = 4096
MAX_REQUESTER_CHARS = 128
MAX_BY_CHARS = 128
MAX_REASON_CHARS = 1024
MAX_ACTION_CHARS = 128

# Approval levels (Phase 2/3). "custom" is not a permission of
# its own: it defers to the per-bot / per-action rules and to
# "ask" when neither is set. Ranks order the levels so delegation
# can take the minimum (a "custom" value ranks 0 — it never
# raises anything, it only defers or caps down to ask).
APPROVAL_LEVELS = ("ask", "edits", "auto", "full", "custom")
LEVEL_RANK = {"ask": 0, "edits": 1, "auto": 2, "full": 3}

# Actions an "edits" level auto-approves: file edits only.
EDIT_ACTIONS = ("edit", "edit-file", "write-file", "patch-file",
                "create-file", "update-file")

# Actions an "auto" level auto-approves on top of the edit set:
# read-only / lookup actions. Anything else still asks a human.
SAFE_ACTIONS = ("read", "read-file", "web-search", "search",
                "list", "list-files", "get-status", "fetch")

# Decision-log rule names: the "which rule" recorded on every
# auto decision (record["rule"] + history[][rule]).
RULE_FULL_ACCESS = "full-access"
RULE_COMMAND_ALLOWLIST = "command-allowlist"
RULE_SAFE_ACTION = "safe-action"
RULE_FILE_EDIT = "file-edit"
RULE_ASK = "ask"
RULE_HUMAN_DECISION = "human-decision"
RULE_EXPIRED = "expired"

MAX_ALWAYS_ALLOW_ENTRIES = 64

# Approval-level settings. The shipped defaults file sets the
# global mode to "full" (the owner's explicit choice, 25 Okt 2026:
# "biar gw g ribet"); the runtime file overrides it. With
# neither file present the code default is "ask" (safe).
# Legacy boolean values are still read: True -> "full",
# False -> "ask" (the pre-levels always-approve switch).
DEFAULTS_FILENAME = "defaults.json"
DEFAULT_SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), DEFAULTS_FILENAME)
SETTINGS_ENV_VAR = "GROKBOT_APPROVAL_SETTINGS_FILE"
DEFAULTS_ENV_VAR = "GROKBOT_APPROVAL_DEFAULTS_FILE"
DEFAULT_RUNTIME_SETTINGS_FILE = os.path.join(
    _REPO_ROOT, "data", "approval_settings.json")


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _parse_iso(value):
    """Epoch seconds for an ISO timestamp string, or None."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


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


class ApprovalQueue:
    """Approval records, persisted as one JSON file. Thread-safe."""

    def __init__(self, path=None, settings_path=None, defaults_path=None):
        self.path = path or os.environ.get(
            "GROKBOT_APPROVALS_FILE", DEFAULT_APPROVALS_FILE)
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.settings_path = settings_path or os.environ.get(
            SETTINGS_ENV_VAR, DEFAULT_RUNTIME_SETTINGS_FILE)
        self.defaults_path = defaults_path or os.environ.get(
            DEFAULTS_ENV_VAR, DEFAULT_SETTINGS_FILE)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {"approvals": [], "next_id": 1}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (isinstance(data, dict)
                    and isinstance(data.get("approvals"), list)
                    and isinstance(data.get("next_id"), int)):
                return data
        except (OSError, ValueError):
            pass
        return {"approvals": [], "next_id": 1}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # -- expiry sweep (called under the lock) ----------------------
    def _sweep_locked(self):
        """Pending or approved records past expires_at become expired.

        An approved-but-unconsumed record expires too: approval is
        not a blank cheque, and the send gate re-checks expiry.
        A consumed record is history and is left alone.
        Returns True if anything changed (so callers save).
        """
        now = _utcnow()
        changed = False
        for rec in self._data["approvals"]:
            if rec.get("status") not in ("pending", "approved"):
                continue
            if rec.get("consumed"):
                continue
            exp = _parse_iso(rec.get("expires_at"))
            if exp is not None and now.timestamp() >= exp:
                rec["status"] = "expired"
                rec["decided_at"] = _iso(now)
                rec["decided_by"] = "system"
                rec["reason"] = "expired before use"
                rec["rule"] = RULE_EXPIRED
                rec["history"].append({
                    "event": "expired", "by": "system",
                    "at": _iso(now),
                    "reason": "expired before use",
                    "rule": RULE_EXPIRED})
                changed = True
        return changed

    def _find_locked(self, approval_id):
        for rec in self._data["approvals"]:
            if rec["id"] == approval_id:
                return rec
        return None

    # -- always-approve settings ------------------------------------
    @staticmethod
    def _read_settings_file(path):
        """A settings file as a dict, or {} when missing/corrupt.

        Keys starting with "_" are comments/notes and ignored.
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items()
                if not str(k).startswith("_")}

    # -- approval levels ------------------------------------------
    @staticmethod
    def _normalize_level(value):
        """A level string, or "ask" for anything unknown.

        Fail-closed: a value that is not one of APPROVAL_LEVELS
        never resolves to something more permissive.
        """
        if isinstance(value, str) and value in APPROVAL_LEVELS:
            return value
        return "ask"

    @staticmethod
    def _coerce_level(value):
        """A stored level value (new string or legacy bool) -> level.

        Returns None for a value that carries no level at all
        (None, a number, ...), so the caller can ignore it.
        Legacy bools map True -> "full", False -> "ask" (the old
        always-approve switch). Unknown strings fail closed.
        """
        if isinstance(value, bool):
            return "full" if value else "ask"
        if isinstance(value, str):
            return ApprovalQueue._normalize_level(value)
        return None

    @staticmethod
    def _rank(level):
        """Ordering rank; "custom" and anything unknown rank 0."""
        return LEVEL_RANK.get(level, 0)

    @staticmethod
    def _min_level(a, b):
        """The less permissive of two levels (ties keep ``a``)."""
        return a if ApprovalQueue._rank(a) <= ApprovalQueue._rank(b) else b

    def get_settings(self) -> dict:
        """Effective approval-level settings.

        Shipped defaults file first, runtime file overlaid on top
        (a key set in the runtime file wins; anything absent there
        falls back to the defaults). With neither file, the code
        default: mode "ask", everything else empty.

        Shape:
        {"mode": "ask"|"edits"|"auto"|"full"|"custom",
         "bots": {bot: level}, "actions": {action: level},
         "alwaysAllow": [action, ...], "engineCaps": {bot: level}}
        """
        base = self._read_settings_file(self.defaults_path)
        runtime = self._read_settings_file(self.settings_path)
        merged = {"mode": "ask", "bots": {}, "actions": {},
                  "alwaysAllow": [], "engineCaps": {}}
        for source in (base, runtime):
            if isinstance(source.get("mode"), str):
                merged["mode"] = self._normalize_level(source["mode"])
            elif isinstance(source.get("global"), bool):
                # Legacy always-approve switch.
                merged["mode"] = ("full" if source["global"] else "ask")
            for key in ("bots", "actions", "engineCaps"):
                section = source.get(key)
                if isinstance(section, dict):
                    for name, value in section.items():
                        level = self._coerce_level(value)
                        if level is None:
                            continue
                        if key == "engineCaps" and level == "custom":
                            # A cap must be a concrete ceiling;
                            # "custom" as a cap fails closed to ask.
                            level = "ask"
                        merged[key][str(name)] = level
            if isinstance(source.get("alwaysAllow"), list):
                merged["alwaysAllow"] = [
                    str(item) for item in source["alwaysAllow"]
                    if isinstance(item, str) and item.strip()]
        return merged

    def update_settings(self, patch) -> dict:
        """Apply a partial settings update, persist it to the
        runtime file, and return the effective settings.

        patch may carry any of mode (level), bots ({id: level}),
        actions ({action: level}), alwaysAllow ([action, ...]),
        engineCaps ({id: level}). Only the given keys change; the
        rest keeps its current effective value. Raises ValueError
        on a wrong type or an unknown level string. The legacy
        "global" bool is still accepted as a shim (True -> mode
        "full", False -> mode "ask").

        This is the ONLY way a level changes — it is called from
        the human settings endpoint. A bot's approval request can
        never reach it, so a bot cannot raise its own level.
        """
        if not isinstance(patch, dict):
            raise ValueError("settings patch must be an object")
        if "mode" in patch:
            if (not isinstance(patch["mode"], str)
                    or patch["mode"] not in APPROVAL_LEVELS):
                raise ValueError("settings.mode must be one of %s"
                                 % (list(APPROVAL_LEVELS),))
        if "global" in patch and not isinstance(patch["global"], bool):
            raise ValueError("settings.global must be true or false")
        for key in ("bots", "actions", "engineCaps"):
            section = patch.get(key)
            if section is not None:
                if not isinstance(section, dict):
                    raise ValueError("settings.%s must be an object" % key)
                for name, value in section.items():
                    # A write rejects an unknown level outright
                    # (the human gets a 400); only values already
                    # stored in a file fail closed to "ask" on read.
                    if isinstance(value, bool):
                        continue
                    if (not isinstance(value, str)
                            or value not in APPROVAL_LEVELS):
                        raise ValueError(
                            "settings.%s.%s must be one of %s"
                            % (key, name, list(APPROVAL_LEVELS)))
                    if key == "engineCaps" and value == "custom":
                        raise ValueError(
                            "settings.engineCaps.%s must not be 'custom'"
                            % name)
        if "alwaysAllow" in patch:
            allow = patch["alwaysAllow"]
            if not isinstance(allow, list):
                raise ValueError("settings.alwaysAllow must be a list")
            if len(allow) > MAX_ALWAYS_ALLOW_ENTRIES:
                raise ValueError(
                    "settings.alwaysAllow holds at most %d entries"
                    % MAX_ALWAYS_ALLOW_ENTRIES)
            for item in allow:
                if (not isinstance(item, str) or not item.strip()
                        or len(item) > MAX_ACTION_CHARS):
                    raise ValueError(
                        "settings.alwaysAllow entries must be "
                        "non-empty action names")
        with self._lock:
            runtime = self._read_settings_file(self.settings_path)
            if "mode" in patch:
                runtime["mode"] = patch["mode"]
                runtime.pop("global", None)
            if "global" in patch:
                runtime["mode"] = ("full" if patch["global"] else "ask")
                runtime.pop("global", None)
            for key in ("bots", "actions", "engineCaps"):
                section = patch.get(key)
                if section is not None:
                    current = runtime.get(key)
                    if not isinstance(current, dict):
                        current = {}
                    current.update(section)
                    runtime[key] = current
            if "alwaysAllow" in patch:
                seen = set()
                runtime["alwaysAllow"] = [
                    item for item in patch["alwaysAllow"]
                    if not (item in seen or seen.add(item))]
            directory = os.path.dirname(os.path.abspath(self.settings_path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = self.settings_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(runtime, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.settings_path)
        return self.get_settings()

    def resolve_level(self, requester, action, inherited_level=None) -> str:
        """The effective level for a (bot, action) request.

        Resolution, strongest first: per-bot beats per-action,
        per-action beats the global mode; nothing set anywhere
        means "ask". A "custom" value defers: global "custom"
        falls through to the per-bot / per-action rules, per-bot
        "custom" falls through to the per-action rule, and with
        nothing to fall through to the answer is "ask".

        Delegation: when ``inherited_level`` is given (the level
        the delegating bot works at), the result is capped to
        min(own rules, inherited). A "custom" inheritance carries
        no grant of its own, so it caps nothing. The result is
        then capped by the bot's engine cap (``engineCaps``):
        effective = min(level so far, engine cap). The return is
        always one of "ask" / "edits" / "auto" / "full" — never
        "custom", never an unknown value.
        """
        settings = self.get_settings()
        level = None
        if requester and requester in settings["bots"]:
            level = settings["bots"][requester]
            if level == "custom":
                level = settings["actions"].get(action)
        if level is None:
            level = settings["actions"].get(action)
        if level is None or level == "custom":
            level = settings["mode"]
        if level == "custom":
            level = "ask"
        level = self._normalize_level(level)
        if inherited_level is not None:
            inherited = self._normalize_level(inherited_level)
            if inherited != "custom":
                level = self._min_level(level, inherited)
        if requester and requester in settings["engineCaps"]:
            level = self._min_level(level,
                                    settings["engineCaps"][requester])
        return level

    def resolve_decision(self, requester, action,
                         inherited_level=None) -> dict:
        """The auto-approval decision for a (bot, action) request.

        Returns {"auto": bool, "rule": str, "level": str} where
        "rule" names the rule that decided it — the decision log.
        The allowlist is checked first: an ``alwaysAllow`` action
        is auto-approved at any level. Then the level decides:
        "full" auto-approves everything, "auto" the safe actions
        (plus file edits), "edits" file edits only, "ask" nothing.
        """
        level = self.resolve_level(requester, action, inherited_level)
        settings = self.get_settings()
        if action and action in settings["alwaysAllow"]:
            return {"auto": True, "rule": RULE_COMMAND_ALLOWLIST,
                    "level": level}
        if level == "full":
            return {"auto": True, "rule": RULE_FULL_ACCESS,
                    "level": level}
        if level == "auto" and action in SAFE_ACTIONS:
            return {"auto": True, "rule": RULE_SAFE_ACTION,
                    "level": level}
        if level in ("edits", "auto") and action in EDIT_ACTIONS:
            return {"auto": True, "rule": RULE_FILE_EDIT,
                    "level": level}
        return {"auto": False, "rule": RULE_ASK, "level": level}

    def always_approve_for(self, requester, action) -> bool:
        """Compatibility wrapper: does this (bot, action) request
        auto-approve under the current levels? Kept for callers
        written against the old binary switch; new code should
        use resolve_decision, which also names the deciding rule.
        """
        return self.resolve_decision(requester, action)["auto"]

    # -- public API -------------------------------------------------
    def request_approval(self, action, target, detail="", requester="",
                         ttl_seconds=DEFAULT_TTL_SECONDS,
                         inherited_level=None) -> dict:
        """Request human approval for an action. Returns the record.

        ``action`` is any non-empty string (the six defaults in
        APPROVAL_ACTIONS always need approval; other modules use
        their own action names). Raises ValueError on an empty
        action/target or a bad TTL. When the resolved level
        auto-approves this (requester, action), the record comes
        back already approved with auto=true and ``rule`` naming
        the deciding rule — the gate itself is never skipped: the
        send endpoint still needs this approval id.

        ``inherited_level`` is INTERNAL: the level a delegating
        bot (Chief) works at, passed only by delegation code. It
        is never read from a request — a bot cannot raise its own
        level by asking, and the HTTP endpoint never passes it.
        """
        action = _validate_text(action, "action", MAX_ACTION_CHARS)
        target = _validate_text(target, "target", MAX_TARGET_CHARS)
        detail = _validate_text(detail, "detail", MAX_DETAIL_CHARS,
                                required=False) or ""
        requester = _validate_text(requester, "requester",
                                   MAX_REQUESTER_CHARS, required=False) or ""
        if isinstance(ttl_seconds, bool) or not isinstance(
                ttl_seconds, (int, float)):
            raise ValueError("ttl_seconds must be a number")
        ttl_seconds = int(ttl_seconds)
        if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
            raise ValueError("ttl_seconds must be between 1 and %d"
                             % MAX_TTL_SECONDS)
        decision = self.resolve_decision(requester, action,
                                         inherited_level)
        auto = decision["auto"]
        rule = decision["rule"]
        with self._lock:
            self._sweep_locked()
            rec_id = "appr-%d" % self._data["next_id"]
            self._data["next_id"] += 1
            now = _utcnow()
            record = {
                "id": rec_id,
                "action": action,
                "target": target,
                "detail": detail,
                "requester": requester,
                "status": "approved" if auto else "pending",
                "created_at": _iso(now),
                "decided_at": _iso(now) if auto else None,
                "decided_by": "auto" if auto else None,
                "reason": rule if auto else None,
                "rule": rule,
                "expires_at": _iso(now + timedelta(seconds=ttl_seconds)),
                "consumed": False,
                "auto": auto,
                "history": [{
                    "event": "approved" if auto else "requested",
                    "by": "auto" if auto else (requester
                                               or "unknown"),
                    "at": _iso(now),
                    "reason": rule if auto else None,
                    "auto": auto,
                    "rule": rule}],
            }
            self._data["approvals"].append(record)
            self._save()
            return copy.deepcopy(record)

    def list_approvals(self, status=None) -> list:
        """All records (optionally filtered by status), newest last.

        Reading sweeps expired pending records first, so a list
        never shows a dead approval as still pending.
        """
        with self._lock:
            if self._sweep_locked():
                self._save()
            records = self._data["approvals"]
            if status is not None:
                if status not in APPROVAL_STATUSES:
                    raise ValueError("status must be one of %s"
                                     % (list(APPROVAL_STATUSES),))
                records = [r for r in records if r["status"] == status]
            return copy.deepcopy(records)

    def get(self, approval_id) -> dict | None:
        """One record by id, or None. Sweeps expiry first."""
        with self._lock:
            if self._sweep_locked():
                self._save()
            rec = self._find_locked(approval_id)
            return copy.deepcopy(rec) if rec is not None else None

    def approve(self, approval_id, by="", reason="") -> dict:
        """Approve a pending record. Returns the updated record.

        Raises KeyError for an unknown id, ValueError when the
        record is not pending (already decided or expired).
        """
        by = _validate_text(by, "by", MAX_BY_CHARS, required=False) or ""
        reason = _validate_text(reason, "reason", MAX_REASON_CHARS,
                                required=False) or ""
        with self._lock:
            if self._sweep_locked():
                self._save()
            rec = self._find_locked(approval_id)
            if rec is None:
                raise KeyError("unknown approval %r" % approval_id)
            if rec["status"] != "pending":
                raise ValueError("approval %s is %s, not pending"
                                 % (approval_id, rec["status"]))
            now = _utcnow()
            rec["status"] = "approved"
            rec["decided_at"] = _iso(now)
            rec["decided_by"] = by
            rec["reason"] = reason or None
            rec["rule"] = RULE_HUMAN_DECISION
            rec["history"].append({
                "event": "approved", "by": by or "unknown",
                "at": _iso(now), "reason": reason or None,
                "rule": RULE_HUMAN_DECISION})
            self._save()
            return copy.deepcopy(rec)

    def deny(self, approval_id, by="", reason="") -> dict:
        """Deny a pending record. Returns the updated record.

        Raises KeyError for an unknown id, ValueError when the
        record is not pending.
        """
        by = _validate_text(by, "by", MAX_BY_CHARS, required=False) or ""
        reason = _validate_text(reason, "reason", MAX_REASON_CHARS,
                                required=False) or ""
        with self._lock:
            if self._sweep_locked():
                self._save()
            rec = self._find_locked(approval_id)
            if rec is None:
                raise KeyError("unknown approval %r" % approval_id)
            if rec["status"] != "pending":
                raise ValueError("approval %s is %s, not pending"
                                 % (approval_id, rec["status"]))
            now = _utcnow()
            rec["status"] = "denied"
            rec["decided_at"] = _iso(now)
            rec["decided_by"] = by
            rec["reason"] = reason or None
            rec["rule"] = RULE_HUMAN_DECISION
            rec["history"].append({
                "event": "denied", "by": by or "unknown",
                "at": _iso(now), "reason": reason or None,
                "rule": RULE_HUMAN_DECISION})
            self._save()
            return copy.deepcopy(rec)

    def consume(self, approval_id, note="") -> dict:
        """Spend an approved record exactly once. Returns it.

        Raises KeyError for an unknown id; ValueError when the
        record is not approved, already consumed, or expired
        (the sweep runs first, so an expired approval fails here
        as expired, never as approved).
        """
        note = _validate_text(note, "note", MAX_REASON_CHARS,
                              required=False) or ""
        with self._lock:
            if self._sweep_locked():
                self._save()
            rec = self._find_locked(approval_id)
            if rec is None:
                raise KeyError("unknown approval %r" % approval_id)
            if rec["status"] == "expired":
                raise ValueError("approval %s expired before use"
                                 % approval_id)
            if rec["status"] != "approved":
                raise ValueError("approval %s is %s, not approved"
                                 % (approval_id, rec["status"]))
            if rec.get("consumed"):
                raise ValueError("approval %s was already consumed"
                                 % approval_id)
            now = _utcnow()
            rec["consumed"] = True
            rec["history"].append({
                "event": "consumed", "by": "system",
                "at": _iso(now), "reason": note or None})
            self._save()
            return copy.deepcopy(rec)

    def pending_summary(self, now=None) -> dict:
        """Pending count + age of the oldest pending item.

        Used by the /api/health check: warn when any pending item
        is older than HEALTH_WARN_AFTER_SECONDS (1 hour).
        """
        if now is None:
            now = _utcnow().timestamp()
        with self._lock:
            if self._sweep_locked():
                self._save()
            pending = [r for r in self._data["approvals"]
                       if r["status"] == "pending"]
        oldest_age = None
        for rec in pending:
            created = _parse_iso(rec.get("created_at"))
            if created is not None:
                age = max(0.0, now - created)
                if oldest_age is None or age > oldest_age:
                    oldest_age = age
        return {"pending": len(pending),
                "oldest_pending_age_seconds": oldest_age}
