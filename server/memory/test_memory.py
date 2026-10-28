"""Real tests for the Grok Bot own-build memory service.

Stdlib ``unittest`` only. Every test uses a temporary SQLite file
created in a temp dir, so the suite never writes to the repo's
``data/memory.db`` and never touches real user data -- all fixture
rows are clearly-labelled TEST rows.

Run from ``~/workspace/grokbot-own/``::

    python3 -m unittest memory.test_memory -v

What is proven here:
  1. store + recall round-trip
  2. scope isolation (private recall never leaks another character)
  3. list (newest first, superseded rows flagged, not hidden)
  4. forget (deletes; forgetting a newer row revives the older one)
  5. newest-supersedes-old (recall returns only the newest)
  6. fully offline operation (no network/provider/vector imports;
     everything works on a local temp file with stdlib only)
  7. ranking: keyword match count beats recency; recency breaks ties
  8. validation: bad scopes, empty text/author, cross-scope and
     already-superseded supersede attempts are rejected
"""

import ast
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.store import (  # noqa: E402
    DEFAULT_DB_PATH,
    MemoryStore,
    private_scope,
    validate_scope,
)

BANNED_IMPORTS = {
    "socket", "urllib", "http", "requests", "httpx", "openai",
    "anthropic", "numpy", "faiss", "chromadb", "qdrant_client",
}


def _temp_store(testcase):
    tmpdir = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmpdir.cleanup)
    return MemoryStore(os.path.join(tmpdir.name, "test-memory.db"))


class TestRoundTrip(unittest.TestCase):
    def test_store_and_recall(self):
        with _temp_store(self) as s:
            mid = s.remember("team", "TEST: weekly theme is Sejarah Islam Nusantara",
                             "tester")
            self.assertIsInstance(mid, int)
            rows = s.recall("team", "theme Sejarah")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], mid)
            self.assertEqual(rows[0]["author"], "tester")
            self.assertIn("Sejarah Islam Nusantara", rows[0]["text"])
            # Non-matching keywords return nothing, never the whole store.
            self.assertEqual(s.recall("team", "zebra quantum"), [])
            # Empty keywords return live rows newest-first.
            self.assertEqual([r["id"] for r in s.recall("team", "")], [mid])


class TestScopeIsolation(unittest.TestCase):
    def test_private_recall_never_leaks(self):
        with _temp_store(self) as s:
            s.remember(private_scope("chief"), "TEST chief quokka vault plan", "chief")
            s.remember(private_scope("scriptwriter"), "TEST writer secret plan", "scriptwriter")
            s.remember("team", "TEST shared plan", "chief")
            chief = s.recall(private_scope("chief"), "quokka vault plan")
            self.assertEqual(len(chief), 1)
            self.assertEqual(chief[0]["scope"], private_scope("chief"))
            # The other character's private scope sees none of chief's
            # rows: 'quokka'/'vault' occur ONLY in chief's private row.
            self.assertEqual(
                s.recall(private_scope("scriptwriter"), "quokka vault"), [])
            writer_rows = s.recall(private_scope("scriptwriter"), "secret plan")
            self.assertTrue(all(r["scope"] == private_scope("scriptwriter")
                                for r in writer_rows))
            # Team scope sees only team rows, never private rows.
            team = s.recall("team", "plan")
            self.assertTrue(all(r["scope"] == "team" for r in team))
            # Listing one scope never lists another scope's rows.
            self.assertTrue(all(r["scope"] == private_scope("chief")
                                for r in s.list(private_scope("chief"))))

    def test_scope_names(self):
        self.assertEqual(private_scope("chief"), "private:chief")
        self.assertEqual(validate_scope("team"), "team")
        for bad in ("", "private:", "public", "private:chief writer",
                    "private:chief/evil", "TEAM", "team:extra"):
            with self.assertRaises(ValueError, msg=bad):
                validate_scope(bad)


class TestListAndForget(unittest.TestCase):
    def test_list_newest_first_and_forget(self):
        with _temp_store(self) as s:
            first = s.remember("team", "TEST first note", "tester")
            second = s.remember("team", "TEST second note", "tester")
            listed = s.list("team")
            self.assertEqual([r["id"] for r in listed], [second, first])
            self.assertFalse(listed[0]["is_superseded"])
            self.assertTrue(s.forget(second))
            self.assertEqual([r["id"] for r in s.list("team")], [first])
            self.assertFalse(s.forget(999999))  # unknown id: no crash, False

    def test_forget_newer_revives_older(self):
        with _temp_store(self) as s:
            old = s.remember("team", "TEST old rule applies", "tester")
            new = s.remember("team", "TEST new rule applies", "tester",
                             supersedes_id=old)
            self.assertEqual(s.recall("team", "rule"), [s.recall("team", "rule")[0]])
            s.forget(new)
            revived = s.recall("team", "rule")
            self.assertEqual([r["id"] for r in revived], [old])


class TestSupersede(unittest.TestCase):
    def test_newest_supersedes_old(self):
        with _temp_store(self) as s:
            old = s.remember("team", "TEST schedule is Monday morning", "tester")
            new = s.remember("team", "TEST schedule is Tuesday morning", "tester",
                             supersedes_id=old)
            # Recall returns ONLY the newest; the old row is not an answer.
            hits = s.recall("team", "schedule morning")
            self.assertEqual([r["id"] for r in hits], [new])
            self.assertIn("Tuesday", hits[0]["text"])
            # But history is preserved: list still shows the old row,
            # flagged as superseded by the new row.
            listed = {r["id"]: r for r in s.list("team")}
            self.assertTrue(listed[old]["is_superseded"])
            self.assertEqual(listed[old]["superseded_by"], new)
            self.assertFalse(listed[new]["is_superseded"])
            # Audit view can still show the superseded row on request.
            audit = s.recall("team", "schedule", include_superseded=True)
            self.assertEqual({r["id"] for r in audit}, {old, new})

    def test_supersede_validation(self):
        with _temp_store(self) as s:
            old = s.remember("team", "TEST team rule", "tester")
            other = s.remember(private_scope("chief"), "TEST chief rule", "chief")
            with self.assertRaises(ValueError):
                s.remember("team", "TEST bad", "tester", supersedes_id=999999)
            with self.assertRaises(ValueError):  # cross-scope supersede
                s.remember("team", "TEST bad", "tester", supersedes_id=other)
            s.remember("team", "TEST newer rule", "tester", supersedes_id=old)
            with self.assertRaises(ValueError):  # already superseded
                s.remember("team", "TEST bad", "tester", supersedes_id=old)


class TestRanking(unittest.TestCase):
    def test_keyword_count_beats_recency(self):
        with _temp_store(self) as s:
            weak = s.remember("team", "TEST wali note", "tester")
            strong = s.remember("team", "TEST wali songo dakwah note", "tester")
            hits = s.recall("team", "wali songo dakwah")
            self.assertEqual(hits[0]["id"], strong)
            self.assertGreater(hits[0]["score"], hits[1]["score"])
            self.assertEqual(hits[1]["id"], weak)

    def test_recency_breaks_ties(self):
        with _temp_store(self) as s:
            first = s.remember("team", "TEST apple note", "tester")
            second = s.remember("team", "TEST apple note", "tester")
            hits = s.recall("team", "apple")
            self.assertEqual([r["id"] for r in hits], [second, first])


class TestOffline(unittest.TestCase):
    def test_no_network_or_provider_imports(self):
        """Static proof: store.py imports stdlib only, nothing networked."""
        here = os.path.dirname(os.path.abspath(__file__))
        for fname in ("store.py", "cli.py"):
            with open(os.path.join(here, fname)) as fh:
                tree = ast.parse(fh.read())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            banned = imported & BANNED_IMPORTS
            self.assertEqual(banned, set(), msg="%s imports %s" % (fname, banned))

    def test_default_db_lives_under_repo_data(self):
        self.assertTrue(DEFAULT_DB_PATH.endswith(
            os.path.join("grokbot-own", "data", "memory.db")),
            msg=DEFAULT_DB_PATH)

    def test_works_on_local_temp_file_with_stdlib_only(self):
        # No network setup of any kind is involved: a plain local
        # SQLite file in a temp dir stores, recalls, lists, forgets.
        with _temp_store(self) as s:
            mid = s.remember("team", "TEST offline proof row", "tester")
            self.assertEqual(s.recall("team", "offline")[0]["id"], mid)
            self.assertEqual(len(s.list("team")), 1)
            self.assertTrue(s.forget(mid))
            self.assertEqual(s.list("team"), [])


class TestValidation(unittest.TestCase):
    def test_empty_text_author_rejected(self):
        with _temp_store(self) as s:
            with self.assertRaises(ValueError):
                s.remember("team", "   ", "tester")
            with self.assertRaises(ValueError):
                s.remember("team", "TEST note", "   ")


if __name__ == "__main__":
    unittest.main(verbosity=2)
