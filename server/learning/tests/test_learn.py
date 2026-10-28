"""Tests for the /learn loop (stdlib unittest only).

Run from ~/workspace/grokbot-own/:
    python3 -m unittest discover -s learning/tests -v

Tests use temp files only: a temp approval queue (always-approve
explicitly OFF, unless a test builds an auto-approve queue on
purpose), a temp shelf with throwaway SKILL.md files, a temp
proposals store, and a temp drafts store. The real shelf
(~/workspace/grokbot/skill-pack/) is never touched. No network,
no model calls, no credentials — distillation is tested through
the validator + draft statuses, never by calling a model.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from approvals.queue import ApprovalQueue
from learning.learn import (
    CREATE_VERSION,
    MAX_DRAFT_BYTES,
    LearnLoop,
    build_distillation_context,
    parse_learn_command,
    validate_draft,
)
from learning.proposals import AUTO_APPROVAL_REFUSAL, LearningLoop

SKILL_TOP = """---
name: demo-skill
description: A demo skill for tests.
version: 1.2.3
---

# Demo Skill

Do the demo thing.
"""

DESC_UPDATE = "Does the demo thing when asked, step by step."
DESC_CREATE = "Teaches the new thing in three short steps."


def make_draft(name="demo-skill", version="1.2.4",
               description=DESC_UPDATE, sections=("When to Use",
               "Procedure", "Pitfalls", "Verification"), extra=""):
    body = "# %s\n\n" % name
    for section in sections:
        body += "## %s\n\nContent for %s.\n\n" % (section, section)
    body += extra
    return ("---\nname: %s\ndescription: %s\nversion: %s\n---\n\n%s"
            % (name, description, version, body))


class LearnTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="learn-test-"))
        self.shelf = self.tmp / "shelf"
        self.skill_dir = self.shelf / "demo-skill"
        self.skill_dir.mkdir(parents=True)
        (self.skill_dir / "SKILL.md").write_text(SKILL_TOP,
                                                 encoding="utf-8")
        # Always-approve is ON globally in the repo's
        # approvals/defaults.json — tests point the queue at temp
        # settings files with it explicitly OFF.
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
        self.learning = LearningLoop(
            path=str(self.tmp / "learning.json"),
            approvals=self.approvals,
            shelf_dir=str(self.shelf))
        self.learn = LearnLoop(
            path=str(self.tmp / "learn.json"),
            learning=self.learning,
            shelf_dir=str(self.shelf))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _approved(self, draft):
        return self.approvals.approve(
            draft["approval_id"], by="tester", reason="looks good")

    def _skill_text(self):
        return (self.skill_dir / "SKILL.md").read_text(encoding="utf-8")

    def _auto_learn(self):
        """A second LearnLoop whose queue has always-approve ON."""
        on = {"global": True, "bots": {}, "actions": {}}
        on_defaults = str(self.tmp / "on-defaults.json")
        on_settings = str(self.tmp / "on-settings.json")
        Path(on_defaults).write_text(json.dumps(on), encoding="utf-8")
        Path(on_settings).write_text(json.dumps(on), encoding="utf-8")
        auto_queue = ApprovalQueue(
            path=str(self.tmp / "auto-approvals.json"),
            settings_path=on_settings, defaults_path=on_defaults)
        auto_learning = LearningLoop(
            path=str(self.tmp / "auto-learning.json"),
            approvals=auto_queue, shelf_dir=str(self.shelf))
        return LearnLoop(
            path=str(self.tmp / "auto-learn.json"),
            learning=auto_learning, shelf_dir=str(self.shelf)), auto_queue


class TestParseLearnCommand(unittest.TestCase):
    def test_parses_skill_and_transcript(self):
        parsed = parse_learn_command(
            "/learn demo-skill from we kept forgetting the checklist")
        self.assertEqual(parsed["skill"], "demo-skill")
        self.assertEqual(parsed["transcript"],
                         "we kept forgetting the checklist")

    def test_parses_skill_without_transcript(self):
        parsed = parse_learn_command("/learn demo-skill")
        self.assertEqual(parsed["skill"], "demo-skill")
        self.assertEqual(parsed["transcript"], "")

    def test_parses_transcript_without_from(self):
        parsed = parse_learn_command("/learn demo-skill hello world")
        self.assertEqual(parsed["transcript"], "hello world")

    def test_rejects_bare_command_and_bad_names(self):
        for bad in ("/learn", "/learn   ", "/learn Demo_Skill",
                    "/learn demo_skill", "/learn -demo",
                    "learn demo-skill", "/learnfoo demo-skill"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_learn_command(bad)

    def test_rejects_non_string(self):
        with self.assertRaises(ValueError):
            parse_learn_command(None)


class TestValidateDraft(unittest.TestCase):
    def test_valid_draft_has_no_violations(self):
        self.assertEqual(validate_draft(make_draft(), skill="demo-skill"),
                         [])

    def test_invalid_name_is_rejected(self):
        draft = make_draft(name="Demo_Skill")
        violations = validate_draft(draft)
        self.assertTrue(any(v.startswith("invalid_name")
                            for v in violations), violations)

    def test_name_mismatch_is_rejected(self):
        violations = validate_draft(make_draft(), skill="other-skill")
        self.assertTrue(any(v.startswith("name_mismatch")
                            for v in violations), violations)

    def test_description_over_60_chars_is_rejected(self):
        long_desc = "A" * 61 + "."
        self.assertGreater(len(long_desc), 60)
        draft = make_draft(description=long_desc)
        violations = validate_draft(draft)
        self.assertTrue(any(v.startswith("description_too_long")
                            for v in violations), violations)

    def test_description_two_sentences_is_rejected(self):
        draft = make_draft(description="Does one thing. Then another.")
        violations = validate_draft(draft)
        self.assertTrue(any(v.startswith("description_not_single_sentence")
                            for v in violations), violations)

    def test_missing_required_section_is_rejected(self):
        draft = make_draft(sections=("When to Use", "Procedure",
                                     "Verification"))
        violations = validate_draft(draft)
        self.assertIn("missing_section: ## Pitfalls", violations)

    def test_draft_over_32kb_is_rejected(self):
        draft = make_draft(extra="x" * (MAX_DRAFT_BYTES + 1))
        self.assertGreater(len(draft.encode("utf-8")), MAX_DRAFT_BYTES)
        violations = validate_draft(draft)
        self.assertTrue(any(v.startswith("draft_too_large")
                            for v in violations), violations)

    def test_unverified_flag_is_rejected(self):
        draft = make_draft(extra="Run it with --dry-run first.\n")
        # No source: the flag cannot be checked, so no violation.
        self.assertEqual(validate_draft(draft), [])
        # Source without the flag: violation.
        violations = validate_draft(
            draft, source_text="we ran it by hand, no flags used")
        self.assertTrue(any(v.startswith("unverified_flag")
                            for v in violations), violations)
        # Source with the flag: clean.
        self.assertEqual(validate_draft(
            draft, source_text="we ran it with --dry-run first"), [])

    def test_missing_frontmatter_and_version(self):
        violations = validate_draft("just prose, no frontmatter")
        self.assertTrue(any(v.startswith("frontmatter_")
                            for v in violations), violations)


class TestCreateDraft(LearnTestBase):
    def test_create_draft_stores_draft_and_touches_nothing(self):
        draft = self.learn.create_draft(
            "demo-skill", make_draft(), transcript="we did the demo")
        self.assertEqual(draft["id"], "learn-draft-1")
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["kind"], "update")
        self.assertEqual(draft["version"], "1.2.4")
        self.assertEqual(draft["version_from"], "1.2.3")
        # The shelf is untouched by creating a draft.
        self.assertIn("version: 1.2.3", self._skill_text())

    def test_create_draft_rejects_invalid_draft(self):
        with self.assertRaises(ValueError) as ctx:
            self.learn.create_draft(
                "demo-skill",
                make_draft(description="A" * 61 + "."))
        self.assertIn("description_too_long", str(ctx.exception))
        self.assertEqual(self.learn.list_drafts(), [])

    def test_create_draft_rejects_wrong_version(self):
        with self.assertRaises(ValueError) as ctx:
            self.learn.create_draft("demo-skill",
                                    make_draft(version="1.2.5"))
        self.assertIn("version_mismatch", str(ctx.exception))

    def test_create_draft_for_new_skill_starts_at_1_0_0(self):
        draft = self.learn.create_draft(
            "new-skill",
            make_draft(name="new-skill", version=CREATE_VERSION,
                       description=DESC_CREATE))
        self.assertEqual(draft["kind"], "create")
        self.assertEqual(draft["version"], "1.0.0")
        self.assertIsNone(draft["version_from"])
        self.assertFalse((self.shelf / "new-skill").exists())


class TestReviewGate(LearnTestBase):
    def test_submit_opens_skill_update_proposal(self):
        draft = self.learn.create_draft("demo-skill", make_draft())
        submitted = self.learn.submit_for_review(draft["id"],
                                                 requester="tester")
        self.assertEqual(submitted["status"], "pending-review")
        self.assertIsNotNone(submitted["proposal_id"])
        proposal = self.learning.get_proposal(
            submitted["proposal_id"])
        self.assertEqual(proposal["status"], "pending-approval")
        self.assertEqual(proposal["approval_action"], "skill-update")
        approval = self.approvals.get(submitted["approval_id"])
        self.assertEqual(approval["status"], "pending")
        self.assertIs(approval["auto"], False)

    def test_update_does_not_touch_active_version_while_pending(self):
        draft = self.learn.create_draft(
            "demo-skill",
            make_draft(extra="NEW STEP that must not go live yet.\n"))
        self.learn.submit_for_review(draft["id"])
        text = self._skill_text()
        self.assertIn("version: 1.2.3", text)
        self.assertNotIn("NEW STEP", text)
        self.assertNotIn("## Changelog", text)

    def test_create_is_not_active_before_approval(self):
        draft = self.learn.create_draft(
            "new-skill",
            make_draft(name="new-skill", version=CREATE_VERSION,
                       description=DESC_CREATE))
        self.learn.submit_for_review(draft["id"])
        kept = self.learn.get_draft(draft["id"])
        self.assertEqual(kept["status"], "pending-review")
        # No file on the shelf at all until a human approves.
        self.assertFalse((self.shelf / "new-skill" / "SKILL.md").exists())

    def test_submit_twice_is_refused(self):
        draft = self.learn.create_draft("demo-skill", make_draft())
        self.learn.submit_for_review(draft["id"])
        with self.assertRaises(ValueError):
            self.learn.submit_for_review(draft["id"])

    def test_reject_draft_rejects_proposal_too(self):
        draft = self.learn.create_draft("demo-skill", make_draft())
        draft = self.learn.submit_for_review(draft["id"])
        rejected = self.learn.reject_draft(draft["id"], by="tester",
                                           reason="too vague")
        self.assertEqual(rejected["status"], "rejected")
        proposal = self.learning.get_proposal(draft["proposal_id"])
        self.assertEqual(proposal["status"], "rejected")
        self.assertIn("version: 1.2.3", self._skill_text())


class TestActivateUndo(LearnTestBase):
    def _pending_update(self):
        draft = self.learn.create_draft(
            "demo-skill",
            make_draft(extra="NEW STEP that goes live on approval.\n"))
        return self.learn.submit_for_review(draft["id"])

    def test_activate_update_writes_new_version(self):
        draft = self._pending_update()
        self._approved(draft)
        activated = self.learn.activate_draft(draft["id"])
        self.assertEqual(activated["status"], "active")
        self.assertEqual(activated["version_to"], "1.2.4")
        text = self._skill_text()
        self.assertIn("version: 1.2.4", text)
        self.assertIn("NEW STEP that goes live on approval.", text)
        self.assertIn("## Changelog", text)
        self.assertIn("- 1.2.4:", text)
        self.assertTrue(
            self.approvals.get(draft["approval_id"])["consumed"])

    def test_activate_refuses_pending_approval(self):
        draft = self._pending_update()
        with self.assertRaises(ValueError):
            self.learn.activate_draft(draft["id"])
        self.assertIn("version: 1.2.3", self._skill_text())
        self.assertEqual(self.learn.get_draft(draft["id"])["status"],
                         "pending-review")

    def test_undo_restores_previous_version(self):
        draft = self._pending_update()
        self._approved(draft)
        self.learn.activate_draft(draft["id"])
        undone = self.learn.undo_draft(draft["id"], by="tester")
        self.assertEqual(undone["status"], "undone")
        text = self._skill_text()
        self.assertIn("version: 1.2.3", text)
        self.assertIn("Do the demo thing.", text)
        self.assertNotIn("NEW STEP", text)
        self.assertIn("Undo:", text)
        # Undo twice is refused: the draft is no longer active.
        with self.assertRaises(ValueError):
            self.learn.undo_draft(draft["id"], by="tester")

    def test_activate_create_then_undo_removes_skill(self):
        draft = self.learn.create_draft(
            "new-skill",
            make_draft(name="new-skill", version=CREATE_VERSION,
                       description=DESC_CREATE))
        draft = self.learn.submit_for_review(draft["id"])
        self._approved(draft)
        activated = self.learn.activate_draft(draft["id"])
        self.assertEqual(activated["status"], "active")
        skill_file = self.shelf / "new-skill" / "SKILL.md"
        self.assertTrue(skill_file.is_file())
        self.assertIn("version: 1.0.0",
                      skill_file.read_text(encoding="utf-8"))
        proposal = self.learning.get_proposal(draft["proposal_id"])
        self.assertEqual(proposal["status"], "applied")
        self.assertEqual(proposal["version_to"], "1.0.0")
        undone = self.learn.undo_draft(draft["id"], by="tester")
        self.assertEqual(undone["status"], "undone")
        self.assertFalse(skill_file.exists())
        self.assertFalse((self.shelf / "new-skill").exists())

    def test_list_drafts_filters_by_status(self):
        a = self.learn.create_draft("demo-skill", make_draft())
        self.learn.create_draft(
            "new-skill",
            make_draft(name="new-skill", version=CREATE_VERSION,
                       description=DESC_CREATE))
        self.learn.reject_draft(a["id"], by="tester")
        self.assertEqual(len(self.learn.list_drafts()), 2)
        self.assertEqual(len(self.learn.list_drafts("rejected")), 1)
        self.assertEqual(len(self.learn.list_drafts("draft")), 1)


class TestAutoApproveRefused(LearnTestBase):
    def test_learn_proposal_rejects_auto_approve_on_update(self):
        auto_learn, auto_queue = self._auto_learn()
        draft = auto_learn.create_draft("demo-skill", make_draft())
        draft = auto_learn.submit_for_review(draft["id"])
        approval = auto_queue.get(draft["approval_id"])
        self.assertEqual(approval["status"], "approved")
        self.assertIs(approval["auto"], True)
        with self.assertRaises(ValueError) as ctx:
            auto_learn.activate_draft(draft["id"], approval)
        self.assertEqual(str(ctx.exception), AUTO_APPROVAL_REFUSAL)
        kept = auto_learn.get_draft(draft["id"])
        self.assertEqual(kept["status"], "pending-review")
        self.assertIn("version: 1.2.3", self._skill_text())
        self.assertFalse(
            auto_queue.get(draft["approval_id"])["consumed"])

    def test_learn_proposal_rejects_auto_approve_on_create(self):
        auto_learn, auto_queue = self._auto_learn()
        draft = auto_learn.create_draft(
            "new-skill",
            make_draft(name="new-skill", version=CREATE_VERSION,
                       description=DESC_CREATE))
        draft = auto_learn.submit_for_review(draft["id"])
        approval = auto_queue.get(draft["approval_id"])
        self.assertIs(approval["auto"], True)
        with self.assertRaises(ValueError) as ctx:
            auto_learn.activate_draft(draft["id"], approval)
        self.assertEqual(str(ctx.exception), AUTO_APPROVAL_REFUSAL)
        self.assertFalse(
            (self.shelf / "new-skill" / "SKILL.md").exists())
        self.assertEqual(
            auto_learn.get_draft(draft["id"])["status"],
            "pending-review")


class TestDistillationContext(unittest.TestCase):
    def test_create_context_labels_transcript_as_data(self):
        ctx = build_distillation_context(
            "new-skill", "we did X then Y")
        self.assertEqual(ctx["kind"], "create")
        self.assertEqual(ctx["skill"], "new-skill")
        self.assertFalse(ctx["transcript_truncated"])
        self.assertIn("DATA", ctx["prompt_text"])
        self.assertIn("new-skill", ctx["prompt_text"])
        self.assertIn("we did X then Y", ctx["prompt_text"])
        self.assertIsNone(ctx["existing_skill_text"])

    def test_update_context_and_long_transcript_truncation(self):
        long_transcript = "word " * 5000  # 25,000 chars
        ctx = build_distillation_context(
            "demo-skill", long_transcript,
            existing_skill_text=SKILL_TOP)
        self.assertEqual(ctx["kind"], "update")
        self.assertTrue(ctx["transcript_truncated"])
        self.assertEqual(len(ctx["transcript"]), MAX_DRAFT_BYTES // 2)

    def test_context_rejects_bad_skill_name(self):
        with self.assertRaises(ValueError):
            build_distillation_context("Bad_Name", "transcript")


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
