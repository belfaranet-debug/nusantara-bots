"""Tests for the Kanban store + evidence verdict (stdlib unittest only).

Run from ~/workspace/grokbot-own/:
    python3 -m unittest discover -s office/tests -v

Tests use temp files only; the real data/kanban.json is never touched.
No network, no model calls, no credentials.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from office.kanban import KanbanStore, verdict


class TestVerdict(unittest.TestCase):
    def test_not_done_is_open(self):
        for status in ("todo", "doing", "review", "blocked"):
            self.assertEqual(verdict({"status": status, "evidence": []}), "open")

    def test_done_without_evidence_is_unproven(self):
        self.assertEqual(verdict({"status": "done", "evidence": []}), "unproven")
        self.assertEqual(verdict({"status": "done"}), "unproven")

    def test_done_with_evidence_is_proven(self):
        card = {"status": "done",
                "evidence": [{"kind": "summary", "label": "shipped"}]}
        self.assertEqual(verdict(card), "proven")

    def test_non_dict_is_open(self):
        self.assertEqual(verdict(None), "open")


class TestKanbanStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kanban-test-"))
        self.path = str(self.tmp / "kanban.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_defaults(self):
        store = KanbanStore(path=self.path)
        card = store.create_card("Write the brief")
        self.assertEqual(card["id"], "card-1")
        self.assertEqual(card["status"], "todo")
        self.assertEqual(card["priority"], 2)
        self.assertEqual(card["origin"], {"kind": "manual"})
        self.assertEqual(card["evidence"], [])
        self.assertIsNone(card["completed_at"])
        self.assertIsNone(card["assignee"])

    def test_create_with_fields(self):
        store = KanbanStore(path=self.path)
        card = store.create_card("Triage inbox", body="morning run",
                                 assignee="chief-officer", priority=3,
                                 origin={"kind": "mail", "ref": "inbox-1"})
        self.assertEqual(card["assignee"], "chief-officer")
        self.assertEqual(card["priority"], 3)
        self.assertEqual(card["origin"], {"kind": "mail", "ref": "inbox-1"})

    def test_create_rejects_bad_input(self):
        store = KanbanStore(path=self.path)
        for bad in ("", "   ", None, 42):
            with self.assertRaises(ValueError, msg=repr(bad)):
                store.create_card(bad)
        with self.assertRaises(ValueError):
            store.create_card("x" * 257)
        with self.assertRaises(ValueError):
            store.create_card("ok", priority=0)
        with self.assertRaises(ValueError):
            store.create_card("ok", priority=4)
        with self.assertRaises(ValueError):
            store.create_card("ok", origin={"kind": "telepathy"})
        with self.assertRaises(ValueError):
            store.create_card("ok", origin={"kind": "mail", "ref": "x" * 257})

    def test_ids_increment_and_persist(self):
        store = KanbanStore(path=self.path)
        first = store.create_card("one")
        second = store.create_card("two")
        self.assertEqual((first["id"], second["id"]), ("card-1", "card-2"))
        reopened = KanbanStore(path=self.path)
        third = reopened.create_card("three")
        self.assertEqual(third["id"], "card-3")
        self.assertEqual(len(reopened.list_cards()), 3)

    def test_malformed_file_loads_empty(self):
        Path(self.path).write_text("{broken json", encoding="utf-8")
        store = KanbanStore(path=self.path)
        self.assertEqual(store.list_cards(), [])
        card = store.create_card("fresh")
        self.assertEqual(card["id"], "card-1")

    def test_env_var_path(self):
        os.environ["GROKBOT_KANBAN_FILE"] = str(self.tmp / "env-kanban.json")
        try:
            store = KanbanStore()
            self.assertEqual(store.path, os.environ["GROKBOT_KANBAN_FILE"])
        finally:
            del os.environ["GROKBOT_KANBAN_FILE"]

    def test_move_sets_and_clears_completed_at(self):
        store = KanbanStore(path=self.path)
        card = store.create_card("ship it")
        moved = store.move_card(card["id"], "doing")
        self.assertEqual(moved["status"], "doing")
        self.assertIsNone(moved["completed_at"])
        done = store.move_card(card["id"], "done")
        self.assertIsNotNone(done["completed_at"])
        back = store.move_card(card["id"], "todo")
        self.assertEqual(back["status"], "todo")
        self.assertIsNone(back["completed_at"])

    def test_move_unknown_status_rejected(self):
        store = KanbanStore(path=self.path)
        card = store.create_card("x")
        with self.assertRaises(ValueError):
            store.move_card(card["id"], "flying")

    def test_move_missing_card_returns_none(self):
        store = KanbanStore(path=self.path)
        self.assertIsNone(store.move_card("card-99", "done"))

    def test_list_filters(self):
        store = KanbanStore(path=self.path)
        a = store.create_card("a", assignee="chief-officer")
        store.create_card("b", assignee="yt-analyst")
        store.move_card(a["id"], "doing")
        self.assertEqual(len(store.list_cards(status="doing")), 1)
        self.assertEqual(len(store.list_cards(assignee="yt-analyst")), 1)
        self.assertEqual(len(store.list_cards(status="todo")), 1)
        self.assertEqual(len(store.list_cards()), 2)

    def test_get_card_returns_copy(self):
        store = KanbanStore(path=self.path)
        card = store.create_card("a")
        fetched = store.get_card(card["id"])
        fetched["title"] = "tampered"
        self.assertEqual(store.get_card(card["id"])["title"], "a")
        self.assertIsNone(store.get_card("card-99"))

    def test_add_evidence_and_verdict(self):
        store = KanbanStore(path=self.path)
        card = store.create_card("ship it")
        store.move_card(card["id"], "done")
        self.assertEqual(store.verdict_for(card["id"]), "unproven")
        updated = store.add_evidence(card["id"], "summary", "Shipped to review",
                                     body="link to the draft", by="chief-officer")
        self.assertEqual(len(updated["evidence"]), 1)
        self.assertEqual(updated["evidence"][0]["kind"], "summary")
        self.assertEqual(store.verdict_for(card["id"]), "proven")

    def test_add_evidence_rejects_bad_kind(self):
        store = KanbanStore(path=self.path)
        card = store.create_card("x")
        with self.assertRaises(ValueError):
            store.add_evidence(card["id"], "screenshot", "proof")
        with self.assertRaises(ValueError):
            store.add_evidence(card["id"], "log", "")
        self.assertIsNone(store.add_evidence("card-99", "log", "x"))

    def test_board_grouping_and_counts(self):
        store = KanbanStore(path=self.path)
        a = store.create_card("todo one")
        b = store.create_card("doing one")
        c = store.create_card("blocked one")
        d = store.create_card("done no proof")
        e = store.create_card("done with proof")
        store.move_card(b["id"], "doing")
        store.move_card(c["id"], "blocked")
        store.move_card(d["id"], "done")
        store.move_card(e["id"], "done")
        store.add_evidence(e["id"], "link", "the result", body="https://example.invalid/x")
        board = store.board()
        self.assertEqual(board["counts"],
                         {"backlog": 1, "doing": 1, "review": 1, "done": 2})
        self.assertEqual([c["id"] for c in board["columns"]["review"]],
                         [c["id"]])
        self.assertEqual(board["unproven_done"], 1)
        # Each card carries the server's verdict (the UI renders this).
        by_id = {c["id"]: c for col in board["columns"].values() for c in col}
        self.assertEqual(by_id[d["id"]]["verdict"], "unproven")
        self.assertEqual(by_id[e["id"]]["verdict"], "proven")
        self.assertEqual(by_id[a["id"]]["verdict"], "open")

    def test_board_orders_by_priority_then_age(self):
        store = KanbanStore(path=self.path)
        low = store.create_card("low", priority=1)
        high = store.create_card("high", priority=3)
        mid = store.create_card("mid", priority=2)
        ids = [c["id"] for c in store.board()["columns"]["backlog"]]
        self.assertEqual(ids, [high["id"], mid["id"], low["id"]])

    def test_file_shape_on_disk(self):
        store = KanbanStore(path=self.path)
        store.create_card("a")
        data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(data["next_id"], 2)
        self.assertEqual(len(data["cards"]), 1)


if __name__ == "__main__":
    unittest.main()
