# routines/ — scheduled routines (Fase 2)

A routine is a small schedule (interval / daily / cron subset)
tied to a character + a pack. When it fires, the server does
exactly two honest things:

1. creates a Kanban card (origin `agent`, ref = the routine id)
   describing the work to do, and
2. writes a team-memory entry noting the routine fired.

That is the whole server-side execution. The real model work
happens through the browser / BYO path: a human (or a
browser-side model call) picks the card up. The server never
calls a model, never pretends it did, and never auto-sends,
auto-publishes, or auto-merges anything a routine produced —
those still need a human approval (see `approvals/`).

## Schedules (no new dependencies)

| kind | example | meaning |
|---|---|---|
| `interval` | `{"kind": "interval", "seconds": 3600}` | every N seconds (N ≥ 60; anything shorter is a polling loop, not a routine) |
| `daily` | `{"kind": "daily", "time": "07:00"}` | once a day at HH:MM (24h) |
| `cron` | `{"kind": "cron", "expression": "0 7 * * 1"}` | 5 fields: minute hour day-of-month month day-of-week |

Times are **UTC** — the server has no timezone configuration.
A routine created for "07:00" fires at 07:00 UTC, not 07:00 WIB.
If the owner wants WIB, the schedule must be written in UTC
(07:00 WIB = 00:00 UTC).

Cron subset — each field supports `*`, a single number, a comma
list (`9,17`), a range (`1-5`), and a step (`*/15`, `0-30/10`).
NOT supported: names (`JAN`, `MON`), `L` / `W` / `#`, seconds
fields, years. Day-of-week accepts 0–7 (both 0 and 7 mean
Sunday). When both day-of-month and day-of-week are restricted,
a run happens when EITHER matches (standard cron rule).

## Behaviour

- `next_run_after(schedule, now)` is a pure function; `tick(now)`
  runs every due + enabled routine once and advances its next run.
- A routine created mid-schedule does not fire immediately for a
  run it already missed: its first `next_run` is computed from
  the creation time.
- A **disabled routine never runs** — `tick` skips it, and a
  direct `run_routine` on one raises `ValueError`.
- `tick` never lets one broken routine stop the others: a
  failure is recorded as `last_error` on that routine, its
  `next_run` still advances, and the result list carries
  `{"id", "ok": false, "error"}` for it.

## Built-in routine: weekly pulse

`WEEKLY_PULSE_ROUTINE` (also via `create_weekly_pulse()`):

```json
{"character": "chief-officer", "pack": "weekly-pulse",
 "schedule": {"kind": "cron", "expression": "0 7 * * 1"},
 "title": "Weekly project pulse"}
```

Monday 07:00 UTC, from NEXT-LEVEL-PLAN §7 Fase 2: Kanban →
draft summary, a human sends it. It is one ordinary routine
like any other — no special code path.

## Store

JSON file `data/routines.json` (env override
`GROKBOT_ROUTINES_FILE`), atomic writes under a lock — the same
pattern as `office/kanban.py`. A corrupt file loads as empty.

Routine record:

```json
{
  "id": "routine-1",
  "character": "chief-officer",
  "pack": "weekly-pulse",
  "title": "Weekly project pulse",
  "schedule": {"kind": "cron", "expression": "0 7 * * 1"},
  "enabled": true,
  "last_run": null,
  "next_run": "2026-10-26T07:00:00+00:00",
  "last_error": null,
  "created_at": "2026-10-25T06:00:00+00:00"
}
```

## Server wiring

`tick()` must be called periodically by the server — see
`automation/server-wiring.patch.md` (hook in `main()`).
Until the coordinator wires it, routines are stored and listed
but never fire on their own: nothing in this module starts a
thread or a timer by itself.

## Tests

```bash
python3 -m unittest discover -s routines/tests -v
```

Tests use temp files only (temp routines store, temp Kanban
store, temp memory DB). The real `data/` files are never touched.
