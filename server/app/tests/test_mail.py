"""Mail v1 tests — receive-only inboxes + approval-gated drafts.

Stdlib unittest, no real bridge, no real addresses, no credentials
anywhere. A FakeMail stands in for MailClient the same way the
deck tests fake the legacy reader and the device bridge.

Contract under test (see server.py / mail_client.py):
* GET /api/mail?character=<stem> -> one entry with exactly
  {character, label, address, address_present, inbox_count,
  unread, available, error}; label == file stem.
* GET /api/mail?all=1 -> {"mail": [...]} for every real bot;
  the format-template character is never a mail identity.
* POST /api/mail/draft accepts ONLY character/to/subject/body,
  stores the draft in memory, and returns
  {draft_id, character, to, subject}.
* There is NO send endpoint in v1: every send-like path is 404.
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
from mail_client import MailClient, _scrub, label_for  # noqa: E402

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

TEMPLATE_MD = """---
name: Example Character
role: Format example only — not a crew member
skills:
  - team-memory
tools:
  - read_file
memory_scope: private+team
---

You are a format example, not a crew member.
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


class FakeMail:
    """Stand-in for MailClient: same summary() surface, no bridge."""

    def __init__(self, fail=False):
        self.fail = fail
        self.labels_seen = []

    def summary(self, label):
        self.labels_seen.append(label)
        if self.fail:
            return {"label": label, "address": None,
                    "address_present": False, "inbox_count": 0,
                    "unread": 0, "available": False,
                    "error": "mail bridge unreachable"}
        return {"label": label, "address": "test-chief@fake.example",
                "address_present": True, "inbox_count": 3,
                "unread": 1, "available": True, "error": None}


class RaisingMail:
    """A misbehaving client: the server must still answer 200."""

    def summary(self, label):
        raise RuntimeError("boom")


class MailServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        chars = Path(self.tmp.name) / "characters"
        chars.mkdir()
        (chars / "test-chief.md").write_text(CHAR_MD)
        (chars / "example-character.md").write_text(TEMPLATE_MD)
        self.fake_mail = FakeMail()
        self.httpd = create_server(
            0, characters_dir=str(chars),
            chat_file=str(Path(self.tmp.name) / "chat.json"),
            memory_db=str(Path(self.tmp.name) / "memory.db"),
            mail=self.fake_mail)
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

    def test_single_character_shape_and_label(self):
        status, data = _req(self.base, "GET",
                            "/api/mail?character=test-chief")
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"character", "label", "address",
                                     "address_present", "inbox_count",
                                     "unread", "available", "error"})
        self.assertEqual(data["character"], "test-chief")
        self.assertEqual(data["label"], "test-chief")
        self.assertTrue(data["address_present"])
        self.assertEqual(data["inbox_count"], 3)
        self.assertEqual(data["unread"], 1)
        self.assertTrue(data["available"])
        self.assertIsNone(data["error"])
        self.assertIn("test-chief", self.fake_mail.labels_seen)

    def test_all_excludes_template_character(self):
        status, data = _req(self.base, "GET", "/api/mail?all=1")
        self.assertEqual(status, 200)
        stems = [entry["character"] for entry in data["mail"]]
        self.assertIn("test-chief", stems)
        self.assertNotIn("example-character", stems)
        for entry in data["mail"]:
            self.assertEqual(entry["label"], entry["character"])

    def test_template_and_unknown_characters_are_404(self):
        for stem in ("example-character", "no-such-bot"):
            status, _ = _req(self.base, "GET",
                             "/api/mail?character=%s" % stem)
            self.assertEqual(status, 404, stem)
        status, _ = _req(self.base, "GET", "/api/mail")
        self.assertEqual(status, 400)

    def test_unavailable_bridge_is_honest_not_fatal(self):
        self.fake_mail.fail = True
        status, data = _req(self.base, "GET",
                            "/api/mail?character=test-chief")
        self.assertEqual(status, 200)
        self.assertFalse(data["available"])
        self.assertFalse(data["address_present"])
        self.assertIsNone(data["address"])
        self.assertEqual(data["inbox_count"], 0)
        self.assertIn("unreachable", data["error"])

    def test_raising_client_still_answers_200(self):
        self.httpd.app_state.mail = RaisingMail()
        status, data = _req(self.base, "GET",
                            "/api/mail?character=test-chief")
        self.assertEqual(status, 200)
        self.assertFalse(data["available"])
        self.assertIsNotNone(data["error"])

    def test_draft_stored_in_memory_and_returned(self):
        status, data = _req(self.base, "POST", "/api/mail/draft",
                            {"character": "test-chief",
                             "to": "someone@fake.example",
                             "subject": "Hello draft",
                             "body": "Draft body text."})
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"draft_id", "character", "to",
                                     "subject"})
        self.assertEqual(data["character"], "test-chief")
        self.assertEqual(data["to"], "someone@fake.example")
        self.assertEqual(data["subject"], "Hello draft")
        stored = self.httpd.app_state.mail_drafts[data["draft_id"]]
        self.assertEqual(stored["body"], "Draft body text.")
        self.assertEqual(stored["character"], "test-chief")
        # A second draft gets a distinct id; nothing was sent.
        status, data2 = _req(self.base, "POST", "/api/mail/draft",
                             {"character": "test-chief",
                              "to": "other@fake.example",
                              "subject": "Second",
                              "body": "More."})
        self.assertEqual(status, 200)
        self.assertNotEqual(data2["draft_id"], data["draft_id"])
        self.assertEqual(len(self.httpd.app_state.mail_drafts), 2)

    def test_draft_validation(self):
        status, _ = _req(self.base, "POST", "/api/mail/draft",
                         {"character": "no-such-bot",
                          "to": "a@fake.example",
                          "subject": "Hi", "body": "x"})
        self.assertEqual(status, 404)
        status, _ = _req(self.base, "POST", "/api/mail/draft",
                         {"character": "example-character",
                          "to": "a@fake.example",
                          "subject": "Hi", "body": "x"})
        self.assertEqual(status, 404)
        for bad in ({"character": "test-chief", "subject": "Hi",
                     "body": "x"},
                    {"character": "test-chief", "to": "  ",
                     "subject": "Hi", "body": "x"},
                    {"character": "test-chief", "to": "a@fake.example",
                     "subject": "Hi", "body": 42}):
            status, _ = _req(self.base, "POST", "/api/mail/draft", bad)
            self.assertEqual(status, 400, bad)
        # Credential fields are rejected before anything is stored.
        status, _ = _req(self.base, "POST", "/api/mail/draft",
                         {"character": "test-chief",
                          "to": "a@fake.example", "subject": "Hi",
                          "body": "x", "api_key": "SENTINEL-NOT-REAL"})
        self.assertEqual(status, 400)
        self.assertEqual(len(self.httpd.app_state.mail_drafts), 0)

    def test_send_route_is_approval_gated(self):
        # the owner's decision (25 Okt 2026 06:01): sending IS allowed,
        # but only with a matching approved approval. Without one
        # the route refuses — it never sends on its own.
        for path in ("/api/mail/send-draft", "/api/mail/deliver",
                     "/api/mail/outbox"):
            status, _ = _req(self.base, "POST", path,
                             {"character": "test-chief",
                              "to": "a@fake.example",
                              "subject": "Hi", "body": "x"})
            self.assertEqual(status, 404, path)
        status, _ = _req(self.base, "GET", "/api/mail/send")
        self.assertEqual(status, 404)
        # POST /api/mail/send with no approval at all: refused,
        # nothing sent (the approval-gated flow is covered in
        # test_approvals.py).
        status, data = _req(self.base, "POST", "/api/mail/send",
                            {"to": "a@fake.example", "subject": "Hi",
                             "body": "x", "from_character": "test-chief"})
        self.assertEqual(status, 404)
        self.assertIn("approval", data["error"])


class MailClientTest(unittest.TestCase):
    """Parsing + graceful failure of the real client, with the
    transport stubbed: no network, no bridge, no real mailbox."""

    def _client_with(self, texts):
        client = MailClient(endpoint="http://127.0.0.1:9/mcp",
                            timeout=1)
        calls = []

        def fake_call(name, arguments):
            calls.append((name, arguments))
            return texts[name]

        client._call_tool = fake_call
        return client, calls

    def test_summary_parses_address_and_counts(self):
        texts = {
            "mail_my_address": "Your email address is: "
                               "bot@fake.example (provider: duckmail, "
                               "receive-only).",
            "mail_inbox": "Inbox bot@fake.example — 3 message(s), "
                          "newest first:\n"
                          "1. [NEW] 2026-10-24 | from: a@x | "
                          "subject: Hi | id: m1\n"
                          "2. [read] 2026-10-23 | from: b@x | "
                          "subject: Yo | id: m2\n",
        }
        client, calls = self._client_with(texts)
        data = client.summary("test-chief")
        self.assertTrue(data["available"])
        self.assertTrue(data["address_present"])
        self.assertEqual(data["address"], "bot@fake.example")
        self.assertEqual(data["inbox_count"], 3)
        self.assertEqual(data["unread"], 1)
        self.assertIsNone(data["error"])
        self.assertEqual([c[0] for c in calls],
                         ["mail_my_address", "mail_inbox"])
        self.assertEqual(calls[0][1], {"label": "test-chief"})

    def test_summary_no_inbox_yet_is_available(self):
        texts = {
            "mail_my_address": 'No inbox for label "test-chief" yet. '
                               "Create one with mail_create_inbox.",
            "mail_inbox": "",
        }
        client, _ = self._client_with(texts)
        data = client.summary("test-chief")
        self.assertTrue(data["available"])
        self.assertFalse(data["address_present"])
        self.assertIsNone(data["address"])
        self.assertEqual(data["error"], "no inbox yet")

    def test_summary_empty_inbox_counts_zero(self):
        texts = {
            "mail_my_address": "Your email address is: "
                               "bot@fake.example (provider: duckmail).",
            "mail_inbox": "Inbox bot@fake.example is empty — "
                          "no messages yet.",
        }
        client, _ = self._client_with(texts)
        data = client.summary("test-chief")
        self.assertEqual((data["inbox_count"], data["unread"]), (0, 0))
        self.assertIsNone(data["error"])

    def test_summary_failure_never_raises(self):
        client = MailClient(endpoint="http://127.0.0.1:9/mcp",
                            timeout=1)
        client._call_tool = lambda n, a: (_ for _ in ()).throw(
            RuntimeError("bridge down"))
        data = client.summary("test-chief")
        self.assertFalse(data["available"])
        self.assertFalse(data["address_present"])
        self.assertIn("bridge down", data["error"])

    # -- send_via_bridge (Phase 2) -----------------------------------
    def test_send_via_bridge_calls_mail_send_tool(self):
        client, calls = self._client_with(
            {"mail_send": "Sent to a@fake.example (message id: msg-1)"})
        result = client.send_via_bridge(
            "test-chief", "a@fake.example", "Hello", "body text")
        self.assertTrue(result["sent"])
        self.assertEqual(result["provider"], "bridge")
        self.assertEqual(result["to"], "a@fake.example")
        self.assertEqual(result["subject"], "Hello")
        self.assertEqual(result["label"], "test-chief")
        self.assertIn("msg-1", result["bridge_reply"])
        self.assertEqual(calls, [("mail_send",
                                  {"label": "test-chief",
                                   "to": "a@fake.example",
                                   "subject": "Hello",
                                   "body": "body text"})])

    def test_send_via_bridge_unknown_tool_is_clean_error(self):
        from mail_client import MailError
        client = MailClient(endpoint="http://127.0.0.1:9/mcp",
                            timeout=1)

        def fake_call(name, arguments):
            raise MailError("Unknown tool: mail_send")

        client._call_tool = fake_call
        with self.assertRaises(MailError) as ctx:
            client.send_via_bridge("test-chief", "a@fake.example",
                                   "Hello", "body")
        self.assertEqual(str(ctx.exception),
                         "bridge belum punya tool kirim — "
                         "pasang patch dulu")

    def test_send_via_bridge_rejects_bad_input(self):
        from mail_client import MailError
        client, calls = self._client_with({"mail_send": "Sent"})
        with self.assertRaises(MailError):
            client.send_via_bridge("", "a@fake.example", "Hi", "x")
        with self.assertRaises(MailError):
            client.send_via_bridge("test-chief", "   ", "Hi", "x")
        with self.assertRaises(MailError):
            client.send_via_bridge("test-chief", "a@fake.example",
                                   "  ", "x")
        self.assertEqual(calls, [])

    def test_send_via_bridge_dry_run_touches_nothing(self):
        client = MailClient(endpoint="http://127.0.0.1:9/mcp",
                            timeout=1, dry_run=True)

        def exploding_call(name, arguments):
            raise AssertionError("dry_run must not call the transport")

        client._call_tool = exploding_call
        result = client.send_via_bridge(
            "test-chief", "a@fake.example", "Hello", "body")
        self.assertFalse(result["sent"])
        self.assertEqual(result["provider"], "dry_run")
        self.assertEqual(result["would_send"]["to"], "a@fake.example")
        self.assertEqual(result["would_send"]["subject"], "Hello")

    def test_error_text_never_carries_an_address(self):
        scrubbed = _scrub("login failed for bot@fake.example here")
        self.assertNotIn("bot@fake.example", scrubbed)
        self.assertIn("[address]", scrubbed)

    def test_label_is_the_file_stem(self):
        self.assertEqual(label_for("chief-officer"), "chief-officer")
        self.assertEqual(label_for("yt-idea-scout"), "yt-idea-scout")

    def test_new_files_carry_no_forbidden_brand(self):
        # Built by concatenation so the literal never appears here.
        banned = ["rak" + "azo", "open" + "dots", "octo" + "bot"]
        for fname in ("mail_client.py", "server.py", "tests/test_mail.py"):
            text = (APP_DIR / fname).read_text().lower()
            for word in banned:
                self.assertNotIn(word, text, fname)


if __name__ == "__main__":
    unittest.main()
