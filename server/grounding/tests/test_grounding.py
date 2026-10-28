"""Tests for grounding — Work IQ-lite context bundle.

Run from ~/workspace/grokbot-own/:
    python3 -m unittest discover -s grounding/tests -v

Stdlib unittest only. Fixture temp only: MemoryStore + KanbanStore
run on temp files, plain lists elsewhere. No network, no model
calls, no credentials, and the real data/ directory is never
touched.
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from grounding.context import (
    gather,
    make_kanban_reader,
    make_mail_reader,
    make_memory_reader,
    render_bundle_text,
    score_item,
    task_keywords,
    task_phrases,
)
from memory.store import MemoryStore
from office.kanban import KanbanStore

NOW = datetime(2026, 10, 25, 6, 30, tzinfo=timezone.utc)


def _iso(days_ago):
    from datetime import timedelta
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


MEMORY_FIXTURE = [
    {"id": 1, "scope": "team", "author": "chief",
     "text": "Tema minggu ini Wali Songo sebagai metode dakwah, bukan debat",
     "created_at": _iso(2)},
    {"id": 2, "scope": "team", "author": "chief",
     "text": "Jadwal Shorts lima kali sehari jam tujuh pagi",
     "created_at": _iso(1)},
    {"id": 3, "scope": "team", "author": "analyst",
     "text": "Wali Songo episode dua sudah tayang dan ramai komentar",
     "created_at": _iso(20)},
]

MAIL_FIXTURE = [
    {"id": "m-1", "from": "penonton@example.com",
     "subject": "Request episode Wali Songo Sunan Kalijaga",
     "snippet": "Tolong buatkan episode tentang Sunan Kalijaga",
     "date": _iso(1)},
    {"id": "m-2", "from": "toko@example.com",
     "subject": "Promo pupuk tanaman bulan ini",
     "snippet": "Diskon besar untuk semua pupuk organik",
     "date": _iso(1)},
]

KANBAN_FIXTURE = [
    {"id": "card-1", "title": "Naskah episode 2 Wali Songo",
     "body": "Revisi closing naskah dakwah Wali Songo",
     "status": "doing", "assignee": "chief", "priority": 2,
     "origin": {"kind": "manual"}, "updated_at": _iso(1)},
    {"id": "card-2", "title": "Beli pupuk untuk kebun",
     "body": "Pupuk kandang dua karung",
     "status": "todo", "assignee": None, "priority": 1,
     "origin": {"kind": "manual"}, "updated_at": _iso(1)},
]

TASK = "Tulis naskah Wali Songo episode 3"


class TestKeywords(unittest.TestCase):
    def test_stopwords_dropped(self):
        kws = task_keywords("Tulis naskah Wali Songo episode 3")
        self.assertEqual(kws, ["tulis", "naskah", "wali", "songo",
                               "episode"])
        self.assertNotIn("3", kws)  # single char dropped

    def test_phrases_are_adjacent_pairs(self):
        self.assertEqual(
            task_phrases("Tulis naskah Wali Songo episode 3"),
            ["tulis naskah", "naskah wali", "wali songo",
             "songo episode"])


class TestScoreItem(unittest.TestCase):
    def test_relevant_scores_higher_than_irrelevant(self):
        kws = task_keywords(TASK)
        phr = task_phrases(TASK)
        good, _ = score_item(kws, phr, MEMORY_FIXTURE[0]["text"],
                             created_at=MEMORY_FIXTURE[0]["created_at"],
                             now=NOW)
        bad, reasons = score_item(kws, phr, MEMORY_FIXTURE[1]["text"],
                                  created_at=MEMORY_FIXTURE[1]["created_at"],
                                  now=NOW)
        self.assertGreater(good, bad)
        self.assertEqual(bad, 0.0)
        self.assertEqual(reasons, [])

    def test_phrase_match_boosts_score(self):
        kws = task_keywords(TASK)
        phr = task_phrases(TASK)
        with_phrase, _ = score_item(
            kws, phr, "Wali Songo sebagai metode dakwah", now=NOW)
        without_phrase, _ = score_item(
            kws, phr, "Wali dan Songo dibahas terpisah", now=NOW)
        # Same keyword hits (wali, songo); only the phrase differs.
        self.assertGreater(with_phrase, without_phrase)

    def test_recency_newer_scores_higher_same_keywords(self):
        kws = task_keywords(TASK)
        phr = task_phrases(TASK)
        fresh, _ = score_item(kws, phr, "naskah Wali Songo",
                              created_at=_iso(2), now=NOW)
        old, _ = score_item(kws, phr, "naskah Wali Songo",
                            created_at=_iso(60), now=NOW)
        self.assertGreater(fresh, old)

    def test_score_bounded_0_to_1(self):
        kws = task_keywords(TASK)
        phr = task_phrases(TASK)
        score, _ = score_item(kws, phr, TASK + " " + TASK,
                              created_at=_iso(0), now=NOW)
        self.assertLessEqual(score, 1.0)
        self.assertGreaterEqual(score, 0.0)


class TestGather(unittest.TestCase):
    def _sources(self):
        return {"memory": list(MEMORY_FIXTURE),
                "mail": list(MAIL_FIXTURE),
                "kanban": list(KANBAN_FIXTURE)}

    def test_relevant_pulled_irrelevant_dropped(self):
        bundle = gather(TASK, sources=self._sources(), now=NOW)
        # memory:3 (3 kata kunci + 2 frasa) lebih relevan dari
        # memory:1 (2 kata kunci + 1 frasa), walau lebih lama.
        self.assertEqual([i["ref"] for i in bundle["memory"]],
                         ["memory:3", "memory:1"])
        self.assertEqual([i["ref"] for i in bundle["mail"]],
                         ["mail:m-1"])
        self.assertEqual([i["ref"] for i in bundle["kanban"]],
                         ["kanban:card-1"])
        self.assertEqual(bundle["counts"],
                         {"memory": 2, "mail": 1, "kanban": 1})

    def test_relevant_scores_above_irrelevant(self):
        # memory:3 (3 kata kunci + 2 frasa) harus outscore memory:1
        # (2 kata kunci + 1 frasa), walau memory:1 lebih baru.
        bundle = gather(TASK, sources=self._sources(), now=NOW)
        scores = {i["ref"]: i["score"] for i in bundle["memory"]}
        self.assertGreater(scores["memory:3"], scores["memory:1"])

    def test_reasons_filled_for_every_item(self):
        bundle = gather(TASK, sources=self._sources(), now=NOW)
        for source in ("memory", "mail", "kanban"):
            for item in bundle[source]:
                self.assertTrue(item["reasons"],
                                "no reasons for %s" % item["ref"])
                self.assertTrue(all(isinstance(r, str)
                                    for r in item["reasons"]))
                self.assertIn("kata kunci cocok", item["reasons"][0])

    def test_cap_respected(self):
        many = [{"id": i, "scope": "team", "author": "chief",
                 "text": "naskah Wali Songo episode %d" % i,
                 "created_at": _iso(1)} for i in range(8)]
        bundle = gather(TASK, sources={"memory": many}, per_source_cap=5,
                        now=NOW)
        self.assertEqual(len(bundle["memory"]), 5)
        self.assertEqual(bundle["counts"]["memory"], 5)

    def test_cap_zero_gives_empty_lists(self):
        bundle = gather(TASK, sources=self._sources(), per_source_cap=0,
                        now=NOW)
        self.assertEqual(bundle["counts"],
                         {"memory": 0, "mail": 0, "kanban": 0})

    def test_empty_sources_give_empty_bundle_not_error(self):
        bundle = gather(TASK, sources={}, now=NOW)
        self.assertEqual(bundle["memory"], [])
        self.assertEqual(bundle["mail"], [])
        self.assertEqual(bundle["kanban"], [])
        self.assertEqual(bundle["counts"],
                         {"memory": 0, "mail": 0, "kanban": 0})
        self.assertEqual(bundle["sources_available"],
                         {"memory": True, "mail": True, "kanban": True})
        bundle2 = gather(TASK, sources=None, now=NOW)
        self.assertEqual(bundle2["counts"],
                         {"memory": 0, "mail": 0, "kanban": 0})

    def test_empty_task_gives_empty_bundle(self):
        bundle = gather("", sources=self._sources(), now=NOW)
        self.assertEqual(bundle["counts"],
                         {"memory": 0, "mail": 0, "kanban": 0})
        bundle = gather("   ", sources=self._sources(), now=NOW)
        self.assertEqual(bundle["counts"],
                         {"memory": 0, "mail": 0, "kanban": 0})

    def test_deterministic_same_input_same_output(self):
        first = gather(TASK, sources=self._sources(), now=NOW)
        second = gather(TASK, sources=self._sources(), now=NOW)
        self.assertEqual(first, second)

    def test_broken_source_does_not_kill_bundle(self):
        def boom(task_text):
            raise RuntimeError("db down")

        bundle = gather(TASK, sources={
            "memory": boom, "mail": list(MAIL_FIXTURE)}, now=NOW)
        self.assertEqual(bundle["memory"], [])
        self.assertEqual(len(bundle["mail"]), 1)
        self.assertFalse(bundle["sources_available"]["memory"])
        self.assertIn("db down", bundle["errors"]["memory"])
        self.assertTrue(bundle["sources_available"]["mail"])

    def test_callable_readers_receive_task_text(self):
        seen = {}

        def reader(task_text):
            seen["task"] = task_text
            return list(MEMORY_FIXTURE)

        gather(TASK, sources={"memory": reader}, now=NOW)
        self.assertEqual(seen["task"], TASK)

    def test_render_bundle_text(self):
        bundle = gather(TASK, sources=self._sources(), now=NOW)
        text = render_bundle_text(bundle)
        self.assertIn("Konteks tugas", text)
        self.assertIn("memory:3", text)
        self.assertIn("mail:m-1", text)
        # Bundle kosong: tiap seksi dirender "(tidak ada)".
        empty = gather(TASK, sources={}, now=NOW)
        self.assertEqual(render_bundle_text(empty).count("(tidak ada)"), 3)


class TestRealStoreAdapters(unittest.TestCase):
    """Adapters over the real stores, on temp files only."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="grounding-test-"))
        self.db_path = str(self.tmp / "memory.db")
        self.kanban_path = str(self.tmp / "kanban.json")
        self.store = MemoryStore(self.db_path)
        self.store.remember("team",
                            "Tema minggu ini Wali Songo sebagai metode dakwah",
                            "chief")
        self.store.remember("team", "Jadwal Shorts lima kali sehari",
                            "chief")
        self.store.remember("private:chief",
                            "Catatan chief: naskah Wali Songo butuh closing kuat",
                            "chief")
        self.kanban = KanbanStore(path=self.kanban_path)
        self.kanban.create_card("Naskah episode 2 Wali Songo",
                                body="Revisi closing dakwah")
        self.kanban.create_card("Beli pupuk untuk kebun")
        done = self.kanban.create_card("Naskah episode 1 Wali Songo")
        self.kanban.move_card(done["id"], "done")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_memory_reader_team_plus_private(self):
        reader = make_memory_reader(self.store, bot="chief")
        bundle = gather(TASK, sources={
            "memory": reader,
            "mail": make_mail_reader(lambda: list(MAIL_FIXTURE)),
            "kanban": make_kanban_reader(self.kanban),
        }, now=NOW)
        refs = [i["ref"] for i in bundle["memory"]]
        self.assertEqual(len(refs), 2)  # team Wali Songo + private chief
        self.assertTrue(any(r.startswith("memory:") for r in refs))
        self.assertEqual([i["ref"] for i in bundle["mail"]], ["mail:m-1"])
        # Done card is history, not running work: not grounded.
        self.assertEqual([i["ref"] for i in bundle["kanban"]],
                         ["kanban:card-1"])

    def test_memory_reader_without_bot_reads_team_only(self):
        reader = make_memory_reader(self.store)
        bundle = gather(TASK, sources={"memory": reader}, now=NOW)
        self.assertEqual(len(bundle["memory"]), 1)
        self.assertEqual(bundle["memory"][0]["extra"]["scope"], "team")


class TestZeroTrace(unittest.TestCase):
    """BYO invariant: no trace of the forbidden upstream names.

    The forbidden strings are written here as split literals so
    this very test does not itself contain them.
    """

    FORBIDDEN = ("rak" + "azo", "open" + "dots", "octo" + "bot",
                 "m3" + "65", "micro" + "soft 365")

    def test_no_forbidden_trace_in_module_or_patch(self):
        repo = Path(__file__).resolve().parents[2]
        files = [repo / "grounding" / "context.py",
                 repo / "grounding" / "__init__.py",
                 repo / "grounding" / "README.md",
                 repo / "grounding" / "server-wiring.patch.md",
                 Path(__file__)]
        for path in files:
            text = path.read_text(encoding="utf-8").lower()
            for bad in self.FORBIDDEN:
                self.assertNotIn(bad, text,
                                 "%s found in %s" % (bad, path))

    def test_no_network_or_model_imports_in_module(self):
        # Only import lines count: docstrings may SAY "no
        # embedding / no network" without importing anything.
        repo = Path(__file__).resolve().parents[2]
        text = (repo / "grounding" / "context.py").read_text(
            encoding="utf-8")
        imports = [ln for ln in text.splitlines()
                   if ln.strip().startswith(("import ", "from "))]
        joined = "\n".join(imports)
        for banned in ("urllib", "requests", "socket", "http.client",
                       "openai", "anthropic", "embedding", "numpy"):
            self.assertNotIn(banned, joined)


if __name__ == "__main__":
    unittest.main()
