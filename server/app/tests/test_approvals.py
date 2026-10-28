"""Phase 2 endpoint tests — stdlib unittest, no network, no real
mail. Covers: /api/approvals (request/approve/deny/list/settings),
always-approve (ON → request comes back approved; OFF → pending
and blocking; per-bot/per-action beat global), credential-field
rejection on the new endpoints, and the approval-gated
/api/mail/send: send without approval is refused, expired and
denied approvals are refused, a dry-run send is recorded, a
replay is refused, and a bridge without the mail_send tool fails
cleanly with no send.

A FakeLegacy / FakeBridge / FakeMail stand in for the PC-side
services, and the approval queue is a temp store, so every test
runs on this machine. The FakeMail records sends instead of
touching a network.
"""

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP_DIR = REPO / "app"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(APP_DIR))

from app.server import create_server  # noqa: E402
from approvals import ApprovalQueue  # noqa: E402
from computers import ComputerRegistry  # noqa: E402
from mail_client import MailError  # noqa: E402
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

LEGACY_DATA = {
    "ok": True,
    "fetched_at": "2026-10-24T08:00:00+0000",
    "bots": [],
    "runs": [],
    "counts": {"bots": 0, "threads": 0, "runs": 0, "messages": 0},
    "model_prefs": [],
}


class FakeLegacy:
    def available(self):
        return True

    def snapshot(self):
        return LEGACY_DATA


class FakeBridge:
    def available(self):
        return True

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
    """Recording stand-in for MailClient: no network, no send.

    ``missing_tool=True`` simulates a bridge without the
    mail_send tool yet (the patch not installed on the PC).
    """

    def __init__(self, missing_tool=False):
        self.missing_tool = missing_tool
        self.sent = []

    def summary(self, label):
        return {"label": label, "address": "test-chief@example.invalid",
                "address_present": True, "inbox_count": 0, "unread": 0,
                "available": True, "error": None}

    def send_via_bridge(self, label, to, subject, body):
        if self.missing_tool:
            raise MailError("bridge belum punya tool kirim — "
                            "pasang patch dulu")
        record = {"provider": "bridge", "sent": True, "label": label,
                  "to": to, "subject": subject,
                  "bridge_reply": "Sent to %s (message id: fake-1)" % to}
        self.sent.append(record)
        return record


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


class ApprovalsServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        chars = base / "characters"
        chars.mkdir()
        (chars / "test-chief.md").write_text(CHAR_MD)
        # No shipped defaults here: code default OFF, so plain
        # requests stay pending unless a test turns the toggle on.
        self.approvals = ApprovalQueue(
            path=str(base / "approvals.json"),
            settings_path=str(base / "settings.json"),
            defaults_path=str(base / "no-defaults.json"))
        self.mail = FakeMail()
        self.httpd = create_server(
            0, characters_dir=str(chars),
            chat_file=str(base / "chat.json"),
            memory_db=str(base / "memory.db"),
            bridge=FakeBridge(), legacy=FakeLegacy(), mail=self.mail,
            kanban=KanbanStore(path=str(base / "kanban.json")),
            computers=ComputerRegistry(path=str(base / "computers.json")),
            beacons=BeaconLog(), approvals=self.approvals)
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

    def _request(self, action="send", target="a@example.invalid",
                 **extra):
        body = {"action": action, "target": target,
                "requester": "test-chief"}
        body.update(extra)
        return _req(self.base, "POST", "/api/approvals/request", body)

    def _approved_send(self, to="a@example.invalid"):
        """Request + approve a send approval; return its id."""
        _, data = self._request("send", to)
        appr_id = data["approval"]["id"]
        status, _ = _req(self.base, "POST", "/api/approvals/approve",
                         {"id": appr_id, "by": "tester"})
        self.assertEqual(status, 200)
        return appr_id

    def _send(self, appr_id, to="a@example.invalid", **extra):
        body = {"approval_id": appr_id, "to": to,
                "subject": "Hello", "body": "hi",
                "from_character": "test-chief"}
        body.update(extra)
        return _req(self.base, "POST", "/api/mail/send", body)

    # -- approvals queue ---------------------------------------------
    def test_request_approve_list(self):
        status, data = self._request(detail="Weekly pulse")
        self.assertEqual(status, 201)
        rec = data["approval"]
        self.assertEqual(rec["id"], "appr-1")
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(rec["detail"], "Weekly pulse")

        status, listed = _req(self.base, "GET", "/api/approvals")
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["approvals"]), 1)
        item = listed["approvals"][0]
        self.assertIn("age_seconds", item)
        self.assertLess(item["age_seconds"], 5)
        self.assertFalse(item["stale"])
        self.assertEqual(listed["stale_after_seconds"], 300)

        status, approved = _req(
            self.base, "POST", "/api/approvals/approve",
            {"id": "appr-1", "by": "tester", "reason": "ok"})
        self.assertEqual(status, 200)
        self.assertEqual(approved["approval"]["status"], "approved")
        self.assertEqual(approved["approval"]["decided_by"], "tester")
        self.assertEqual(approved["approval"]["history"][-1]["event"],
                         "approved")

        status, listed = _req(self.base, "GET",
                              "/api/approvals?status=pending")
        self.assertEqual(listed["approvals"], [])
        status, listed = _req(self.base, "GET",
                              "/api/approvals?status=approved")
        self.assertEqual(len(listed["approvals"]), 1)

    def test_request_accepts_open_action_names(self):
        for action in ("skill-update", "vault-release",
                       "template-import", "routine-run"):
            status, data = self._request(action=action,
                                         target="target-%s" % action)
            self.assertEqual(status, 201, action)
            self.assertEqual(data["approval"]["action"], action)

    def test_request_rejects_empty_action(self):
        status, data = self._request(action="   ")
        self.assertEqual(status, 400)
        self.assertIn("action", data["error"])

    def test_deny_and_reapprove_refused(self):
        self._request()
        status, denied = _req(self.base, "POST", "/api/approvals/deny",
                              {"id": "appr-1", "by": "tester",
                               "reason": "not yet"})
        self.assertEqual(status, 200)
        self.assertEqual(denied["approval"]["status"], "denied")
        self.assertEqual(denied["approval"]["reason"], "not yet")
        status, _ = _req(self.base, "POST", "/api/approvals/approve",
                         {"id": "appr-1", "by": "tester"})
        self.assertEqual(status, 400)

    def test_approve_unknown_404(self):
        status, _ = _req(self.base, "POST", "/api/approvals/approve",
                         {"id": "appr-99", "by": "tester"})
        self.assertEqual(status, 404)
        status, _ = _req(self.base, "POST", "/api/approvals/deny",
                         {"id": "appr-99", "by": "tester"})
        self.assertEqual(status, 404)

    def test_list_bad_status_400(self):
        status, _ = _req(self.base, "GET", "/api/approvals?status=nope")
        self.assertEqual(status, 400)

    def test_expiry_sweeps_on_list(self):
        self._request(ttl_seconds=1)
        time.sleep(1.2)
        _, listed = _req(self.base, "GET", "/api/approvals?status=pending")
        self.assertEqual(listed["approvals"], [])
        _, listed = _req(self.base, "GET", "/api/approvals?status=expired")
        self.assertEqual(len(listed["approvals"]), 1)

    # -- approval-level settings --------------------------------------
    def test_settings_get_default_ask(self):
        status, data = _req(self.base, "GET", "/api/approvals/settings")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"mode": "ask", "bots": {}, "actions": {},
                                "alwaysAllow": [], "engineCaps": {}})

    def test_settings_round_trip(self):
        status, data = _req(self.base, "POST", "/api/approvals/settings",
                            {"mode": "full",
                             "bots": {"test-chief": "ask"}})
        self.assertEqual(status, 200)
        self.assertEqual(data, {"mode": "full",
                                "bots": {"test-chief": "ask"},
                                "actions": {}, "alwaysAllow": [],
                                "engineCaps": {}})
        _, data = _req(self.base, "GET", "/api/approvals/settings")
        self.assertEqual(data["mode"], "full")
        self.assertEqual(data["bots"], {"test-chief": "ask"})
        # Partial update: only the given keys change.
        status, data = _req(self.base, "POST", "/api/approvals/settings",
                            {"actions": {"send": "full"}})
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "full")
        self.assertEqual(data["actions"], {"send": "full"})
        # The allowlist replaces as a whole.
        status, data = _req(self.base, "POST", "/api/approvals/settings",
                            {"alwaysAllow": ["web-search"]})
        self.assertEqual(status, 200)
        self.assertEqual(data["alwaysAllow"], ["web-search"])
        self.assertEqual(data["mode"], "full")

    def test_settings_rejects_bad_types(self):
        status, _ = _req(self.base, "POST", "/api/approvals/settings",
                         {"mode": "yes"})
        self.assertEqual(status, 400)
        status, _ = _req(self.base, "POST", "/api/approvals/settings",
                         {"mode": True})
        self.assertEqual(status, 400)
        status, _ = _req(self.base, "POST", "/api/approvals/settings",
                         {"bots": {"x": 1}})
        self.assertEqual(status, 400)
        status, _ = _req(self.base, "POST", "/api/approvals/settings",
                         {"alwaysAllow": "web-search"})
        self.assertEqual(status, 400)
        status, _ = _req(self.base, "POST", "/api/approvals/settings",
                         {"engineCaps": {"x": "custom"}})
        self.assertEqual(status, 400)

    def test_settings_legacy_global_shim(self):
        # The old {"global": true} body still works: true -> full.
        status, data = _req(self.base, "POST", "/api/approvals/settings",
                            {"global": True})
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "full")

    def test_full_mode_request_is_approved(self):
        _req(self.base, "POST", "/api/approvals/settings",
             {"mode": "full"})
        status, data = self._request()
        self.assertEqual(status, 201)
        rec = data["approval"]
        self.assertEqual(rec["status"], "approved")
        self.assertTrue(rec["auto"])
        self.assertEqual(rec["decided_by"], "auto")
        self.assertEqual(rec["reason"], "full-access")
        self.assertEqual(rec["rule"], "full-access")
        self.assertEqual(rec["decided_at"], rec["created_at"])
        self.assertTrue(rec["history"][-1]["auto"])
        self.assertEqual(rec["history"][-1]["rule"], "full-access")

    def test_ask_mode_request_stays_pending(self):
        _req(self.base, "POST", "/api/approvals/settings",
             {"mode": "full"})
        _req(self.base, "POST", "/api/approvals/settings",
             {"mode": "ask"})
        _, data = self._request()
        self.assertEqual(data["approval"]["status"], "pending")
        self.assertEqual(data["approval"]["rule"], "ask")

    def test_level_per_bot_beats_global(self):
        _req(self.base, "POST", "/api/approvals/settings",
             {"mode": "full", "bots": {"test-chief": "ask"}})
        _, data = self._request()
        self.assertEqual(data["approval"]["status"], "pending")
        _, data = self._request(requester="other-bot")
        self.assertEqual(data["approval"]["status"], "approved")

    def test_level_per_action_beats_global(self):
        _req(self.base, "POST", "/api/approvals/settings",
             {"mode": "ask", "actions": {"deploy": "full"}})
        _, data = self._request(action="deploy", target="deck")
        self.assertEqual(data["approval"]["status"], "approved")
        _, data = self._request(action="send")
        self.assertEqual(data["approval"]["status"], "pending")

    def test_always_allow_via_endpoint(self):
        _req(self.base, "POST", "/api/approvals/settings",
             {"mode": "ask", "alwaysAllow": ["web-search"]})
        _, data = self._request(action="web-search", target="kucing")
        rec = data["approval"]
        self.assertEqual(rec["status"], "approved")
        self.assertEqual(rec["rule"], "command-allowlist")
        _, data = self._request(action="send")
        self.assertEqual(data["approval"]["status"], "pending")

    def test_full_mode_still_needs_approval_id_to_send(self):
        # Full only pre-approves the request; the send endpoint
        # still requires the approval id — no bypass.
        _req(self.base, "POST", "/api/approvals/settings",
             {"mode": "full"})
        status, _ = _req(self.base, "POST", "/api/mail/send",
                         {"to": "a@example.invalid", "subject": "Hello",
                          "body": "hi", "from_character": "test-chief"})
        self.assertEqual(status, 404)

    def test_bot_cannot_raise_its_own_level(self):
        # A bot that tries to smuggle a level into its approval
        # request gets the field dropped: the request endpoint
        # allows no level field, so the request stays at the
        # bot's own level (ask) and waits for a human.
        _req(self.base, "POST", "/api/approvals/settings",
             {"mode": "ask"})
        status, data = _req(
            self.base, "POST", "/api/approvals/request",
            {"action": "send", "target": "a@example.invalid",
             "requester": "test-chief", "mode": "full",
             "level": "full", "inherited_level": "full"})
        self.assertEqual(status, 201)
        self.assertEqual(data["approval"]["status"], "pending")
        self.assertEqual(data["approval"]["rule"], "ask")

    # -- credential-field rejection ----------------------------------
    def test_new_endpoints_reject_credential_fields(self):
        status, _ = _req(self.base, "POST", "/api/approvals/request",
                         {"action": "send", "target": "a@example.invalid",
                          "api_key": "not-a-real-key"})
        self.assertEqual(status, 400)
        status, _ = _req(self.base, "POST", "/api/mail/send",
                         {"approval_id": "appr-1", "to": "a@example.invalid",
                          "subject": "x", "body": "y",
                          "password": "not-a-real-password"})
        self.assertEqual(status, 400)
        status, _ = _req(self.base, "POST", "/api/approvals/settings",
                         {"global": True, "token": "not-a-real-token"})
        self.assertEqual(status, 400)

    # -- mail send gate ----------------------------------------------
    def test_send_without_approval_refused(self):
        status, data = _req(self.base, "POST", "/api/mail/send",
                            {"to": "a@example.invalid",
                             "subject": "Hello", "body": "hi",
                             "from_character": "test-chief"})
        self.assertEqual(status, 404)
        self.assertIn("approval", data["error"])
        self.assertEqual(self.mail.sent, [])

    def test_send_with_pending_approval_refused(self):
        _, data = self._request()
        appr_id = data["approval"]["id"]
        status, data = self._send(appr_id)
        self.assertEqual(status, 400)
        self.assertIn("pending", data["error"])
        self.assertEqual(self.mail.sent, [])

    def test_send_with_denied_approval_refused(self):
        _, data = self._request()
        appr_id = data["approval"]["id"]
        _req(self.base, "POST", "/api/approvals/deny",
             {"id": appr_id, "by": "tester", "reason": "no"})
        status, data = self._send(appr_id)
        self.assertEqual(status, 400)
        self.assertIn("denied", data["error"])
        self.assertEqual(self.mail.sent, [])

    def test_send_with_expired_approval_refused(self):
        _, data = self._request(ttl_seconds=1)
        appr_id = data["approval"]["id"]
        _req(self.base, "POST", "/api/approvals/approve",
             {"id": appr_id, "by": "tester"})
        time.sleep(1.2)
        status, data = self._send(appr_id)
        self.assertEqual(status, 400)
        self.assertIn("expired", data["error"])
        self.assertEqual(self.mail.sent, [])

    def test_send_with_wrong_action_refused(self):
        _, data = self._request(action="publish", target="weekly-pulse")
        appr_id = data["approval"]["id"]
        _req(self.base, "POST", "/api/approvals/approve",
             {"id": appr_id, "by": "tester"})
        status, data = self._send(appr_id)
        self.assertEqual(status, 400)
        self.assertIn("publish", data["error"])
        self.assertEqual(self.mail.sent, [])

    def test_send_with_mismatched_target_refused(self):
        appr_id = self._approved_send(to="a@example.invalid")
        status, data = self._send(appr_id,
                                  to="someone-else@example.invalid")
        self.assertEqual(status, 400)
        self.assertIn("does not match", data["error"])
        self.assertEqual(self.mail.sent, [])

    def test_send_records_dispatch_and_consumes_once(self):
        appr_id = self._approved_send()
        status, data = self._send(appr_id)
        self.assertEqual(status, 200)
        self.assertTrue(data["mail"]["sent"])
        self.assertEqual(data["mail"]["provider"], "bridge")
        self.assertEqual(data["mail"]["to"], "a@example.invalid")
        self.assertEqual(data["mail"]["subject"], "Hello")
        self.assertTrue(data["consumed"])
        self.assertEqual(len(self.mail.sent), 1)
        self.assertEqual(self.mail.sent[0]["label"], "test-chief")
        # The approval is spent: a replay is refused, and nothing
        # is dispatched a second time.
        status, data = self._send(appr_id)
        self.assertEqual(status, 400)
        self.assertIn("consumed", data["error"])
        self.assertEqual(len(self.mail.sent), 1)
        # And the consume is in the approval's audit history.
        _, listed = _req(self.base, "GET",
                         "/api/approvals?status=approved")
        rec = listed["approvals"][0]
        self.assertTrue(rec["consumed"])
        self.assertEqual(rec["history"][-1]["event"], "consumed")

    def test_send_from_draft(self):
        status, draft = _req(self.base, "POST", "/api/mail/draft",
                             {"character": "test-chief",
                              "to": "a@example.invalid",
                              "subject": "Draft subject",
                              "body": "draft body"})
        self.assertEqual(status, 200)
        appr_id = self._approved_send()
        status, data = _req(self.base, "POST", "/api/mail/send",
                            {"approval_id": appr_id,
                             "draft_id": draft["draft_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(data["mail"]["to"], "a@example.invalid")
        self.assertEqual(data["mail"]["subject"], "Draft subject")
        self.assertEqual(self.mail.sent[0]["label"], "test-chief")

    def test_send_unknown_draft_404(self):
        appr_id = self._approved_send()
        status, _ = _req(self.base, "POST", "/api/mail/send",
                         {"approval_id": appr_id, "draft_id": "draft-99"})
        self.assertEqual(status, 404)

    def test_send_needs_a_mail_character(self):
        appr_id = self._approved_send()
        status, data = _req(self.base, "POST", "/api/mail/send",
                            {"approval_id": appr_id,
                             "to": "a@example.invalid",
                             "subject": "Hello", "body": "hi"})
        self.assertEqual(status, 400)
        self.assertIn("from_character", data["error"])
        self.assertEqual(self.mail.sent, [])

    def test_bridge_without_tool_fails_cleanly_no_send(self):
        # The bridge has no mail_send tool yet (patch not
        # installed on the PC): the send fails with a clean
        # message, nothing is dispatched, and the approval stays
        # unconsumed so the send can be retried after the patch.
        self.httpd.app_state.mail = FakeMail(missing_tool=True)
        appr_id = self._approved_send()
        status, data = self._send(appr_id)
        self.assertEqual(status, 503)
        self.assertIn("bridge belum punya tool kirim", data["error"])
        rec = self.approvals.get(appr_id)
        self.assertEqual(rec["status"], "approved")
        self.assertFalse(rec["consumed"])

    # -- health -------------------------------------------------------
    def test_health_includes_approvals_check(self):
        _, data = _req(self.base, "GET", "/api/health")
        self.assertEqual(data["level"], "ok")
        self.assertIn("approvals", data["checks"])
        self.assertTrue(data["checks"]["approvals"]["ok"])
        self.assertIn("no pending", data["checks"]["approvals"]["detail"])

    def test_health_warns_on_old_pending_approval(self):
        self._request()
        # Age the record past the 1-hour health bar by editing the
        # stored created_at (the queue itself is untouched).
        rec = self.approvals.get("appr-1")
        rec["created_at"] = "2026-10-24T00:00:00+00:00"
        path = Path(self.tmp.name) / "approvals.json"
        stored = json.loads(path.read_text())
        stored["approvals"][0]["created_at"] = rec["created_at"]
        path.write_text(json.dumps(stored))
        self.httpd.app_state.approvals = ApprovalQueue(
            path=str(path),
            settings_path=str(Path(self.tmp.name) / "settings.json"),
            defaults_path=str(Path(self.tmp.name) / "no-defaults.json"))
        _, data = _req(self.base, "GET", "/api/health")
        self.assertEqual(data["level"], "warn")
        self.assertFalse(data["checks"]["approvals"]["ok"])
        self.assertTrue(any("approval" in r for r in data["reasons"]))

    def test_zero_trace_banned_names(self):
        import app.server as srv
        src = Path(srv.__file__).read_text()
        for banned in ("rak" + "azo", "octo" + "bot", "open" + "dots"):
            self.assertNotIn(banned, src.lower())


if __name__ == "__main__":
    unittest.main()
