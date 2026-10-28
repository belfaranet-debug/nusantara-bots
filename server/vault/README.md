# vault/ — encrypted credential vault (Phase 2, i)

Local encrypted storage for BOT credentials (for example a
per-bot mailbox token). A secret is released ONLY through the
human approval queue (`approvals/`) and ONLY to server-side
executor code that uses it directly — never to a chat/model
path, and the deck only ever shows metadata.

## Choice of crypto (decided at build time)

`cryptography` **is installed on this VM** (version 50.0.2,
checked with `python3 -c "import cryptography"`), so the vault
uses **Fernet** (AES-128-CBC + HMAC-SHA256) from that package.
No `pip install` was needed and no OpenSSL-CLI fallback was
built: Fernet gives authenticated encryption in one call
(wrong key / tampered ciphertext fails loudly), while an
openssl-CLI fallback would have meant shelling out per
operation and hand-rolling the authentication. Standard
library only would have meant unauthenticated AES — not
acceptable for a credential store.

## Files

| File | What it does |
|---|---|
| `store.py` | `VaultStore` — `put_secret` / `list_secrets` (metadata only) / `release_secret` / `delete_secret` / `audit_log`. Store JSON `data/vault.json` (ciphertext + nonce inside the Fernet token; NO plaintext anywhere), atomic writes under a lock, same pattern as `office/kanban.py`. |
| `tests/test_vault.py` | unittest, temp-dir store, throwaway keys generated inside the tests. |

## The vault key — the owner must create it

The key comes from env var `GROKBOT_VAULT_KEY`: base64 of 32
random bytes. Create it with this command (the command only —
never print or share the value it outputs):

```bash
python3 -c "import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

Then set it in the server's environment (same place the other
`GROKBOT_*` vars live — NOT in the repo, NOT in a file that
gets committed):

```
GROKBOT_VAULT_KEY=<output perintah di atas>
```

Rules:

- **One key per deployment.** The same key must be set every
  time the server starts, or previously stored secrets become
  undecryptable (loud `VaultError`, not silent garbage).
- **Losing the key = losing the secrets.** There is no
  recovery, no backdoor, no plaintext backup. Re-create the
  secret with `put_secret` under a new key if the old one is
  lost.
- **Rotating the key** means: release each secret under the
  old key, `put_secret` it again under the new key. There is
  no in-place re-key (by design — re-keying without an
  approval would bypass the release gate).
- **Never** put the key in git, in a chat message, in a deck
  file, or in a `data/` file.

Env var for the store path (tests only): `GROKBOT_VAULT_FILE`.

## Release rules (enforced in `store.py`)

`release_secret(name, approval)` checks, in order:

1. approval record exists and has an `id`;
2. `status == "approved"`;
3. `action == "vault-release"`;
4. `target == name` (exact);
5. not expired (`expires_at` in the future);
6. not `consumed`;
7. this approval `id` has not already released/deleted
   anything (replay protection at the vault layer — the
   server wiring must ALSO call `approvals.consume()`).

Any failure raises `VaultError` and nothing is returned.
A wrong vault key raises `VaultError` ("dekripsi gagal —
kunci vault salah atau data rusak").

`delete_secret(name, approval)` needs the same checks with
`action == "delete"`.

**Note for the coordinator:** the approval queue's
`APPROVAL_ACTIONS` (in `approvals/queue.py`) currently lists
`send, publish, merge, deploy, delete, change-production-data`
— it does NOT include `"vault-release"`. The server wiring
(`vault/server-wiring.patch.md`) tells the coordinator to add
`"vault-release"` to that tuple, otherwise no release approval
can ever be requested through the queue. Delete approvals
already work (`"delete"` is in the list).

## Isolation — stated honestly

- The released plaintext is returned ONLY to the server-side
  executor that called `release_secret` (for example the mail
  sender about to use a mailbox token). It must be used
  immediately and not stored, logged, or forwarded.
- It is NEVER returned to a chat/model path, never rendered
  in the deck, and never written to any file. `list_secrets`
  returns metadata only; the audit log records events only.
- The model's API keys stay in the browser (BYO-API) and are
  NEVER in this vault. This vault is for BOT credentials
  (mailbox tokens, per-bot service tokens), not model keys.
- What this vault does NOT do: protect against a compromised
  server process (anyone who can read the process memory or
  the env var can decrypt). It protects credentials **at
  rest** and gates every release behind a human approval.
  That is the honest scope for a local single-owner build.

## Tests

```bash
python3 -m unittest discover -s vault/tests -v
```

Tests use a temp-dir store and keys generated inside the test
run (`secrets.token_bytes`) — never a real value, never the
real `data/` directory.
