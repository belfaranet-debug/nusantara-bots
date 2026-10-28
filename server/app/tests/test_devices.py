"""DEVICES.md slices 2-4 tests — stdlib unittest, no real devices,
no real API calls, no credentials anywhere.

A FakeBridge stands in for the PC-side bridge CLI, so every
server-level test runs on this machine. The subprocess transport
itself is tested against a stub bridge script. Browser validators
are tested by importing the real bridge.py validators.
"""

import base64
import importlib.util
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
from device_client import BridgeClient  # noqa: E402

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

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

# Real bridge validators (pure functions, no adb needed).
_spec = importlib.util.spec_from_file_location(
    "grokbot_bridge", str(REPO / "devices" / "bridge.py"))
_bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bridge)


class FakeBridge:
    """Stand-in for BridgeClient: same method surface, no devices."""

    def __init__(self):
        self.calls = []

    def available(self):
        return True

    def status(self):
        return {"display_100": True, "mirror_running": False,
                "adb_devices": [{"serial": "FAKE123", "state": "device"}]}

    def devices(self):
        return {"devices": [{"serial": "FAKE123", "state": "device"}]}

    def shot(self, kind):
        assert kind in ("phone", "display")
        return PNG_1X1

    def shot_meta(self, kind):
        assert kind in ("phone", "display")
        return {"ok": True, "bytes": len(PNG_1X1), "error": None,
                "ts": "2026-10-24T00:00:00+0000"}

    def presence(self, n=50):
        return [{"ts": "2026-10-24T00:00:00+0000", "action": "tap",
                 "detail": {"x": 1, "y": 2}, "app": "com.example",
                 "blocked": False}]

    def phone_input(self, action, params):
        self.calls.append(("phone", action, params))
        return {"ok": True, "event": {"action": action}}

    def browser_action(self, action, params):
        self.calls.append(("browser", action, params))
        return {"ok": True, "event": {"action": "browser-" + action}}


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


def _req_raw(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return resp.status, resp.headers.get_content_type(), resp.read()


class BrowserValidationTest(unittest.TestCase):
    def test_key_allowlist(self):
        for good in ("Return", "Tab", "Escape", "space", "ctrl+c",
                     "alt+Tab", "F5", "a", "9", "ctrl+alt+Delete"):
            self.assertTrue(_bridge.validate_browser_key(good), good)
        for bad in ("", "rm -rf", "$(x)", "a;b", "key with space",
                     "x" * 101, "Super_L+Meta", "enter;clear"):
            self.assertFalse(_bridge.validate_browser_key(bad), bad)

    def test_coords_and_text(self):
        self.assertTrue(_bridge.validate_browser_coords(0, 0))
        self.assertTrue(_bridge.validate_browser_coords(1600, 900))
        self.assertFalse(_bridge.validate_browser_coords(-1, 10))
        self.assertFalse(_bridge.validate_browser_coords(1921, 10))
        self.assertFalse(_bridge.validate_browser_coords("10", 10))
        self.assertIsNotNone(_bridge.sanitize_text("hello world"))
        self.assertIsNone(_bridge.sanitize_text("a; rm -rf"))
        self.assertIsNone(_bridge.sanitize_text("x" * 501))


class BridgeClientTransportTest(unittest.TestCase):
    def test_shot_returns_bytes_and_deletes_temp(self):
        tmp = tempfile.TemporaryDirectory()
        stub = Path(tmp.name) / "stub_bridge.py"
        stub.write_text(
            "import json, sys\n"
            "out = sys.argv[3]\n"
            "open(out, 'wb').write(__import__('base64').b64decode(%r))\n"
            "print(json.dumps({'ok': True, 'bytes': 70}))\n"
            % base64.b64encode(PNG_1X1).decode())
        client = BridgeClient(bridge_py=str(stub))
        data = client.shot("phone")
        self.assertEqual(data, PNG_1X1)
        # Only the stub remains: the temp PNG was deleted.
        self.assertEqual([p.name for p in Path(tmp.name).iterdir()],
                         ["stub_bridge.py"])
        tmp.cleanup()

    def test_unavailable_bridge(self):
        client = BridgeClient(bridge_py="/nonexistent/bridge.py")
        self.assertFalse(client.available())


class DevicesServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        chars = Path(self.tmp.name) / "characters"
        chars.mkdir()
        (chars / "test-chief.md").write_text(CHAR_MD)
        self.fake = FakeBridge()
        self.httpd = create_server(
            0, characters_dir=str(chars),
            chat_file=str(Path(self.tmp.name) / "chat.json"),
            memory_db=str(Path(self.tmp.name) / "memory.db"),
            bridge=self.fake)
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

    def test_devices_status_and_shot(self):
        status, data = _req(self.base, "GET", "/api/devices")
        self.assertEqual(status, 200)
        self.assertTrue(data["available"])
        self.assertTrue(data["status"]["display_100"])
        self.assertEqual(data["operating"], {})
        status, ctype, body = _req_raw(
            self.base, "/api/devices/shot?kind=phone")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/png")
        self.assertEqual(body, PNG_1X1)

    def test_look_records_character_turn_with_both_shots(self):
        status, data = _req(self.base, "POST", "/api/devices/look",
                            {"room": "general", "character": "test-chief",
                             "model_path": "mock"})
        self.assertEqual(status, 200)
        turn = data["vision_turn"]
        self.assertEqual(turn["character_name"], "Test Chief")
        self.assertEqual(turn["character_role"], "Test leadership")
        self.assertEqual(turn["room"], "general")
        self.assertEqual(turn["model_path"], "mock")
        self.assertTrue(turn["shots"]["phone"]["ok"])
        self.assertTrue(turn["shots"]["display"]["ok"])
        self.assertTrue(turn["shots"]["phone"]["bytes"] > 0)
        status, data = _req(self.base, "GET",
                            "/api/devices/turns?room=general")
        self.assertEqual(len(data["turns"]), 1)
        self.assertEqual(data["turns"][0]["character_id"], "test-chief")

    def test_action_is_attributed_to_character(self):
        status, data = _req(self.base, "POST", "/api/devices/action",
                            {"character": "test-chief", "device": "phone",
                             "action": "tap", "params": {"x": 100, "y": 200}})
        self.assertEqual(status, 200)
        attr = data["attribution"]
        self.assertEqual(attr["character_name"], "Test Chief")
        self.assertEqual(attr["character_role"], "Test leadership")
        self.assertEqual(attr["device"], "phone")
        self.assertEqual(attr["action"], "tap")
        self.assertTrue(attr["ok"])
        self.assertEqual(self.fake.calls[-1],
                         ("phone", "tap", {"x": 100, "y": 200}))
        status, data = _req(self.base, "GET", "/api/devices")
        op = data["operating"]["test-chief"]
        self.assertEqual(op["device"], "phone")
        self.assertEqual(op["character_name"], "Test Chief")

    def test_browser_action_updates_operating_device(self):
        _req(self.base, "POST", "/api/devices/action",
             {"character": "test-chief", "device": "browser",
              "action": "mousemove", "params": {"x": 800, "y": 450}})
        _, data = _req(self.base, "GET", "/api/devices")
        self.assertEqual(data["operating"]["test-chief"]["device"],
                         "browser")

    def test_text_detail_is_length_class_only(self):
        _, data = _req(self.base, "POST", "/api/devices/action",
                       {"character": "test-chief", "device": "phone",
                        "action": "text",
                        "params": {"text": "SECRET-TEXT-MUST-NOT-APPEAR"}})
        detail = data["attribution"]["detail"]
        self.assertEqual(detail, {"length_class": "medium"})
        self.assertNotIn("SECRET-TEXT-MUST-NOT-APPEAR", json.dumps(data))

    def test_action_rejections(self):
        for body, want in (
                ({"character": "nope", "device": "phone",
                  "action": "tap", "params": {"x": 1, "y": 2}}, 404),
                ({"character": "test-chief", "device": "watch",
                  "action": "tap", "params": {}}, 400),
                ({"character": "test-chief", "device": "phone",
                  "action": "explode", "params": {}}, 400),
                ({"character": "test-chief", "device": "phone",
                  "action": "tap", "params": {"x": 1, "y": 2},
                  "api_key": "SENTINEL-NOT-A-REAL-CREDENTIAL"}, 400)):
            status, _ = _req(self.base, "POST", "/api/devices/action", body)
            self.assertEqual(status, want, body)

    def test_mock_chat_vision_flag_records_turn(self):
        status, data = _req(self.base, "POST", "/api/chat",
                            {"room": "general", "character": "test-chief",
                             "text": "look around", "vision": True})
        self.assertEqual(status, 200)
        kinds = [e["type"] for e in data["presence"]]
        self.assertIn("status", kinds)
        vision_notes = [e["text"] for e in data["presence"]
                        if e["type"] == "status" and "shot" in e["text"]]
        self.assertTrue(vision_notes)
        _, data = _req(self.base, "GET", "/api/devices/turns")
        self.assertTrue(any(t["model_path"] == "mock"
                            for t in data["turns"]))

    def test_feed_is_fully_attributed_plus_labelled_raw(self):
        _req(self.base, "POST", "/api/devices/action",
             {"character": "test-chief", "device": "browser",
              "action": "key", "params": {"key": "Return"}})
        _, data = _req(self.base, "GET", "/api/devices/feed")
        for entry in data["attributed"]:
            self.assertTrue(entry["attributed"])
            self.assertEqual(entry["character_name"], "Test Chief")
            self.assertEqual(entry["character_role"], "Test leadership")
        for entry in data["bridge_raw"]:
            self.assertFalse(entry["attributed"])
            self.assertIsNone(entry["character_name"])

    def test_device_client_has_no_http_client(self):
        src = (APP_DIR / "device_client.py").read_text()
        for banned in ("urllib.request", "http.client", "requests",
                       "socket.create_connection"):
            self.assertNotIn(banned, src)
        # The browser only sends shots to the model when BYO is
        # configured AND the user pressed Look first.
        js = (APP_DIR / "static" / "app.js").read_text()
        self.assertIn("image_url", js)
        self.assertIn("pendingVision", js)
        self.assertIn("byo-armed", js)


if __name__ == "__main__":
    unittest.main()
