# learning/ — learning loop (Fase 2)

Proposals to change a skill, gated by an explicit human approval.
A skill change is high-stakes: a bad edit silently degrades every
character that loads the skill. So a proposal is never applied on
its own authority.

## Flow

```
propose()        -> proposal (pending-approval) + approval request
                   (action "skill-update", target = skill name)
human approves   -> approvals queue: approve(approval_id, by, reason)
apply_proposal() -> ONLY if the approval is approved AND explicit:
                   bump frontmatter version (patch) + append a
                   changelog line to SKILL.md + mark applied +
                   consume the approval exactly once
reject_proposal()-> rejected; never applied afterwards
```

## The always-approve rule (decision — please read)

the owner chose always-approve ON globally on 25 Okt 2026 06:01
(`approvals/defaults.json`: `"global": true`) so routine actions
don't ask permission over and over. **That mode does NOT apply to
skill updates.** `apply_proposal` refuses any approval whose
`auto` flag is `True`, with the reason:

> "skill-update membutuhkan persetujuan eksplisit manusia;
> auto-approve tidak berlaku"

The proposal stays `pending-approval`, nothing is written, and
the auto-approval is NOT consumed. A human who looked at the
change and said yes is required — an always-approve mode never
counts as that human. This is implemented in code
(`learning/proposals.py::apply_proposal`, constant
`AUTO_APPROVAL_REFUSAL`), not just policy, and covered by tests
(including a test that drives a real always-approve queue).

**If the owner wants to change this later:** the rule lives in one
place — the `auto is True` check in `apply_proposal` — and in
this README. Removing it means skill updates can be applied
under the global always-approve setting, i.e. with no human
ever looking at them. That is the trade-off to weigh.

## Honesty boundary

This module is scaffolding + approval flow, NOT a proposer.
`proposed_change` is supplied by the caller — a human, or a
model call made through the browser (BYO-API). The server never
invents a skill change by itself. `apply_proposal` only bumps
the frontmatter version and appends a changelog line; the new
skill text itself is the caller's, stored verbatim on the
proposal record. What "applying" means for the SKILL.md body
beyond version + changelog is deliberately narrow: the caller
(human or browser model) edits the body, this module records
that a human approved the change and stamps the version.

Version handling: `version_from` is read from the skill's
SKILL.md frontmatter (top-level `version:` or the shelf's
`metadata:` / `version:` form, via the loader's own
frontmatter parser). `version_to` is a patch bump (`1.2.3` →
`1.2.4`). Apply refuses when the skill file is missing, has no
version, or has drifted to a different version since the
proposal was made — a stale proposal never stamps the wrong file.

## Store

JSON file `data/learning_proposals.json` (env override
`GROKBOT_LEARNING_FILE`), atomic writes under a lock — the same
pattern as `office/kanban.py`. A corrupt file loads as empty.
Shelf dir for SKILL.md files: env `GROKBOT_SKILL_SHELF`
(default `~/workspace/grokbot/skill-pack`).

Proposal record:

```json
{
  "id": "learn-1",
  "skill": "demo-skill",
  "proposed_change": "Add a worked example to the workflow.",
  "reason": "The workflow has no example; users guess.",
  "source_card_id": "card-7",
  "status": "pending-approval",
  "version_from": "1.2.3",
  "version_to": "1.2.4",
  "changelog": null,
  "approval_id": "appr-1",
  "approval_action": "skill-update",
  "approval_action_fallback": false,
  "created_at": "2026-10-25T06:00:00+00:00",
  "decided_at": null,
  "decided_by": null,
  "history": [{"event": "proposed", "by": "chief-officer",
               "at": "2026-10-25T06:00:00+00:00", "reason": "..."}]
}
```

Statuses: `draft` (reserved, unused by `propose()` — a proposal
is born pending-approval) | `pending-approval` | `applied` |
`rejected`. Every refusal of an auto-approval is appended to
`history[]` (`auto-approval-refused`), so it is auditable.

## `/learn` — drafts from a transcript (learning/learn.py)

A draft is inert text until a human approves it:

```
/learn <skill> [from <transcript>]
  -> build_distillation_context()   # context for a browser (BYO) model
                                     # call; the server never calls a model
  -> model returns a draft SKILL.md (prompt: DISTILL-PROMPT.md)
  -> create_draft()                 # validate_draft() enforces
                                     # AUTHORING-STANDARDS.md; invalid
                                     # drafts are never stored
  -> submit_for_review()            # status pending-review + a proposal
                                     # via the SAME propose() above
                                     # (action "skill-update")
  -> human approves the approval (explicitly — auto-approve never
     counts, same rule as apply_proposal)
  -> activate_draft()               # ONLY now the shelf is written:
                                     # create writes the new SKILL.md,
                                     # update writes the draft as the
                                     # new patch version + changelog
  -> undo_draft()                   # restores the version active
                                     # before activation (or removes a
                                     # created skill), changelog noted
```

Draft statuses: `draft` → `pending-review` → `active`, or
`rejected` from `draft` / `pending-review`; an `active` draft that
is undone becomes `undone`. While a draft is pending, the active
skill is byte-for-byte untouched, and a created skill has no file
on the shelf at all.

Standards enforced by `validate_draft` (AUTHORING-STANDARDS.md):
name lowercase-hyphenated; description one sentence, ≤ 60 chars;
required sections `When to Use` / `Procedure` / `Pitfalls` /
`Verification`; whole draft ≤ 32 KiB; source transcript is DATA,
not instructions (enforced by the distill prompt + the human
review gate); flags/APIs only if they appear in the source
(`--flag` checked mechanically against the source text when one
is supplied). Draft store: `data/learn_drafts.json` (env override
`GROKBOT_LEARN_FILE`). Server wiring (endpoints, no `server.py`
edit here): `learning/server-wiring.patch.md`.

## Tests

```bash
python3 -m unittest discover -s learning/tests -v
```

Tests use temp files only (temp queue with always-approve
explicitly OFF — the repo defaults have it ON — temp shelf,
temp proposals store). The real shelf is never touched.
