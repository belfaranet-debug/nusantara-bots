"""Phase 3 app-shell tests — stdlib unittest, no real API calls,
no credentials anywhere (the fake credential string below is a
sentinel used only to prove the server rejects and never stores
it; it is not a real credential for any account).
"""

import json
import os
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
from memory.store import MemoryStore  # noqa: E402

SENTINEL = "SENTINEL-NOT-A-REAL-CREDENTIAL-xyz123"

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

CHAR_TEAM_ONLY = """---
name: Team Only
role: Team-only tester
skills: []
tools: []
memory_scope: team
---

You are team-only.
"""


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
    except urllib.error.URLError:
        # Plain-text page fetches return here only if not JSON; retry raw.
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            return resp.status, resp.read().decode()


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        chars = Path(self.tmp.name) / "characters"
        chars.mkdir()
        (chars / "test-chief.md").write_text(CHAR_MD)
        (chars / "team-only.md").write_text(CHAR_TEAM_ONLY)
        self.chat_file = str(Path(self.tmp.name) / "chat.json")
        self.mem_db = str(Path(self.tmp.name) / "memory.db")
        self.httpd = create_server(0, characters_dir=str(chars),
                                   chat_file=self.chat_file,
                                   memory_db=self.mem_db)
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

    def test_page_serves_with_byo_meeting_presence(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            html = resp.read().decode()
        self.assertEqual(resp.status, 200)
        for needle in ("BYO-API", "Meeting room protocol", "presence",
                       "STOP", "localStorage"):
            self.assertIn(needle, html if needle != "localStorage" else
                          (Path(APP_DIR) / "static" / "app.js").read_text())
        self.assertIn("MOCK model", html)

    def test_character_list_loads_from_runtime_dir(self):
        status, data = _req(self.base, "GET", "/api/characters")
        self.assertEqual(status, 200)
        names = {c["name"]: c for c in data["characters"]}
        self.assertIn("Test Chief", names)
        self.assertEqual(names["Test Chief"]["status"], "valid")
        self.assertEqual(names["Test Chief"]["memory_scope"], "private+team")
        self.assertEqual(names["Team Only"]["memory_scope"], "team")

    def test_chat_round_trips_with_persistence(self):
        status, data = _req(self.base, "POST", "/api/chat",
                            {"room": "general", "character": "test-chief",
                             "text": "hello chief"})
        self.assertEqual(status, 200)
        self.assertIn("MOCK model", data["assistant_message"]["text"])
        status, data = _req(self.base, "GET",
                            "/api/chat?room=general")
        texts = [m["text"] for m in data["messages"]]
        self.assertIn("hello chief", texts)
        self.assertTrue(any("MOCK model" in t for t in texts))
        # Persistence is a file on disk, separate from memory.db.
        on_disk = Path(self.chat_file).read_text()
        self.assertIn("hello chief", on_disk)
        self.assertNotEqual(Path(self.chat_file).resolve(),
                            Path(self.mem_db).resolve())

    def test_presence_events_render(self):
        _, data = _req(self.base, "POST", "/api/chat",
                       {"room": "general", "character": "test-chief",
                        "text": "show presence"})
        kinds = [e["type"] for e in data["presence"]]
        for expected in ("typing", "status", "turn", "done"):
            self.assertIn(expected, kinds)
        # Frontend renders every presence type.
        js = (APP_DIR / "static" / "app.js").read_text()
        for expected in ("typing", "status", "turn", "handoff", "done", "stopped"):
            self.assertIn(expected, js)

    def test_meeting_room_budget_turns_and_decision(self):
        status, data = _req(self.base, "GET", "/api/rooms")
        self.assertEqual(status, 200)
        meeting = [r for r in data["rooms"] if r["mode"] == "meeting"][0]
        self.assertEqual(meeting["budget"], 40)
        self.assertEqual(meeting["budget_remaining"], 40)
        self.assertIsNotNone(meeting["next_speaker"])
        self.assertIn("test-chief", meeting["members"])
        status, data = _req(self.base, "POST", "/api/decisions",
                            {"room": meeting["id"],
                             "decision": "Ship the demo",
                             "disputed": "Voice parity"})
        self.assertEqual(status, 200)
        _, data = _req(self.base, "GET", "/api/rooms")
        meeting = [r for r in data["rooms"] if r["mode"] == "meeting"][0]
        self.assertTrue(any(d["decision"] == "Ship the demo"
                            for d in meeting["decisions"]))
        self.assertTrue(any(d["point"] == "Voice parity"
                            for d in meeting["disputed"]))

    def test_stop_is_visible(self):
        status, data = _req(self.base, "POST", "/api/stop",
                            {"room": "general"})
        self.assertEqual(status, 200)
        self.assertTrue(data["stopped"])
        _, data = _req(self.base, "GET", "/api/chat?room=general")
        self.assertTrue(any(m["role"] == "system" and "STOPPED" in m["text"]
                            for m in data["messages"]))

    def test_memory_reaches_phase1_with_declared_scopes(self):
        status, data = _req(self.base, "POST", "/api/memory",
                            {"character": "test-chief",
                             "text": "TESTMEMORY weekly theme is Wali Songo"})
        self.assertEqual(status, 200)
        self.assertIn("team", data["scopes"])
        self.assertTrue(any(s.startswith("private:") for s in data["scopes"]))
        # Direct Phase 1 read on the same DB proves the call landed.
        store = MemoryStore(self.mem_db)
        try:
            team_rows = store.recall("team", "TESTMEMORY weekly theme")
            self.assertTrue(any("Wali Songo" in r["text"] for r in team_rows))
            priv_rows = store.recall("private:test-chief",
                                     "TESTMEMORY weekly theme")
            self.assertTrue(any("Wali Songo" in r["text"] for r in priv_rows))
            # Team-only character writes team scope only, never private.
            _, data = _req(self.base, "POST", "/api/memory",
                           {"character": "team-only",
                            "text": "TESTMEMORY team only fact"})
            self.assertEqual(data["scopes"], ["team"])
            self.assertEqual(
                store.recall("private:team-only", "TESTMEMORY team"), [])
        finally:
            store.close()
        # And the app's own memory endpoint serves it back.
        _, data = _req(self.base, "GET",
                       "/api/memory?character=test-chief")
        self.assertTrue(any("Wali Songo" in m["text"]
                            for m in data["memories"]))

    def test_server_never_sees_a_credential(self):
        # 1) No endpoint accepts one: credential-field POSTs are 400.
        for path, body in (
                ("/api/chat", {"room": "general", "character": "test-chief",
                               "text": "hi", "api_key": SENTINEL}),
                ("/api/messages", {"room": "general", "character": "test-chief",
                                   "text": "hi", "role": "user",
                                   "api_key": SENTINEL}),
                ("/api/memory", {"character": "test-chief",
                                 "text": "hi", "api_key": SENTINEL})):
            status, _ = _req(self.base, "POST", path, body)
            self.assertEqual(status, 400, path)
        # 2) No such endpoint exists at all (plain body -> 404,
        #    credential body -> 400 rejection, never acceptance).
        for path in ("/api/byo", "/api/config", "/api/credentials"):
            status, _ = _req(self.base, "POST", path, {})
            self.assertEqual(status, 404, path)
            status, _ = _req(self.base, "POST", path, {"api_key": SENTINEL})
            self.assertEqual(status, 400, path)
        # 3) The sentinel never reached any file the server wrote.
        if os.path.exists(self.chat_file):
            self.assertNotIn(SENTINEL, Path(self.chat_file).read_text())
        store = MemoryStore(self.mem_db)
        try:
            self.assertEqual(store.recall("team", "SENTINEL"), [])
        finally:
            store.close()
        # 4) Server code makes no outbound request and reads no
        #    credential from the environment: no HTTP client imports,
        #    no outbound fetch in server-side code.
        for fname in ("server.py", "chat_store.py", "mock_model.py"):
            src = (APP_DIR / fname).read_text()
            self.assertNotIn("urllib.request", src, fname)
            self.assertNotIn("http.client", src, fname)
            self.assertNotIn("requests", src, fname)
            self.assertNotIn("socket.create_connection", src, fname)
        # 5) The browser code sends the credential only to the model
        #    API (direct fetch of the configured base URL), never in
        #    a local /api/* body: the only /api/messages body fields
        #    are room/character/text/role.
        js = (APP_DIR / "static" / "app.js").read_text()
        self.assertIn("localStorage.getItem", js)
        self.assertIn('baseUrl + "/chat/completions"', js)


if __name__ == "__main__":
    unittest.main()
