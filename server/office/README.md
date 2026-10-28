# office/ — Kanban + evidence, duty, health, presence beacon

Office pieces for the own build. Pure Python stdlib, all with tests.

## Modules

| File | What it is |
|---|---|
| `kanban.py` | `KanbanStore` — cards + evidence in `data/kanban.json` (atomic writes under lock, same pattern as `app/chat_store.py`). Statuses todo/doing/review/blocked/done; 4 display columns; `verdict(card)` = open / proven / unproven. |
| `duty.py` | `duty_of(...)` — meeting > desk > review > idle. No `a2a` state (no autonomous agent-to-agent chat in this build). `CHAT_LIVE_MS = 120_000`. |
| `health.py` | `assess_health(checks, ...)` — level ok/warn/bad + reasons + per-check detail + uptime. Core check failing = bad; optional failing = warn. No cost thresholds (BYO). |
| `presence.py` | `BeaconLog` — latest beacon per character (active/idle/stopped) + age + staleness (>120 s). In-memory. |
| `circuit_breaker.py` | `CircuitBreaker` — meeting-room post guard above the 40-message budget: duplicate 60 s, 3+ bot reply ring → 5-min cooldown, 10 posts/min per bot, escalation after 2 unanswered bot posts. Refused posts are never recorded. Pure, in-memory, injectable clock. |

## Rules

- **Done alone is never proof.** A done card with no evidence is `unproven`.
- **Stale data looks stale.** Beacons older than 120 s, deck ages, and health
  reasons always show their age/failure — never as empty or live.
- **No autonomous debate.** Duty has no `a2a`; meeting duty means a character
  is in a live meeting-room turn with budget left, nothing more.

## Tests

```bash
python3 -m unittest discover -s office/tests -v
```

Full spec: `NEXT-LEVEL-PLAN.md` §5. Shape attribution: `SOURCE.md`.
