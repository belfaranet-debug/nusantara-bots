"""Fase 2 (e): computers as the core of the deck + always-approve UI.

Covers the pure registry+bridge merge (``computers/devices_merge.py``),
the extended ``GET /api/computers`` / ``GET /api/computers/primary``
endpoints, and the deck UI markers (main computers panel + the
always-approve toggle, defensive while its endpoint does not exist
yet). Stdlib unittest only; FakeBridge stands in for the device
bridge, so no phone, display, or network is involved.

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
from computers import ComputerRegistry  # noqa: E402
from computers.devices_merge import (merge_machine_devices,  # noqa: E402
                                     primary_machine)
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

BRIDGE_STATUS = {
    "display_100": True,
    "mirror_running": True,
    "adb_devices": [{"serial": "TEST123", "state": "device"},
                    {"serial": "OFFLINE9", "state": "offline"}],
}


def _machines():
    return [
        {"id": "local-1", "label": "PC Utama", "owner": "owner",
         "status": "active", "capabilities": ["browser", "adb"],
         "notes": "", "created_at": "2026-10-25T00:00:00+00:00",
         "last_seen": "2026-10-25T06:00:00+00:00"},
        {"id": "local-2", "label": "Laptop", "owner": "owner",
         "status": "paused", "capabilities": [],
         "notes": "", "created_at": "2026-10-25T00:00:00+00:00",
         "last_seen": "2026-10-25T06:00:00+00:00"},
    ]


class TestMerge(unittest.TestCase):
    """Pure merge: registry records + live bridge status."""

    def test_bridge_present_marks_primary_and_devices(self):
        merged = merge_machine_devices(
            _machines(), True, bridge_status=BRIDGE_STATUS,
            last_shots={"phone": {"ts": "2026-10-25T06:00:00+00:00",
                                  "ok": True, "bytes": 123},
                        "display": None},
            now=1761360000.0)
        self.assertEqual(len(merged), 2)
        self.assertTrue(merged[0]["primary"])
        self.assertFalse(merged[1]["primary"])
        self.assertEqual(primary_machine(merged)["id"], "local-1")
        dev = merged[0]["devices"]
        self.assertTrue(dev["available"])
        self.assertTrue(dev["shared"])
        self.assertTrue(dev["phone"]["connected"])
        self.assertEqual(dev["phone"]["serial"], "TEST123")
        self.assertTrue(dev["display"]["running"])
        self.assertTrue(dev["mirror_running"])
        self.assertEqual(dev["last_shot"]["phone"]["age_seconds"], 0.0)
        self.assertIsNone(dev["last_shot"]["display"])
        self.assertIsNone(merged[0]["devices_reason"])
        # The second machine carries the SAME shared view.
        self.assertEqual(merged[1]["devices"], dev)

    def test_first_active_is_primary_not_first_record(self):
        machines = _machines()
        machines[0]["status"] = "paused"
        machines[1]["status"] = "active"
        merged = merge_machine_devices(machines, True,
                                       bridge_status=BRIDGE_STATUS)
        self.assertFalse(merged[0]["primary"])
        self.assertTrue(merged[1]["primary"])

    def test_no_active_machine_means_no_primary(self):
        machines = _machines()
        for m in machines:
            m["status"] = "paused"
        merged = merge_machine_devices(machines, True,
                                       bridge_status=BRIDGE_STATUS)
        self.assertFalse(any(m["primary"] for m in merged))
        self.assertIsNone(primary_machine(merged))

    def test_bridge_absent_is_honest_not_empty(self):
        merged = merge_machine_devices(
            _machines(), False, bridge_error="bridge CLI not found")
        for m in merged:
            self.assertIsNone(m["devices"])
            self.assertEqual(m["devices_reason"], "bridge CLI not found")
        # Primary still resolves: the registry is independent of
        # the bridge.
        self.assertTrue(merged[0]["primary"])

    def test_bridge_available_but_status_unreadable(self):
        merged = merge_machine_devices(_machines(), True,
                                       bridge_status=None)
        self.assertIsNone(merged[0]["devices"])
        self.assertIn("unreadable", merged[0]["devices_reason"])

    def test_no_phone_attached(self):
        status = dict(BRIDGE_STATUS, adb_devices=[])
        merged = merge_machine_devices(_machines(), True,
                                       bridge_status=status)
        phone = merged[0]["devices"]["phone"]
        self.assertFalse(phone["connected"])
        self.assertIsNone(phone["serial"])

    def test_failed_shot_is_not_a_last_shot(self):
        merged = merge_machine_devices(
            _machines(), True, bridge_status=BRIDGE_STATUS,
            last_shots={"phone": {"ts": "2026-10-25T06:00:00+00:00",
                                  "ok": False, "bytes": 0}},
            now=1761360000.0)
        self.assertIsNone(merged[0]["devices"]["last_shot"]["phone"])

    def test_input_records_are_not_mutated(self):
        machines = _machines()
        merge_machine_devices(machines, True, bridge_status=BRIDGE_STATUS)
        for m in machines:
            self.assertNotIn("primary", m)
            self.assertNotIn("devices", m)


class FakeBridge:
    def __init__(self, available=True, status=None):
        self._available = available
        self._status = status if status is not None else BRIDGE_STATUS

    def available(self):
        return self._available

    def status(self):
        if not self._available:
            raise BridgeError("bridge CLI not found")
        return self._status

    def shot(self, kind):
        return b"\x89PNG\r\n\x1a\n"

    def shot_meta(self, kind):
        return {"ok": True, "bytes": 8, "error": None,
                "ts": "2026-10-25T06:00:00+00:00"}

    def presence(self, n=50):
        return []

    def phone_input(self, action, params):
        return {"ok": True}

    def browser_action(self, action, params):
        return {"ok": True}


# The server catches BridgeError from device_client; the fake
# raises that same class so the handler's except clause catches it.
from device_client import BridgeError, vision_turn as make_turn  # noqa: E402


class FakeLegacy:
    def available(self):
        return True

    def snapshot(self):
        return {"ok": True, "fetched_at": "2026-10-25T06:00:00+00:00",
                "bots": [], "runs": [],
                "counts": {"bots": 0, "threads": 0, "runs": 0,
                           "messages": 0},
                "model_prefs": []}


class FakeMail:
    def summary(self, label):
        return {"label": label, "address": None,
                "address_present": False, "inbox_count": 0, "unread": 0,
                "available": False, "error": "no bridge"}


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


class ComputersEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        chars = base / "characters"
        chars.mkdir()
        (chars / "test-chief.md").write_text(CHAR_MD)
        self.kanban = KanbanStore(path=str(base / "kanban.json"))
        self.computers = ComputerRegistry(path=str(base / "computers.json"))
        self.computers.register("PC Utama", capabilities=["browser", "adb"])
        self.computers.register("Laptop")
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

    def test_computers_lists_merged_fields(self):
        status, data = _req(self.base, "GET", "/api/computers")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["machines"]), 2)
        self.assertEqual(data["primary_id"], "local-1")
        first = data["machines"][0]
        self.assertTrue(first["primary"])
        self.assertFalse(data["machines"][1]["primary"])
        dev = first["devices"]
        self.assertTrue(dev["available"])
        self.assertTrue(dev["phone"]["connected"])
        self.assertEqual(dev["phone"]["serial"], "TEST123")
        self.assertTrue(dev["display"]["running"])
        self.assertTrue(dev["mirror_running"])
        # No vision turn yet: no last shot is claimed.
        self.assertIsNone(dev["last_shot"]["phone"])
        self.assertIsNone(dev["last_shot"]["display"])
        self.assertIsNone(first["devices_reason"])

    def test_computers_primary_endpoint(self):
        status, data = _req(self.base, "GET", "/api/computers/primary")
        self.assertEqual(status, 200)
        self.assertEqual(data["machine"]["id"], "local-1")
        self.assertTrue(data["machine"]["primary"])

    def test_primary_moves_when_first_is_paused(self):
        _req(self.base, "POST", "/api/computers/status",
             {"machine_id": "local-1", "status": "paused"})
        status, data = _req(self.base, "GET", "/api/computers/primary")
        self.assertEqual(status, 200)
        self.assertEqual(data["machine"]["id"], "local-2")

    def test_primary_404_when_none_active(self):
        _req(self.base, "POST", "/api/computers/status",
             {"machine_id": "local-1", "status": "paused"})
        _req(self.base, "POST", "/api/computers/status",
             {"machine_id": "local-2", "status": "paused"})
        status, data = _req(self.base, "GET", "/api/computers/primary")
        self.assertEqual(status, 404)
        self.assertIn("no active machine", data["error"])
        _, listed = _req(self.base, "GET", "/api/computers")
        self.assertIsNone(listed["primary_id"])
        self.assertFalse(any(m["primary"] for m in listed["machines"]))

    def test_bridge_down_reports_reason_not_empty_devices(self):
        self.httpd.app_state.bridge = FakeBridge(available=False)
        status, data = _req(self.base, "GET", "/api/computers")
        self.assertEqual(status, 200)
        for m in data["machines"]:
            self.assertIsNone(m["devices"])
            self.assertIn("bridge CLI not found", m["devices_reason"])

    def test_last_shot_age_comes_from_vision_turn(self):
        # A vision turn records shot metadata; the computers view
        # must surface its age, not take a fresh screenshot.
        state = self.httpd.app_state
        char = state.valid_characters()[0]
        state.record_vision(make_turn(
            char, "general",
            {"phone": {"ok": True, "bytes": 10, "error": None,
                       "ts": "2026-10-25T06:00:00+00:00"},
             "display": {"ok": False, "bytes": 0,
                         "error": "no display",
                         "ts": "2026-10-25T06:00:00+00:00"}},
            "mock"))
        _, data = _req(self.base, "GET", "/api/computers")
        shot = data["machines"][0]["devices"]["last_shot"]
        self.assertIsNotNone(shot["phone"])
        self.assertIsNotNone(shot["phone"]["age_seconds"])
        self.assertGreaterEqual(shot["phone"]["age_seconds"], 0)
        self.assertIsNone(shot["display"])


class DeckUiTest(unittest.TestCase):
    """Static markers: the main computers panel + always-approve."""

    def test_computers_panel_is_the_deck_headline(self):
        html = (APP_DIR / "static" / "index.html").read_text(
            encoding="utf-8")
        for element_id in ("computers-panel", "computers-primary",
                           "computers-primary-badge", "computers-devices",
                           "computers-list", "computers-note",
                           "deck-devices-status"):
            self.assertIn('id="%s"' % element_id, html)
        # The panel sits at the top of the deck, before the room layout.
        self.assertLess(html.index('id="computers-panel"'),
                        html.index('class="vo-layout"'))
        # The old small "PANE PERANGKAT" section is gone (merged
        # into the computers panel); the shot panes live there now,
        # exactly once each.
        self.assertNotIn("PANE PERANGKAT", html)
        self.assertEqual(html.count('id="deck-shot-phone"'), 1)
        self.assertEqual(html.count('id="deck-shot-display"'), 1)

    def test_approval_level_picker_present(self):
        html = (APP_DIR / "static" / "index.html").read_text(
            encoding="utf-8")
        for element_id in ("approval-level-select",
                           "always-approve-indicator",
                           "always-approve-status"):
            self.assertIn('id="%s"' % element_id, html)
        self.assertIn("ALWAYS-APPROVE", html)
        # The picker offers the five levels (no binary checkbox).
        for level in ("ask", "edits", "auto", "full", "custom"):
            self.assertIn('value="%s"' % level, html)
        self.assertNotIn('id="always-approve-toggle"', html)

    def test_computers_and_toggle_render_functions(self):
        js = (APP_DIR / "static" / "app.js").read_text(encoding="utf-8")
        for fn in ("renderComputers", "loadComputers",
                   "setComputerStatus", "loadApprovalSettings",
                   "shotAgeText"):
            self.assertIn(fn, js)
        # The picker follows the approvals contract exactly.
        self.assertIn("/api/approvals/settings", js)
        # Defensive while the endpoint does not exist yet: disabled
        # + "belum tersedia", never a claimed state.
        self.assertIn("belum tersedia", js)
        # A failed POST reverts the select (no fake state).
        self.assertIn("select.value = previous", js)
        # loadDeck wires the computers panel in.
        load_deck = re.search(r"async function loadDeck\(\)\s*\{.*?\n\}",
                              js, re.S)
        self.assertIsNotNone(load_deck, "no loadDeck function in app.js")
        self.assertIn("loadComputers()", load_deck.group(0))
        self.assertIn("loadApprovalSettings()", load_deck.group(0))

    def test_computers_and_toggle_classes_defined(self):
        css = (APP_DIR / "static" / "style.css").read_text(
            encoding="utf-8")
        for cls in (".vo-computers", ".computer-primary",
                    ".computer-devices", ".always-approve",
                    ".always-approve-indicator"):
            self.assertIn(cls, css)

    def test_no_forbidden_brand_strings_in_changed_files(self):
        banned = ["rak" + "azo", "octo" + "bot", "open" + "dots"]
        for path in (APP_DIR / "static" / "app.js",
                     APP_DIR / "static" / "style.css",
                     APP_DIR / "static" / "index.html",
                     REPO / "computers" / "devices_merge.py",
                     REPO / "app" / "server.py"):
            text = path.read_text(encoding="utf-8").lower()
            for word in banned:
                self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
