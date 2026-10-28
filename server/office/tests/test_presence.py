"""Tests for the presence beacon log (stdlib unittest only).

Run from ~/workspace/grokbot-own/:
    python3 -m unittest discover -s office/tests -v
"""

import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from office.presence import STALE_AFTER_SECONDS, BeaconLog


class TestBeaconLog(unittest.TestCase):
    def test_record_and_last_seen(self):
        log = BeaconLog()
        entry = log.record("chief-officer", "active", note="writing", ts=1000.0)
        self.assertEqual(entry["character_id"], "chief-officer")
        self.assertEqual(entry["state"], "active")
        self.assertEqual(entry["note"], "writing")
        self.assertEqual(entry["ts"], 1000.0)
        self.assertEqual(log.last_seen("chief-officer"), entry)

    def test_record_rejects_bad_input(self):
        log = BeaconLog()
        for bad_id in ("", "   ", None, 7):
            with self.assertRaises(ValueError, msg=repr(bad_id)):
                log.record(bad_id, "active", ts=1.0)
        with self.assertRaises(ValueError):
            log.record("chief-officer", "sleeping", ts=1.0)
        with self.assertRaises(ValueError):
            log.record("chief-officer", "active", note="x" * 257, ts=1.0)
        with self.assertRaises(ValueError):
            log.record("chief-officer", "active", note=42, ts=1.0)

    def test_latest_wins(self):
        log = BeaconLog()
        log.record("chief-officer", "active", ts=1000.0)
        log.record("chief-officer", "idle", ts=1010.0)
        self.assertEqual(log.last_seen("chief-officer")["state"], "idle")
        self.assertEqual(log.age_seconds("chief-officer", now=1020.0), 10.0)

    def test_unknown_character(self):
        log = BeaconLog()
        self.assertIsNone(log.last_seen("nobody"))
        self.assertIsNone(log.age_seconds("nobody", now=1000.0))
        self.assertTrue(log.is_stale("nobody", now=1000.0))

    def test_age_and_stale_boundary(self):
        log = BeaconLog()
        log.record("chief-officer", "active", ts=1000.0)
        self.assertEqual(STALE_AFTER_SECONDS, 120)
        self.assertFalse(log.is_stale("chief-officer", now=1000.0 + 120))
        self.assertTrue(log.is_stale("chief-officer", now=1000.0 + 121))

    def test_history_is_capped(self):
        log = BeaconLog(max_history=3)
        for i in range(5):
            log.record("chief-officer", "active", ts=1000.0 + i)
        history = log.history()
        self.assertEqual(len(history), 3)
        self.assertEqual([h["ts"] for h in history], [1002.0, 1003.0, 1004.0])
        self.assertEqual(len(log.history(limit=1)), 1)

    def test_snapshot_shape(self):
        log = BeaconLog()
        log.record("chief-officer", "active", note="writing", ts=1000.0)
        log.record("yt-analyst", "idle", ts=900.0)
        snap = log.snapshot(now=1060.0)
        self.assertEqual(snap["stale_after_seconds"], 120)
        self.assertEqual(snap["characters"]["chief-officer"]["state"], "active")
        self.assertEqual(snap["characters"]["chief-officer"]["age_seconds"], 60.0)
        self.assertEqual(snap["characters"]["yt-analyst"]["age_seconds"], 160.0)
        self.assertIn("T", snap["characters"]["chief-officer"]["last_seen"])

    def test_snapshot_returns_copies(self):
        log = BeaconLog()
        log.record("chief-officer", "active", ts=1000.0)
        snap = log.snapshot(now=1000.0)
        snap["characters"]["chief-officer"]["state"] = "tampered"
        self.assertEqual(log.last_seen("chief-officer")["state"], "active")


if __name__ == "__main__":
    unittest.main()
