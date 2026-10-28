"""Push tests — stdlib unittest, no network, no real credentials.

Covers: token registry CRUD + validation; sender refuses cleanly
without credentials; message body building (FCM data values are
strings); hooks fan out to a fake sender and are honest no-ops
when there are no tokens or no credentials; zero-trace check
(forbidden product/fork names, written as split literals).
"""

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from push.hooks import (  # noqa: E402
    approval_payload,
    notify_approval_requested,
    notify_task_done,
    task_done_payload,
)
from push.send import (  # noqa: E402
    NOT_CONFIGURED_MESSAGE,
    FcmSender,
    PushError,
    PushNotConfigured,
    _build_jwt,
    build_message,
)
from push.tokens import PushTokenRegistry  # noqa: E402


class FakeSender:
    """Records sends; configurable 'configured' state."""

    def __init__(self, configured=True):
        self.configured_flag = configured
        self.sent = []

    def configured(self):
        if self.configured_flag:
            return True, ""
        return False, NOT_CONFIGURED_MESSAGE

    def send_notification(self, token, title, body, data=None):
        self.sent.append({"token": token, "title": title,
                          "body": body, "data": data or {}})
        return {"ok": True, "name": "projects/x/messages/1"}


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = PushTokenRegistry(
            path=str(Path(self.tmp.name) / "push_tokens.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_and_list(self):
        rec = self.registry.register(
            "fcm-token-abcdef1234", "chief-officer",
            device_label="Pixel 7 Pro", platform="web")
        self.assertEqual(rec["character_id"], "chief-officer")
        self.assertEqual(rec["platform"], "web")
        self.assertIsNotNone(rec["created_at"])
        listed = self.registry.list_tokens()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["token"], "fcm-token-abcdef1234")

    def test_register_same_token_refreshes_not_duplicates(self):
        self.registry.register("fcm-token-abcdef1234", "chief-officer")
        self.registry.register("fcm-token-abcdef1234", "chief-officer",
                               device_label="Pixel 7 Pro")
        listed = self.registry.list_tokens()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["device_label"], "Pixel 7 Pro")

    def test_unregister(self):
        self.registry.register("fcm-token-abcdef1234", "chief-officer")
        self.assertTrue(self.registry.unregister("fcm-token-abcdef1234"))
        self.assertFalse(self.registry.unregister("fcm-token-abcdef1234"))
        self.assertEqual(self.registry.list_tokens(), [])

    def test_public_list_hides_token_values(self):
        self.registry.register("fcm-token-abcdef1234", "chief-officer")
        public = self.registry.public_list()
        self.assertEqual(len(public), 1)
        self.assertNotIn("token", public[0])
        self.assertEqual(public[0]["character_id"], "chief-officer")

    def test_token_values_for_sender(self):
        self.registry.register("fcm-token-abcdef1234", "chief-officer")
        self.registry.register("fcm-token-xyz9876543", "yt-analyst")
        self.assertEqual(
            self.registry.token_values("chief-officer"),
            ["fcm-token-abcdef1234"])
        self.assertEqual(len(self.registry.token_values()), 2)

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.registry.register("short", "chief-officer")
        with self.assertRaises(ValueError):
            self.registry.register("fcm-token-abcdef1234", "")
        with self.assertRaises(ValueError):
            self.registry.register("fcm-token-abcdef1234",
                                   "chief-officer", platform="carrier-pigeon")

    def test_corrupt_file_loads_empty(self):
        path = Path(self.tmp.name) / "push_tokens.json"
        path.write_text("{not json", encoding="utf-8")
        registry = PushTokenRegistry(path=str(path))
        self.assertEqual(registry.list_tokens(), [])


class SendTest(unittest.TestCase):
    def test_unconfigured_sender_fails_cleanly(self):
        sender = FcmSender(service_account_path="/nonexistent/nope.json")
        ok, reason = sender.configured()
        self.assertFalse(ok)
        self.assertIn("kredensial FCM belum dikonfigurasi", reason)
        with self.assertRaises(PushNotConfigured) as ctx:
            sender.send_notification("tok", "T", "B")
        self.assertIn("kredensial FCM belum dikonfigurasi", str(ctx.exception))

    def test_unconfigured_env_missing(self):
        old = os.environ.pop("GROKBOT_FCM_SERVICE_ACCOUNT", None)
        try:
            sender = FcmSender(service_account_path=None)
            ok, _ = sender.configured()
            self.assertFalse(ok)
        finally:
            if old is not None:
                os.environ["GROKBOT_FCM_SERVICE_ACCOUNT"] = old

    def test_bad_service_account_json_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sa.json"
            path.write_text('{"project_id": "x"}', encoding="utf-8")
            sender = FcmSender(service_account_path=str(path))
            ok, reason = sender.configured()
            self.assertFalse(ok)
            self.assertIn("field hilang", reason)

    def test_build_message_stringifies_data(self):
        msg = build_message("tok123", "Hi", "Body",
                            data={"n": 3, "flag": True})
        self.assertEqual(msg["message"]["token"], "tok123")
        self.assertEqual(msg["message"]["notification"]["title"], "Hi")
        self.assertEqual(msg["message"]["data"], {"n": "3", "flag": "True"})

    def test_build_message_validation(self):
        with self.assertRaises(ValueError):
            build_message("", "T", "B")
        with self.assertRaises(ValueError):
            build_message("tok", "", "B")

    def test_jwt_shape_with_generated_key(self):
        # A throwaway RSA key made inside the test — never a real key.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode("utf-8")
        account = {"project_id": "demo-project",
                   "private_key": pem,
                   "client_email": "sa@demo-project.iam.gserviceaccount.com"}
        jwt = _build_jwt(account, now=1700000000)
        parts = jwt.split(".")
        self.assertEqual(len(parts), 3)
        header = json.loads(base64.urlsafe_b64decode(
            parts[0] + "=" * (-len(parts[0]) % 4)))
        self.assertEqual(header["alg"], "RS256")
        claims = json.loads(base64.urlsafe_b64decode(
            parts[1] + "=" * (-len(parts[1]) % 4)))
        self.assertEqual(claims["iss"], account["client_email"])
        self.assertEqual(claims["exp"], 1700003600)


class HooksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = PushTokenRegistry(
            path=str(Path(self.tmp.name) / "push_tokens.json"))
        self.registry.register("fcm-token-abcdef1234", "chief-officer")
        self.registry.register("fcm-token-xyz9876543", "yt-analyst")

    def tearDown(self):
        self.tmp.cleanup()

    def test_approval_payload(self):
        payload = approval_payload(
            {"id": "appr-1", "action": "send",
             "target": "chief@example.invalid"})
        self.assertEqual(payload["title"], "Persetujuan diminta")
        self.assertIn("send", payload["body"])
        self.assertEqual(payload["data"]["type"], "approval_requested")
        self.assertEqual(payload["data"]["approval_id"], "appr-1")

    def test_task_done_payload(self):
        payload = task_done_payload(
            {"id": "card-2", "title": "Tulis naskah", "assignee": "chief-officer"})
        self.assertEqual(payload["title"], "Tugas selesai")
        self.assertEqual(payload["body"], "Tulis naskah")
        self.assertEqual(payload["data"]["type"], "task_done")

    def test_notify_approval_fans_out(self):
        sender = FakeSender()
        result = notify_approval_requested(
            {"id": "appr-1", "action": "send", "target": "a@example.invalid"},
            registry=self.registry, sender=sender)
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["sent"], 2)
        self.assertEqual(result["failed"], [])
        self.assertIsNone(result["skipped_reason"])
        self.assertEqual(sender.sent[0]["title"], "Persetujuan diminta")

    def test_notify_task_done_fans_out(self):
        sender = FakeSender()
        result = notify_task_done(
            {"id": "card-2", "title": "Tulis naskah"},
            registry=self.registry, sender=sender)
        self.assertEqual(result["sent"], 2)
        self.assertEqual(sender.sent[0]["title"], "Tugas selesai")

    def test_no_tokens_is_honest_noop(self):
        empty = PushTokenRegistry(
            path=str(Path(self.tmp.name) / "empty.json"))
        sender = FakeSender()
        result = notify_approval_requested(
            {"id": "appr-1", "action": "send", "target": "a@example.invalid"},
            registry=empty, sender=sender)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["skipped_reason"], "no registered tokens")
        self.assertEqual(sender.sent, [])

    def test_unconfigured_sender_is_honest_noop(self):
        sender = FakeSender(configured=False)
        result = notify_task_done(
            {"id": "card-2", "title": "Tulis naskah"},
            registry=self.registry, sender=sender)
        self.assertEqual(result["sent"], 0)
        self.assertIn("kredensial FCM belum dikonfigurasi",
                      result["skipped_reason"])

    def test_sender_error_per_token_does_not_stop_others(self):
        class FlakySender(FakeSender):
            def send_notification(self, token, title, body, data=None):
                if token.startswith("fcm-token-abc"):
                    raise PushError("FCM HTTP 404")
                return super().send_notification(token, title, body, data)

        sender = FlakySender()
        result = notify_approval_requested(
            {"id": "appr-1", "action": "send", "target": "a@example.invalid"},
            registry=self.registry, sender=sender)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(result["failed"]), 1)


class ZeroTraceTest(unittest.TestCase):
    def test_no_forbidden_product_names_in_push_files(self):
        banned = ["rak" + "azo", "octo" + "bot", "open" + "dots"]
        here = Path(__file__).resolve()
        files = [here, REPO / "push" / "__init__.py",
                 REPO / "push" / "tokens.py", REPO / "push" / "send.py",
                 REPO / "push" / "hooks.py", REPO / "push" / "README.md"]
        for path in files:
            text = path.read_text(encoding="utf-8").lower()
            for name in banned:
                self.assertNotIn(name, text, "%s found in %s" % (name, path))


if __name__ == "__main__":
    unittest.main()
