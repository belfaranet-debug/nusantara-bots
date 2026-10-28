# approvals/ — human approval queue (Phase 2)

The gate between "an agent wants to do something external" and
"it actually happens". The six default actions that always need
approval are the ones in `review.required_before` (every pack
manifest must list them): `send`, `publish`, `merge`, `deploy`,
`delete`, `change-production-data`.

## Actions are an open set

`request_approval` accepts **any non-empty action string** —
there is no closed enum. Other modules gate their own actions
through the same queue (`skill-update`, `vault-release`,
`template-import`, `routine-run`, ...). The queue does not judge
whether an action *should* need approval; it only records the
request, the decision, and the single use.

## Model

Store: JSON file `data/approvals.json` (env override
`GROKBOT_APPROVALS_FILE`), atomic writes under a lock — the same
pattern as `office/kanban.py` and `computers/registry.py`. A
corrupt file loads as empty.

Record:

```json
{
  "id": "appr-1",
  "action": "send",
  "target": "someone@example.invalid",
  "detail": "Weekly pulse draft",
  "requester": "chief-officer",
  "status": "pending",
  "created_at": "2026-10-25T06:00:00+00:00",
  "decided_at": null,
  "decided_by": null,
  "reason": null,
  "rule": "ask",
  "expires_at": "2026-10-26T06:00:00+00:00",
  "consumed": false,
  "auto": false,
  "history": [{"event": "requested", "by": "chief-officer",
               "at": "2026-10-25T06:00:00+00:00", "reason": null,
               "auto": false, "rule": "ask"}]
}
```

## Rules

- **One approval, one action.** `consume()` flips `consumed` to
  true; a second consume is refused. A send (or any gated action)
  must check, in order: record exists → status is `approved` →
  action matches → target matches → not expired → not consumed.
- **Expiry is enforced on read.** Any read or decision first
  sweeps pending AND approved-but-unconsumed records past
  `expires_at` to `expired`, so a dead approval can never
  authorize anything — approval is not a blank cheque.
- **Every decision is audited.** `history[]` records requested /
  approved / denied / expired / consumed with who, when, and why.
- **Stale looks stale.** The queue view flags pending items older
  than 5 minutes (`STALE_AFTER_SECONDS = 300`); `/api/health`
  warns when any pending item is older than 1 hour
  (`HEALTH_WARN_AFTER_SECONDS = 3600`).

## Approval levels (Phase 2/3 — replaces the binary switch)

The owner chose always-approve ON globally to avoid repeated permission prompts. The old binary always-approve switch is now a
**level** — `ask` / `edits` / `auto` / `full` / `custom`:

| Level | Auto-approves |
|---|---|
| `ask` | nothing — every action asks a human |
| `edits` | file-edit actions only (`edit`, `edit-file`, `write-file`, `patch-file`, `create-file`, `update-file`) |
| `auto` | the safe (read-only) actions (`read`, `read-file`, `web-search`, `search`, `list`, `list-files`, `get-status`, `fetch`) **plus** the file-edit set |
| `full` | everything — behaves exactly like the old always-approve ON |
| `custom` | nothing by itself — per-bot / per-action rules decide, else `ask` |

Levels are cumulative (each level permits everything the one
below it permits). Resolution, strongest first: **per-bot beats
per-action, which beats the global mode; nothing set anywhere
means `ask`.** An unknown level value — in a file, anywhere —
**fails closed to `ask`**, never to something more permissive.

`alwaysAllow` is a command allowlist (shipped: `read-file`,
`web-search`): an action on it is auto-approved **at any
level**. A request that auto-approves comes back already
`approved`, with `auto: true`, `decided_by: "auto"`,
`reason`/`rule` naming the deciding rule, `decided_at =
created_at`, and the history still records the auto-decision.
**This never bypasses the gate:** `POST /api/mail/send` still
needs the approval id — the request just arrives approved.

### Decision log — "which rule"

Every decision records its rule, on the record (`rule`) and in
every `history[]` entry (`rule`):

| Rule | Meaning |
|---|---|
| `full-access` | level `full` auto-approved |
| `command-allowlist` | the action is on `alwaysAllow` |
| `safe-action` | level `auto` + a safe action |
| `file-edit` | level `edits` (or `auto`) + a file-edit action |
| `ask` | the level blocked auto-approval — the record stays pending |
| `human-decision` | a human approved / denied |
| `expired` | the sweep expired the record |

### Delegation — a level flows down, capped by the engine

When Chief delegates to a teammate, the teammate works at
`min(inherited level, engine cap)` — `engineCaps` in settings
holds the cap per bot (`resolve_level(..., inherited_level=...)`
in `queue.py`). A teammate capped at `edits` never auto-approves
a `send`, even when Chief runs at `full`.

**A bot cannot raise its own level.** Levels change only
through `update_settings` — the human settings endpoint
(`POST /api/approvals/settings`, driven by the deck level
picker). `request_approval` takes no level at all, and the
request endpoint's field allowlist drops any level field a bot
tries to smuggle in (`inherited_level` is an internal parameter
for delegation code only, never read from HTTP).

Settings live in two files:

| File | Role |
|---|---|
| `approvals/defaults.json` (shipped) | Instance defaults — mode `full` (the owner's explicit choice), `alwaysAllow: ["read-file", "web-search"]`. Contains a `_comment` explaining why. |
| `data/approval_settings.json` (runtime, env `GROKBOT_APPROVAL_SETTINGS_FILE`) | Overrides the defaults; a key set here wins, anything absent falls back to the defaults. Written by `POST /api/approvals/settings`. |

The CODE default (neither file present) is `ask` — safe by
default; the shipped defaults file is what turns it to `full`
for this instance.

Legacy boolean values are still read and migrated: a stored
`global: true/false` becomes mode `full`/`ask`, and `true/false`
inside `bots`/`actions`/`engineCaps` becomes `full`/`ask`.
`POST /api/approvals/settings` also still accepts a legacy
`{"global": true}` body as a shim.

### Settings endpoint contract (used by the deck level picker)

```
GET  /api/approvals/settings
→ {"mode": "ask", "bots": {}, "actions": {}, "alwaysAllow": [], "engineCaps": {}}

POST /api/approvals/settings
body {"mode"?: level, "bots"?: {bot: level}, "actions"?: {action: level},
      "alwaysAllow"?: [action, ...], "engineCaps"?: {bot: level}}
→ the effective settings after the update (only the given keys change)
```

`bots` keys are character stems (the `requester` of the approval
request); `actions` keys are action strings; `engineCaps` keys
are bot ids and values are concrete levels (`ask`/`edits`/
`auto`/`full` — never `custom`). Values must be level strings
(`ask`, `edits`, `auto`, `full`, `custom`); an unknown level is
rejected with 400, and a level stored in a file that is unknown
fails closed to `ask` on read.

## API (see `app/server.py`)

| Endpoint | What it does |
|---|---|
| `POST /api/approvals/request` | `{action, target, detail?, requester?, ttl_seconds?}` → 201 + record (already `approved` when always-approve applies) |
| `GET /api/approvals` (+ `?status=pending`) | list + `age_seconds` + `stale` per item |
| `POST /api/approvals/approve` | `{id, by?, reason?}` |
| `POST /api/approvals/deny` | `{id, by?, reason?}` |
| `GET /api/approvals/settings` | effective always-approve settings |
| `POST /api/approvals/settings` | partial settings update → effective settings |
| `POST /api/mail/send` | gated send via the bridge `mail_send` tool — see `docs/mail-send-bridge-spec.md` (no SMTP anywhere in this repo) |

All of these reject bodies containing credential fields, like
every other endpoint (the server-wide `_read_json` check).

## Tests

```bash
python3 -m unittest discover -s approvals/tests -v
python3 -m unittest discover -s app/tests -v   # includes test_approvals.py
```
