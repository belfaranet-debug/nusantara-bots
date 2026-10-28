"""Tests for plans + checkpoints (stdlib unittest only).

Run from ~/workspace/grokbot-own/:
    python3 -m unittest discover -s plans/tests -v

Tests use temp files only: a temp plans store + a temp Kanban
store. The real data/ files are never touched. No network, no
model calls, no credentials.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from office.kanban import KanbanStore
from plans.plan import (
    PLAN_STATUSES,
    STEP_STATUSES,
    PlanStore,
    plan_for_routine_run,
    plan_summary,
)

STEPS = ["Riset topik", "Tulis naskah", "Rekam VO", "Rakit video"]


class PlanTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plans-test-"))
        self.kanban = KanbanStore(path=str(self.tmp / "kanban.json"))
        self.store = PlanStore(path=str(self.tmp / "plans.json"),
                               kanban=self.kanban)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_plan(self, steps=STEPS, **kwargs):
        kwargs.setdefault("assignee", "chief-officer")
        return self.store.create_plan("Produksi Short", steps, **kwargs)

    def card_status(self, step):
        card = self.kanban.get_card(step["card_id"])
        self.assertIsNotNone(card, "step %s has no card" % step["id"])
        return card["status"]


class TestCreate(PlanTestBase):
    def test_create_defaults(self):
        plan = self.make_plan()
        self.assertEqual(plan["id"], "plan-1")
        self.assertEqual(plan["status"], "active")
        self.assertEqual(plan["assignee"], "chief-officer")
        self.assertEqual(plan["origin"], {"kind": "manual"})
        self.assertEqual(plan["steering"], [])
        self.assertIsNone(plan["paused_at"])
        self.assertIsNone(plan["completed_at"])
        self.assertEqual([s["status"] for s in plan["steps"]],
                         ["pending"] * 4)
        self.assertEqual([s["id"] for s in plan["steps"]],
                         ["step-1", "step-2", "step-3", "step-4"])
        # One Kanban card per step, all todo.
        for step in plan["steps"]:
            self.assertIsNotNone(step["card_id"])
            self.assertEqual(self.card_status(step), "todo")

    def test_create_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            self.store.create_plan("x", [])
        with self.assertRaises(ValueError):
            self.store.create_plan("x", ["a"] * 51)
        with self.assertRaises(ValueError):
            self.store.create_plan("", STEPS)
        with self.assertRaises(ValueError):
            self.store.create_plan("x", ["ok", ""])
        with self.assertRaises(ValueError):
            self.store.create_plan("x", STEPS,
                                   origin={"kind": "nope"})

    def test_summary(self):
        plan = self.make_plan()
        summary = plan_summary(plan)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["done"], 0)
        self.assertEqual(summary["pending"], 4)
        self.assertEqual(summary["percent_done"], 0)
        self.assertEqual(summary["current_step_id"], "step-1")
        self.assertEqual(summary["steering_count"], 0)

    def test_persists_across_reload(self):
        plan = self.make_plan()
        again = PlanStore(path=str(self.tmp / "plans.json"),
                          kanban=self.kanban)
        self.assertEqual(again.get_plan(plan["id"])["title"],
                         "Produksi Short")
        self.assertEqual(len(again.list_plans()), 1)


class TestStepTransitions(PlanTestBase):
    def test_running_then_done(self):
        plan = self.make_plan()
        plan = self.store.start_step(plan["id"], "step-1")
        self.assertEqual(plan["steps"][0]["status"], "running")
        self.assertEqual(self.card_status(plan["steps"][0]), "doing")
        summary = plan_summary(plan)
        self.assertEqual(summary["current_step_id"], "step-1")
        self.assertEqual(summary["running"], 1)
        plan = self.store.complete_step(plan["id"], "step-1",
                                        note="riset selesai, 3 sumber")
        self.assertEqual(plan["steps"][0]["status"], "done")
        self.assertEqual(self.card_status(plan["steps"][0]), "done")
        self.assertEqual(plan["status"], "active")  # steps remain
        # The note lands as summary evidence on the card.
        card = self.kanban.get_card(plan["steps"][0]["card_id"])
        self.assertEqual(len(card["evidence"]), 1)
        self.assertEqual(card["evidence"][0]["kind"], "summary")
        self.assertIn("riset selesai", card["evidence"][0]["body"])

    def test_plan_done_when_last_step_completes(self):
        plan = self.make_plan(steps=["Satu langkah"])
        plan = self.store.start_step(plan["id"], "step-1")
        plan = self.store.complete_step(plan["id"], "step-1")
        self.assertEqual(plan["status"], "done")
        self.assertIsNotNone(plan["completed_at"])
        self.assertEqual(plan_summary(plan)["percent_done"], 100)

    def test_one_running_step_at_a_time(self):
        plan = self.make_plan()
        self.store.start_step(plan["id"], "step-1")
        with self.assertRaises(ValueError):
            self.store.start_step(plan["id"], "step-2")

    def test_bad_transitions_raise(self):
        plan = self.make_plan()
        # complete / fail need a running step first.
        with self.assertRaises(ValueError):
            self.store.complete_step(plan["id"], "step-1")
        with self.assertRaises(ValueError):
            self.store.fail_step(plan["id"], "step-1")
        # Unknown plan / step.
        with self.assertRaises(KeyError):
            self.store.start_step("plan-99", "step-1")
        with self.assertRaises(KeyError):
            self.store.start_step(plan["id"], "step-99")

    def test_fail_marks_plan_failed_and_card_blocked(self):
        plan = self.make_plan()
        self.store.start_step(plan["id"], "step-2")
        plan = self.store.fail_step(plan["id"], "step-2",
                                    note="API model menolak request")
        self.assertEqual(plan["steps"][1]["status"], "failed")
        self.assertEqual(plan["status"], "failed")
        self.assertEqual(self.card_status(plan["steps"][1]), "blocked")
        self.assertEqual(plan["steps"][1]["note"],
                         "API model menolak request")
        self.assertEqual(plan_summary(plan)["failed"], 1)
        # A failed plan cannot start another step until steered.
        with self.assertRaises(ValueError):
            self.store.start_step(plan["id"], "step-3")


class TestPauseResume(PlanTestBase):
    def test_pause_returns_running_step_to_pending(self):
        plan = self.make_plan()
        self.store.start_step(plan["id"], "step-1")
        plan = self.store.pause_plan(plan["id"])
        self.assertEqual(plan["status"], "paused")
        self.assertIsNotNone(plan["paused_at"])
        self.assertEqual(plan["steps"][0]["status"], "pending")
        self.assertEqual(self.card_status(plan["steps"][0]), "todo")
        # Nothing can start while paused.
        with self.assertRaises(ValueError):
            self.store.start_step(plan["id"], "step-1")
        with self.assertRaises(ValueError):
            self.store.pause_plan(plan["id"])

    def test_resume_continues_same_step(self):
        plan = self.make_plan()
        self.store.start_step(plan["id"], "step-1")
        self.store.pause_plan(plan["id"])
        plan = self.store.resume_plan(plan["id"])
        self.assertEqual(plan["status"], "active")
        self.assertIsNone(plan["paused_at"])
        # The same step restarts from pending.
        plan = self.store.start_step(plan["id"], "step-1")
        self.assertEqual(plan["steps"][0]["status"], "running")
        plan = self.store.complete_step(plan["id"], "step-1")
        self.assertEqual(plan["steps"][0]["status"], "done")
        with self.assertRaises(ValueError):
            self.store.resume_plan(plan["id"])

    def test_pause_with_no_running_step(self):
        plan = self.make_plan()
        plan = self.store.pause_plan(plan["id"])
        self.assertEqual(plan["status"], "paused")
        plan = self.store.resume_plan(plan["id"])
        self.assertEqual(plan["status"], "active")


class TestSteer(PlanTestBase):
    def test_steer_inserts_steps_after_running(self):
        plan = self.make_plan()
        self.store.start_step(plan["id"], "step-2")
        plan = self.store.steer(
            plan["id"], direction="Tambah pengecekan fakta sebelum VO",
            by="the owner", new_steps=["Cek fakta naskah"])
        titles = [s["title"] for s in plan["steps"]]
        self.assertEqual(
            titles,
            ["Riset topik", "Tulis naskah", "Cek fakta naskah",
             "Rekam VO", "Rakit video"])
        inserted = plan["steps"][2]
        self.assertEqual(inserted["status"], "pending")
        self.assertEqual(self.card_status(inserted), "todo")
        self.assertEqual(len(plan["steering"]), 1)
        entry = plan["steering"][0]
        self.assertEqual(entry["by"], "the owner")
        self.assertEqual(entry["direction"],
                         "Tambah pengecekan fakta sebelum VO")
        self.assertEqual(entry["inserted_step_ids"], ["step-5"])
        self.assertEqual(entry["replaced_step_ids"], [])

    def test_steer_after_explicit_anchor(self):
        plan = self.make_plan()
        plan = self.store.steer(
            plan["id"], direction="Sisipkan thumbnail di akhir",
            by="the owner", after_step_id="step-4",
            new_steps=["Buat thumbnail"])
        self.assertEqual(plan["steps"][-1]["title"], "Buat thumbnail")
        self.assertEqual(plan["steering"][0]["after_step_id"], "step-4")

    def test_steer_replaces_pending_step(self):
        plan = self.make_plan()
        plan = self.store.steer(
            plan["id"], direction="VO diganti teks layar saja",
            by="the owner", replace={"step-3": "Tulis teks layar"})
        self.assertEqual(plan["steps"][2]["title"], "Tulis teks layar")
        self.assertEqual(plan["steps"][2]["status"], "pending")
        self.assertEqual(plan["steering"][0]["replaced_step_ids"],
                         ["step-3"])
        # The card keeps the change as evidence, not a silent edit.
        card = self.kanban.get_card(plan["steps"][2]["card_id"])
        self.assertTrue(any("steer" in e["label"]
                            for e in card["evidence"]))

    def test_steer_rejects_bad_input(self):
        plan = self.make_plan()
        with self.assertRaises(ValueError):  # no change at all
            self.store.steer(plan["id"], direction="ubah", by="the owner")
        with self.assertRaises(ValueError):  # empty direction
            self.store.steer(plan["id"], direction="  ", by="the owner",
                             new_steps=["x"])
        with self.assertRaises(KeyError):  # unknown anchor
            self.store.steer(plan["id"], direction="ubah", by="the owner",
                             after_step_id="step-99", new_steps=["x"])
        with self.assertRaises(ValueError):  # replace a done step
            self.store.start_step(plan["id"], "step-1")
            self.store.complete_step(plan["id"], "step-1")
            self.store.steer(plan["id"], direction="ubah", by="the owner",
                             replace={"step-1": "lain"})

    def test_steer_reopens_failed_plan(self):
        plan = self.make_plan()
        self.store.start_step(plan["id"], "step-1")
        self.store.fail_step(plan["id"], "step-1", note="riset buntu")
        plan = self.store.steer(
            plan["id"], direction="Lewati riset, pakai naskah lama",
            by="the owner", new_steps=["Pakai naskah lama sebagai dasar"])
        self.assertEqual(plan["status"], "active")
        # The failed step stays failed as history.
        self.assertEqual(plan["steps"][0]["status"], "failed")
        plan = self.store.start_step(plan["id"], "step-5")
        self.assertEqual(plan["steps"][4]["status"], "running")

    def test_steer_done_plan_rejected(self):
        plan = self.make_plan(steps=["Satu langkah"])
        self.store.start_step(plan["id"], "step-1")
        self.store.complete_step(plan["id"], "step-1")
        with self.assertRaises(ValueError):
            self.store.steer(plan["id"], direction="ubah", by="the owner",
                             new_steps=["lagi"])


class TestRoutineIntegration(PlanTestBase):
    def test_plan_for_routine_run(self):
        routine = {"id": "routine-3", "character": "chief-officer",
                   "pack": "weekly-pulse",
                   "title": "Weekly project pulse"}
        card = self.kanban.create_card(
            "Weekly project pulse", assignee="chief-officer",
            origin={"kind": "agent", "ref": "routine-3"})
        plan = plan_for_routine_run(routine, card,
                                    plan_store=self.store)
        self.assertEqual(plan["origin"],
                         {"kind": "agent", "ref": "routine-3"})
        self.assertEqual(plan["assignee"], "chief-officer")
        self.assertEqual(plan["status"], "active")
        self.assertEqual(len(plan["steps"]), 3)
        self.assertIn(card["id"], plan["steps"][1]["title"])
        # Explicit steps override the defaults.
        plan2 = plan_for_routine_run(
            routine, card, plan_store=self.store,
            steps=["Kumpulkan kartu minggu ini", "Tulis ringkasan"])
        self.assertEqual([s["title"] for s in plan2["steps"]],
                         ["Kumpulkan kartu minggu ini", "Tulis ringkasan"])

    def test_plan_for_routine_run_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            plan_for_routine_run({}, {"id": "card-1"},
                                 plan_store=self.store)
        with self.assertRaises(ValueError):
            plan_for_routine_run({"id": "routine-1"}, {},
                                 plan_store=self.store)


class TestListAndSummary(PlanTestBase):
    def test_list_filters(self):
        a = self.make_plan()
        b = self.store.create_plan("Lain", ["x"], assignee="yt-analyst")
        self.assertEqual(len(self.store.list_plans()), 2)
        self.assertEqual(
            [p["id"] for p in self.store.list_plans(
                assignee="yt-analyst")], [b["id"]])
        self.store.pause_plan(a["id"])
        self.assertEqual(
            [p["id"] for p in self.store.list_plans(status="paused")],
            [a["id"]])
        with self.assertRaises(ValueError):
            self.store.list_plans(status="nope")

    def test_summary_via_store(self):
        plan = self.make_plan()
        self.assertEqual(self.store.summary(plan["id"])["total"], 4)
        self.assertIsNone(self.store.summary("plan-99"))


class TestZeroTrace(unittest.TestCase):
    """No trace of the reference implementations may leak into
    this module or its patch docs. The forbidden names are built
    from split literals with ``join`` on purpose: plain ``+`` of
    two literals would be constant-folded into the forbidden
    word inside the compiled ``.pyc``, so this very test file
    must not contain them either — in source OR in bytecode."""

    FORBIDDEN = ["".join(["rak", "azo"]), "".join(["open", "dots"]),
                 "".join(["octo", "bot"])]

    def test_no_forbidden_traces(self):
        root = Path(__file__).resolve().parents[1]
        checked = [root / "plan.py", root / "__init__.py",
                   root / "server-wiring.patch.md",
                   root / "deck.patch.md"]
        for path in checked:
            if not path.exists():
                continue  # patch docs land with the builder, not here
            text = path.read_text(encoding="utf-8").lower()
            for bad in self.FORBIDDEN:
                self.assertNotIn(bad, text,
                                 "%s leaks %r" % (path.name, bad))


if __name__ == "__main__":
    unittest.main()
