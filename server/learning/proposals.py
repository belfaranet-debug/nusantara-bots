"""Learning loop — proposals to change a skill, gated by explicit humans.

A skill change is high-stakes: a bad edit silently degrades every
character that loads the skill. So a proposal here is NEVER applied
on its own authority. The flow is:

    propose()  -> record (pending-approval) + an approval request
    approve()  -> a HUMAN approves that request (approvals/queue.py)
    apply_proposal() -> only if the approval is approved AND explicit

The one hard rule, decided for this build (see learning/README.md):
an approval produced by an always-approve / auto-approve mode MUST
NOT authorize a skill change. ``apply_proposal`` refuses any
approval whose ``auto`` flag is True, with a plain reason, and the
proposal stays pending-approval. A skill update needs a human who
looked at it and said yes — an auto-approve mode never counts as
that human. The owner can change this rule later; the refusal and
the reason for it are documented in the README, not hidden.

Honesty boundary
----------------
This module is scaffolding + approval flow, NOT a proposer. The
``proposed_change`` text comes from the caller — a human, or a
model call made through the browser (BYO-API). The server never
invents a skill change by itself. ``apply_proposal`` only bumps
the frontmatter version and appends a changelog line; the actual
new skill text is supplied by the caller in ``proposed_change``
and is stored verbatim on the proposal record. What "applying"
means for the SKILL.md body beyond version + changelog is
deliberately narrow: the caller (human or browser model) edits
the body, this module records that a human approved the change
and stamps the version. See README.md.

Approval action
---------------
Proposals request the ``skill-update`` action from the approval
queue, target = the skill name. The queue accepts any non-empty
action name, so ``skill-update`` is requested directly; the
``change-production-data`` fallback below is kept only as
defense for an older queue that still whitelists actions, and
which action was actually requested is recorded on the proposal
(``approval_action`` + ``approval_action_fallback``).

Zero-dependency, Python standard library only. Persistence is the
same atomic-JSON-under-a-lock pattern as ``office/kanban.py``.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LEARNING_FILE = os.path.join(_REPO_ROOT, "data",
                                     "learning_proposals.json")
DEFAULT_SHELF_DIR = os.path.join(
    os.path.dirname(_REPO_ROOT), "grokbot", "skill-pack")

# The approval action a skill change SHOULD request. See the module
# docstring: the queue may not allow it yet, hence the fallback.
PREFERRED_APPROVAL_ACTION = "skill-update"
FALLBACK_APPROVAL_ACTION = "change-production-data"

# Refusal reason when an auto-approved approval is presented.
# Kept as one constant so the code, the README, and the tests all
# quote the same sentence.
AUTO_APPROVAL_REFUSAL = (
    "skill-update membutuhkan persetujuan eksplisit manusia; "
    "auto-approve tidak berlaku"
)

PROPOSAL_STATUSES = ("draft", "pending-approval", "applied", "rejected")

MAX_SKILL_CHARS = 128
MAX_CHANGE_CHARS = 8192
MAX_REASON_CHARS = 2048
MAX_REF_CHARS = 256
MAX_BY_CHARS = 128

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


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


def bump_patch(version):
    """Bump a ``X.Y.Z`` version's patch number. Raises ValueError
    on anything that is not a plain three-part version."""
    if not isinstance(version, str):
        raise ValueError("version must be a string, got %r" % (version,))
    m = _VERSION_RE.match(version.strip())
    if not m:
        raise ValueError("version %r is not X.Y.Z" % (version,))
    major, minor, patch = (int(g) for g in m.groups())
    return "%d.%d.%d" % (major, minor, patch + 1)


def _read_skill_version(skill_path):
    """Current version from a SKILL.md frontmatter, or None.

    Accepts both ``version: 1.0.0`` and the shelf's
    ``metadata:`` / ``version: 1.0.0`` form (via the loader's
    own frontmatter parser — never a second parser).
    """
    try:
        text = Path(skill_path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        from loader.frontmatter import parse_frontmatter
    except ImportError:
        return None
    data, _body, errors = parse_frontmatter(text)
    if errors:
        return None
    if isinstance(data.get("version"), str) and data["version"]:
        return data["version"]
    metadata = data.get("metadata")
    if isinstance(metadata, dict) and isinstance(
            metadata.get("version"), str) and metadata["version"]:
        return metadata["version"]
    return None


def _bump_version_in_text(text, old_version, new_version):
    """Replace the version line in a SKILL.md frontmatter.

    Handles both forms (top-level ``version:`` and nested
    ``metadata:`` / ``version:``). Raises ValueError if no
    version line carrying ``old_version`` is found, so an apply
    never silently stamps the wrong file.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md has no frontmatter block")
    closing = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing = i
            break
    if closing is None:
        raise ValueError("SKILL.md frontmatter is not closed")
    replaced = False
    for i in range(1, closing):
        line = lines[i]
        m = re.match(r"^(\s*version:\s*)([\"']?)(.+?)([\"']?)\s*$", line)
        if m and m.group(3).strip() == old_version:
            lines[i] = "%s%s%s%s" % (m.group(1), m.group(2),
                                     new_version, m.group(4))
            replaced = True
            break
    if not replaced:
        raise ValueError(
            "no version line for %r found in SKILL.md frontmatter"
            % old_version)
    return "\n".join(lines) + "\n"


def _append_changelog(text, new_version, entry):
    """Append a changelog bullet for the new version.

    If the body already has a ``## Changelog`` section, the bullet
    goes right under its heading; otherwise a section is appended
    at the end of the file.
    """
    lines = text.splitlines()
    bullet = "- %s: %s" % (new_version, entry)
    for i, line in enumerate(lines):
        if line.strip().lower() == "## changelog":
            lines.insert(i + 1, bullet)
            return "\n".join(lines) + "\n"
    if lines and lines[-1].strip():
        lines.append("")
    lines.append("## Changelog")
    lines.append("")
    lines.append(bullet)
    return "\n".join(lines) + "\n"


class LearningLoop:
    """Skill-change proposals, persisted as one JSON file.

    ``approvals`` is an ``ApprovalQueue`` (injectable so tests can
    pass a temp one); ``shelf_dir`` is the skill shelf whose
    SKILL.md files get version-stamped on apply (injectable so
    tests use a temp shelf, never the real one).
    """

    def __init__(self, path=None, approvals=None, shelf_dir=None):
        self.path = path or os.environ.get(
            "GROKBOT_LEARNING_FILE", DEFAULT_LEARNING_FILE)
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._data = self._load()
        if approvals is None:
            from approvals.queue import ApprovalQueue
            approvals = ApprovalQueue()
        self.approvals = approvals
        self.shelf_dir = shelf_dir or os.environ.get(
            "GROKBOT_SKILL_SHELF", DEFAULT_SHELF_DIR)

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {"proposals": [], "next_id": 1}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (isinstance(data, dict)
                    and isinstance(data.get("proposals"), list)
                    and isinstance(data.get("next_id"), int)):
                return data
        except (OSError, ValueError):
            pass
        return {"proposals": [], "next_id": 1}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _find_locked(self, proposal_id):
        for rec in self._data["proposals"]:
            if rec["id"] == proposal_id:
                return rec
        return None

    def _skill_path(self, skill):
        return os.path.join(self.shelf_dir, skill, "SKILL.md")

    def _request_approval(self, skill, reason, requester):
        """Ask the approval queue for a skill-update.

        Tries ``skill-update`` first; if the queue does not allow
        that action yet, falls back to ``change-production-data``
        and says so in the returned record (``action`` + the
        ``fallback`` flag). Returns (record, used_action, fell_back).
        """
        detail = "Proposed skill change: %s" % reason
        try:
            rec = self.approvals.request_approval(
                PREFERRED_APPROVAL_ACTION, skill, detail=detail,
                requester=requester)
            return rec, PREFERRED_APPROVAL_ACTION, False
        except ValueError:
            rec = self.approvals.request_approval(
                FALLBACK_APPROVAL_ACTION, skill, detail=detail,
                requester=requester)
            return rec, FALLBACK_APPROVAL_ACTION, True

    # -- public API -------------------------------------------------
    def propose(self, skill, proposed_change, reason,
                source_card_id=None, requester="") -> dict:
        """Create a proposal + its approval request.

        ``proposed_change`` is supplied by the caller (human, or a
        browser/BYO model call) — this module never invents it.
        Returns the proposal record (a copy), status
        ``pending-approval``, carrying ``approval_id`` and the
        ``approval_action`` actually requested.
        """
        skill = _validate_text(skill, "skill", MAX_SKILL_CHARS)
        proposed_change = _validate_text(
            proposed_change, "proposed_change", MAX_CHANGE_CHARS)
        reason = _validate_text(reason, "reason", MAX_REASON_CHARS)
        if source_card_id is not None:
            source_card_id = _validate_text(
                source_card_id, "source_card_id", MAX_REF_CHARS)
        requester = _validate_text(
            requester, "requester", MAX_BY_CHARS, required=False) or ""
        version_from = _read_skill_version(self._skill_path(skill))
        try:
            version_to = (bump_patch(version_from)
                          if version_from is not None else None)
        except ValueError:
            version_to = None
        approval, action_used, fell_back = self._request_approval(
            skill, reason, requester)
        with self._lock:
            prop_id = "learn-%d" % self._data["next_id"]
            self._data["next_id"] += 1
            now = _iso(_utcnow())
            record = {
                "id": prop_id,
                "skill": skill,
                "proposed_change": proposed_change,
                "reason": reason,
                "source_card_id": source_card_id,
                "status": "pending-approval",
                "version_from": version_from,
                "version_to": version_to,
                "changelog": None,
                "approval_id": approval["id"],
                "approval_action": action_used,
                "approval_action_fallback": fell_back,
                "created_at": now,
                "decided_at": None,
                "decided_by": None,
                "history": [{
                    "event": "proposed", "by": requester or "unknown",
                    "at": now, "reason": reason}],
            }
            self._data["proposals"].append(record)
            self._save()
            return copy.deepcopy(record)

    def list_proposals(self, status=None) -> list:
        with self._lock:
            records = self._data["proposals"]
            if status is not None:
                if status not in PROPOSAL_STATUSES:
                    raise ValueError("status must be one of %s"
                                     % (list(PROPOSAL_STATUSES),))
                records = [r for r in records if r["status"] == status]
            return copy.deepcopy(records)

    def get_proposal(self, proposal_id) -> dict | None:
        with self._lock:
            rec = self._find_locked(proposal_id)
            return copy.deepcopy(rec) if rec is not None else None

    def apply_proposal(self, proposal_id, approval) -> dict:
        """Apply an approved proposal: stamp the new version on the
        skill's SKILL.md + append a changelog line.

        ``approval`` is the approval record dict (from the queue).
        Refuses, in order, with a clear ValueError:
        * unknown proposal, or proposal not pending-approval;
        * approval missing / id mismatch / not ``approved``;
        * approval ``auto == True`` — an always-approve mode can
          never authorize a skill change (see module docstring).
          The proposal stays pending-approval; nothing is written
          and the approval is NOT consumed.
        On success the approval is consumed exactly once (when it
        lives in this queue), the SKILL.md is rewritten atomically,
        and the proposal becomes ``applied`` with its changelog.
        """
        if not isinstance(approval, dict):
            raise ValueError("approval must be an approval record dict")
        with self._lock:
            rec = self._find_locked(proposal_id)
            if rec is None:
                raise KeyError("unknown proposal %r" % proposal_id)
            if rec["status"] != "pending-approval":
                raise ValueError("proposal %s is %s, not pending-approval"
                                 % (proposal_id, rec["status"]))
            approval_id = approval.get("id")
            if approval_id != rec.get("approval_id"):
                raise ValueError(
                    "approval %r does not match this proposal's "
                    "approval %r" % (approval_id, rec.get("approval_id")))
            if approval.get("status") != "approved":
                raise ValueError(
                    "approval %s is %s, not approved"
                    % (approval_id, approval.get("status")))
            if approval.get("auto") is True:
                rec["history"].append({
                    "event": "auto-approval-refused",
                    "by": "system", "at": _iso(_utcnow()),
                    "reason": AUTO_APPROVAL_REFUSAL})
                self._save()
                raise ValueError(AUTO_APPROVAL_REFUSAL)
            if approval.get("consumed"):
                raise ValueError("approval %s was already consumed"
                                 % approval_id)
            skill_path = self._skill_path(rec["skill"])
            version_from = _read_skill_version(skill_path)
            if version_from is None:
                raise ValueError(
                    "cannot apply: SKILL.md for skill %r not found "
                    "or has no version" % rec["skill"])
            if (rec.get("version_from") is not None
                    and version_from != rec["version_from"]):
                raise ValueError(
                    "cannot apply: skill %r is now at version %s, "
                    "proposal was made against %s"
                    % (rec["skill"], version_from, rec["version_from"]))
            version_to = bump_patch(version_from)
            try:
                text = Path(skill_path).read_text(encoding="utf-8")
            except OSError as e:
                raise ValueError("cannot read SKILL.md for skill %r: %s"
                                 % (rec["skill"], e))
            new_text = _bump_version_in_text(text, version_from, version_to)
            entry = ("%s (proposal %s, approved by %s)"
                     % (rec["reason"],
                        rec["id"],
                        approval.get("decided_by") or "human"))
            new_text = _append_changelog(new_text, version_to, entry)
            tmp = skill_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new_text)
            os.replace(tmp, skill_path)
            # Spend the approval exactly once, when it lives in
            # this queue (a hand-built dict in tests has no queue
            # record and is simply left alone).
            if self.approvals is not None:
                try:
                    if self.approvals.get(approval_id) is not None:
                        self.approvals.consume(
                            approval_id,
                            note="applied to skill %s (%s -> %s)"
                                 % (rec["skill"], version_from, version_to))
                except (KeyError, ValueError):
                    pass
            now = _iso(_utcnow())
            rec["status"] = "applied"
            rec["version_from"] = version_from
            rec["version_to"] = version_to
            rec["changelog"] = entry
            rec["decided_at"] = now
            rec["decided_by"] = approval.get("decided_by") or "human"
            rec["history"].append({
                "event": "applied", "by": rec["decided_by"],
                "at": now,
                "reason": "%s -> %s" % (version_from, version_to)})
            self._save()
            return copy.deepcopy(rec)

    def apply_create_proposal(self, proposal_id, approval,
                              version="1.0.0", changelog_entry=None) -> dict:
        """Mark a CREATE proposal applied (no SKILL.md to stamp).

        Used only by ``learning/learn.py`` for a ``/learn`` draft
        that creates a brand-new skill: there is no existing file
        for ``apply_proposal`` to bump, so the caller writes the
        new SKILL.md first and this method records the decision.
        It enforces exactly the same gate as ``apply_proposal`` —
        unknown / not pending / id mismatch / not approved /
        auto-approved (refused with ``AUTO_APPROVAL_REFUSAL``,
        proposal stays pending-approval, approval NOT consumed)
        / already consumed — and consumes the approval exactly
        once on success. It writes nothing to the shelf itself.
        """
        if not isinstance(approval, dict):
            raise ValueError("approval must be an approval record dict")
        with self._lock:
            rec = self._find_locked(proposal_id)
            if rec is None:
                raise KeyError("unknown proposal %r" % proposal_id)
            if rec["status"] != "pending-approval":
                raise ValueError("proposal %s is %s, not pending-approval"
                                 % (proposal_id, rec["status"]))
            approval_id = approval.get("id")
            if approval_id != rec.get("approval_id"):
                raise ValueError(
                    "approval %r does not match this proposal's "
                    "approval %r" % (approval_id, rec.get("approval_id")))
            if approval.get("status") != "approved":
                raise ValueError(
                    "approval %s is %s, not approved"
                    % (approval_id, approval.get("status")))
            if approval.get("auto") is True:
                rec["history"].append({
                    "event": "auto-approval-refused",
                    "by": "system", "at": _iso(_utcnow()),
                    "reason": AUTO_APPROVAL_REFUSAL})
                self._save()
                raise ValueError(AUTO_APPROVAL_REFUSAL)
            if approval.get("consumed"):
                raise ValueError("approval %s was already consumed"
                                 % approval_id)
            if self.approvals is not None:
                try:
                    if self.approvals.get(approval_id) is not None:
                        self.approvals.consume(
                            approval_id,
                            note="applied to new skill %s (%s)"
                                 % (rec["skill"], version))
                except (KeyError, ValueError):
                    pass
            now = _iso(_utcnow())
            rec["status"] = "applied"
            rec["version_from"] = None
            rec["version_to"] = version
            rec["changelog"] = (changelog_entry
                                if changelog_entry is not None
                                else rec["reason"])
            rec["decided_at"] = now
            rec["decided_by"] = approval.get("decided_by") or "human"
            rec["history"].append({
                "event": "applied", "by": rec["decided_by"],
                "at": now,
                "reason": "created at version %s" % version})
            self._save()
            return copy.deepcopy(rec)

    def reject_proposal(self, proposal_id, by="", reason="") -> dict:
        """Reject a pending proposal. It is never applied afterwards.

        Raises KeyError for an unknown id, ValueError when the
        proposal is not pending-approval.
        """
        by = _validate_text(by, "by", MAX_BY_CHARS, required=False) or ""
        reason = _validate_text(reason, "reason", MAX_REASON_CHARS,
                                required=False) or ""
        with self._lock:
            rec = self._find_locked(proposal_id)
            if rec is None:
                raise KeyError("unknown proposal %r" % proposal_id)
            if rec["status"] != "pending-approval":
                raise ValueError("proposal %s is %s, not pending-approval"
                                 % (proposal_id, rec["status"]))
            now = _iso(_utcnow())
            rec["status"] = "rejected"
            rec["decided_at"] = now
            rec["decided_by"] = by
            rec["history"].append({
                "event": "rejected", "by": by or "unknown",
                "at": now, "reason": reason or None})
            self._save()
            return copy.deepcopy(rec)
