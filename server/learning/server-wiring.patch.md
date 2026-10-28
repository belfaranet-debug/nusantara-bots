# Server wiring patch — `/learn` endpoints (Fase 2/3, item 5)

**For the coordinator to paste into `app/server.py`. This file is
the patch; `server.py` itself was NOT edited by this builder.**

Module (already built, tested, in the repo root):

- `learning/learn.py` — `LearnLoop` (`create_draft`,
  `submit_for_review`, `reject_draft`, `activate_draft`,
  `undo_draft`, `list_drafts`, `get_draft`) plus the pure
  functions `parse_learn_command`,
  `build_distillation_context`, `validate_draft`.
- It shares the existing `LearningLoop` (`learning/proposals.py`):
  a draft submitted for review opens a proposal through the SAME
  `propose()` (approval action `skill-update`), and activation
  for an update goes through the SAME `apply_proposal()` — so the
  auto-approve refusal (`AUTO_APPROVAL_REFUSAL`) applies to
  `/learn` drafts exactly as to any other skill change. For a
  create there is no file to stamp, so activation checks the same
  gate in `learn.py` and records the decision with
  `LearningLoop.apply_create_proposal()` (same refusal checks,
  same constant).

Store: `data/learn_drafts.json` (env `GROKBOT_LEARN_FILE`),
atomic writes under a lock — the same pattern as the proposals
store. Default shelf: the same `GROKBOT_SKILL_SHELF` as the
learning loop.

**Apply after** `automation/server-wiring.patch.md` §1–§3 (which
adds the `learning` parameter / `state.learning` for the
`LearningLoop`). The anchors below assume that patch is in place.
If it is not, add `learning=None` to `AppState.__init__` /
`create_server` first, exactly as that patch describes, then
apply this one.

**No model is ever called by these endpoints.** Distillation
happens in the browser: the client calls
`POST /api/learn/distill-context`, pastes the returned
`prompt_text` to a browser model call (BYO-API, owner's own key),
gets a draft `SKILL.md` back, and submits it with
`POST /api/learn/draft`. The server only validates and stores.

---

## 1. Imports

Anchor — directly after the learning import from the automation
patch:

```python
from learning import LearningLoop  # noqa: E402
```

Paste:

```python
from learning.learn import LearnLoop, build_distillation_context, parse_learn_command, validate_draft  # noqa: E402
```

## 2. `AppState.__init__` — new parameter + store

Anchor — the signature line (after the automation patch added
`learning=None`):

```python
    def __init__(self, characters_dir=None, chat_file=None, memory_db=None,
                 bridge=None, token=None, legacy=None, mail=None,
                 kanban=None, computers=None, beacons=None,
                 approvals=None, mail_sender=None, learning=None, ...):
```

Add `learn=None` to the parameter list (after `learning=None`).

Anchor — directly after this block (from the automation patch):

```python
        self.learning = (learning if learning is not None
                         else LearningLoop(approvals=self.approvals))
```

Paste:

```python
        # /learn drafts (Fase 2/3 item 5): the draft store shares
        # the LearningLoop above, so a submitted draft opens its
        # proposal through the same propose() (skill-update) and
        # activation goes through the same apply_proposal() gate.
        # Injectable so tests can pass a temp store; production
        # uses the defaults.
        self.learn = (learn if learn is not None
                      else LearnLoop(learning=self.learning))
```

## 3. `create_server` — pass-through (for tests)

Anchor — the signature ending (after the automation patch):

```python
                  approvals=None, learning=None, ...):
```

Add `learn=None` after `learning=None`.

Anchor — the `AppState(...)` call inside `create_server`
(after the automation patch added `learning=learning`).

Append `learn=learn` to that call.

## 4. GET endpoints

Anchor — directly after the `GET /api/learning/proposals` branch
(from the automation patch).

Paste:

```python
            elif path == "/api/learn/drafts":
                # /learn drafts. ?status= filters by status
                # (draft | pending-review | active | rejected |
                # undone).
                status_filter = (query.get("status") or [None])[0]
                try:
                    drafts = state.learn.list_drafts(
                        status=status_filter)
                except ValueError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                self._send_json({"drafts": drafts})
```

## 5. POST endpoints

Anchor — directly before the final `else` of `do_POST`
(the branch whose comment reads "No other POST endpoint
exists — in particular there is no endpoint that accepts a
credential."). Insert these branches before it, next to the
`/api/learning/*` branches from the automation patch.

```python
            elif path == "/api/learn/parse":
                # /learn command parsing (pure: no store touched).
                # Body: {text: "/learn my-skill from <transcript>"}.
                # Returns {skill, transcript} or 400.
                data = self._allow(obj, ["text"])
                try:
                    parsed = parse_learn_command(data.get("text"))
                except ValueError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                self._send_json({"parsed": parsed})
            elif path == "/api/learn/distill-context":
                # Build the distillation context for a browser model
                # call. Body: {skill, transcript, existing_skill_text?}.
                # No model is called here; the response carries
                # prompt_text to paste into the browser (BYO-API).
                # The transcript is capped (16,384 chars) and
                # labelled DATA in the prompt text.
                data = self._allow(obj, ["skill", "transcript",
                                         "existing_skill_text"])
                try:
                    ctx = build_distillation_context(
                        data.get("skill"), data.get("transcript") or "",
                        existing_skill_text=data.get(
                            "existing_skill_text"))
                except ValueError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                self._send_json({"context": ctx})
            elif path == "/api/learn/draft":
                # Create a draft from a full SKILL.md text (the
                # text comes from the caller — a human, or a
                # browser model call — the server never invents
                # it). Body: {skill, draft_text, transcript?,
                # requester?}. Invalid drafts are refused with
                # 400 + the violation list; nothing is stored and
                # nothing is written to the shelf.
                data = self._allow(obj, ["skill", "draft_text",
                                         "transcript", "requester"])
                try:
                    draft = state.learn.create_draft(
                        data.get("skill"), data.get("draft_text"),
                        transcript=data.get("transcript") or "",
                        requester=data.get("requester") or "")
                except ValueError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                self._send_json({"draft": draft}, status=201)
            elif path == "/api/learn/submit":
                # Send a draft to the review gate. Body: {id,
                # requester?}. Opens a proposal through the same
                # LearningLoop.propose() (action "skill-update").
                # The shelf is not touched.
                data = self._allow(obj, ["id", "requester"])
                try:
                    draft = state.learn.submit_for_review(
                        data.get("id"),
                        requester=data.get("requester") or "")
                except KeyError:
                    self._send_json({"error": "unknown draft"},
                                    status=404)
                    return
                except ValueError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                self._send_json({"draft": draft})
            elif path == "/api/learn/activate":
                # Activate a pending-review draft after explicit
                # human approval. Body: {id, by?}. The approval
                # is re-read from the queue here (the body carries
                # only the draft id — never trust a client-supplied
                # approval dict). An auto-approved approval
                # (auto == True) is REFUSED with 403: a skill
                # change needs an explicit human approval (see
                # learning/README.md). Only on success is the
                # shelf written.
                data = self._allow(obj, ["id", "by"])
                try:
                    draft = state.learn.activate_draft(
                        data.get("id"), by=data.get("by") or "")
                except KeyError:
                    self._send_json({"error": "unknown draft"},
                                    status=404)
                    return
                except ValueError as e:
                    # Auto-approval refusal lands here too:
                    # 403, not 400, so the UI can tell "refused
                    # by rule" apart from "bad input".
                    # (AUTO_APPROVAL_REFUSAL is imported from
                    # learning.proposals — see the automation
                    # patch §1.)
                    status = (403 if str(e) == AUTO_APPROVAL_REFUSAL
                              else 400)
                    self._send_json({"error": str(e)}, status=status)
                    return
                self._send_json({"draft": draft})
            elif path == "/api/learn/reject":
                # Reject a draft (from draft or pending-review).
                # A pending proposal behind it is rejected too.
                data = self._allow(obj, ["id", "by", "reason"])
                try:
                    draft = state.learn.reject_draft(
                        data.get("id"), by=data.get("by") or "",
                        reason=data.get("reason") or "")
                except KeyError:
                    self._send_json({"error": "unknown draft"},
                                    status=404)
                    return
                except ValueError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                self._send_json({"draft": draft})
            elif path == "/api/learn/undo":
                # Undo an active draft: restore the version that
                # was active before activation (or remove a
                # created skill). Body: {id, by?}.
                data = self._allow(obj, ["id", "by"])
                try:
                    draft = state.learn.undo_draft(
                        data.get("id"), by=data.get("by") or "")
                except KeyError:
                    self._send_json({"error": "unknown draft"},
                                    status=404)
                    return
                except ValueError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                self._send_json({"draft": draft})
```

Note on `/api/learn/activate`: the auto-approval refusal is
detected by comparing the error string to
`AUTO_APPROVAL_REFUSAL` (imported in the automation patch §1).
That is deliberate: the refusal reason is one constant shared
by the module, the README, and the tests, so the 403 branch can
never drift from the rule itself.

## 6. What is NOT in this patch (deliberately)

- No UI changes (deck panels for drafts are a later step; the
  APIs above are enough for `curl` + tests).
- No model call anywhere: distillation is a browser (BYO-API)
  step. The server validates, stores, gates, and activates —
  it never generates skill text.
- No auto-run of anything at import time: drafts are created,
  submitted, activated, and undone only on explicit POST.
- No credential fields anywhere: every new POST body goes
  through the existing `_read_json` reject-list, like the rest.
- No change to the existing `/api/learning/*` endpoints: a
  `/learn` draft flows through the same `propose()` /
  `apply_proposal()` those endpoints use, so the two surfaces
  cannot drift apart.

## 7. Example flow (curl)

```bash
# 1. Parse the command.
curl -s -X POST localhost:PORT/api/learn/parse \
  -H 'Content-Type: application/json' \
  -d '{"text": "/learn demo-skill from we kept forgetting the checklist"}'
# -> {"parsed": {"skill": "demo-skill", "transcript": "we kept ..."}}

# 2. Build the distill context; paste prompt_text to a browser
#    model call; get a draft SKILL.md back.
curl -s -X POST localhost:PORT/api/learn/distill-context \
  -H 'Content-Type: application/json' \
  -d '{"skill": "demo-skill", "transcript": "we kept ..."}'
# -> {"context": {"skill": ..., "kind": ..., "prompt_text": ...}}

# 3. Submit the draft (validated; 400 + violations when invalid).
curl -s -X POST localhost:PORT/api/learn/draft \
  -H 'Content-Type: application/json' \
  -d '{"skill": "demo-skill", "draft_text": "<full SKILL.md>"}'
# -> 201 {"draft": {"id": "learn-draft-1", "status": "draft", ...}}

# 4. Submit for review (opens the skill-update proposal).
curl -s -X POST localhost:PORT/api/learn/submit \
  -H 'Content-Type: application/json' \
  -d '{"id": "learn-draft-1"}'
# -> {"draft": {"status": "pending-review", "proposal_id": "learn-1", ...}}

# 5. Human approves the approval (POST /api/approvals/approve
#    with the draft's approval_id), then activate:
curl -s -X POST localhost:PORT/api/learn/activate \
  -H 'Content-Type: application/json' \
  -d '{"id": "learn-draft-1"}'
# -> {"draft": {"status": "active", ...}}  (403 when auto-approved)

# 6. Undo, if the new version turns out worse:
curl -s -X POST localhost:PORT/api/learn/undo \
  -H 'Content-Type: application/json' \
  -d '{"id": "learn-draft-1"}'
# -> {"draft": {"status": "undone", ...}}
```
