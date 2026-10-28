"""Approval queue tests — stdlib unittest, no network, no email.

Covers: request → approve → consume; deny is recorded; automatic
expiry (short TTL, including after approval); a second consume is
refused; list + age; open action names (any non-empty string);
approval levels (code default ask, per-bot > per-action > global
mode, settings round-trip, unknown levels fail closed to ask,
alwaysAllow, full behaves like the old always-approve ON,
decision-log rules, delegation caps, bots cannot raise their
own level).
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from approvals.queue import ApprovalQueue  # noqa: E402


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        # No defaults file here on purpose: code default is OFF,
        # so plain requests stay pending unless a test opts in.
        self.queue = ApprovalQueue(
            path=str(base / "approvals.json"),
            settings_path=str(base / "settings.json"),
            defaults_path=str(base / "no-defaults.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_request_approve_consume(self):
        rec = self.queue.request_approval(
            "send", "chief@example.invalid",
            detail="Weekly pulse", requester="chief-officer")
        self.assertEqual(rec["id"], "appr-1")
        self.assertEqual(rec["status"], "pending")
        self.assertFalse(rec["consumed"])
        self.assertFalse(rec["auto"])
        self.assertEqual(rec["history"][0]["event"], "requested")
        self.assertEqual(rec["history"][0]["by"], "chief-officer")

        approved = self.queue.approve(
            "appr-1", by="tester", reason="looks good")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["decided_by"], "tester")
        self.assertEqual(approved["reason"], "looks good")
        self.assertIsNotNone(approved["decided_at"])
        self.assertEqual(approved["history"][-1]["event"], "approved")

        consumed = self.queue.consume("appr-1", note="sent via bridge")
        self.assertTrue(consumed["consumed"])
        self.assertEqual(consumed["history"][-1]["event"], "consumed")

    def test_second_consume_refused(self):
        self.queue.request_approval("send", "a@example.invalid")
        self.queue.approve("appr-1", by="tester")
        self.queue.consume("appr-1")
        with self.assertRaises(ValueError):
            self.queue.consume("appr-1")

    def test_consume_before_approve_refused(self):
        self.queue.request_approval("send", "a@example.invalid")
        with self.assertRaises(ValueError):
            self.queue.consume("appr-1")

    def test_deny_recorded(self):
        self.queue.request_approval("publish", "weekly-pulse",
                                    requester="chief-officer")
        denied = self.queue.deny("appr-1", by="tester",
                                 reason="not ready yet")
        self.assertEqual(denied["status"], "denied")
        self.assertEqual(denied["decided_by"], "tester")
        self.assertEqual(denied["reason"], "not ready yet")
        self.assertEqual(denied["history"][-1]["event"], "denied")
        # A denied approval can never be approved or consumed.
        with self.assertRaises(ValueError):
            self.queue.approve("appr-1", by="tester")
        with self.assertRaises(ValueError):
            self.queue.consume("appr-1")

    def test_expiry_is_automatic(self):
        self.queue.request_approval("deploy", "deck",
                                    ttl_seconds=1)
        self.assertEqual(self.queue.get("appr-1")["status"], "pending")
        time.sleep(1.2)
        # Any read sweeps the record to expired.
        rec = self.queue.get("appr-1")
        self.assertEqual(rec["status"], "expired")
        self.assertEqual(rec["decided_by"], "system")
        self.assertEqual(rec["history"][-1]["event"], "expired")
        # An expired approval can never be approved or consumed.
        with self.assertRaises(ValueError):
            self.queue.approve("appr-1", by="tester")
        with self.assertRaises(ValueError):
            self.queue.consume("appr-1")

    def test_approved_record_expires_too(self):
        # Approval is not a blank cheque: an approved record past
        # its expiry refuses the send at the gate.
        self.queue.request_approval("send", "a@example.invalid",
                                    ttl_seconds=1)
        self.queue.approve("appr-1", by="tester")
        time.sleep(1.2)
        rec = self.queue.get("appr-1")
        self.assertEqual(rec["status"], "expired")
        with self.assertRaises(ValueError):
            self.queue.consume("appr-1")

    def test_expired_not_listed_as_pending(self):
        self.queue.request_approval("delete", "old-draft",
                                    ttl_seconds=1)
        self.queue.request_approval("send", "b@example.invalid")
        time.sleep(1.2)
        pending = self.queue.list_approvals(status="pending")
        self.assertEqual([r["id"] for r in pending], ["appr-2"])
        expired = self.queue.list_approvals(status="expired")
        self.assertEqual([r["id"] for r in expired], ["appr-1"])

    def test_list_and_pending_summary(self):
        self.queue.request_approval("send", "a@example.invalid")
        self.queue.request_approval("merge", "pr-12")
        self.queue.approve("appr-1", by="tester")
        all_recs = self.queue.list_approvals()
        self.assertEqual(len(all_recs), 2)
        pending = self.queue.list_approvals(status="pending")
        self.assertEqual([r["id"] for r in pending], ["appr-2"])
        summary = self.queue.pending_summary()
        self.assertEqual(summary["pending"], 1)
        self.assertIsNotNone(summary["oldest_pending_age_seconds"])
        self.assertLess(summary["oldest_pending_age_seconds"], 5)

    def test_open_action_names_accepted(self):
        # The queue is not a closed enum: any non-empty action
        # string is accepted (other modules gate their own).
        for action in ("send", "publish", "merge", "deploy",
                       "delete", "change-production-data",
                       "skill-update", "vault-release",
                       "template-import", "routine-run"):
            rec = self.queue.request_approval(action, "target-%s" % action)
            self.assertEqual(rec["action"], action)
            self.assertEqual(rec["status"], "pending")

    def test_bad_action_and_target_rejected(self):
        for bad in ("", "   ", None, 42):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.queue.request_approval(bad, "a@example.invalid")
        with self.assertRaises(ValueError):
            self.queue.request_approval("send", "   ")
        with self.assertRaises(ValueError):
            self.queue.request_approval("send", "a@example.invalid",
                                        ttl_seconds=0)
        with self.assertRaises(ValueError):
            self.queue.request_approval("send", "a@example.invalid",
                                        ttl_seconds=999999999)

    def test_unknown_id_errors(self):
        with self.assertRaises(KeyError):
            self.queue.approve("appr-99", by="tester")
        with self.assertRaises(KeyError):
            self.queue.deny("appr-99", by="tester")
        with self.assertRaises(KeyError):
            self.queue.consume("appr-99")
        self.assertIsNone(self.queue.get("appr-99"))

    def test_corrupt_file_loads_empty(self):
        path = Path(self.tmp.name) / "bad.json"
        path.write_text("{not json at all")
        queue = ApprovalQueue(path=str(path))
        self.assertEqual(queue.list_approvals(), [])
        rec = queue.request_approval("send", "a@example.invalid")
        self.assertEqual(rec["id"], "appr-1")

    def test_env_override_path(self):
        path = str(Path(self.tmp.name) / "env-approvals.json")
        old = os.environ.get("GROKBOT_APPROVALS_FILE")
        os.environ["GROKBOT_APPROVALS_FILE"] = path
        try:
            queue = ApprovalQueue()
            self.assertEqual(queue.path, path)
            queue.request_approval("send", "a@example.invalid")
            self.assertTrue(Path(path).exists())
        finally:
            if old is None:
                del os.environ["GROKBOT_APPROVALS_FILE"]
            else:
                os.environ["GROKBOT_APPROVALS_FILE"] = old

    def test_zero_trace_banned_names(self):
        # The build must carry zero trace of the forked products:
        # the banned names are written split so this test file
        # itself does not trip the grep.
        import approvals.queue as qmod
        src = Path(qmod.__file__).read_text()
        for banned in ("rak" + "azo", "octo" + "bot", "open" + "dots"):
            self.assertNotIn(banned, src.lower())


class ApprovalLevelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _queue(self, defaults=None, settings=None):
        defaults_path = str(self.base / "defaults.json")
        settings_path = str(self.base / "settings.json")
        if defaults is not None:
            Path(defaults_path).write_text(json.dumps(defaults))
        else:
            defaults_path = str(self.base / "no-defaults.json")
        if settings is not None:
            Path(settings_path).write_text(json.dumps(settings))
        return ApprovalQueue(
            path=str(self.base / "approvals.json"),
            settings_path=settings_path,
            defaults_path=defaults_path)

    def test_code_default_is_ask(self):
        queue = self._queue()
        self.assertEqual(queue.get_settings(),
                         {"mode": "ask", "bots": {}, "actions": {},
                          "alwaysAllow": [], "engineCaps": {}})
        rec = queue.request_approval("send", "a@example.invalid",
                                     requester="chief-officer")
        self.assertEqual(rec["status"], "pending")
        self.assertFalse(rec["auto"])
        self.assertEqual(rec["rule"], "ask")
        self.assertEqual(rec["history"][0]["rule"], "ask")

    def test_defaults_file_sets_mode_full(self):
        queue = self._queue(defaults={"mode": "full"})
        self.assertEqual(queue.get_settings()["mode"], "full")
        rec = queue.request_approval("send", "a@example.invalid",
                                     requester="chief-officer")
        self.assertEqual(rec["status"], "approved")
        self.assertTrue(rec["auto"])
        self.assertEqual(rec["decided_by"], "auto")
        self.assertEqual(rec["reason"], "full-access")
        self.assertEqual(rec["rule"], "full-access")
        self.assertEqual(rec["decided_at"], rec["created_at"])
        self.assertEqual(rec["history"][-1]["event"], "approved")
        self.assertTrue(rec["history"][-1]["auto"])
        self.assertEqual(rec["history"][-1]["rule"], "full-access")
        # An auto-approved record still needs consuming, once.
        queue.consume("appr-1", note="sent via bridge")
        with self.assertRaises(ValueError):
            queue.consume("appr-1")

    def test_runtime_file_overrides_defaults(self):
        queue = self._queue(defaults={"mode": "full"},
                            settings={"mode": "ask"})
        self.assertEqual(queue.get_settings()["mode"], "ask")
        rec = queue.request_approval("send", "a@example.invalid")
        self.assertEqual(rec["status"], "pending")

    def test_update_settings_round_trip(self):
        queue = self._queue()
        updated = queue.update_settings(
            {"mode": "full", "bots": {"chief-officer": "ask"}})
        self.assertEqual(updated, {"mode": "full",
                                   "bots": {"chief-officer": "ask"},
                                   "actions": {}, "alwaysAllow": [],
                                   "engineCaps": {}})
        # The runtime file now holds it; a fresh queue sees it.
        again = ApprovalQueue(
            path=str(self.base / "approvals.json"),
            settings_path=str(self.base / "settings.json"),
            defaults_path=str(self.base / "no-defaults.json"))
        self.assertEqual(again.get_settings(), updated)
        # A partial update only changes the given keys.
        updated = queue.update_settings({"actions": {"send": "full"}})
        self.assertEqual(updated["mode"], "full")
        self.assertEqual(updated["bots"], {"chief-officer": "ask"})
        self.assertEqual(updated["actions"], {"send": "full"})

    def test_update_settings_rejects_bad_types(self):
        queue = self._queue()
        with self.assertRaises(ValueError):
            queue.update_settings({"mode": "yes"})
        with self.assertRaises(ValueError):
            queue.update_settings({"mode": True})
        with self.assertRaises(ValueError):
            queue.update_settings({"bots": {"x": 1}})
        with self.assertRaises(ValueError):
            queue.update_settings({"actions": "send"})
        with self.assertRaises(ValueError):
            queue.update_settings({"alwaysAllow": "web-search"})
        with self.assertRaises(ValueError):
            queue.update_settings({"engineCaps": {"x": "nope"}})
        with self.assertRaises(ValueError):
            queue.update_settings({"engineCaps": {"x": "custom"}})

    def test_per_bot_beats_per_action_beats_global(self):
        queue = self._queue(settings={
            "mode": "full",
            "bots": {"chief-officer": "ask"},
            "actions": {"send": "ask", "deploy": "full"}})
        # Per-bot ask beats global full.
        rec = queue.request_approval("send", "a@example.invalid",
                                     requester="chief-officer")
        self.assertEqual(rec["status"], "pending")
        # Per-action ask beats global full (other bot).
        rec = queue.request_approval("send", "a@example.invalid",
                                     requester="other-bot")
        self.assertEqual(rec["status"], "pending")
        # Per-action full approves even when global is ask for it.
        queue.update_settings({"mode": "ask"})
        rec = queue.request_approval("deploy", "deck",
                                     requester="other-bot")
        self.assertEqual(rec["status"], "approved")
        self.assertEqual(rec["rule"], "full-access")
        # Per-bot full beats per-action ask.
        queue.update_settings({"bots": {"chief-officer": "full"}})
        rec = queue.request_approval("send", "a@example.invalid",
                                     requester="chief-officer")
        self.assertEqual(rec["status"], "approved")

    def test_shipped_defaults_file_is_mode_full(self):
        # The file shipped in the repo (the owner's explicit choice)
        # sets mode full; the code default stays ask.
        shipped = REPO / "approvals" / "defaults.json"
        self.assertTrue(shipped.exists())
        queue = ApprovalQueue(
            path=str(self.base / "approvals.json"),
            settings_path=str(self.base / "settings.json"),
            defaults_path=str(shipped))
        self.assertEqual(queue.get_settings()["mode"], "full")
        self.assertEqual(queue.get_settings()["alwaysAllow"],
                         ["read-file", "web-search"])

    def test_unknown_level_fails_closed_to_ask(self):
        # An unknown mode in a file never resolves permissive.
        queue = self._queue(defaults={"mode": "banana"})
        self.assertEqual(queue.get_settings()["mode"], "ask")
        rec = queue.request_approval("send", "a@example.invalid")
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(rec["rule"], "ask")
        # Same for an unknown per-bot level.
        queue = self._queue(settings={"mode": "full",
                                      "bots": {"chief-officer": "nope"}})
        self.assertEqual(queue.get_settings()["bots"],
                         {"chief-officer": "ask"})
        rec = queue.request_approval("send", "a@example.invalid",
                                     requester="chief-officer")
        self.assertEqual(rec["status"], "pending")

    def test_legacy_bool_settings_migrate(self):
        # Pre-levels files (the old always-approve switch) still
        # read: True -> full, False -> ask.
        queue = self._queue(defaults={"global": True})
        self.assertEqual(queue.get_settings()["mode"], "full")
        queue = self._queue(settings={"global": False,
                                      "bots": {"x": True},
                                      "actions": {"send": False}})
        self.assertEqual(queue.get_settings()["mode"], "ask")
        self.assertEqual(queue.get_settings()["bots"], {"x": "full"})
        self.assertEqual(queue.get_settings()["actions"],
                         {"send": "ask"})
        # The legacy POST shim works too.
        queue.update_settings({"global": True})
        self.assertEqual(queue.get_settings()["mode"], "full")

    def test_always_allow_approves_at_any_level(self):
        queue = self._queue(settings={
            "mode": "ask", "alwaysAllow": ["web-search", "read-file"]})
        rec = queue.request_approval("web-search", "kucing lucu",
                                     requester="chief-officer")
        self.assertEqual(rec["status"], "approved")
        self.assertTrue(rec["auto"])
        self.assertEqual(rec["rule"], "command-allowlist")
        self.assertEqual(rec["history"][0]["rule"],
                         "command-allowlist")
        # Anything not on the list still asks at level ask.
        rec = queue.request_approval("send", "a@example.invalid")
        self.assertEqual(rec["status"], "pending")

    def test_edits_level_only_file_edits(self):
        queue = self._queue(settings={"mode": "edits"})
        rec = queue.request_approval("edit-file", "deck/app.js")
        self.assertEqual(rec["status"], "approved")
        self.assertEqual(rec["rule"], "file-edit")
        rec = queue.request_approval("web-search", "kucing")
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(rec["rule"], "ask")
        rec = queue.request_approval("send", "a@example.invalid")
        self.assertEqual(rec["status"], "pending")

    def test_auto_level_safe_actions(self):
        queue = self._queue(settings={"mode": "auto"})
        rec = queue.request_approval("web-search", "kucing")
        self.assertEqual(rec["status"], "approved")
        self.assertEqual(rec["rule"], "safe-action")
        # Auto also covers file edits (levels are cumulative).
        rec = queue.request_approval("edit-file", "deck/app.js")
        self.assertEqual(rec["status"], "approved")
        self.assertEqual(rec["rule"], "file-edit")
        rec = queue.request_approval("send", "a@example.invalid")
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(rec["rule"], "ask")

    def test_custom_mode_defers_to_per_rule(self):
        queue = self._queue(settings={
            "mode": "custom",
            "actions": {"deploy": "full"}})
        # Per-action full decides; anything else asks.
        rec = queue.request_approval("deploy", "deck")
        self.assertEqual(rec["status"], "approved")
        rec = queue.request_approval("send", "a@example.invalid")
        self.assertEqual(rec["status"], "pending")
        # A per-bot custom defers to the per-action rule too.
        queue = self._queue(settings={
            "mode": "custom",
            "bots": {"chief-officer": "custom"},
            "actions": {"send": "full"}})
        rec = queue.request_approval("send", "a@example.invalid",
                                     requester="chief-officer")
        self.assertEqual(rec["status"], "approved")

    def test_full_behaves_like_old_always_approve(self):
        # Mode full is the old global ON: every request arrives
        # approved with auto=true and the full-access rule, and
        # the gate itself (consume once) still applies.
        queue = self._queue(settings={"mode": "full"})
        for action in ("send", "publish", "merge", "deploy",
                       "delete", "change-production-data"):
            rec = queue.request_approval(action, "target-%s" % action,
                                         requester="chief-officer")
            self.assertEqual(rec["status"], "approved", action)
            self.assertTrue(rec["auto"], action)
            self.assertEqual(rec["rule"], "full-access", action)
        queue.consume("appr-1")
        with self.assertRaises(ValueError):
            queue.consume("appr-1")

    def test_ask_blocks_everything(self):
        queue = self._queue(settings={"mode": "ask"})
        for action in ("send", "web-search", "edit-file"):
            rec = queue.request_approval(action, "t")
            self.assertEqual(rec["status"], "pending", action)
            self.assertFalse(rec["auto"], action)

    def test_decision_log_rules(self):
        # Every decision names its rule, on the record and in
        # history — including the human decision afterwards.
        queue = self._queue(settings={"mode": "full"})
        rec = queue.request_approval("send", "a@example.invalid")
        self.assertEqual(rec["rule"], "full-access")
        queue = self._queue(settings={"mode": "ask"})
        rec = queue.request_approval("send", "a@example.invalid")
        self.assertEqual(rec["rule"], "ask")
        self.assertEqual(rec["history"][0]["rule"], "ask")
        approved = queue.approve(rec["id"], by="tester", reason="ok")
        self.assertEqual(approved["rule"], "human-decision")
        self.assertEqual(approved["history"][-1]["rule"],
                         "human-decision")
        denied = queue.request_approval("send", "b@example.invalid")
        denied = queue.deny(denied["id"], by="tester", reason="no")
        self.assertEqual(denied["rule"], "human-decision")
        self.assertEqual(denied["history"][-1]["rule"],
                         "human-decision")

    def test_bot_cannot_raise_its_own_level(self):
        # request_approval takes no level/mode parameter at all:
        # a bot asking cannot smuggle one in.
        import inspect
        params = inspect.signature(
            ApprovalQueue.request_approval).parameters
        self.assertNotIn("mode", params)
        self.assertNotIn("level", params)
        # A plain request from a bot at level ask stays pending
        # no matter what the bot claims in its own fields.
        queue = self._queue(settings={"mode": "ask"})
        rec = queue.request_approval("send", "a@example.invalid",
                                     requester="sneaky-bot")
        self.assertEqual(rec["status"], "pending")
        # The only way the level moves is update_settings — the
        # human settings path — never a request.
        queue.update_settings({"mode": "full"})
        rec = queue.request_approval("send", "a@example.invalid",
                                     requester="sneaky-bot")
        self.assertEqual(rec["status"], "approved")

    def test_delegation_effective_is_min_of_inherited_and_cap(self):
        # Chief at full delegates to a teammate whose engine caps
        # at edits: the teammate auto-approves file edits only.
        queue = self._queue(settings={
            "mode": "full",
            "engineCaps": {"teammate": "edits"}})
        self.assertEqual(
            queue.resolve_level("teammate", "send",
                                inherited_level="full"), "edits")
        rec = queue.request_approval(
            "send", "a@example.invalid", requester="teammate",
            inherited_level="full")
        self.assertEqual(rec["status"], "pending")
        rec = queue.request_approval(
            "edit-file", "deck/app.js", requester="teammate",
            inherited_level="full")
        self.assertEqual(rec["status"], "approved")
        self.assertEqual(rec["rule"], "file-edit")
        # The inherited level caps too, even without an engine cap.
        queue = self._queue(settings={"mode": "full"})
        self.assertEqual(
            queue.resolve_level("teammate", "send",
                                inherited_level="edits"), "edits")
        # A "custom" inheritance carries no grant: the teammate
        # falls back to its own rules (here: global full).
        self.assertEqual(
            queue.resolve_level("teammate", "send",
                                inherited_level="custom"), "full")
        # The cap also binds a bot acting on its own.
        queue = self._queue(settings={
            "mode": "full",
            "engineCaps": {"teammate": "ask"}})
        rec = queue.request_approval("send", "a@example.invalid",
                                     requester="teammate")
        self.assertEqual(rec["status"], "pending")


if __name__ == "__main__":
    unittest.main()
