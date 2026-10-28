# Computer Registry — Phase 1 (Next-Level Build)

The local answer to the official app's "Sand Machine" (RE report
§5): one persistent computer per account. Ours is a **record**,
not infrastructure: a JSON file (`data/computers.json` by default,
override with `GROKBOT_COMPUTERS_FILE`) holding the machines the
owner already has — this PC, the phone on the device bridge.

## What it is

- `register(label, capabilities, notes)` → record with a stable
  id (`local-1`, `local-2`, …), owner `owner`, status `active`,
  `created_at` / `last_seen` timestamps.
- `list_machines()` / `get(id)` — read-only views.
- `set_status(id, status)` — `active` | `paused` | `removed`.
  `removed` is terminal: a removed machine is history, never
  resurrected.
- `touch(id)` — heartbeat: refreshes `last_seen` (used by the
  presence beacon, `/api/presence/beacon`).

## What it is NOT

- It does not provision, start, stop, or execute anything on a
  machine. The actual work on a machine goes through the existing
  device bridge (`devices/bridge.py`), which is a separate concern.
- It issues no credentials (the official `IssueGrokBotUserComputerCredential`
  has no equivalent here — BYO-API invariant: keys live only in the
  browser).
- It is single-owner by design: every record's owner is `owner`.

## Persistence

Atomic JSON writes under a lock (same pattern as `chat_store.py`).
A malformed file loads as an empty registry rather than crashing
the server. Tests use temp files only; the real `data/` file is
never touched by tests.

## Tests

```bash
cd server
python3 -m unittest discover -s computers/tests -v
```
