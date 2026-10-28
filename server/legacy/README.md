# Legacy bridge — Virtual Office deck (read-only)

Authorized by the owner (2026-10-24). This bridge exists for one
purpose: let the own-build app's Virtual Office deck show the
owner's CURRENT live office state (bot names/titles, thread
message counts, run statuses, timestamps) without ever touching
the old system.

## What it is

- `read_legacy.py` — a standard-library-only script that runs on
  the server machine. It resolves the legacy database container
  at runtime (live `docker ps` + the container's own environment
  + a probe for a `bots` table), then runs a fixed set of SELECT
  statements with the session forced read-only, and prints one
  JSON object. It is invoked by the app server via subprocess —
  the same transport pattern as the device bridge
  (`app/device_client.py`). No HTTP client, no outbound request.
- `../app/legacy_client.py` — the app-side client: finds the
  reader (env `GROKBOT_LEGACY_READER`, repo `legacy/`, or the
  deployed copy), runs it, and assembles bots with their thread
  and latest run. Server endpoint: `GET /api/deck`.

## What it never does

- Never writes: no statement kind other than SELECT exists in
  the reader, and every session is forced read-only at the
  database level. No container is stopped, started, restarted,
  or changed; no session row is created; no database row is
  touched.
- Never reads: message text, message blocks, bot instructions,
  secrets, credentials, session tokens, or any table holding
  them. The deck shows metadata only.
- Never names: no old brand name and no container name is stored
  in this repo. Resolution happens fresh on every run.
- Never invents: if the legacy database is absent or a feed
  switch is off, the deck says so (empty-but-live, or paused) —
  it never fabricates bots to fill the view.

## Deck switch

The deck view has a "Live legacy feed" switch (default on,
authorized). Turning it off stops all reader calls until it is
turned back on; the deck then reports the feed as paused.
