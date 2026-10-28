"""Tests for the learning loop (stdlib unittest only).

Run from ~/workspace/grokbot-own/:
    python3 -m unittest discover -s learning/tests -v

Tests use temp files only: a temp approval queue, a temp shelf
with throwaway SKILL.md files, and a temp proposals store. The
real shelf (~/workspace/grokbot/skill-pack/) is never touched.
No network, no model calls, no credentials.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from approvals.queue import ApprovalQueue
from learning.proposals import (
    AUTO_APPROVAL_REFUSAL,
    LearningLoop,
    bump_patch,
)

SKILL_TOP = """---
name: demo-skill
description: A demo skill for tests.
version: 1.2.3
---

# Demo Skill

Do the demo thing.
"""

SKILL_NESTED = """---
name: demo-skill
description: A demo skill for tests.
metadata:
  version: "2.0.0"
---

# Demo Skill

Do the demo thing.
"""


class LearningTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="learning-test-"))
        self.shelf = self.tmp / "shelf"
        self.skill_dir = self.shelf / "demo-skill"
        self.skill_dir.mkdir(parents=True)
        (self.skill_dir / "SKILL.md").write_text(SKILL_TOP, encoding="utf-8")
        # Always-approve is ON globally in the repo's
        # approvals/defaults.json (the owner's choice, 25 Okt 2026) — so
        # tests must point the queue at temp settings files with
        # it explicitly OFF, or every request comes back
        # auto-approved and explicit-approve tests cannot run.
        off = {"global": False, "bots": {}, "actions": {}}
        self.defaults_path = str(self.tmp / "defaults.json")
        self.settings_path = str(self.tmp / "settings.json")
        Path(self.defaults_path).write_text(json.dumps(off),
                                            encoding="utf-8")
        Path(self.settings_path).write_text(json.dumps(off),
                                            encoding="utf-8")
        self.approvals = ApprovalQueue(
            path=str(self.tmp / "approvals.json"),
            settings_path=self.settings_path,
            defaults_path=self.defaults_path)
        self.loop = LearningLoop(
            path=str(self.tmp / "learning.json"),
            approvals=self.approvals,
            shelf_dir=str(self.shelf))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _approved(self, proposal, auto=False):
        rec = self.approvals.approve(
            proposal["approval_id"], by="tester", reason="looks good")
        if auto:
            rec = dict(rec, auto=True)
        return rec


class TestBumpPatch(unittest.TestCase):
    def test_bumps_patch_only(self):
        self.assertEqual(bump_patch("1.2.3"), "1.2.4")
        self.assertEqual(bump_patch("0.0.9"), "0.0.10")

    def test_rejects_non_semver(self):
        for bad in ("1.2", "v1.2.3", "1.2.3.4", "", None, 123):
            with self.assertRaises(ValueError, msg=repr(bad)):
                bump_patch(bad)


class TestPropose(LearningTestBase):
    def test_propose_creates_pending_proposal_and_approval(self):
        prop = self.loop.propose(
            "demo-skill", "Add a worked example to the workflow.",
            "The workflow has no example; users guess.",
            source_card_id="card-7", requester="chief-officer")
        self.assertEqual(prop["id"], "learn-1")
        self.assertEqual(prop["status"], "pending-approval")
        self.assertEqual(prop["skill"], "demo-skill")
        self.assertEqual(prop["version_from"], "1.2.3")
        self.assertEqual(prop["version_to"], "1.2.4")
        self.assertEqual(prop["source_card_id"], "card-7")
        self.assertIsNotNone(prop["approval_id"])
        approval = self.approvals.get(prop["approval_id"])
        self.assertIsNotNone(approval)
        self.assertEqual(approval["status"], "pending")
        self.assertEqual(approval["target"], "demo-skill")
        # Action actually requested is recorded on the proposal.
        self.assertEqual(prop["approval_action"],
                         approval["action"])
        self.assertIn(prop["approval_action"],
                      ("skill-update", "change-production-data"))

    def test_propose_reads_nested_metadata_version(self):
        (self.skill_dir / "SKILL.md").write_text(SKILL_NESTED,
                                                 encoding="utf-8")
        prop = self.loop.propose("demo-skill", "change", "reason")
        self.assertEqual(prop["version_from"], "2.0.0")
        self.assertEqual(prop["version_to"], "2.0.1")

    def test_propose_unknown_skill_has_no_version(self):
        prop = self.loop.propose("no-such-skill", "change", "reason")
        self.assertIsNone(prop["version_from"])
        self.assertIsNone(prop["version_to"])

    def test_propose_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            self.loop.propose("", "change", "reason")
        with self.assertRaises(ValueError):
            self.loop.propose("demo-skill", "", "reason")
        with self.assertRaises(ValueError):
            self.loop.propose("demo-skill", "change", "")
        with self.assertRaises(ValueError):
            self.loop.propose("demo-skill", "change", "reason",
                              source_card_id="x" * 257)

    def test_malformed_store_loads_empty(self):
        Path(self.tmp / "learning.json").write_text("{broken", encoding="utf-8")
        loop = LearningLoop(path=str(self.tmp / "learning.json"),
                            approvals=self.approvals,
                            shelf_dir=str(self.shelf))
        self.assertEqual(loop.list_proposals(), [])


class TestApply(LearningTestBase):
    def test_explicit_approve_then_apply_bumps_version_and_changelog(self):
        prop = self.loop.propose("demo-skill", "Add an example.",
                                 "No example in the workflow.")
        approved = self._approved(prop)
        applied = self.loop.apply_proposal(prop["id"], approved)
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["version_from"], "1.2.3")
        self.assertEqual(applied["version_to"], "1.2.4")
        self.assertIn("No example in the workflow.", applied["changelog"])
        self.assertIn("learn-1", applied["changelog"])
        text = (self.skill_dir / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("version: 1.2.4", text)
        self.assertIn("## Changelog", text)
        self.assertIn("- 1.2.4:", text)
        self.assertIn("Do the demo thing.", text)  # body untouched
        # The approval was spent exactly once.
        self.assertTrue(
            self.approvals.get(prop["approval_id"])["consumed"])
        # A second apply is refused: the proposal is no longer pending.
        with self.assertRaises(ValueError):
            self.loop.apply_proposal(prop["id"], approved)

    def test_apply_bumps_nested_metadata_version(self):
        (self.skill_dir / "SKILL.md").write_text(SKILL_NESTED,
                                                 encoding="utf-8")
        prop = self.loop.propose("demo-skill", "change", "reason")
        self.loop.apply_proposal(prop["id"], self._approved(prop))
        text = (self.skill_dir / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('version: "2.0.1"', text)

    def test_auto_approved_apply_is_refused(self):
        prop = self.loop.propose("demo-skill", "Add an example.",
                                 "No example in the workflow.")
        auto = self._approved(prop, auto=True)
        with self.assertRaises(ValueError) as ctx:
            self.loop.apply_proposal(prop["id"], auto)
        self.assertEqual(str(ctx.exception), AUTO_APPROVAL_REFUSAL)
        # Nothing changed: proposal still pending, file untouched,
        # approval NOT consumed (a human can still decide).
        kept = self.loop.get_proposal(prop["id"])
        self.assertEqual(kept["status"], "pending-approval")
        self.assertTrue(any(
            h["event"] == "auto-approval-refused"
            for h in kept["history"]))
        text = (self.skill_dir / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("version: 1.2.3", text)
        self.assertNotIn("## Changelog", text)
        self.assertFalse(
            self.approvals.get(prop["approval_id"])["consumed"])

    def test_real_always_approve_request_is_refused_on_apply(self):
        # The live configuration: always-approve ON globally
        # (approvals/defaults.json, the owner's choice). A proposal made
        # under it comes back already approved with auto=True —
        # and apply must still refuse it.
        on = {"global": True, "bots": {}, "actions": {}}
        on_defaults = str(self.tmp / "on-defaults.json")
        on_settings = str(self.tmp / "on-settings.json")
        Path(on_defaults).write_text(json.dumps(on), encoding="utf-8")
        Path(on_settings).write_text(json.dumps(on), encoding="utf-8")
        auto_queue = ApprovalQueue(
            path=str(self.tmp / "auto-approvals.json"),
            settings_path=on_settings, defaults_path=on_defaults)
        auto_loop = LearningLoop(
            path=str(self.tmp / "auto-learning.json"),
            approvals=auto_queue, shelf_dir=str(self.shelf))
        prop = auto_loop.propose("demo-skill", "Add an example.",
                                 "No example in the workflow.")
        approval = auto_queue.get(prop["approval_id"])
        self.assertEqual(approval["status"], "approved")
        self.assertIs(approval["auto"], True)
        with self.assertRaises(ValueError) as ctx:
            auto_loop.apply_proposal(prop["id"], approval)
        self.assertEqual(str(ctx.exception), AUTO_APPROVAL_REFUSAL)
        text = (self.skill_dir / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("version: 1.2.3", text)

    def test_apply_refuses_pending_or_denied_approval(self):
        prop = self.loop.propose("demo-skill", "change", "reason")
        pending = self.approvals.get(prop["approval_id"])
        with self.assertRaises(ValueError):
            self.loop.apply_proposal(prop["id"], pending)
        self.approvals.deny(prop["approval_id"], by="tester", reason="no")
        denied = self.approvals.get(prop["approval_id"])
        with self.assertRaises(ValueError):
            self.loop.apply_proposal(prop["id"], denied)

    def test_apply_refuses_wrong_approval_id(self):
        first = self.loop.propose("demo-skill", "change one", "reason")
        second = self.loop.propose("demo-skill", "change two", "reason")
        with self.assertRaises(ValueError):
            self.loop.apply_proposal(first["id"],
                                     self._approved(second))

    def test_apply_refuses_unknown_skill(self):
        prop = self.loop.propose("no-such-skill", "change", "reason")
        with self.assertRaises(ValueError):
            self.loop.apply_proposal(prop["id"], self._approved(prop))

    def test_apply_refuses_version_drift(self):
        prop = self.loop.propose("demo-skill", "change", "reason")
        # Someone else bumped the skill meanwhile.
        text = (self.skill_dir / "SKILL.md").read_text(encoding="utf-8")
        (self.skill_dir / "SKILL.md").write_text(
            text.replace("version: 1.2.3", "version: 1.2.4"),
            encoding="utf-8")
        with self.assertRaises(ValueError):
            self.loop.apply_proposal(prop["id"], self._approved(prop))


class TestReject(LearningTestBase):
    def test_rejected_proposal_is_never_applied(self):
        prop = self.loop.propose("demo-skill", "change", "reason")
        rejected = self.loop.reject_proposal(prop["id"], by="tester",
                                             reason="too risky")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["decided_by"], "tester")
        with self.assertRaises(ValueError):
            self.loop.apply_proposal(prop["id"], self._approved(prop))
        text = (self.skill_dir / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("version: 1.2.3", text)

    def test_reject_unknown_or_decided_raises(self):
        with self.assertRaises(KeyError):
            self.loop.reject_proposal("learn-99", by="tester")
        prop = self.loop.propose("demo-skill", "change", "reason")
        self.loop.reject_proposal(prop["id"], by="tester")
        with self.assertRaises(ValueError):
            self.loop.reject_proposal(prop["id"], by="tester")

    def test_list_filters_by_status(self):
        a = self.loop.propose("demo-skill", "one", "reason")
        self.loop.propose("demo-skill", "two", "reason")
        self.loop.reject_proposal(a["id"], by="tester")
        self.assertEqual(len(self.loop.list_proposals()), 2)
        self.assertEqual(len(self.loop.list_proposals("rejected")), 1)
        self.assertEqual(
            len(self.loop.list_proposals("pending-approval")), 1)


class TestZeroTrace(unittest.TestCase):
    def test_no_forbidden_brand_strings_in_new_files(self):
        banned = ["rak" + "azo", "octo" + "bot", "open" + "dots"]
        root = Path(__file__).resolve().parents[2]
        for path in (root / "learning").rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            text = path.read_text(encoding="utf-8").lower()
            for word in banned:
                self.assertNotIn(word, text, str(path))
        for path in (root / "learning").rglob("*.md"):
            text = path.read_text(encoding="utf-8").lower()
            for word in banned:
                self.assertNotIn(word, text, str(path))


if __name__ == "__main__":
    unittest.main()
