"""Vault tests — stdlib unittest, no network, no real keys.

Covers: put/list (no value leaks into list or the file);
release without approval refused (missing / pending / denied /
wrong action / wrong target / expired / consumed); release
with an approved approval returns the value once + audit is
recorded; replay refused; wrong key fails loudly; delete needs
an approval; zero-trace check (forbidden product/fork names,
written as split literals).

Keys are generated inside each test (secrets.token_bytes) and
the store lives in a temp dir — never a real value, never the
real data/ directory.
"""

import base64
import json
import os
import secrets
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from vault.store import (  # noqa: E402
    DELETE_ACTION,
    RELEASE_ACTION,
    VaultError,
    VaultStore,
    load_vault_key,
)


def _make_key():
    return base64.b64encode(secrets.token_bytes(32)).decode()


def _approval(**overrides):
    now = datetime.now(timezone.utc)
    rec = {
        "id": "appr-1",
        "action": RELEASE_ACTION,
        "target": "mailbox-chief",
        "status": "approved",
        "decided_by": "tester",
        "consumed": False,
        "expires_at": (now + timedelta(hours=1)).isoformat(
            timespec="seconds"),
    }
    rec.update(overrides)
    return rec


class VaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key = _make_key()
        self.store = VaultStore(
            path=str(Path(self.tmp.name) / "vault.json"), key=self.key)

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_and_list_hides_value(self):
        meta = self.store.put_secret(
            "mailbox-chief", "tok-very-secret-value", owner="chief-officer")
        self.assertEqual(meta["name"], "mailbox-chief")
        self.assertNotIn("value", meta)
        self.assertNotIn("ciphertext", meta)
        listed = self.store.list_secrets()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "mailbox-chief")
        self.assertEqual(listed[0]["owner"], "chief-officer")
        self.assertIsNone(listed[0]["last_used"])
        # The file itself holds ciphertext only — no plaintext.
        raw = (Path(self.tmp.name) / "vault.json").read_text(
            encoding="utf-8")
        self.assertNotIn("tok-very-secret-value", raw)
        self.assertIn("ciphertext", raw)

    def test_put_validation(self):
        with self.assertRaises(VaultError):
            self.store.put_secret("bad name!", "v")
        with self.assertRaises(VaultError):
            self.store.put_secret("good-name", "")
        with self.assertRaises(VaultError):
            self.store.put_secret("good-name", "x" * 8193)

    def test_release_with_approved_approval_once(self):
        self.store.put_secret("mailbox-chief", "tok-very-secret-value")
        value = self.store.release_secret(
            "mailbox-chief", _approval())
        self.assertEqual(value, "tok-very-secret-value")
        # last_used is now set, and the audit recorded the release.
        listed = self.store.list_secrets()
        self.assertIsNotNone(listed[0]["last_used"])
        audit = self.store.audit_log()
        events = [(e["event"], e.get("approval_id")) for e in audit]
        self.assertIn(("put", None), events)
        self.assertIn(("released", "appr-1"), events)

    def test_release_without_approval_refused(self):
        self.store.put_secret("mailbox-chief", "tok-very-secret-value")
        with self.assertRaises(VaultError):
            self.store.release_secret("mailbox-chief", None)

    def test_release_pending_denied_consumed_refused(self):
        self.store.put_secret("mailbox-chief", "tok-very-secret-value")
        for bad in (
                _approval(status="pending"),
                _approval(status="denied"),
                _approval(status="expired"),
                _approval(consumed=True)):
            with self.assertRaises(VaultError, msg=str(bad)):
                self.store.release_secret("mailbox-chief", bad)

    def test_release_wrong_action_or_target_refused(self):
        self.store.put_secret("mailbox-chief", "tok-very-secret-value")
        with self.assertRaises(VaultError):
            self.store.release_secret(
                "mailbox-chief", _approval(action="send"))
        with self.assertRaises(VaultError):
            self.store.release_secret(
                "mailbox-chief", _approval(target="mailbox-other"))
        with self.assertRaises(VaultError):
            self.store.release_secret(
                "other-name", _approval())

    def test_release_expired_approval_refused(self):
        self.store.put_secret("mailbox-chief", "tok-very-secret-value")
        past = (datetime.now(timezone.utc) - timedelta(seconds=10)
                ).isoformat(timespec="seconds")
        with self.assertRaises(VaultError):
            self.store.release_secret(
                "mailbox-chief", _approval(expires_at=past))

    def test_replay_refused(self):
        self.store.put_secret("mailbox-chief", "tok-very-secret-value")
        approval = _approval()
        self.store.release_secret("mailbox-chief", approval)
        # Same approval dict again (still marked unconsumed on the
        # dict itself) — the vault's audit refuses the replay.
        with self.assertRaises(VaultError):
            self.store.release_secret("mailbox-chief", approval)

    def test_release_missing_secret_refused(self):
        with self.assertRaises(VaultError):
            self.store.release_secret("no-such-secret", _approval())

    def test_wrong_key_fails_loudly(self):
        self.store.put_secret("mailbox-chief", "tok-very-secret-value")
        other = VaultStore(
            path=str(Path(self.tmp.name) / "vault.json"),
            key=_make_key())
        with self.assertRaises(VaultError) as ctx:
            other.release_secret("mailbox-chief", _approval())
        self.assertIn("dekripsi gagal", str(ctx.exception))

    def test_delete_needs_approval(self):
        self.store.put_secret("mailbox-chief", "tok-very-secret-value")
        with self.assertRaises(VaultError):
            self.store.delete_secret(
                "mailbox-chief", _approval(action=RELEASE_ACTION))
        with self.assertRaises(VaultError):
            self.store.delete_secret("mailbox-chief", None)
        self.assertTrue(self.store.delete_secret(
            "mailbox-chief",
            _approval(action=DELETE_ACTION, id="appr-2")))
        self.assertEqual(self.store.list_secrets(), [])
        events = [e["event"] for e in self.store.audit_log()]
        self.assertIn("deleted", events)
        with self.assertRaises(VaultError):
            self.store.delete_secret(
                "mailbox-chief",
                _approval(action=DELETE_ACTION, id="appr-3"))

    def test_missing_key_refuses_to_start(self):
        old = os.environ.pop("GROKBOT_VAULT_KEY", None)
        try:
            with self.assertRaises(VaultError):
                VaultStore(path=str(Path(self.tmp.name) / "v2.json"))
            with self.assertRaises(VaultError):
                load_vault_key("not-base64!!")
            with self.assertRaises(VaultError):
                load_vault_key(base64.b64encode(b"too-short").decode())
        finally:
            if old is not None:
                os.environ["GROKBOT_VAULT_KEY"] = old

    def test_env_key_is_used(self):
        os.environ["GROKBOT_VAULT_KEY"] = self.key
        try:
            store = VaultStore(
                path=str(Path(self.tmp.name) / "v3.json"))
            store.put_secret("x", "y")
            self.assertEqual(store.release_secret("x", _approval(
                target="x", id="appr-9")), "y")
        finally:
            del os.environ["GROKBOT_VAULT_KEY"]

    def test_corrupt_file_loads_empty(self):
        path = Path(self.tmp.name) / "vault.json"
        path.write_text("{not json", encoding="utf-8")
        store = VaultStore(path=str(path), key=self.key)
        self.assertEqual(store.list_secrets(), [])


class ZeroTraceTest(unittest.TestCase):
    def test_no_forbidden_product_names_in_vault_files(self):
        banned = ["rak" + "azo", "octo" + "bot", "open" + "dots"]
        here = Path(__file__).resolve()
        files = [here, REPO / "vault" / "__init__.py",
                 REPO / "vault" / "store.py", REPO / "vault" / "README.md"]
        for path in files:
            text = path.read_text(encoding="utf-8").lower()
            for name in banned:
                self.assertNotIn(name, text, "%s found in %s" % (name, path))


if __name__ == "__main__":
    unittest.main()
