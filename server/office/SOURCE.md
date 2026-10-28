# Source — office/ (shape attribution)

The SHAPES in this folder (duty state machine, Kanban + evidence model,
three-level health, approval-queue-as-blocked-tasks, "stale data must look
stale") are adapted from:

- **hermes-virtual-office** by JuniorXcoder — https://github.com/JuniorXcoder/hermes-virtual-office
- License: **MIT** (Copyright (c) 2026 Hermes Virtual Office Contributors)
- Studied 2026-10-25 from the repo (README + `src/lib/office/duty.ts`,
  `src/lib/office/health.ts`, `src/lib/hermes/kanban.ts`,
  `src/lib/hermes/evidence.ts`, `src/lib/hermes/approvals.ts`,
  `src/lib/office/board.ts`, `src/types/hermes.ts`).

**No code was copied.** Every module here was written from scratch in
Python for this build's architecture (stdlib only, JSON files, no CLI
backend, no Three.js, no Hermes runtime). The adaptations are deliberate
and documented in each module's docstring:

- Duty: `meeting > desk > review > idle` — the upstream `a2a` state was
  REMOVED (this build has no autonomous agent-to-agent conversation) and
  the `cron` input was dropped (a cron run cannot be attributed to one
  character).
- Kanban: 5 statuses in 4 columns instead of 9 in 4; no
  `model_override`/`provider_override` (BYO: the model is the key owner's
  choice in the browser, not a per-task pin).
- Evidence: kinds `summary | log | link | file`; the core rule is kept —
  a `done` status alone is never proof.
- Health: no cost thresholds (BYO — cost sits with the key owner) and no
  error-log thresholds (this server writes no error log); the "one waiting
  approval = warn" rule was not adopted (our drafts stop no agent).
- The 3D room, A2A meetings, and the Hermes CLI backend were NOT adopted.
