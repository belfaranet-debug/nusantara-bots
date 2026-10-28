"""Tests for snapshots (stdlib unittest only).

Run from ~/workspace/grokbot-own/:
    python3 -m unittest discover -s snapshots/tests -v

Tests use temp files only: a temp snapshots dir plus temp
registry + Kanban stores. The real data/ files are never
touched. No network, no model calls, no credentials.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from computers import ComputerRegistry
from office.kanban import KanbanStore
from snapshots.snapshot import SnapshotStore


class SnapshotTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="snapshot-test-"))
        self.registry_path = str(self.tmp / "computers.json")
        self.kanban_path = str(self.tmp / "kanban.json")
        self.registry = ComputerRegistry(path=self.registry_path)
        self.kanban = KanbanStore(path=self.kanban_path)
        self.store = SnapshotStore(
            snapshots_dir=str(self.tmp / "snapshots"),
            computers_file=self.registry_path,
            kanban_file=self.kanban_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        self.registry.register("test-pc", ["ssh"])
        card = self.kanban.create_card("Ship the pulse",
                                       assignee="chief-officer")
        self.kanban.move_card(card["id"], "doing")
        return card


class TestCreateAndList(SnapshotTestBase):
    def test_create_copies_state_and_lists(self):
        self._seed()
        snap = self.store.create_snapshot(
            device_state={"bridge": "unavailable"})
        self.assertEqual(snap["id"], "snap-1")
        self.assertTrue(snap["created_at"])
        self.assertEqual(snap["file_names"],
                         ["computers.json", "kanban.json"])
        self.assertTrue(snap["files"]["computers.json"]["present"])
        self.assertTrue(snap["files"]["kanban.json"]["present"])
        self.assertEqual(snap["device_state"], {"bridge": "unavailable"})
        self.assertEqual(len(snap["state"]["computers.json"]["machines"]), 1)
        self.assertEqual(len(snap["state"]["kanban.json"]["cards"]), 1)
        summaries = self.store.list_snapshots()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["id"], "snap-1")
        self.assertTrue(summaries[0]["ok"])
        self.assertTrue(summaries[0]["has_device_state"])
        # The snapshot file itself exists on disk.
        self.assertTrue((self.tmp / "snapshots" / "snap-1.json").is_file())

    def test_create_rejects_non_dict_device_state(self):
        with self.assertRaises(ValueError):
            self.store.create_snapshot(device_state=["not", "a", "dict"])

    def test_snapshot_of_missing_files_records_absent(self):
        empty = SnapshotStore(
            snapshots_dir=str(self.tmp / "empty-snaps"),
            computers_file=str(self.tmp / "no-computers.json"),
            kanban_file=str(self.tmp / "no-kanban.json"))
        snap = empty.create_snapshot()
        self.assertFalse(snap["files"]["computers.json"]["present"])
        self.assertFalse(snap["files"]["kanban.json"]["present"])
        self.assertIsNone(snap["state"]["computers.json"])


class TestRestore(SnapshotTestBase):
    def test_snapshot_change_restore_returns_original_state(self):
        card = self._seed()
        snap = self.store.create_snapshot()
        # Mutate both stores after the snapshot.
        self.registry.register("phone", ["tap"])
        self.registry.set_status("local-1", "paused")
        self.kanban.create_card("A new card after the snapshot")
        self.kanban.move_card(card["id"], "done")
        self.assertEqual(len(self.registry.list_machines()), 2)
        self.assertEqual(len(self.kanban.list_cards()), 2)
        # Restore.
        result = self.store.restore_snapshot(snap["id"])
        self.assertEqual(result["id"], snap["id"])
        self.assertEqual(result["restored"],
                         ["computers.json", "kanban.json"])
        self.assertEqual(result["skipped"], [])
        # Fresh readers see the snapshot's state, not the mutations.
        registry = ComputerRegistry(path=self.registry_path)
        kanban = KanbanStore(path=self.kanban_path)
        machines = registry.list_machines()
        self.assertEqual(len(machines), 1)
        self.assertEqual(machines[0]["label"], "test-pc")
        self.assertEqual(machines[0]["status"], "active")
        cards = kanban.list_cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["status"], "doing")
        self.assertEqual(cards[0]["title"], "Ship the pulse")

    def test_restore_returns_device_state(self):
        self._seed()
        snap = self.store.create_snapshot(
            device_state={"bridge": "ok", "shots": 2})
        result = self.store.restore_snapshot(snap["id"])
        self.assertEqual(result["device_state"],
                         {"bridge": "ok", "shots": 2})

    def test_restore_skips_files_absent_at_snapshot_time(self):
        empty = SnapshotStore(
            snapshots_dir=str(self.tmp / "empty-snaps"),
            computers_file=str(self.tmp / "no-computers.json"),
            kanban_file=str(self.tmp / "no-kanban.json"))
        snap = empty.create_snapshot()
        result = empty.restore_snapshot(snap["id"])
        self.assertEqual(result["restored"], [])
        self.assertEqual(result["skipped"],
                         ["computers.json", "kanban.json"])

    def test_restore_unknown_snapshot_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.store.restore_snapshot("snap-99")

    def test_restore_is_audited(self):
        snap = self.store.create_snapshot()
        self.store.restore_snapshot(snap["id"])
        audit = (self.tmp / "snapshots" / "audit.jsonl").read_text(
            encoding="utf-8")
        events = [json.loads(line)["event"] for line in audit.splitlines()]
        self.assertIn("created", events)
        self.assertIn("restored", events)


class TestDelete(SnapshotTestBase):
    def test_delete_removes_and_unknown_raises(self):
        snap = self.store.create_snapshot()
        self.assertEqual(len(self.store.list_snapshots()), 1)
        self.assertTrue(self.store.delete_snapshot(snap["id"]))
        self.assertEqual(self.store.list_snapshots(), [])
        with self.assertRaises(KeyError):
            self.store.delete_snapshot(snap["id"])
        with self.assertRaises(KeyError):
            self.store.delete_snapshot("snap-99")

    def test_ids_are_not_reused_after_delete(self):
        first = self.store.create_snapshot()
        self.store.delete_snapshot(first["id"])
        second = self.store.create_snapshot()
        self.assertNotEqual(second["id"], first["id"])

    def test_get_snapshot_round_trip(self):
        snap = self.store.create_snapshot()
        fetched = self.store.get_snapshot(snap["id"])
        self.assertEqual(fetched["id"], snap["id"])
        self.assertIsNone(self.store.get_snapshot("snap-99"))


class TestZeroTrace(unittest.TestCase):
    def test_no_forbidden_brand_strings_in_new_files(self):
        banned = ["rak" + "azo", "octo" + "bot", "open" + "dots"]
        root = Path(__file__).resolve().parents[2]
        for path in list((root / "snapshots").rglob("*.py")) + \
                list((root / "snapshots").rglob("*.md")):
            if "__pycache__" in str(path):
                continue
            text = path.read_text(encoding="utf-8").lower()
            for word in banned:
                self.assertNotIn(word, text, str(path))


if __name__ == "__main__":
    unittest.main()
