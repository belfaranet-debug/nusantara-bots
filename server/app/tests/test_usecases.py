"""Use-case pack tests — inbox-triage + weekly-pulse (Fase 2).

Stdlib unittest only. No network, no bridge, no real mailbox, no
credentials anywhere. The triage/pulse modules are pure functions;
the Kanban part uses a temp store file only.

Contract under test (see app/triage.py, app/pulse.py, and the two
skill packs on the shelf):
* classify_mail_item: task (reply-wanted -> send, else create_card),
  info (-> create_card, priority 1), spam (-> delete, never a card).
* mail_to_card_draft: Kanban card dict with origin.kind == "mail"
  and a ref (message id, or from:<sender>); None for spam.
* compose_pulse: done cards split proven/unproven by evidence,
  blocked + unproven-done land in needs_decision, memory + mail
  numbers are carried through, and the draft NEVER sends
  (sent is False).
* Both new packs validate (packs validator + skill loader).
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from office.kanban import KanbanStore  # noqa: E402
from pulse import compose_pulse  # noqa: E402
from triage import classify_mail_item, mail_to_card_draft  # noqa: E402
from loader.skills import load_skill  # noqa: E402
from packs.pack import DEFAULT_REVIEW_ACTIONS, validate_pack  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SHELF = REPO.parent / "grokbot" / "skill-pack"
APP_DIR = REPO / "app"

TASK_QUESTION = {
    "id": "m-101",
    "from": "klien@fake.example",
    "subject": "Mohon review draf proposal",
    "snippet": "Bisa tolong review draf proposal ini sebelum Jumat? Terima kasih.",
    "character": "chief-officer",
}

TASK_PLAIN = {
    "id": "m-102",
    "from": "tim@fake.example",
    "subject": "Review draf laporan",
    "snippet": "Draf laporan sudah siap untuk direview.",
    "character": "chief-officer",
}

TASK_URGENT = {
    "id": "m-103",
    "from": "ops@fake.example",
    "subject": "URGENT: server down",
    "snippet": "Server down, butuh penanganan segera.",
    "character": "chief-officer",
}

INFO_ITEM = {
    "id": "m-104",
    "from": "newsletter@fake.example",
    "subject": "Buletin mingguan: rilis fitur baru",
    "snippet": "Minggu ini kami merilis tiga fitur baru. Selamat membaca.",
    "character": "chief-officer",
}

SPAM_ITEM = {
    "id": "m-105",
    "from": "winner@fake.example",
    "subject": "Congratulations, you won the lottery!",
    "snippet": "Claim your prize now before it expires.",
    "character": "chief-officer",
}


def _board():
    """A board shaped like KanbanStore.board() output."""
    return {
        "columns": {
            "backlog": [],
            "doing": [
                {"id": "card-1", "title": "Kerjakan X", "status": "doing",
                 "evidence": [], "verdict": "open"},
            ],
            "review": [
                {"id": "card-2", "title": "Blocked Y", "status": "blocked",
                 "evidence": [], "verdict": "open"},
                {"id": "card-3", "title": "Review Z", "status": "review",
                 "evidence": [], "verdict": "open"},
            ],
            "done": [
                {"id": "card-4", "title": "Selesai berbukti",
                 "status": "done",
                 "evidence": [{"kind": "summary", "label": "shipped"}],
                 "verdict": "proven"},
                {"id": "card-5", "title": "Selesai tanpa bukti",
                 "status": "done", "evidence": [], "verdict": "unproven"},
                # No "verdict" key: the composer re-applies the rule.
                {"id": "card-6", "title": "Selesai lama berbukti",
                 "status": "done",
                 "evidence": [{"kind": "link", "label": "pr"}]},
            ],
        },
        "counts": {"backlog": 0, "doing": 1, "review": 2, "done": 3},
        "unproven_done": 1,
    }


class TestClassifyMailItem(unittest.TestCase):
    def test_task_question_wants_a_reply(self):
        result = classify_mail_item(TASK_QUESTION)
        self.assertEqual(result["kind"], "task")
        self.assertEqual(result["suggested_action"], "send")
        self.assertEqual(result["priority"], 2)
        self.assertEqual(result["ref"], "m-101")
        self.assertIn("review", result["draft_title"].lower())
        # A reply draft is sketched, but nothing is sent here.
        self.assertIn("BELUM dikirim", result["draft_body"])

    def test_task_without_reply_markers_is_card_only(self):
        result = classify_mail_item(TASK_PLAIN)
        self.assertEqual(result["kind"], "task")
        self.assertEqual(result["suggested_action"], "create_card")
        self.assertEqual(result["priority"], 2)
        self.assertNotIn("BELUM dikirim", result["draft_body"])

    def test_urgent_task_is_top_priority(self):
        result = classify_mail_item(TASK_URGENT)
        self.assertEqual(result["kind"], "task")
        self.assertEqual(result["priority"], 3)

    def test_info_is_filed_low_priority(self):
        result = classify_mail_item(INFO_ITEM)
        self.assertEqual(result["kind"], "info")
        self.assertEqual(result["suggested_action"], "create_card")
        self.assertEqual(result["priority"], 1)

    def test_spam_is_delete_never_a_card(self):
        result = classify_mail_item(SPAM_ITEM)
        self.assertEqual(result["kind"], "spam")
        self.assertEqual(result["suggested_action"], "delete")
        self.assertIsNone(mail_to_card_draft(SPAM_ITEM))

    def test_empty_and_bad_input_never_raise(self):
        for bad in ({}, {"subject": ""}, None, "not-a-dict", 42):
            result = classify_mail_item(bad)
            self.assertEqual(result["kind"], "info")
            self.assertEqual(result["suggested_action"], "create_card")


class TestMailToCardDraft(unittest.TestCase):
    def test_origin_mail_recorded_with_message_id(self):
        draft = mail_to_card_draft(TASK_QUESTION)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["origin"], {"kind": "mail", "ref": "m-101"})
        self.assertEqual(draft["assignee"], "chief-officer")
        self.assertEqual(draft["classification"], "task")
        self.assertEqual(draft["suggested_action"], "send")

    def test_ref_falls_back_to_sender(self):
        item = {"from": "a@fake.example", "subject": "Hai",
                "snippet": "Tolong cek ini ya?"}
        draft = mail_to_card_draft(item)
        self.assertEqual(draft["origin"]["ref"], "from:a@fake.example")

    def test_title_and_body_fit_kanban_limits(self):
        item = {"id": "m-200", "from": "a@fake.example",
                "subject": "S" * 500, "snippet": "B" * 9000,
                "character": "chief-officer"}
        draft = mail_to_card_draft(item)
        self.assertLessEqual(len(draft["title"]), 250)
        self.assertLessEqual(len(draft["body"]), 4000)

    def test_draft_goes_through_the_real_kanban_store(self):
        tmp = Path(tempfile.mkdtemp(prefix="usecase-kanban-"))
        try:
            store = KanbanStore(path=str(tmp / "kanban.json"))
            draft = mail_to_card_draft(TASK_PLAIN)
            card = store.create_card(
                draft["title"], body=draft["body"],
                assignee=None, priority=draft["priority"],
                origin=draft["origin"])
            self.assertEqual(card["status"], "todo")
            self.assertEqual(card["origin"]["kind"], "mail")
            self.assertEqual(card["origin"]["ref"], "m-102")
            self.assertEqual(store.verdict_for(card["id"]), "open")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestComposePulse(unittest.TestCase):
    def test_done_split_proven_unproven_and_needs_decision(self):
        pulse = compose_pulse(
            _board(),
            {"team_count": 7, "scopes": {"team": 7}},
            [{"character": "chief-officer", "inbox_count": 4,
              "unread": 1, "available": True, "error": None}])
        self.assertEqual(pulse["kind"], "weekly-pulse-draft")
        self.assertFalse(pulse["sent"])
        self.assertEqual(pulse["done_proven"],
                         ["Selesai berbukti", "Selesai lama berbukti"])
        self.assertEqual(pulse["done_unproven"], ["Selesai tanpa bukti"])
        self.assertEqual(pulse["blocked"], ["Blocked Y"])
        self.assertEqual(pulse["in_review"], ["Review Z"])
        self.assertEqual(pulse["doing"], ["Kerjakan X"])
        self.assertEqual(pulse["needs_decision"],
                         ["Blocked Y", "Selesai tanpa bukti"])
        counts = pulse["counts"]
        self.assertEqual(counts["done"], 3)
        self.assertEqual(counts["done_proven"], 2)
        self.assertEqual(counts["done_unproven"], 1)
        self.assertEqual(counts["needs_decision"], 2)
        self.assertEqual(counts["memory_team"], 7)
        self.assertEqual(counts["mail_total"], 4)
        self.assertEqual(counts["mail_unread"], 1)
        # The markdown body carries the same sections.
        self.assertIn("Selesai — terbukti", pulse["body"])
        self.assertIn("Selesai tanpa bukti", pulse["body"])
        self.assertIn("Blocked Y", pulse["body"])
        self.assertIn("TIDAK dikirim", pulse["body"])

    def test_unavailable_mail_is_reported_not_hidden(self):
        pulse = compose_pulse(
            _board(), {"team_count": 0, "scopes": {}},
            [{"character": "chief-officer", "inbox_count": 0,
              "unread": 0, "available": False,
              "error": "mail bridge unreachable"}])
        self.assertFalse(pulse["sent"])
        self.assertIn("tidak tersedia", pulse["body"])
        self.assertEqual(pulse["counts"]["mail_total"], 0)

    def test_empty_inputs_still_compose_a_draft(self):
        pulse = compose_pulse({}, {}, [])
        self.assertFalse(pulse["sent"])
        self.assertEqual(pulse["counts"]["done"], 0)
        self.assertEqual(pulse["needs_decision"], [])
        self.assertIn("(tidak ada)", pulse["body"])


class TestNewPacksValidate(unittest.TestCase):
    def test_both_packs_pass_the_validator(self):
        for name in ("inbox-triage", "weekly-pulse"):
            result = validate_pack(SHELF / name)
            self.assertTrue(result.ok, (name, result.errors))

    def test_both_skills_load_and_match_their_manifest(self):
        for name in ("inbox-triage", "weekly-pulse"):
            skill = load_skill(SHELF / name)
            self.assertTrue(skill.ok, (name, skill.error_codes()))
            manifest = json.loads(
                (SHELF / name / "manifest.json").read_text())
            self.assertEqual(manifest["description"], skill.description)
            self.assertEqual(manifest["version"], skill.version)
            self.assertEqual(set(DEFAULT_REVIEW_ACTIONS),
                             set(manifest["review"]["required_before"]))
            self.assertEqual(manifest["source"], {"origin": "own-build"})

    def test_new_files_carry_no_forbidden_brand(self):
        # Built by concatenation so the literal never appears here.
        banned = ["rak" + "azo", "open" + "dots", "octo" + "bot"]
        files = [
            APP_DIR / "triage.py",
            APP_DIR / "pulse.py",
            APP_DIR / "usecase_endpoints.patch.md",
            APP_DIR / "tests" / "test_usecases.py",
            SHELF / "inbox-triage" / "SKILL.md",
            SHELF / "inbox-triage" / "manifest.json",
            SHELF / "weekly-pulse" / "SKILL.md",
            SHELF / "weekly-pulse" / "manifest.json",
            REPO / "packs" / "tests" / "test_packs.py",
        ]
        for path in files:
            text = path.read_text(encoding="utf-8").lower()
            for word in banned:
                self.assertNotIn(word, text, str(path))


if __name__ == "__main__":
    unittest.main()
