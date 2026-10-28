# costs/ — cost ledger / spend cap per bot (FinOps-lite)

Internal ledger of provider usage per task. The server NEVER
sees a credential: the browser reads the `usage` object from the
provider's own response (the call the browser already made with
its BYO key, Bearer ONLY to the provider URL — same invariant
as `app/static/voice.js`) and POSTs just the numbers + the model
name to `/api/costs/record`. No billing, no invoicing, no
external service: this is an internal ledger only.

## Files

| File | What it does |
|---|---|
| `ledger.py` | `CostLedger` — per-task records, daily aggregates per bot, cap gate (`check_cap`). Store JSON `data/costs.json`, atomic writes under a lock, same pattern as `office/kanban.py` / `approvals/queue.py`. |
| `prices.py` | Configurable price table (USD per 1M tokens) + `estimate_cost`. Every cost is an ESTIMATE; unknown model → cost 0 + `price_known: false`. |
| `config.py` | `CostConfig` — daily caps from env / settings files. Default: UNLIMITED until the owner sets one. |
| `defaults.json` | Shipped defaults (price table + empty caps). the owner edits the RUNTIME file, never this one. |
| `server-wiring.patch.md` | Endpoints + client snippet for the coordinator to paste into `app/server.py` / `app/static/app.js`. |
| `tests/test_costs.py` | unittest, temp-dir stores, no network. |

## Record shape (one per task)

```json
{
  "id": "cost-1",
  "bot": "yt-scriptwriter",
  "task_id": "card-12",
  "task_kind": "chat",
  "model": "gpt-4o-mini",
  "prompt_tokens": 1200,
  "completion_tokens": 300,
  "total_tokens": 1500,
  "cost_usd": 0.00036,
  "price_known": true,
  "day": "2026-10-25",
  "recorded_at": "2026-10-25T06:40:00+00:00"
}
```

`day` is the UTC date of `recorded_at` — the cap and the daily
summary aggregate on UTC days.

## Cap — default unlimited

A cap is a daily USD limit on the ledger's *estimated* cost for
one bot. When today's spend reaches it, `check_cap(bot)` returns
`allowed: false` and the task-start path refuses the new task
with **"cap harian tercapai"**. Recording itself never refuses:
a record is a fact about spend that already happened (the
provider already charged it) — the gate is pre-flight, before
starting a task.

How the owner sets a cap (strongest first):

1. **Settings endpoint** (after wiring): `POST /api/costs/caps`
   `{"bot": "yt-scriptwriter", "cap_usd": 2.00}` — writes the
   runtime file `data/cost_settings.json`. `cap_usd: null`
   clears it (back to unlimited / global).
2. **Runtime file** directly: `data/cost_settings.json`
   `{"caps": {"yt-scriptwriter": 2.00}, "global_cap_usd": null}`.
3. **Env vars** (restart the server after changing):
   `GROKBOT_DAILY_CAP_USD=5` (global, all bots) or
   `GROKBOT_DAILY_CAP_USD_YT_SCRIPTWRITER=2` (one bot — bot id
   upper-cased, non-alphanumeric → `_`).

Per-bot beats global. A cap must be a number > 0; zero,
negative or non-numeric values mean "no cap" — a typo can never
lock a bot out by accident. (To freeze a bot on purpose, set a
tiny cap such as 0.01.)

## Prices — estimates, configurable

`prices.py` ships list prices for common OpenAI-compatible
models (USD per 1M tokens, input/output split). the owner overrides
them in `data/cost_settings.json` (`"prices": {"model-x":
{"input_per_1m": 1.0, "output_per_1m": 3.0}}`) or via env
`GROKBOT_COST_PRICES_JSON` (a JSON object). Dated variants
(`gpt-4o-mini-2024-07-18`) price like their base model. A model
with no price: tokens are still recorded, `cost_usd` is 0 and
`price_known` is `false` — the deck shows "harga tak diketahui"
instead of a guessed number.

## Deck

`GET /api/costs/summary?bot=&day=` returns per-bot aggregates
for the day (prompt/completion/total tokens, estimated cost,
task count, tasks with unknown price), totals, and the single
most expensive task. The deck renders "spend hari ini per bot,
total, tugas termahal" from it — see the snippet in
`server-wiring.patch.md` §5.

## Tests

```bash
python3 -m unittest discover -s costs/tests -v
```
