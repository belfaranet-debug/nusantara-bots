"""Tests for the computer registry (stdlib unittest only).

Run from ~/workspace/grokbot-own/:
    python3 -m unittest discover -s computers/tests -v

Tests use temp files only; the real data/computers.json is never
touched. No network, no model calls, no credentials.
"""

import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from computers import ComputerRegistry


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "computers.json"
        self.reg = ComputerRegistry(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_defaults(self):
        rec = self.reg.register("test-pc")
        self.assertEqual(rec["id"], "local-1")
        self.assertEqual(rec["label"], "test-pc")
        self.assertEqual(rec["owner"], "owner")
        self.assertEqual(rec["status"], "active")
        self.assertEqual(rec["capabilities"], [])
        self.assertEqual(rec["notes"], "")
        self.assertTrue(rec["created_at"])
        self.assertEqual(rec["last_seen"], rec["created_at"])

    def test_register_with_capabilities_and_notes(self):
        rec = self.reg.register("pixel-7-pro", ["phone", "browser"],
                                notes="owner phone")
        self.assertEqual(rec["capabilities"], ["phone", "browser"])
        self.assertEqual(rec["notes"], "owner phone")

    def test_ids_are_sequential(self):
        a = self.reg.register("one")
        b = self.reg.register("two")
        self.assertEqual((a["id"], b["id"]), ("local-1", "local-2"))

    def test_label_validation(self):
        for bad in ("", "   ", None, 42):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.reg.register(bad)
        with self.assertRaises(ValueError):
            self.reg.register("x" * 129)

    def test_capabilities_validation(self):
        with self.assertRaises(ValueError):
            self.reg.register("m", capabilities="phone")
        with self.assertRaises(ValueError):
            self.reg.register("m", capabilities=[""])
        with self.assertRaises(ValueError):
            self.reg.register("m", capabilities=["x" * 65])
        with self.assertRaises(ValueError):
            self.reg.register("m", capabilities=["c"] * 17)

    def test_notes_validation(self):
        with self.assertRaises(ValueError):
            self.reg.register("m", notes="x" * 1025)
        with self.assertRaises(ValueError):
            self.reg.register("m", notes=123)

    def test_list_and_get(self):
        self.reg.register("one")
        self.reg.register("two")
        listed = self.reg.list_machines()
        self.assertEqual([m["label"] for m in listed], ["one", "two"])
        got = self.reg.get("local-2")
        self.assertEqual(got["label"], "two")
        self.assertIsNone(self.reg.get("local-99"))
        # Returned records are copies: mutating one must not
        # corrupt the registry.
        listed[0]["label"] = "tampered"
        self.assertEqual(self.reg.get("local-1")["label"], "one")

    def test_set_status_transitions(self):
        self.reg.register("m")
        rec = self.reg.set_status("local-1", "paused")
        self.assertEqual(rec["status"], "paused")
        rec = self.reg.set_status("local-1", "active")
        self.assertEqual(rec["status"], "active")
        rec = self.reg.set_status("local-1", "removed")
        self.assertEqual(rec["status"], "removed")

    def test_removed_is_terminal(self):
        self.reg.register("m")
        self.reg.set_status("local-1", "removed")
        for nxt in ("active", "paused", "removed"):
            with self.assertRaises(ValueError, msg=nxt):
                self.reg.set_status("local-1", nxt)
        with self.assertRaises(ValueError):
            self.reg.touch("local-1")

    def test_set_status_errors(self):
        self.reg.register("m")
        with self.assertRaises(ValueError):
            self.reg.set_status("local-1", "flying")
        with self.assertRaises(KeyError):
            self.reg.set_status("local-99", "paused")
        with self.assertRaises(KeyError):
            self.reg.touch("local-99")

    def test_touch_refreshes_last_seen(self):
        rec = self.reg.register("m")
        before = rec["last_seen"]
        touched = self.reg.touch("local-1")
        self.assertEqual(touched["id"], "local-1")
        self.assertGreaterEqual(touched["last_seen"], before)

    def test_persistence_round_trip(self):
        self.reg.register("pc", ["browser"])
        self.reg.register("phone", ["phone"])
        self.reg.set_status("local-2", "paused")
        reopened = ComputerRegistry(self.path)
        listed = reopened.list_machines()
        self.assertEqual([m["id"] for m in listed], ["local-1", "local-2"])
        self.assertEqual(reopened.get("local-2")["status"], "paused")
        # The sequence continues after reopening, never reuses ids.
        third = reopened.register("third")
        self.assertEqual(third["id"], "local-3")

    def test_malformed_file_loads_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        reg = ComputerRegistry(self.path)
        self.assertEqual(reg.list_machines(), [])
        rec = reg.register("fresh")
        self.assertEqual(rec["id"], "local-1")

    def test_wrong_shape_file_loads_empty(self):
        self.path.write_text(json.dumps({"nope": True}), encoding="utf-8")
        reg = ComputerRegistry(self.path)
        self.assertEqual(reg.list_machines(), [])


if __name__ == "__main__":
    unittest.main()
