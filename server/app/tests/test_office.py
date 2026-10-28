"""Next-level Phase 1 endpoint tests — stdlib unittest, no real
legacy database, no real devices, no real mail bridge, no
credentials anywhere.

Covers: /api/health, /api/kanban (+ cards/move/evidence),
/api/computers (+ status/touch), /api/presence (+ beacon), and the
new /api/deck fields (ages, health, kanban, duty).

A FakeLegacy stands in for the read-only legacy reader, a
FakeBridge for the device bridge, and a FakeMail for the mail
bridge, so every server-level test runs on this machine.
"""

import json
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
from computers import ComputerRegistry  # noqa: E402
from legacy_client import LegacyError  # noqa: E402
from office.kanban import KanbanStore  # noqa: E402
from office.presence import BeaconLog  # noqa: E402

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

BAD_CHAR_MD = """No frontmatter here, so this character is invalid.
"""

LEGACY_DATA = {
    "ok": True,
    "fetched_at": "2026-10-24T08:00:00+0000",
    "bots": [],
    "runs": [],
    "counts": {"bots": 0, "threads": 0, "runs": 0, "messages": 0},
    "model_prefs": [],
}


class FakeLegacy:
    def __init__(self, fail=False):
        self.fail = fail

    def available(self):
        return True

    def snapshot(self):
        if self.fail:
            raise LegacyError("reader down")
        return LEGACY_DATA


class FakeBridge:
    def __init__(self, available=True):
        self._available = available

    def available(self):
        return self._available

    def status(self):
        return {"display_100": True, "mirror_running": False,
                "adb_devices": []}

    def shot(self, kind):
        return b"\x89PNG\r\n\x1a\n"

    def shot_meta(self, kind):
        return {"ok": True, "bytes": 8, "error": None,
                "ts": "2026-10-24T00:00:00+00:00"}

    def presence(self, n=50):
        return []

    def phone_input(self, action, params):
        return {"ok": True}

    def browser_action(self, action, params):
        return {"ok": True}


class FakeMail:
    def summary(self, label):
        return {"label": label, "address": "test-chief@example.invalid",
                "address_present": True, "inbox_count": 0, "unread": 0,
                "available": True, "error": None}


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


class OfficeServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        chars = base / "characters"
        chars.mkdir()
        (chars / "test-chief.md").write_text(CHAR_MD)
        self.kanban = KanbanStore(path=str(base / "kanban.json"))
        self.computers = ComputerRegistry(path=str(base / "computers.json"))
        self.beacons = BeaconLog()
        self.httpd = create_server(
            0, characters_dir=str(chars),
            chat_file=str(base / "chat.json"),
            memory_db=str(base / "memory.db"),
            bridge=FakeBridge(), legacy=FakeLegacy(), mail=FakeMail(),
            kanban=self.kanban, computers=self.computers,
            beacons=self.beacons)
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

    # -- health ------------------------------------------------------
    def test_health_all_ok(self):
        status, data = _req(self.base, "GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["level"], "ok")
        self.assertEqual(data["reasons"], [])
        self.assertEqual(data["version"], "next-level-phase-1")
        self.assertIsInstance(data["uptime_seconds"], int)
        self.assertGreaterEqual(data["uptime_seconds"], 0)
        for name in ("memory", "chat", "characters", "kanban",
                     "computers", "bridge", "legacy", "mail", "deck_feed"):
            self.assertIn(name, data["checks"])
            self.assertTrue(data["checks"][name]["ok"], name)

    def test_health_warn_when_bridge_down(self):
        self.httpd.app_state.bridge = FakeBridge(available=False)
        _, data = _req(self.base, "GET", "/api/health")
        self.assertEqual(data["level"], "warn")
        self.assertTrue(any("bridge" in r for r in data["reasons"]))

    def test_health_warn_when_deck_feed_off(self):
        _req(self.base, "POST", "/api/deck/feed", {"active": False})
        _, data = _req(self.base, "GET", "/api/health")
        self.assertEqual(data["level"], "warn")
        self.assertTrue(any("deck feed" in r for r in data["reasons"]))

    # -- kanban ------------------------------------------------------
    def test_kanban_full_flow(self):
        status, data = _req(self.base, "POST", "/api/kanban/cards",
                            {"title": "Write the brief",
                             "assignee": "test-chief"})
        self.assertEqual(status, 200)
        card = data["card"]
        self.assertEqual(card["id"], "card-1")
        self.assertEqual(card["status"], "todo")

        _, board = _req(self.base, "GET", "/api/kanban")
        self.assertEqual(board["board"]["counts"]["backlog"], 1)
        self.assertEqual(board["board"]["unproven_done"], 0)

        status, moved = _req(self.base, "POST", "/api/kanban/move",
                             {"card_id": "card-1", "status": "doing"})
        self.assertEqual(status, 200)
        self.assertEqual(moved["card"]["status"], "doing")

        status, ev = _req(self.base, "POST", "/api/kanban/evidence",
                          {"card_id": "card-1", "kind": "summary",
                           "label": "Draft done", "by": "test-chief"})
        self.assertEqual(status, 200)
        self.assertEqual(len(ev["card"]["evidence"]), 1)

        status, moved = _req(self.base, "POST", "/api/kanban/move",
                             {"card_id": "card-1", "status": "done"})
        self.assertEqual(status, 200)
        self.assertIsNotNone(moved["card"]["completed_at"])

        _, board = _req(self.base, "GET", "/api/kanban")
        self.assertEqual(board["board"]["counts"]["done"], 1)
        self.assertEqual(board["board"]["unproven_done"], 0)

        # A done card without evidence is unproven.
        _req(self.base, "POST", "/api/kanban/cards",
             {"title": "No proof card"})
        _req(self.base, "POST", "/api/kanban/move",
             {"card_id": "card-2", "status": "done"})
        _, board = _req(self.base, "GET", "/api/kanban")
        self.assertEqual(board["board"]["unproven_done"], 1)

    def test_kanban_rejects_unknown_assignee(self):
        status, data = _req(self.base, "POST", "/api/kanban/cards",
                            {"title": "x", "assignee": "ghost"})
        self.assertEqual(status, 400)

    def test_kanban_rejects_empty_title(self):
        status, _ = _req(self.base, "POST", "/api/kanban/cards",
                         {"title": "   "})
        self.assertEqual(status, 400)

    def test_kanban_move_unknown_card_and_bad_status(self):
        status, _ = _req(self.base, "POST", "/api/kanban/move",
                         {"card_id": "card-99", "status": "doing"})
        self.assertEqual(status, 404)
        _req(self.base, "POST", "/api/kanban/cards", {"title": "x"})
        status, _ = _req(self.base, "POST", "/api/kanban/move",
                         {"card_id": "card-1", "status": "flying"})
        self.assertEqual(status, 400)

    def test_kanban_evidence_bad_kind(self):
        _req(self.base, "POST", "/api/kanban/cards", {"title": "x"})
        status, _ = _req(self.base, "POST", "/api/kanban/evidence",
                         {"card_id": "card-1", "kind": "screenshot",
                          "label": "proof"})
        self.assertEqual(status, 400)
        status, _ = _req(self.base, "POST", "/api/kanban/evidence",
                         {"card_id": "card-99", "kind": "log",
                          "label": "proof"})
        self.assertEqual(status, 404)

    # -- computers ----------------------------------------------------
    def test_computers_full_flow(self):
        status, data = _req(self.base, "POST", "/api/computers",
                            {"label": "test-pc",
                             "capabilities": ["adb", "ffmpeg"]})
        self.assertEqual(status, 200)
        machine = data["machine"]
        self.assertEqual(machine["id"], "local-1")
        self.assertEqual(machine["status"], "active")

        status, listed = _req(self.base, "GET", "/api/computers")
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["machines"]), 1)

        status, paused = _req(self.base, "POST", "/api/computers/status",
                              {"machine_id": "local-1", "status": "paused"})
        self.assertEqual(status, 200)
        self.assertEqual(paused["machine"]["status"], "paused")

        status, touched = _req(self.base, "POST", "/api/computers/touch",
                               {"machine_id": "local-1"})
        self.assertEqual(status, 200)

        status, removed = _req(self.base, "POST", "/api/computers/status",
                               {"machine_id": "local-1", "status": "removed"})
        self.assertEqual(status, 200)
        # Removed is terminal: touch and status changes now fail.
        status, _ = _req(self.base, "POST", "/api/computers/touch",
                         {"machine_id": "local-1"})
        self.assertEqual(status, 400)
        status, _ = _req(self.base, "POST", "/api/computers/status",
                         {"machine_id": "local-1", "status": "active"})
        self.assertEqual(status, 400)

    def test_computers_unknown_machine_404(self):
        status, _ = _req(self.base, "POST", "/api/computers/status",
                         {"machine_id": "local-99", "status": "paused"})
        self.assertEqual(status, 404)
        status, _ = _req(self.base, "POST", "/api/computers/touch",
                         {"machine_id": "local-99"})
        self.assertEqual(status, 404)

    def test_computers_rejects_empty_label(self):
        status, _ = _req(self.base, "POST", "/api/computers",
                         {"label": ""})
        self.assertEqual(status, 400)

    # -- presence ------------------------------------------------------
    def test_presence_beacon_and_snapshot(self):
        status, data = _req(self.base, "POST", "/api/presence/beacon",
                            {"character_id": "test-chief",
                             "state": "active", "note": "writing"})
        self.assertEqual(status, 200)
        self.assertEqual(data["beacon"]["state"], "active")

        status, snap = _req(self.base, "GET", "/api/presence")
        self.assertEqual(status, 200)
        entry = snap["characters"]["test-chief"]
        self.assertEqual(entry["state"], "active")
        self.assertEqual(entry["note"], "writing")
        self.assertLess(entry["age_seconds"], 5)
        self.assertEqual(snap["stale_after_seconds"], 120)

    def test_presence_rejects_unknown_character_and_bad_state(self):
        status, _ = _req(self.base, "POST", "/api/presence/beacon",
                         {"character_id": "ghost", "state": "active"})
        self.assertEqual(status, 404)
        status, _ = _req(self.base, "POST", "/api/presence/beacon",
                         {"character_id": "test-chief", "state": "sleeping"})
        self.assertEqual(status, 400)

    # -- deck additions -------------------------------------------------
    def test_deck_has_new_fields(self):
        status, data = _req(self.base, "GET", "/api/deck")
        self.assertEqual(status, 200)
        self.assertEqual(data["ages"]["legacy"], 0.0)
        self.assertIsNone(data["ages"]["devices"])
        self.assertIsNone(data["ages"]["presence"])
        self.assertIsNone(data["ages"]["kanban"])
        self.assertIsNone(data["ages"]["mail"])
        self.assertEqual(data["stale_after_seconds"], 120)
        self.assertEqual(data["health"]["level"], "ok")
        self.assertEqual(data["kanban"]["counts"],
                         {"backlog": 0, "doing": 0, "review": 0, "done": 0})
        self.assertEqual(data["kanban"]["unproven_done"], 0)
        self.assertEqual(data["duty"], {"test-chief": "idle"})

    def test_deck_duty_follows_cards(self):
        _req(self.base, "POST", "/api/kanban/cards",
             {"title": "Work", "assignee": "test-chief"})
        _req(self.base, "POST", "/api/kanban/move",
             {"card_id": "card-1", "status": "doing"})
        _, data = _req(self.base, "GET", "/api/deck")
        self.assertEqual(data["duty"], {"test-chief": "desk"})
        self.assertIsNotNone(data["ages"]["kanban"])
        self.assertLess(data["ages"]["kanban"], 5)

    def test_deck_duty_meeting_after_meeting_message(self):
        status, _ = _req(self.base, "POST", "/api/chat",
                         {"room": "meeting-room",
                          "character": "test-chief", "text": "hello"})
        self.assertEqual(status, 200)
        _, data = _req(self.base, "GET", "/api/deck")
        self.assertEqual(data["duty"], {"test-chief": "meeting"})

    def test_deck_presence_age_tracks_beacon(self):
        _req(self.base, "POST", "/api/presence/beacon",
             {"character_id": "test-chief", "state": "idle"})
        _, data = _req(self.base, "GET", "/api/deck")
        self.assertIsNotNone(data["ages"]["presence"])
        self.assertLess(data["ages"]["presence"], 5)

    def test_deck_health_warn_when_legacy_fails(self):
        self.httpd.app_state.legacy = FakeLegacy(fail=True)
        _, data = _req(self.base, "GET", "/api/deck")
        self.assertFalse(data["legacy"]["ok"])
        self.assertEqual(data["health"]["level"], "warn")
        self.assertTrue(any("legacy" in r for r in data["health"]["reasons"]))
        self.assertIsNone(data["ages"]["legacy"])

    # -- BYO-API invariant extends to the new endpoints ------------------
    def test_new_endpoints_reject_credential_fields(self):
        status, data = _req(self.base, "POST", "/api/kanban/cards",
                            {"title": "x", "api_key": "not-a-real-key"})
        self.assertEqual(status, 400)
        status, _ = _req(self.base, "POST", "/api/computers",
                         {"label": "x", "token": "not-a-real-token"})
        self.assertEqual(status, 400)


class HealthNoValidCharactersTest(unittest.TestCase):
    def test_health_bad_with_no_valid_characters(self):
        tmp = tempfile.TemporaryDirectory()
        base = Path(tmp.name)
        chars = base / "characters"
        chars.mkdir()
        (chars / "broken.md").write_text(BAD_CHAR_MD)
        httpd = create_server(
            0, characters_dir=str(chars),
            chat_file=str(base / "chat.json"),
            memory_db=str(base / "memory.db"),
            bridge=FakeBridge(), legacy=FakeLegacy(), mail=FakeMail(),
            kanban=KanbanStore(path=str(base / "kanban.json")),
            computers=ComputerRegistry(path=str(base / "computers.json")),
            beacons=BeaconLog())
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            status, data = _req("http://127.0.0.1:%d" % port,
                                "GET", "/api/health")
            self.assertEqual(status, 200)
            self.assertEqual(data["level"], "bad")
            self.assertTrue(any("character" in r for r in data["reasons"]))
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
