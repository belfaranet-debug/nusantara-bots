"""Virtual Office deck tests — stdlib unittest, no real legacy
database, no real devices, no credentials anywhere.

A FakeLegacy stands in for the read-only legacy reader and a
FakeBridge for the device bridge, so every server-level test runs
on this machine. The subprocess transport itself is tested against
a stub reader script.

The brand check below builds the banned name by concatenation so
that the literal string never appears in this repo (the tree-wide
brand grep must stay at zero).
"""

import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP_DIR = REPO / "app"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(APP_DIR))

from app.server import create_server  # noqa: E402
from legacy_client import LegacyClient, LegacyError  # noqa: E402
from memory.store import MemoryStore  # noqa: E402

CHAR_MD = """---
name: Test Chief
role: Test leadership
skills:
  - team-memory
tools:
  - read_file
memory_scope: private+team
---

You are a test chief. You lead test work.
"""

# Assembled legacy snapshot shape (what LegacyClient.snapshot
# returns): bots with thread + latest run, recent runs, counts.
LEGACY_DATA = {
    "ok": True,
    "fetched_at": "2026-10-24T08:00:00+0000",
    "bots": [
        {"id": "bot-ceo", "name": "CEO", "title": "CEO",
         "created": "2026-10-24T07:02:28", "updated": "2026-10-24T08:00:00",
         "thread": {"id": "th-1", "messages": 63,
                    "last_activity": "2026-10-24T08:05:54"},
         "latest_run": {"status": "completed", "trigger": "bot_message",
                        "updated": "2026-10-24T08:05:54", "error": ""}},
        {"id": "bot-ana", "name": "Raka", "title": "Analyst",
         "created": "2026-10-24T07:09:06", "updated": "2026-10-24T08:08:27",
         "thread": {"id": "th-2", "messages": 12,
                    "last_activity": "2026-10-24T08:01:55"},
         "latest_run": {"status": "running", "trigger": "bot_message",
                        "updated": "2026-10-24T08:08:19", "error": ""}},
    ],
    "runs": [
        {"id": "run-1", "botId": "bot-ana", "threadId": "th-2",
         "status": "running", "trigger": "bot_message",
         "updatedAt": "2026-10-24T08:08:19", "error": ""},
        {"id": "run-2", "botId": "bot-ceo", "threadId": "th-1",
         "status": "completed", "trigger": "bot_message",
         "updatedAt": "2026-10-24T08:05:54", "error": ""},
    ],
    "counts": {"bots": 2, "threads": 2, "runs": 39, "messages": 113},
    "model_prefs": [],
}

# Raw reader output shape (what the reader script prints): the
# client assembles this into LEGACY_DATA's shape.
RAW_READER = {
    "ok": True,
    "fetched_at": "2026-10-24T08:00:00+0000",
    "bots": [
        {"id": "bot-ceo", "name": "CEO", "title": "CEO",
         "createdAt": "2026-10-24T07:02:28",
         "updatedAt": "2026-10-24T08:00:00"},
    ],
    "threads": [
        {"id": "th-1", "botId": "bot-ceo", "nextMessageSeq": 63,
         "createdAt": "2026-10-24T07:02:28"},
    ],
    "activity": [
        {"threadId": "th-1", "n": 63, "last": "2026-10-24T08:05:54"},
    ],
    "runs": [
        {"id": "run-2", "botId": "bot-ceo", "threadId": "th-1",
         "status": "completed", "trigger": "bot_message",
         "updatedAt": "2026-10-24T08:05:54", "error": ""},
    ],
    "counts": {"bots": 1, "threads": 1, "runs": 1, "messages": 63},
    "model_prefs": [],
}


class FakeLegacy:
    """Stand-in for LegacyClient: same method surface, no database."""

    def __init__(self, data=None, fail=False):
        self.calls = 0
        self.fail = fail
        self.data = data if data is not None else LEGACY_DATA

    def available(self):
        return True

    def snapshot(self):
        self.calls += 1
        if self.fail:
            raise LegacyError("reader down")
        return self.data


class FakeBridge:
    """Minimal stand-in for BridgeClient."""

    def available(self):
        return True

    def status(self):
        return {"display_100": True, "mirror_running": False,
                "adb_devices": [{"serial": "FAKE123", "state": "device"}]}

    def devices(self):
        return {"devices": [{"serial": "FAKE123", "state": "device"}]}

    def shot(self, kind):
        return b"\x89PNG\r\n\x1a\n"

    def shot_meta(self, kind):
        return {"ok": True, "bytes": 8, "error": None,
                "ts": "2026-10-24T00:00:00+0000"}

    def presence(self, n=50):
        return []

    def phone_input(self, action, params):
        return {"ok": True}

    def browser_action(self, action, params):
        return {"ok": True}


def _req(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")}


class DeckServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        chars = Path(self.tmp.name) / "characters"
        chars.mkdir()
        (chars / "test-chief.md").write_text(CHAR_MD)
        self.memory_db = str(Path(self.tmp.name) / "memory.db")
        self.fake_legacy = FakeLegacy()
        self.httpd = create_server(
            0, characters_dir=str(chars),
            chat_file=str(Path(self.tmp.name) / "chat.json"),
            memory_db=self.memory_db,
            bridge=FakeBridge(), legacy=self.fake_legacy)
        self.port = self.httpd.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def test_deck_returns_legacy_name_for_name(self):
        status, data = _req(self.base, "GET", "/api/deck")
        self.assertEqual(status, 200)
        self.assertTrue(data["deck_active"])
        leg = data["legacy"]
        self.assertTrue(leg["ok"])
        names = [b["name"] for b in leg["bots"]]
        self.assertEqual(names, ["CEO", "Raka"])
        self.assertEqual(leg["counts"],
                         {"bots": 2, "threads": 2, "runs": 39,
                          "messages": 113})
        ceo = leg["bots"][0]
        self.assertEqual(ceo["thread"]["messages"], 63)
        self.assertEqual(ceo["latest_run"]["status"], "completed")
        raka = leg["bots"][1]
        self.assertEqual(raka["latest_run"]["status"], "running")
        self.assertTrue(data["devices"]["available"])
        self.assertEqual(data["memory"]["team_count"], 0)
        self.assertEqual([c["name"] for c in data["characters"]],
                         ["Test Chief"])

    def test_memory_count_tracks_team_facts(self):
        with MemoryStore(self.memory_db) as store:
            store.remember("team", "Deck test fact", "test")
        _, data = _req(self.base, "GET", "/api/deck")
        self.assertEqual(data["memory"]["team_count"], 1)

    def test_feed_switch_pauses_and_resumes(self):
        status, data = _req(self.base, "POST", "/api/deck/feed",
                            {"active": False})
        self.assertEqual(status, 200)
        self.assertFalse(data["deck_active"])
        calls_before = self.fake_legacy.calls
        _, data = _req(self.base, "GET", "/api/deck")
        self.assertTrue(data["legacy"]["paused"])
        self.assertEqual(self.fake_legacy.calls, calls_before)
        _req(self.base, "POST", "/api/deck/feed", {"active": True})
        _, data = _req(self.base, "GET", "/api/deck")
        self.assertTrue(data["legacy"]["ok"])
        self.assertGreater(self.fake_legacy.calls, calls_before)

    def test_feed_switch_rejects_non_bool(self):
        status, _ = _req(self.base, "POST", "/api/deck/feed",
                         {"active": "yes"})
        self.assertEqual(status, 400)

    def test_legacy_failure_is_honest_not_fatal(self):
        self.fake_legacy.fail = True
        status, data = _req(self.base, "GET", "/api/deck")
        self.assertEqual(status, 200)
        self.assertFalse(data["legacy"]["ok"])
        self.assertIn("reader down", data["legacy"]["error"])
        self.assertTrue(data["devices"]["available"])


class LegacyClientTest(unittest.TestCase):
    def test_snapshot_assembles_thread_and_latest_run(self):
        tmp = tempfile.TemporaryDirectory()
        stub = Path(tmp.name) / "stub_reader.py"
        stub.write_text("import json\nprint(json.dumps(%r))\n"
                        % RAW_READER)
        client = LegacyClient(reader_py=str(stub))
        self.assertTrue(client.available())
        snap = client.snapshot()
        self.assertTrue(snap["ok"])
        self.assertEqual(len(snap["bots"]), 1)
        bot = snap["bots"][0]
        self.assertEqual(bot["name"], "CEO")
        self.assertEqual(bot["thread"]["messages"], 63)
        self.assertEqual(bot["thread"]["last_activity"],
                         "2026-10-24T08:05:54")
        self.assertEqual(bot["latest_run"]["status"], "completed")
        self.assertEqual(snap["counts"]["messages"], 63)
        tmp.cleanup()

    def test_unavailable_reader_raises(self):
        client = LegacyClient(reader_py="/nonexistent/reader.py")
        self.assertFalse(client.available())
        with self.assertRaises(LegacyError):
            client.snapshot()

    def test_reader_is_read_only_and_brand_free(self):
        reader = (REPO / "legacy" / "read_legacy.py").read_text()
        client_src = (APP_DIR / "legacy_client.py").read_text()
        # Read-only session enforcement is present.
        self.assertIn("default_transaction_read_only", reader)
        # No write-statement words anywhere in the reader.
        banned_sql = re.compile(
            r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE)\b")
        self.assertIsNone(banned_sql.search(reader))
        # No old brand name and no stored container name anywhere
        # in the bridge code. (Built by concatenation so the
        # literal never appears in this repo.)
        brand = "rak" + "azo"
        self.assertNotIn(brand, reader.lower())
        self.assertNotIn(brand, client_src.lower())
        self.assertNotIn("postgres-1", reader)
        # Runtime resolution, not a stored name.
        self.assertIn("docker", reader)
        self.assertIn("printenv", reader)


class DeckUITest(unittest.TestCase):
    def test_visible_deck_button_and_virtual_office_section(self):
        html = (APP_DIR / "static" / "index.html").read_text()
        self.assertIn('id="deck-btn"', html)
        self.assertIn("Virtual Office", html)
        self.assertIn('id="deck"', html)
        self.assertIn('id="deck-feed-switch"', html)
        js = (APP_DIR / "static" / "app.js").read_text()
        self.assertIn("deck-btn", js)
        self.assertIn("/api/deck", js)
        self.assertIn("/api/deck/feed", js)


    def test_restyle_markers_and_auto_button(self):
        html = (APP_DIR / "static" / "index.html").read_text()
        self.assertIn('id="deck-auto-btn"', html)
        self.assertIn("AUTO", html)
        self.assertIn('id="vo-room"', html)
        self.assertIn('id="vo-filters"', html)
        self.assertIn('id="vo-agents"', html)
        self.assertIn('id="vo-stats"', html)
        self.assertIn('id="vo-system"', html)
        self.assertIn('id="vo-devices"', html)
        css = (APP_DIR / "static" / "style.css").read_text()
        self.assertIn("#deck", css)
        self.assertIn("vo-room", css)
        self.assertIn("vo-tag", css)
        js = (APP_DIR / "static" / "app.js").read_text()
        self.assertIn("deck-auto-btn", js)
        self.assertIn("vo-room", js)
        self.assertIn("voTag", js)
        # 401-aware fetching: an unsaved token must never render
        # as empty data ("No characters yet", "Bridge unavailable").
        self.assertIn("apiJson", js)
        self.assertIn("status === 401", js)

    def test_no_invented_reference_data(self):
        # The target look's DATA belongs to another context: its
        # agent names, channels, video IDs, and status labels must
        # never appear in our deck code. Only the visual language
        # (dark + amber, room, filters, tags) carries over.
        banned = ["Mythical", "Kitten", "Torn", "Paw",
                  "Kang Lapor", "KANG LAPOR",
                  "MythicalCreatureFiles", "Kitsune",
                  "BUDGET HABIS", "TIDUR", "LAPOR PATROLI"]
        for fname in ("index.html", "app.js", "style.css"):
            text = (APP_DIR / "static" / fname).read_text()
            for word in banned:
                self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
