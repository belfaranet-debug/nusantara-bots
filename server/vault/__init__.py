"""Encrypted credential vault for the own build — Phase 2 (i).

Local encrypted storage for BOT credentials (for example a
per-bot mailbox token). A secret is released ONLY through the
human approval queue (``approvals/``) and ONLY to server-side
executor code that uses it directly (a mail sender, a tool
call) — never to a chat/model path, and the deck only ever
shows metadata (name, owner, created/used times).

Encryption: Fernet (AES-128-CBC + HMAC-SHA256, via the
``cryptography`` package, already installed on this VM —
version 50.0.2 at build time). The key comes from the env var
``GROKBOT_VAULT_KEY`` (base64 of 32 random bytes) and is never
written to disk, never committed, never logged. A wrong key
fails decryption loudly (``VaultError``), never silently.

Isolation, stated honestly (Vellum-style, as far as a local
single-owner build can go): the released plaintext lives only
in the executor's memory for the one call that needs it; the
model's keys stay in the browser (BYO-API) and are never in
this vault. This vault does not protect against a compromised
server process — it protects stored credentials at rest and
gates every release behind a human approval.
"""

from .store import VaultError, VaultStore, load_vault_key

__all__ = ["VaultError", "VaultStore", "load_vault_key"]
