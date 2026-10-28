"""POST /api/characters tests — create a new bot (character file)
from the UI. All writes go to a TEMP characters dir; the real
characters/ dir is never touched.
"""

import json
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
from loader import load_character  # noqa: E402


class CharactersCreateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chars = Path(self.tmp.name) / "characters"
        self.chars.mkdir()
        self.httpd = create_server(
            0, characters_dir=str(self.chars),
            chat_file=str(Path(self.tmp.name) / "chat.json"),
            memory_db=str(Path(self.tmp.name) / "memory.db"))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def post(self, body):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/characters" % self.port,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_valid_create_per_focus(self):
        expected = {
            "day-to-day": (
                "Day-to-day assistant — handles the owner's daily work "
                "across the team",
                ["team-memory", "bot-live-presence"]),
            "inbox": (
                "Inbox & email assistant — triages the owner's inbox "
                "into actions",
                ["team-memory", "bot-live-presence"]),
            "research": (
                "Research & writing assistant — researches topics and "
                "drafts in the owner's voice",
                ["team-memory", "bot-live-presence"]),
            "everything": (
                "General assistant — day-to-day work, inbox, and research",
                ["team-memory", "bot-live-presence"]),
        }
        for focus, (role, skills) in expected.items():
            name = "Focus %s Bot" % focus
            status, payload = self.post({"name": name, "focus": focus})
            self.assertEqual(status, 201, payload)
            char = payload["character"]
            stem = "focus-%s-bot" % focus
            self.assertEqual(char["id"], stem)
            self.assertEqual(char["name"], name)
            self.assertEqual(char["role"], role)
            self.assertEqual(char["skills"], skills)
            self.assertEqual(char["tools"], [])
            self.assertEqual(char["memory_scope"], "private+team")
            self.assertEqual(char["status"], "valid")
            self.assertEqual(char["errors"], [])
            path = self.chars / ("%s.md" % stem)
            self.assertTrue(path.is_file(), "file missing: %s" % path)
            loaded = load_character(path)
            self.assertEqual(loaded.status, "valid",
                             loaded.error_codes())
            self.assertEqual(loaded.name, name)
            self.assertEqual(loaded.role, role)
            self.assertEqual(loaded.skills, skills)
            self.assertEqual(loaded.memory_scope, "private+team")

    def test_custom_role_and_skills(self):
        status, payload = self.post({
            "name": "Mail Helper",
            "focus": "inbox",
            "role": "Sorts the owner's mail into next actions",
            "skills": ["inbox-triage", "team-memory"],
        })
        self.assertEqual(status, 201, payload)
        char = payload["character"]
        self.assertEqual(char["role"],
                         "Sorts the owner's mail into next actions")
        self.assertEqual(char["skills"], ["inbox-triage", "team-memory"])
        loaded = load_character(self.chars / "mail-helper.md")
        self.assertEqual(loaded.status, "valid")

    def test_duplicate_name_is_409_and_file_unchanged(self):
        status, _ = self.post({"name": "Dup Bot", "focus": "research"})
        self.assertEqual(status, 201)
        path = self.chars / "dup-bot.md"
        before = path.read_bytes()
        status, payload = self.post({"name": "Dup Bot",
                                     "focus": "day-to-day"})
        self.assertEqual(status, 409, payload)
        self.assertIn("error", payload)
        self.assertEqual(path.read_bytes(), before,
                         "duplicate create must not touch the file")
        # A different name that slugifies to the same stem is the
        # same conflict.
        status, _ = self.post({"name": "Dup_Bot", "focus": "research"})
        self.assertEqual(status, 409)

    def test_bad_focus_is_400(self):
        status, payload = self.post({"name": "Bad Focus",
                                     "focus": "cooking"})
        self.assertEqual(status, 400, payload)
        self.assertFalse((self.chars / "bad-focus.md").exists())
        status, _ = self.post({"name": "No Focus"})
        self.assertEqual(status, 400)

    def test_empty_name_is_400(self):
        for name in ("", "   "):
            status, _ = self.post({"name": name, "focus": "inbox"})
            self.assertEqual(status, 400, repr(name))
        status, _ = self.post({"focus": "inbox"})
        self.assertEqual(status, 400)

    def test_name_that_slugifies_to_empty_is_400(self):
        for name in ("!!!", "---", "___", "***"):
            status, _ = self.post({"name": name, "focus": "everything"})
            self.assertEqual(status, 400, repr(name))
        self.assertEqual(list(self.chars.glob("*.md")), [])

    def test_name_too_long_or_slug_too_long_is_400(self):
        status, _ = self.post({"name": "x" * 61, "focus": "inbox"})
        self.assertEqual(status, 400)
        # 60 chars is allowed for the name, but the slug is over 40.
        status, _ = self.post({"name": "x" * 60, "focus": "inbox"})
        self.assertEqual(status, 400)
        # A 40-char slug passes.
        status, payload = self.post({"name": "x" * 40, "focus": "inbox"})
        self.assertEqual(status, 201, payload)

    def test_unknown_skill_is_400(self):
        status, payload = self.post({
            "name": "Skill Bot", "focus": "research",
            "skills": ["team-memory", "no-such-skill"]})
        self.assertEqual(status, 400, payload)
        self.assertFalse((self.chars / "skill-bot.md").exists())
        status, _ = self.post({
            "name": "Skill Bot", "focus": "research",
            "skills": "team-memory"})
        self.assertEqual(status, 400)

    def test_credential_field_is_refused(self):
        for field in ("api_key", "token", "password"):
            status, payload = self.post({
                "name": "Cred Bot", "focus": "inbox", field: "secret"})
            self.assertEqual(status, 400, (field, payload))
        self.assertFalse((self.chars / "cred-bot.md").exists())

    def test_role_too_long_is_400(self):
        status, _ = self.post({
            "name": "Long Role", "focus": "everything",
            "role": "r" * 121})
        self.assertEqual(status, 400)
        status, payload = self.post({
            "name": "Long Role", "focus": "everything",
            "role": "r" * 120})
        self.assertEqual(status, 201, payload)


if __name__ == "__main__":
    unittest.main()
