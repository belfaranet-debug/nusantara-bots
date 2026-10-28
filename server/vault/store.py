"""Encrypted credential vault — Fernet over a local JSON store.

Store: JSON file ``data/vault.json`` (env override
``GROKBOT_VAULT_FILE``), atomic writes under a lock — the same
pattern as ``office/kanban.py`` and ``computers/registry.py``.
A corrupt file loads as empty (secrets in a corrupt file are
unrecoverable by design — the file holds ciphertext only, and
there is no plaintext backup anywhere).

File shape::

    {"secrets": {"mailbox-chief": {
                    "name": "mailbox-chief",
                    "owner": "chief-officer",
                    "ciphertext": "<Fernet token, base64>",
                    "created_at": "2026-10-25T06:00:00+00:00",
                    "last_used": null}},
     "audit": [{"event": "put", "name": "mailbox-chief",
                "by": "chief-officer", "at": "..."}, ...]}

Rules (enforced here, not left to callers):
* ``list_secrets`` returns metadata ONLY — name, owner,
  created_at, last_used. No value, no ciphertext.
* ``release_secret`` needs an approval record dict that is
  approved, action ``"vault-release"``, target exactly the
  secret name, unexpired, and unconsumed. The plaintext is
  returned ONCE: the approval id is recorded in the audit, and
  a second release with the same approval id is refused
  (replay protection at the vault layer, on top of the queue's
  own ``consume()`` — the server wiring must call both).
* ``delete_secret`` also needs an approval (action ``"delete"``,
  target = the secret name).
* A wrong vault key fails decryption loudly (``VaultError``).
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import threading
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_VAULT_FILE = os.path.join(_REPO_ROOT, "data", "vault.json")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
MAX_VALUE_CHARS = 8192
MAX_OWNER_CHARS = 128

RELEASE_ACTION = "vault-release"
DELETE_ACTION = "delete"


class VaultError(Exception):
    """The vault refused an operation (bad key, bad approval, ...)."""


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value):
    """Epoch seconds for an ISO timestamp string, or None."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def load_vault_key(key=None):
    """Validate a Fernet key (base64 of 32 bytes).

    ``key`` may be the raw env value or passed explicitly.
    Returns the key string. Raises VaultError when missing or
    invalid — never generates one silently (a silently-made key
    would encrypt secrets nobody can ever decrypt again).
    """
    from cryptography.fernet import Fernet

    raw = key if key is not None else os.environ.get("GROKBOT_VAULT_KEY")
    if not raw or not isinstance(raw, str):
        raise VaultError(
            "vault belum dikonfigurasi: set GROKBOT_VAULT_KEY "
            "(base64 32 byte acak) — lihat vault/README.md")
    try:
        raw_bytes = raw.strip().encode("ascii")
        decoded = base64.urlsafe_b64decode(raw_bytes)
        if len(decoded) != 32:
            raise ValueError("wrong length")
        Fernet(raw_bytes)  # validates the encoding too
    except Exception:
        raise VaultError(
            "GROKBOT_VAULT_KEY tidak valid: harus base64 dari 32 byte "
            "acak — lihat vault/README.md")
    return raw.strip()


class VaultStore:
    """Encrypted secrets, persisted as one JSON file. Thread-safe."""

    def __init__(self, path=None, key=None):
        self.path = path or os.environ.get(
            "GROKBOT_VAULT_FILE", DEFAULT_VAULT_FILE)
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._key = load_vault_key(key)
        self._lock = threading.Lock()
        self._data = self._load()

    # -- persistence -------------------------------------------------
    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {"secrets": {}, "audit": []}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (isinstance(data, dict)
                    and isinstance(data.get("secrets"), dict)
                    and isinstance(data.get("audit"), list)):
                return data
        except (OSError, ValueError):
            pass
        return {"secrets": {}, "audit": []}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _audit_locked(self, event, name, by="", approval_id=None):
        entry = {"event": event, "name": name,
                 "by": by or "", "at": _utcnow()}
        if approval_id is not None:
            entry["approval_id"] = approval_id
        self._data["audit"].append(entry)

    def _fernet(self):
        from cryptography.fernet import Fernet
        return Fernet(self._key.encode("ascii"))

    # -- validation --------------------------------------------------
    def _validate_name(self, name):
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise VaultError(
                "name harus 1-128 karakter: huruf/angka, boleh - dan _")
        return name

    def _check_approval(self, approval, name, action):
        """Validate an approval record dict for this action/target.

        Returns the approval id. Raises VaultError on any failure:
        missing record, not approved, wrong action, wrong target,
        expired, or already consumed.
        """
        if not isinstance(approval, dict):
            raise VaultError("release butuh approval yang valid")
        approval_id = approval.get("id")
        if not isinstance(approval_id, str) or not approval_id.strip():
            raise VaultError("approval tanpa id tidak bisa dipakai")
        if approval.get("status") != "approved":
            raise VaultError("approval %s berstatus %s, bukan approved"
                             % (approval_id, approval.get("status")))
        if approval.get("action") != action:
            raise VaultError("approval %s untuk aksi %r, bukan %r"
                             % (approval_id, approval.get("action"), action))
        if approval.get("target") != name:
            raise VaultError("approval %s untuk target %r, bukan %r"
                             % (approval_id, approval.get("target"), name))
        if approval.get("consumed"):
            raise VaultError("approval %s sudah dipakai" % approval_id)
        expires = _parse_iso(approval.get("expires_at"))
        if expires is not None and datetime.now(timezone.utc).timestamp() >= expires:
            raise VaultError("approval %s sudah kedaluwarsa" % approval_id)
        return approval_id

    def _approval_already_used_locked(self, approval_id):
        return any(
            entry.get("approval_id") == approval_id
            and entry.get("event") in ("released", "deleted")
            for entry in self._data["audit"])

    # -- public API --------------------------------------------------
    def put_secret(self, name, value, owner="") -> dict:
        """Encrypt + store a secret. Returns its metadata (no value)."""
        name = self._validate_name(name)
        if not isinstance(value, str) or not value:
            raise VaultError("value harus string non-kosong")
        if len(value) > MAX_VALUE_CHARS:
            raise VaultError("value melebihi %d karakter" % MAX_VALUE_CHARS)
        if owner is not None and not isinstance(owner, str):
            raise VaultError("owner harus string")
        owner = (owner or "")[:MAX_OWNER_CHARS]
        ciphertext = self._fernet().encrypt(value.encode("utf-8")).decode("ascii")
        now = _utcnow()
        with self._lock:
            record = self._data["secrets"].get(name) or {}
            record.update({
                "name": name,
                "owner": owner,
                "ciphertext": ciphertext,
                "created_at": record.get("created_at") or now,
                "last_used": record.get("last_used"),
            })
            self._data["secrets"][name] = record
            self._audit_locked("put", name, by=owner)
            self._save()
            return self._metadata(record)

    def _metadata(self, record) -> dict:
        return {"name": record["name"], "owner": record.get("owner", ""),
                "created_at": record.get("created_at"),
                "last_used": record.get("last_used")}

    def list_secrets(self) -> list:
        """Metadata ONLY — name, owner, created_at, last_used.

        No values, no ciphertext. Ever.
        """
        with self._lock:
            return [self._metadata(r) for _, r in
                    sorted(self._data["secrets"].items())]

    def audit_log(self) -> list:
        """The audit trail (events only — no secret values)."""
        with self._lock:
            return copy.deepcopy(self._data["audit"])

    def release_secret(self, name, approval) -> str:
        """Decrypt + return a secret ONCE, under an approval.

        ``approval`` is the record dict from ``approvals/queue.py``.
        Checks, in order: approval exists + approved + action
        ``"vault-release"`` + target == name + unexpired +
        unconsumed + this approval id not already used for a
        release. A wrong vault key raises VaultError (loud
        decryption failure). On success the approval id is
        recorded in the audit, ``last_used`` is set, and the
        plaintext is returned to the caller — the server-side
        executor that will use it directly. The caller must also
        call ``approvals.consume(approval_id)`` (see
        ``vault/server-wiring.patch.md``).
        """
        name = self._validate_name(name)
        approval_id = self._check_approval(approval, name, RELEASE_ACTION)
        with self._lock:
            if self._approval_already_used_locked(approval_id):
                raise VaultError("approval %s sudah dipakai" % approval_id)
            record = self._data["secrets"].get(name)
            if record is None:
                raise VaultError("secret %r tidak ada" % name)
            try:
                value = self._fernet().decrypt(
                    record["ciphertext"].encode("ascii")).decode("utf-8")
            except Exception:
                raise VaultError(
                    "dekripsi gagal — kunci vault salah atau data rusak")
            record["last_used"] = _utcnow()
            self._audit_locked("released", name,
                               by=approval.get("decided_by") or "",
                               approval_id=approval_id)
            self._save()
            return value

    def delete_secret(self, name, approval) -> bool:
        """Delete a secret — also needs an approval (action "delete").

        Returns True. Raises VaultError when the secret is missing
        or the approval fails any check.
        """
        name = self._validate_name(name)
        approval_id = self._check_approval(approval, name, DELETE_ACTION)
        with self._lock:
            if self._approval_already_used_locked(approval_id):
                raise VaultError("approval %s sudah dipakai" % approval_id)
            if name not in self._data["secrets"]:
                raise VaultError("secret %r tidak ada" % name)
            del self._data["secrets"][name]
            self._audit_locked("deleted", name,
                               by=approval.get("decided_by") or "",
                               approval_id=approval_id)
            self._save()
            return True
