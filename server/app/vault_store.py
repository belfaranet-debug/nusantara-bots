"""Login-credential vault for the own build (0.10.0, bagian G).

One encrypted record per service: a label (display name, NOT
secret), a username and a password. The username + password are
stored as ONE encrypted blob per service; the file on disk holds
ciphertext only — never a plaintext username or password.

Encryption: Fernet (AES-128-CBC + HMAC-SHA256, via the
``cryptography`` package — verified installed on the PC,
version 50.0.2). No hand-rolled cipher, no stdlib-only
"encryption": the standard library has no AES, so this module
refuses to work without ``cryptography`` instead of pretending.

Key resolution (first match wins):
  1. env ``GROKBOT_VAULT_KEY`` (base64 of 32 random bytes) — the
     same key the older approval-gated vault uses, so one key
     serves both stores when the server is started with it;
  2. key file (default ``<repo>/vault.key``, env
     ``GROKBOT_VAULT_KEY_FILE``), mode 0600, created once at
     install time — NEVER auto-generated on import (a silently
     made key would encrypt secrets nobody can ever decrypt
     again; the installer creates it explicitly and says so).

Losing the key = losing the secrets. There is no recovery, no
backdoor, no plaintext backup. A wrong key fails decryption
loudly (``CredentialVaultError``), never silently.

Rules (enforced here, not left to callers):
* ``list_services`` / ``has`` return metadata ONLY — service,
  label, created_at, updated_at. No username, no password, no
  ciphertext. Ever.
* ``get`` returns the full record and is for SERVER-SIDE use
  only (the login executor that types the values into the
  browser directly). It must NEVER be called from a handler
  that returns its result to a chat/model/client path, and its
  return value must NEVER be logged.
* This module does no logging at all: no value, no username,
  no password ever reaches a log line. Callers must keep it
  that way (never log what ``get`` returns).
* Writes are atomic (temp file + os.replace) under a lock,
  file mode 0600, parent directory 0700.
* Service ids are ``^[a-z0-9][a-z0-9-]{0,63}$`` (same rule the
  app validates in its credential form).

File shape (``vault.json``)::

    {"version": 1,
     "services": {"youtube-studio": {
        "label": "Login YouTube Studio (Google)",
        "created_at": "2026-10-26T07:00:00+00:00",
        "updated_at": "2026-10-26T07:00:00+00:00",
        "ciphertext": "<Fernet token of JSON {username, password}>"}}}
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VAULT_FILE = _REPO_ROOT / "vault.json"
DEFAULT_KEY_FILE = _REPO_ROOT / "vault.key"

SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_LABEL_CHARS = 128
MAX_USERNAME_CHARS = 256
MAX_PASSWORD_CHARS = 8192


class CredentialVaultError(Exception):
    """The vault refused an operation (no key, bad key, bad input)."""


class VaultNotConfigured(CredentialVaultError):
    """No vault key is available (env nor key file)."""


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def generate_key():
    """Return a fresh Fernet key string (base64 of 32 random bytes).

    For the installer only. The value must be stored exactly
    once (env or key file) and never printed, logged, or
    committed — the caller is responsible for that.
    """
    import secrets

    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def load_vault_key(key=None, key_file=None):
    """Validate a Fernet key (base64 of 32 bytes) and return it.

    ``key`` may be passed explicitly; otherwise env
    ``GROKBOT_VAULT_KEY`` wins, then the key file (``key_file``,
    env ``GROKBOT_VAULT_KEY_FILE``, or ``<repo>/vault.key``).
    Raises ``VaultNotConfigured`` when no key is found and
    ``CredentialVaultError`` when the found key is invalid.
    Never generates a key silently.
    """
    from cryptography.fernet import Fernet

    raw = key
    if raw is None:
        raw = os.environ.get("GROKBOT_VAULT_KEY") or ""
    if raw:
        raw = raw.strip()
    if not raw:
        key_path = key_file or os.environ.get("GROKBOT_VAULT_KEY_FILE")
        key_path = Path(key_path) if key_path else DEFAULT_KEY_FILE
        try:
            raw = key_path.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
    if not raw:
        raise VaultNotConfigured(
            "vault belum dikonfigurasi: set GROKBOT_VAULT_KEY "
            "(base64 32 byte acak) atau buat file kunci sekali "
            "di vault.key (mode 0600)")
    try:
        raw_bytes = raw.encode("ascii")
        decoded = base64.urlsafe_b64decode(raw_bytes)
        if len(decoded) != 32:
            raise ValueError("wrong length")
        Fernet(raw_bytes)  # validates the encoding too
    except Exception:
        raise CredentialVaultError(
            "kunci vault tidak valid: harus base64 dari 32 byte acak")
    return raw


def _validate_service(service):
    if not isinstance(service, str) or not SERVICE_RE.match(service):
        raise CredentialVaultError(
            "service harus 1-64 karakter: huruf kecil/angka, boleh -")
    return service


class CredentialVault:
    """Encrypted login credentials, one record per service."""

    def __init__(self, path=None, key=None, key_file=None):
        self.path = Path(path or os.environ.get(
            "GROKBOT_LOGIN_VAULT_FILE", str(DEFAULT_VAULT_FILE)))
        directory = self.path.parent
        if str(directory):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        self._key = load_vault_key(key, key_file)
        self._lock = threading.Lock()
        self._data = self._load()

    # -- persistence -------------------------------------------------
    def _load(self):
        if not self.path.exists():
            return {"version": 1, "services": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (isinstance(data, dict)
                    and isinstance(data.get("services"), dict)):
                return {"version": 1, "services": data["services"]}
        except (OSError, ValueError):
            pass
        # A corrupt file loads as empty: its secrets are
        # unrecoverable by design (ciphertext only, no backup).
        return {"version": 1, "services": {}}

    def _save_locked(self):
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _fernet(self):
        from cryptography.fernet import Fernet

        return Fernet(self._key.encode("ascii"))

    # -- public API --------------------------------------------------
    def list_services(self):
        """Metadata ONLY — service, label, created_at, updated_at.

        No username, no password, no ciphertext. Ever.
        """
        with self._lock:
            out = []
            for service in sorted(self._data["services"]):
                rec = self._data["services"][service]
                out.append({
                    "service": service,
                    "label": rec.get("label", service),
                    "created_at": rec.get("created_at"),
                    "updated_at": rec.get("updated_at"),
                })
            return out

    def has(self, service):
        """True when a credential for this service is stored."""
        try:
            service = _validate_service(service)
        except CredentialVaultError:
            return False
        with self._lock:
            return service in self._data["services"]

    def get(self, service):
        """Full record — SERVER-SIDE ONLY, never to chat/model/log.

        Returns {"service", "label", "username", "password",
        "created_at", "updated_at"}. Raises KeyError when the
        service is not stored, CredentialVaultError when the
        record cannot be decrypted (wrong key / corrupt data).
        """
        service = _validate_service(service)
        with self._lock:
            rec = self._data["services"].get(service)
            if rec is None:
                raise KeyError(service)
            try:
                plain = self._fernet().decrypt(
                    rec["ciphertext"].encode("ascii")).decode("utf-8")
                creds = json.loads(plain)
                username = creds["username"]
                password = creds["password"]
            except Exception:
                raise CredentialVaultError(
                    "dekripsi gagal — kunci vault salah atau data rusak")
            return {
                "service": service,
                "label": rec.get("label", service),
                "username": username,
                "password": password,
                "created_at": rec.get("created_at"),
                "updated_at": rec.get("updated_at"),
            }

    def put(self, service, label, username, password):
        """Encrypt + store one service credential. Returns metadata.

        Overwriting an existing service keeps its created_at.
        """
        service = _validate_service(service)
        if not isinstance(username, str) or not username.strip():
            raise CredentialVaultError("username tidak boleh kosong")
        if len(username) > MAX_USERNAME_CHARS:
            raise CredentialVaultError("username terlalu panjang")
        if not isinstance(password, str) or not password:
            raise CredentialVaultError("password tidak boleh kosong")
        if len(password) > MAX_PASSWORD_CHARS:
            raise CredentialVaultError("password terlalu panjang")
        label = (label or service) if isinstance(label, str) else service
        label = label.strip()[:MAX_LABEL_CHARS] or service
        blob = json.dumps({"username": username, "password": password},
                          ensure_ascii=False)
        ciphertext = self._fernet().encrypt(
            blob.encode("utf-8")).decode("ascii")
        now = _utcnow()
        with self._lock:
            rec = self._data["services"].get(service) or {}
            rec.update({
                "label": label,
                "ciphertext": ciphertext,
                "created_at": rec.get("created_at") or now,
                "updated_at": now,
            })
            self._data["services"][service] = rec
            self._save_locked()
            return {
                "service": service,
                "label": label,
                "created_at": rec["created_at"],
                "updated_at": now,
            }

    def delete(self, service):
        """Delete one service credential. True when it existed."""
        service = _validate_service(service)
        with self._lock:
            if service not in self._data["services"]:
                return False
            del self._data["services"][service]
            self._save_locked()
            return True
