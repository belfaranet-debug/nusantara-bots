# App Shell — Nusantara Bots (Phase 3)

Minimal but runnable web app, zero-dependency (Python standard
library `http.server` only), localhost only. Built on Phase 1
(memory) and Phase 2 (loader) exactly as specified — nothing in
those phases was rebuilt or modified.

## Run

From `server/` (repo root):

```bash
python3 app/server.py
# -> http://127.0.0.1:8780/  (localhost only)
#    GROKBOT_APP_PORT=8899 python3 app/server.py   # other port
```

## Tests

From `server/` (repo root):

```bash
python3 -m unittest app.tests.test_app -v
```

Tests use temp character dirs, temp chat files, and temp memory
DBs, start a real server on an OS-assigned localhost port, and
make no real model API calls and use no real credentials (the
sentinel string in the tests is a fake value used only to prove
rejection/non-storage).

## Files

| File | What it is |
|---|---|
| `app/server.py` | HTTP server + JSON APIs + security guard (loopback by default; LAN needs a token) |
| `app/auth.py` | Token resolution + fail-closed bind check + header comparison (no network, no logging) |
| `app/chat_store.py` | Chat persistence (own JSON file, never `data/memory.db`) |
| `app/mock_model.py` | Clearly-labelled MOCK adapter (demo text, no API call) |
| `app/static/index.html` | The one chat page |
| `app/static/app.js` | Browser code; BYO-API lives here only |
| `app/static/style.css` | Page styling |
| `app/tests/test_app.py` | Test suite (see below) |
| `app/tests/test_auth.py` | Token auth tests: resolution, fail-closed bind, header gate on every /api/* route |
| `data/chat.json` | Chat history, created on first run (separate from `data/memory.db`) |

## What the page has

- **Character list** — loaded every request via the Phase 2 loader
  from exactly `server/characters/`. Only
  `status == "valid"` characters are selectable; invalid files are
  shown with their error codes, never silently dropped.
- **Rooms** — a room is a set of characters. `general` (chat) and
  `meeting-room` (meeting) are created on first run.
- **Meeting-room mode** — live UI: message budget display (40, per
  the structured meeting protocol), turn order, next-speaker label,
  and a decision / disputed-points area. Decisions are recorded in
  the room and also saved to Phase 1 team memory. Honest label on
  the page: the protocol UI is live, but replies are MOCK (or the
  user's BYO model) — no autonomous multi-agent debate runs yet.
- **Message input + presence** — per PRESENCE.md: typing indicator,
  one-line action status, turn markers, handoff steps, completion
  notes, and a visible STOP (one in-flight status report, then
  silence until the next message).
- **Memory panel** — save a fact to the selected character's
  declared memory scope(s) via Phase 1, and list what it remembers.

## Character memory scopes (Phase 1 wiring)

The character's file stem is the private-scope id (display names
may contain spaces, which are not valid scope characters):

- `private` → `private:<file-stem>`
- `team` → `team`
- `private+team` → both scopes (a save writes both; a list merges both)

## BYO-API security invariant — how it is enforced and verified

1. **Browser only.** The base URL, model, and credential are stored
   in the browser's `localStorage` (`grokbot_byo`) by
   `static/app.js` and nowhere else.
2. **Direct call.** When BYO-API is configured, the browser fetches
   `<baseUrl>/chat/completions` directly. It then persists only the
   finished messages to the local server via `POST /api/messages`
   with a body of exactly `{room, character, text, role}`.
3. **No server endpoint accepts a credential.** The server's POST
   bodies are allow-listed to chat text, room names, decisions, and
   memory text. Any POST whose top-level JSON fields include a
   credential field (`api_key`, `apikey`, `api-key`, `secret`,
   `token`, `authorization`, `password`, `access_key`) is rejected
   with HTTP 400 before any persistence. Unknown POST paths are 404.
4. **No logging, no forwarding.** Server logs record method + path
   only, never a body. The server imports no HTTP client and makes
   no outbound request, so it cannot forward a credential.
5. **Tests prove it** (`test_server_never_sees_a_credential`):
   credential-field POSTs → 400; unknown credential endpoints →
   404; the sentinel value appears in neither `chat.json` nor the
   memory DB; server-side code contains no HTTP client import.
   The reject-list in `server.py` is the enforcement mechanism —
   the values are never read, used, or forwarded.

If no BYO-API is configured, the app uses the MOCK adapter:
server-side demo text labelled `MOCK model — demo reply, no real
API call`, never a real API call.

## Ambition steer — where this beats the reference app, honestly

- **BYO-API:** any OpenAI-compatible model API, never locked to one
  provider's backend. The reference app's brain is its provider's.
- **Ownable/sellable:** self-hosted, own server, own data files
  (`data/chat.json`, `data/memory.db`, `characters/*.md`).
- **Characters that really work:** live presence + real memory
  with zero providers (Phase 1 floor guarantee).
- **Crew rooms:** structured meeting protocol UI (budget, turns,
  decisions/disputed points).
- **Where the real app still leads:** voice. Not claimed, not
  faked — noted on the page itself and out of scope for this phase.

## Devices view (slices 2-4, character-centered)

- **Two panes, side by side**: phone mirror shot (left) and
  browser display shot on :100 (right), fetched fresh per
  request from GET /api/devices/shot — never stored, no-store
  headers. On a machine without the bridge/devices the API
  honestly reports unavailability (shot → 503), never a fake
  image.
- **Character-centered, not a remote-control panel**: the view
  shows which character operates which device right now
  (operating map) and who last looked at both screens (viewing
  map). Every device action made through the app is attributed
  to its character (name + role) in the presence feed; the
  bridge's raw log (direct CLI use, no character) is shown
  separately, explicitly labelled unattributed.
- **Agent sees both screens**: POST /api/devices/look attaches
  fresh shots of both panes to a character's own turn (record
  holds shot metadata only). MOCK path: POST /api/chat with
  `vision: true` adds a visible presence event to the mock reply
  and records the turn. BYO path: press Look, then send — the
  browser sends the shots browser → model API directly, and
  only because you pressed Look first.
- **Device actions**: POST /api/devices/action is the one
  attributed door for phone actions (tap/swipe/keyevent/text/
  launch, bridge guards in force) and browser actions (key/
  click/mousemove/type via xdotool on :100). Text details are
  recorded as a length class only, never the text itself.
- **Tests**: `python3 -m unittest app.tests.test_devices -v`
  (13 tests: validators, subprocess transport + temp cleanup,
  server endpoints, attribution, vision flag, feed labelling,
  no-HTTP-client invariant).

## Access token (Phase 5 upgrade — opening the server from a phone)

Loopback stays open with no token (the safe default). A LAN bind
is refused at startup unless a token is configured — fail closed,
before any socket opens:

```bash
# On the server machine only (token file, chmod 600, outside the repo):
python3 -c "import secrets; open('/path/to/token','w').write(secrets.token_hex(32))"
chmod 600 /path/to/token
# Start LAN-bound with the token:
GROKBOT_APP_HOST=0.0.0.0 GROKBOT_APP_TOKEN_FILE=/path/to/token python3 app/server.py
```

Rules: the token is compared (constant-time) against the
`X-Grokbot-Token` header on every `/api/*` request — 401 without
it, 200 with it. It is never printed, logged, stored, or put in
a JSON body (a body field named `token` is rejected outright).
The static page (`/`) stays public — it is only a UI shell; all
data flows through `/api/*`. The browser keeps its copy in
localStorage (`grokbot_auth`), entered once in the Access token
panel; phone screenshots ride blob fetches with the header (an
`<img>` tag cannot send headers, so shots never travel
unauthenticated). Tests: `python3 -m unittest app.tests.test_auth -v`
(7 tests).

## Virtual Office deck (live, read-only legacy feed)

- **DECK button, top of the page**: clicking it opens the live
  Virtual Office view — the owner's current office work, not a
  demo. It polls while visible (20 s) and stops when hidden.
- **AUTO button, next to DECK**: one tap opens the live deck,
  forces the legacy feed on, and keeps deck data AND device
  shots auto-refreshing every 20 s — no manual refresh needed.
  A second tap stops auto-refresh.
- **Look (dark industrial + amber)**: header with LIVE badge,
  sidebar (system, devices, summary counts), a room panel with
  one CSS avatar (initials) per live bot plus a status tag from
  its REAL latest run (JALAN / ANTRE / SELESAI / GAGAL / BATAL /
  TANPA RUN), filter buttons (SEMUA + one per bot), and a
  per-agent live status text section. The look recreates a
  reference design language in code — no reference artwork,
  names, channels, IDs, or numbers are used; every number and
  name on the deck comes from a live source.
- **Unsaved-token state is honest**: before the server token is
  saved in the browser, API calls answer 401 and the page says
  so ("save the server token above") instead of rendering
  empty lists as "No characters yet" or "Bridge unavailable".
  That misleading empty-state rendering was the cause of two
  phone-screenshot reports; the server-side bridge path and the
  character files were verified fine at the time.
- **What it shows, all live**: legacy office state (bot names
  and titles, thread message counts, run statuses, timestamps),
  devices panes + operating map, presence feed, own-build team
  memory count, and own-build characters.
- **Read-only by construction**: the legacy panel is fed by
  `legacy/read_legacy.py` via subprocess (`app/legacy_client.py`,
  `GET /api/deck`). The reader runs fixed SELECT statements
  with the session forced read-only, resolves the database
  container fresh on every run (never a stored name), and never
  reads message text, instructions, secrets, or credentials. The
  old system is never written to, stopped, or changed from here.
  A "Live legacy feed" switch (default on, authorized
  2026-10-24) pauses all reader calls when off; an unavailable
  or empty office is shown as unavailable or empty-but-live —
  never filled with invented bots.
- **Tests**: `python3 -m unittest app.tests.test_deck -v`
  (server endpoint shape, feed switch pause/resume, honest
  failure, reader read-only + brand-free checks, transport
  parsing, visible DECK button).

## Limits of this phase (not hidden)

- Replies are MOCK unless the user configures BYO-API in the browser.
- Loopback needs no token by design; a LAN bind without a token
  is refused — never expose the port without one.
- No Android app yet — this phase is the web shell; the Android
  shell is a later phase.
