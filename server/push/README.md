# push/ — FCM push notifications (Phase 2, h)

Server-side plumbing for Firebase Cloud Messaging. The deck
today polls every 20 seconds; push is the honest upgrade: the
server tells the browser (via Google's FCM) when an approval is
waiting or a task finishes, instead of the browser asking.

**Status: plumbing only — nothing can be sent yet.** Sending
needs Firebase credentials from the owner (see `PUSH-SETUP.md` at the
repo root). Until then every send fails cleanly with
"kredensial FCM belum dikonfigurasi" and nothing is delivered
silently. There is no fake "sent" anywhere in this package.

## Files

| File | What it does |
|---|---|
| `tokens.py` | `PushTokenRegistry` — token per character/device in `data/push_tokens.json` (atomic + lock, same pattern as `office/kanban.py`). `register` / `unregister` / `list_tokens` / `public_list` (metadata only, no token values) / `token_values`. |
| `send.py` | `FcmSender` — FCM HTTP v1 over urllib. Service-account JSON → RS256 JWT → OAuth2 access token (cached in memory) → `POST /v1/projects/<project_id>/messages`. `PushNotConfigured` when credentials are missing; `PushError` on any transport/FCM failure. |
| `hooks.py` | `notify_approval_requested(approval)` and `notify_task_done(card)` — build the payload (pure functions `approval_payload` / `task_done_payload`) and fan out to every registered token. Honest no-ops: `{"sent": 0, "skipped_reason": ...}` when there are no tokens or no credentials. Never raises into the caller. |
| `tests/test_push.py` | unittest, no network, no real credentials. |

## Env vars

| Var | Meaning |
|---|---|
| `GROKBOT_FCM_SERVICE_ACCOUNT` | Path to the Firebase service-account JSON (project_id, private_key, client_email). Server-side only; never committed, never shipped to the browser. |
| `GROKBOT_PUSH_TOKENS_FILE` | Override for `data/push_tokens.json` (tests use a temp dir). |

## Events wired (by the coordinator — see `server-wiring.patch.md`)

1. **Approval requested** — after `POST /api/approvals/request`
   succeeds, `notify_approval_requested(record)` runs best-effort.
   Title "Persetujuan diminta", body "`<action>` → `<target>`".
2. **Task done** — after `POST /api/kanban/move` lands a card on
   `done`, `notify_task_done(card)` runs best-effort. Title
   "Tugas selesai", body = card title.

Both hooks are best-effort: a push failure never fails the
approval request or the card move.

## What the client does (see `PUSH-CLIENT.md`)

Browser: service worker `app/static/firebase-messaging-sw.js`
(Firebase JS SDK pinned via CDN, background messages only, no
keys inside) + registration snippet (permission → `getToken`
with the VAPID key → `POST /api/push/register`). The deck files
themselves are untouched by this package — the coordinator wires
the snippet in.

## Tests

```bash
python3 -m unittest discover -s push/tests -v
```
