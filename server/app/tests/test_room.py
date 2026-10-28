"""Room scene tests — the deck room panel is a live scene: one
original room illustration as background, six agent chips overlaid
at fixed desk spots, driven by the same live deck data and status
tags. No backend change, no invented statuses.
"""

import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
APP_DIR = REPO / "app"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(APP_DIR))

from app.server import create_server  # noqa: E402

CHAR_MD = """---
name: Test Chief
role: Test leadership
skills:
  - team-memory
tools: []
memory_scope: private+team
---

You are a test chief.
"""


class RoomAssetTest(unittest.TestCase):
    def test_room_image_exists_and_is_jpeg(self):
        room = APP_DIR / "static" / "room.jpg"
        self.assertTrue(room.is_file(), "room.jpg missing from static/")
        data = room.read_bytes()
        self.assertTrue(data.startswith(b"\xff\xd8\xff"),
                        "room.jpg is not a JPEG")
        self.assertGreater(len(data), 50_000,
                           "room.jpg suspiciously small")

    def test_css_references_room_in_vo_room_rule(self):
        css = (APP_DIR / "static" / "style.css").read_text()
        rule = re.search(r"#deck\s+\.vo-room\s*\{[^}]*\}", css)
        self.assertIsNotNone(rule, "no #deck .vo-room rule in style.css")
        body = rule.group(0)
        self.assertIn("/static/room.jpg", body)
        self.assertIn("position:relative", body.replace(" ", ""))

    def test_all_six_chip_positions_defined(self):
        css = (APP_DIR / "static" / "style.css").read_text()
        for n in range(6):
            rule = re.search(
                r"\.vo-room\s+\.vo-agent\.pos-%d\s*\{[^}]*\}" % n, css)
            self.assertIsNotNone(rule, "missing .pos-%d chip rule" % n)
            body = rule.group(0)
            self.assertIn("left:", body)
            self.assertIn("top:", body)

    def test_render_assigns_positions_and_keeps_tag_mapping(self):
        js = (APP_DIR / "static" / "app.js").read_text()
        self.assertIn("pos-${i % 6}", js)
        self.assertIn("vo-agent pos-", js)
        # voTag mapping unchanged: real run statuses only.
        for status, label in (("running", "JALAN"),
                              ("queued", "ANTRE"),
                              ("completed", "SELESAI"),
                              ("failed", "GAGAL"),
                              ("cancelled", "BATAL")):
            self.assertIn('case "%s"' % status, js)
            self.assertIn('"%s"' % label, js)
        self.assertIn("TANPA RUN", js)
        # Filter dimming still applies to chips.
        self.assertIn("dim", js)

    def test_room_and_filter_containers_intact(self):
        html = (APP_DIR / "static" / "index.html").read_text()
        self.assertIn('id="vo-room"', html)
        self.assertIn('id="vo-filters"', html)

    def test_deck_deep_link_opens_room(self):
        js = (APP_DIR / "static" / "app.js").read_text()
        self.assertIn('location.hash === "#deck"', js)
        self.assertIn("openDeck()", js)


class RoomServedTest(unittest.TestCase):
    def test_server_serves_room_jpeg(self):
        tmp = tempfile.TemporaryDirectory()
        chars = Path(tmp.name) / "characters"
        chars.mkdir()
        (chars / "test-chief.md").write_text(CHAR_MD)
        httpd = create_server(
            0, characters_dir=str(chars),
            chat_file=str(Path(tmp.name) / "chat.json"),
            memory_db=str(Path(tmp.name) / "memory.db"))
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/static/room.jpg" % port,
                    timeout=10) as resp:
                self.assertEqual(resp.status, 200)
                body = resp.read()
            self.assertTrue(body.startswith(b"\xff\xd8\xff"))
            self.assertGreater(len(body), 50_000)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
