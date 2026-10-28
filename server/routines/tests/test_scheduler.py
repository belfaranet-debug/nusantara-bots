"""Tests for routines (stdlib unittest only).

Run from ~/workspace/grokbot-own/:
    python3 -m unittest discover -s routines/tests -v

Tests use temp files only: a temp routines store, a temp Kanban
store, and a temp memory DB. The real data/ files are never
touched. No network, no model calls, no credentials.
"""

import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from memory.store import MemoryStore
from office.kanban import KanbanStore
from routines.scheduler import (
    RoutineStore,
    WEEKLY_PULSE_ROUTINE,
    next_run_after,
    parse_cron,
    validate_schedule,
)

MONDAY = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)  # a Monday


class TestScheduleParsing(unittest.TestCase):
    def test_interval_adds_seconds(self):
        now = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)
        nxt = next_run_after({"kind": "interval", "seconds": 3600}, now)
        self.assertEqual(nxt, datetime(2026, 10, 25, 7, 0,
                                       tzinfo=timezone.utc))

    def test_interval_rejects_too_short(self):
        with self.assertRaises(ValueError):
            validate_schedule({"kind": "interval", "seconds": 30})
        with self.assertRaises(ValueError):
            validate_schedule({"kind": "interval", "seconds": "hourly"})

    def test_daily_same_day_and_next_day(self):
        now = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(
            next_run_after({"kind": "daily", "time": "07:00"}, now),
            datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc))
        late = datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(
            next_run_after({"kind": "daily", "time": "07:00"}, late),
            datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            validate_schedule({"kind": "daily", "time": "25:00"})
        with self.assertRaises(ValueError):
            validate_schedule({"kind": "daily", "time": "7am"})

    def test_cron_star_and_number(self):
        parsed = parse_cron("* * * * *")
        self.assertEqual(parsed, (None, None, None, None, None))
        self.assertEqual(parse_cron("0 7 * * 1")[1], {7})
        self.assertEqual(parse_cron("0 7 * * 1")[4], {1})

    def test_cron_step_list_range(self):
        self.assertEqual(parse_cron("*/15 * * * *")[0],
                         {0, 15, 30, 45})
        self.assertEqual(parse_cron("0 9,17 * * *")[1], {9, 17})
        self.assertEqual(parse_cron("0 0 * * 1-5")[4], {1, 2, 3, 4, 5})
        self.assertEqual(parse_cron("0 0 * * */2")[4], {0, 2, 4, 6})
        self.assertEqual(parse_cron("0 9-17/4 * * *")[1], {9, 13, 17})

    def test_cron_sunday_is_0_and_7(self):
        self.assertEqual(parse_cron("0 0 * * 7")[4], {0})
        self.assertEqual(parse_cron("0 0 * * 0")[4], {0})

    def test_cron_rejects_names_and_bad_fields(self):
        for bad in ("0 7 * * MON", "0 7 * * L", "0 7 * JAN *",
                    "0 7 * *", "0 7 * * * *", "61 * * * *",
                    "0 25 * * *", "0 0 32 * *", "0 0 * 13 *",
                    "0 0 * * 8", "*/0 * * * *", "0 7 * * 1-"):
            with self.assertRaises(ValueError, msg=bad):
                parse_cron(bad)

    def test_cron_next_run_weekly_monday(self):
        # Sunday 06:00 -> next Monday 07:00.
        sunday = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(sunday.weekday(), 6)
        nxt = next_run_after({"kind": "cron", "expression": "0 7 * * 1"},
                             sunday)
        self.assertEqual(nxt, datetime(2026, 10, 26, 7, 0,
                                       tzinfo=timezone.utc))
        # Monday 07:00 exactly is not "after" itself -> next week.
        self.assertEqual(
            next_run_after({"kind": "cron", "expression": "0 7 * * 1"},
                           datetime(2026, 10, 26, 7, 0,
                                    tzinfo=timezone.utc)),
            datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc))

    def test_cron_every_15_minutes(self):
        now = datetime(2026, 10, 25, 6, 7, tzinfo=timezone.utc)
        self.assertEqual(
            next_run_after({"kind": "cron", "expression": "*/15 * * * *"},
                           now),
            datetime(2026, 10, 25, 6, 15, tzinfo=timezone.utc))

    def test_cron_dom_or_dow(self):
        # 1st of the month OR Friday: Friday 30 Oct comes first
        # from Sunday 25 Oct (1 Nov is later).
        now = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)
        nxt = next_run_after(
            {"kind": "cron", "expression": "0 7 1 * 5"}, now)
        self.assertEqual(nxt, datetime(2026, 10, 30, 7, 0,
                                       tzinfo=timezone.utc))

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            validate_schedule({"kind": "whenever"})
        with self.assertRaises(ValueError):
            validate_schedule("daily")


class RoutineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="routines-test-"))
        self.store = RoutineStore(path=str(self.tmp / "routines.json"))
        self.kanban = KanbanStore(path=str(self.tmp / "kanban.json"))
        self.memory = MemoryStore(db_path=str(self.tmp / "memory.db"))

    def tearDown(self):
        self.memory.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestRoutineStore(RoutineTestBase):
    def test_create_computes_first_next_run(self):
        rec = self.store.create_routine(
            "chief-officer", "weekly-pulse",
            {"kind": "daily", "time": "07:00"}, now=MONDAY)
        self.assertEqual(rec["id"], "routine-1")
        self.assertTrue(rec["enabled"])
        self.assertIsNone(rec["last_run"])
        self.assertEqual(rec["next_run"], "2026-10-26T07:00:00+00:00")

    def test_create_rejects_bad_schedule_and_character(self):
        with self.assertRaises(ValueError):
            self.store.create_routine("chief-officer", "p",
                                      {"kind": "nope"})
        with self.assertRaises(ValueError):
            self.store.create_routine("", "p",
                                      {"kind": "interval", "seconds": 3600})

    def test_weekly_pulse_builtin(self):
        rec = self.store.create_weekly_pulse(now=MONDAY)
        self.assertEqual(rec["character"],
                         WEEKLY_PULSE_ROUTINE["character"])
        self.assertEqual(rec["pack"], WEEKLY_PULSE_ROUTINE["pack"])
        self.assertEqual(rec["schedule"],
                         WEEKLY_PULSE_ROUTINE["schedule"])
        self.assertEqual(rec["next_run"], "2026-10-26T07:00:00+00:00")

    def test_due_only_when_enabled_and_time_reached(self):
        rec = self.store.create_routine(
            "chief-officer", "pulse",
            {"kind": "interval", "seconds": 3600}, now=MONDAY)
        # Not due before its next run.
        self.assertEqual(self.store.due_routines(MONDAY), [])
        # Due at next_run.
        due = self.store.due_routines(
            datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc))
        self.assertEqual([r["id"] for r in due], [rec["id"]])
        # Disabled routines are never due.
        self.store.set_enabled(rec["id"], False)
        self.assertEqual(self.store.due_routines(
            datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc)), [])

    def test_run_creates_card_and_memory(self):
        rec = self.store.create_routine(
            "chief-officer", "weekly-pulse",
            {"kind": "interval", "seconds": 3600}, now=MONDAY)
        out = self.store.run_routine(
            rec["id"], kanban=self.kanban, memory=self.memory,
            now=datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc))
        card = out["card"]
        self.assertEqual(card["status"], "todo")
        self.assertEqual(card["assignee"], "chief-officer")
        self.assertEqual(card["origin"],
                         {"kind": "agent", "ref": rec["id"]})
        self.assertIn("weekly-pulse", card["body"])
        memories = self.memory.recall("team", "weekly-pulse")
        self.assertEqual(len(memories), 1)
        self.assertIn(rec["id"], memories[0]["text"])
        self.assertEqual(out["memory_id"], memories[0]["id"])
        updated = out["routine"]
        self.assertEqual(updated["last_run"], "2026-10-26T07:00:00+00:00")
        self.assertEqual(updated["next_run"], "2026-10-26T08:00:00+00:00")

    def test_run_disabled_raises_and_creates_nothing(self):
        rec = self.store.create_routine(
            "chief-officer", "pulse",
            {"kind": "interval", "seconds": 3600}, now=MONDAY)
        self.store.set_enabled(rec["id"], False)
        with self.assertRaises(ValueError):
            self.store.run_routine(rec["id"], kanban=self.kanban,
                                   memory=self.memory, now=MONDAY)
        self.assertEqual(self.kanban.list_cards(), [])
        self.assertEqual(self.memory.list("team"), [])

    def test_run_unknown_raises(self):
        with self.assertRaises(KeyError):
            self.store.run_routine("routine-99", kanban=self.kanban,
                                   memory=self.memory)

    def test_tick_runs_due_only(self):
        due = self.store.create_routine(
            "chief-officer", "pulse-a",
            {"kind": "interval", "seconds": 3600}, now=MONDAY)
        later = self.store.create_routine(
            "chief-officer", "pulse-b",
            {"kind": "daily", "time": "07:00"}, now=MONDAY)
        off = self.store.create_routine(
            "chief-officer", "pulse-c",
            {"kind": "interval", "seconds": 60}, now=MONDAY)
        self.store.set_enabled(off["id"], False)
        results = self.store.tick(
            datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc),
            kanban=self.kanban, memory=self.memory)
        # pulse-a (07:00) and pulse-b (07:00 daily) fire; disabled c does not.
        self.assertEqual(sorted(r["id"] for r in results),
                         sorted([due["id"], later["id"]]))
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(len(self.kanban.list_cards()), 2)
        # A second tick at the same time fires nothing (next runs advanced).
        self.assertEqual(self.store.tick(
            datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc),
            kanban=self.kanban, memory=self.memory), [])

    def test_delete_and_enable(self):
        rec = self.store.create_routine(
            "chief-officer", "p",
            {"kind": "interval", "seconds": 3600}, now=MONDAY)
        self.assertTrue(self.store.delete_routine(rec["id"]))
        self.assertFalse(self.store.delete_routine(rec["id"]))
        self.assertIsNone(self.store.get_routine(rec["id"]))
        with self.assertRaises(KeyError):
            self.store.set_enabled("routine-99", False)


class TestZeroTrace(unittest.TestCase):
    def test_no_forbidden_brand_strings_in_new_files(self):
        banned = ["rak" + "azo", "octo" + "bot", "open" + "dots"]
        root = Path(__file__).resolve().parents[2]
        for path in list((root / "routines").rglob("*.py")) + \
                list((root / "routines").rglob("*.md")):
            if "__pycache__" in str(path):
                continue
            text = path.read_text(encoding="utf-8").lower()
            for word in banned:
                self.assertNotIn(word, text, str(path))


if __name__ == "__main__":
    unittest.main()
