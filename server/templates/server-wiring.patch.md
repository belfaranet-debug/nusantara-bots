# Server wiring patch — template review + install (Fase 2, item 4)

**For the coordinator to paste into `app/server.py`. This file is
the patch; `server.py` itself was NOT edited by this builder.**

Modules (already built, tested, in the repo root):

- `templates/importer.py` — `TemplateReviewStore`
  (`create`, `get`, `take`, `release`), `review_template_package`,
  `install_reviewed_template`, `validate_package_bytes`,
  `TemplateImportError`, `TemplateInstallError`
- `templates/resolve.py` — `parse_template_link`,
  `resolve_template`, `TemplateResolveError`
- `packs/pack.py` — `MAX_PACKAGE_BYTES` (4 MB),
  `MAX_SKILLS_PER_PACKAGE` (60)
- `routines/` — `RoutineStore` (already on AppState per
  `automation/server-wiring.patch.md` as `state.routines`; the
  install step creates the template's routines there, paused)

Security shape of the two endpoints below:

* **Review first, install second.** `POST /api/templates/review`
  validates a package and returns a review payload (what will be
  installed, which fields were stripped, warnings). It installs
  NOTHING. `POST /api/templates/install` installs ONLY with a
  valid `review_id` from that review — no review, no install.
  Reviews are single-use and expire after 30 minutes.
* **The 4 MB package cap vs the 64 KB body cap.** The server's
  generic `_read_json` refuses bodies over `MAX_BODY` (64 KB).
  An inline template package may be up to 4 MB, so the review
  endpoint uses its own reader (`_read_template_body`, §5) that
  reads up to `MAX_PACKAGE_BYTES` and answers 413 above it.
  The generic reader is untouched; nothing else gets a bigger
  body limit.
* **No credential ever reaches these endpoints.** The review
  reader runs the same top-level `REJECTED_FIELDS` check as
  `_read_json`, and the importer rejects forbidden fields and
  secret-like values with redacted messages. The handlers never
  log a request body.
* **Install is inert-by-default.** Routines are created with
  `enabled=False` (paused) and connections are recorded with
  `enabled: false` (disabled) — see `install_reviewed_template`.

---

## 1. Imports

Anchor — directly after this line in `app/server.py`:

```python
from office.presence import BeaconLog, STALE_AFTER_SECONDS  # noqa: E402
```

Paste:

```python
from packs.pack import MAX_PACKAGE_BYTES  # noqa: E402
from routines import RoutineStore  # noqa: E402  (only if not already imported by the automation patch — do not import twice)
from templates import (  # noqa: E402
    TemplateInstallError,
    TemplateImportError,
    TemplateResolveError,
    TemplateReviewStore,
    install_reviewed_template,
    resolve_template,
    review_template_package,
    validate_package_bytes,
)
```

Note: `RoutineStore` is already imported by
`automation/server-wiring.patch.md` §1 (`from routines import
RoutineStore`). If that patch is applied, skip the duplicate
import line here.

## 2. Shelf directory

Anchor — directly after this line:

```python
MAX_BODY = 64 * 1024
```

Paste:

```python
# Template shelf: the pack folders live next to the repo, not in
# it (~/workspace/grokbot/skill-pack). Overridable for tests.
TEMPLATE_SHELF_DIR = Path(os.environ.get(
    "GROKBOT_SHELF_DIR",
    str(Path(__file__).resolve().parents[2] / "grokbot" / "skill-pack")))
```

## 3. `AppState.__init__` — review store + shelf

Anchor — the signature line:

```python
    def __init__(self, characters_dir=None, chat_file=None, memory_db=None,
                 bridge=None, token=None, legacy=None, mail=None,
                 kanban=None, computers=None, beacons=None,
                 approvals=None):
```

Add `template_reviews=None, template_shelf_dir=None` to the
parameter list (after `approvals=None`).

Anchor — directly after the `self.approvals = ...` assignment
(the approvals store must exist first; the review store is
independent of it, but keep the new stores together).

Paste:

```python
        # Fase 2 (item 4): template review -> install. The review
        # store is in-memory, single-use, 30-minute TTL; the shelf
        # is where staged template packs live. Both injectable so
        # tests can pass a temp store / temp shelf; production uses
        # the defaults. Routines created at install go through
        # self.routines (RoutineStore, automation patch) — a
        # template's routines are created PAUSED there.
        self.template_reviews = (template_reviews if template_reviews
                                 is not None else TemplateReviewStore())
        self.template_shelf_dir = Path(
            template_shelf_dir if template_shelf_dir is not None
            else TEMPLATE_SHELF_DIR)
```

## 4. `create_server` — pass-through (for tests)

Anchor — the signature ending:

```python
                  kanban=None, computers=None, beacons=None,
                  approvals=None):
```

Add `template_reviews=None, template_shelf_dir=None` after
`approvals=None`.

Anchor — the `AppState(...)` call inside `create_server`:

```python
    state = AppState(characters_dir, chat_file, memory_db, bridge=bridge,
                     token=token, legacy=legacy, mail=mail,
                     kanban=kanban, computers=computers, beacons=beacons,
                     approvals=approvals)
```

Append `template_reviews=template_reviews,
template_shelf_dir=template_shelf_dir` to that call.

## 5. `_read_template_body` — the 4 MB review reader

Anchor — directly after the `_allow` method:

```python
        def _allow(self, obj, fields):
            return {k: obj.get(k) for k in fields if k in obj}
```

Paste:

```python
        def _read_template_body(self):
            """Read a template-review body (up to 4 MB).

            The generic _read_json caps at MAX_BODY (64 KB); a
            template package may be up to MAX_PACKAGE_BYTES (4 MB),
            so this reader has its own cap and answers 413 above
            it. Returns (obj, error): obj is the parsed JSON
            object, error is a message string (or None). The
            top-level REJECTED_FIELDS check is the same as in
            _read_json — a credential field is refused before
            anything is persisted or logged.
            """
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return None, "empty body"
            if length > MAX_PACKAGE_BYTES:
                return None, ("package body is %d bytes, limit is %d"
                              % (length, MAX_PACKAGE_BYTES))
            raw = self.rfile.read(length)
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None, "invalid JSON"
            if not isinstance(obj, dict):
                return None, "body must be a JSON object"
            bad = [k for k in obj if str(k).lower() in REJECTED_FIELDS]
            if bad:
                return None, ("credential fields are never accepted by "
                              "this server; keep credentials in the "
                              "browser only")
            return obj, None
```

## 6. POST endpoints

Anchor — directly before the final `else` of `do_POST`
(the branch whose comment reads "No other POST endpoint
exists — in particular there is no endpoint that accepts a
credential."). Insert these branches before it.

```python
            elif path == "/api/templates/review":
                # Fase 2 (item 4): review a template package before
                # installing it. Body is ONE of:
                #   {"link": "https://x.ai/bot/<id>"} — resolve the
                #       public link server-side, then review it;
                #   {"template": {...}} — an inline grokbot.team
                #       package object (up to 4 MB; the dedicated
                #       reader above enforces the cap, 413 over it).
                # Installs NOTHING. Returns 200 + the review
                # payload (review_id, what will be installed, which
                # fields were stripped, warnings). A rejected
                # package returns 400 + the validation errors
                # (redacted — no secret value ever appears).
                obj, err = self._read_template_body()
                if err is not None:
                    status = 413 if err.startswith("package body is") \
                        else 400
                    self._send_json({"error": err}, status=status)
                    return
                try:
                    if "link" in obj:
                        definition = resolve_template(obj["link"])
                        payload = review_template_package(
                            definition, store=state.template_reviews,
                            source_url=obj["link"])
                    elif "template" in obj:
                        payload = review_template_package(
                            obj["template"],
                            store=state.template_reviews)
                    else:
                        self._send_json(
                            {"error": "body needs 'link' or 'template'"},
                            status=400)
                        return
                except TemplateResolveError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                except TemplateImportError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                except ValueError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                self._send_json({"review": payload})
            elif path == "/api/templates/install":
                # Fase 2 (item 4): install a REVIEWED template.
                # Body: {"review_id": "..."}. No review_id (or an
                # unknown / expired / already-used one) is refused
                # with 400 — there is no install without a review.
                # On success: the pack is staged on the shelf, the
                # template's routines are created PAUSED in
                # state.routines, and its connections are recorded
                # DISABLED (manifest mcp.servers + install-state.json).
                # A name clash with an existing pack is 409 (the
                # review is released, so it can be retried after
                # the clash is cleared).
                obj, err = self._read_json()
                if err is not None:
                    self._send_json({"error": err}, status=400)
                    return
                data = self._allow(obj, ["review_id"])
                try:
                    result = install_reviewed_template(
                        data.get("review_id"),
                        state.template_shelf_dir,
                        store=state.template_reviews,
                        routine_store=state.routines)
                except TemplateInstallError as e:
                    self._send_json({"error": str(e)}, status=400)
                    return
                except TemplateImportError as e:
                    status = 409 if "already exists" in str(e) else 400
                    self._send_json({"error": str(e)}, status=status)
                    return
                self._send_json({"installed": result}, status=201)
```

### Request / response shapes

`POST /api/templates/review` — link form:

```json
// request
{"link": "https://x.ai/bot/Ub3T7usX-c6yRQibQq83P"}
// response 200
{"review": {
  "review_id": "9f2c…(32 hex)",
  "format": "grokbot.team", "format_version": 1,
  "package": {"name": "morning-crew", "version": "2.0.0",
    "description": "…", "size_bytes": 1234,
    "character_count": 2, "skill_count": 2,
    "routine_count": 1, "connection_count": 1},
  "characters": [{"name": "Bina", "role": "…",
    "skills": ["inbox-triage"], "prompt_chars": 19,
    "prompt_preview": "Summarise the news."}],
  "skills": ["inbox-triage", "weekly-pulse"],
  "routines": [{"title": "Morning pulse", "character": "Bina",
    "schedule": {"kind": "daily", "time": "07:00"},
    "enabled": false,
    "note": "starts PAUSED — resume it explicitly after install"}],
  "connections": [{"name": "notes-mcp", "type": "http",
    "url": "https://example.invalid/mcp", "enabled": false,
    "note": "starts DISABLED — enable it explicitly after install"}],
  "source": {"origin": "own-build"},
  "stripped_fields": ["flavor_text"],
  "warnings": ["1 connection(s) will start DISABLED — …",
               "1 routine(s) will start PAUSED — …",
               "1 unknown field(s) were stripped …"],
  "requires_review_before": ["send", "publish", "merge", "deploy",
    "delete", "change-production-data"],
  "expires_in_seconds": 1800
}}
```

`POST /api/templates/review` — inline form: same response;
request is `{"template": {…grokbot.team package object…}}`.
Error: 400 `{"error": "template definition invalid: <code>: <message>; …"}`,
413 `{"error": "package body is N bytes, limit is 4194304"}`.

`POST /api/templates/install`:

```json
// request
{"review_id": "9f2c…(32 hex)"}
// response 201
{"installed": {
  "name": "morning-crew",
  "path": "/home/user/workspace/grokbot/skill-pack/morning-crew",
  "review_id": "9f2c…(32 hex)",
  "characters": ["Bina", "Jaka"],
  "skills": ["inbox-triage", "weekly-pulse"],
  "routines": [{"id": "routine-1", "title": "Morning pulse",
    "character": "Bina",
    "schedule": {"kind": "daily", "time": "07:00"},
    "enabled": false}],
  "connections": [{"name": "notes-mcp", "type": "http",
    "url": "https://example.invalid/mcp", "enabled": false}],
  "manifest": {…the staged pack manifest…},
  "install_state_path": "…/morning-crew/install-state.json"
}}
```

Errors: 400 `{"error": "unknown review … — review the template
first"}` / `"review … expired — review the template again"` /
`"review … was already installed — …"`; 409 on a pack-name
clash (`{"error": "a pack named '…' already exists on the shelf;
refusing to overwrite it"}`).

## 7. What is NOT in this patch (deliberately)

- No UI changes (the review screen is
  `templates/deck-review.patch.md`, applied by the deck builder).
- No `GET /api/templates/review/<id>` endpoint: the review
  payload is returned once by the review call and rendered by
  the deck from that response; the server keeps the review only
  as install state, not as a readable resource.
- No endpoint that installs a template from a raw pack folder or
  from a link without a review — the review step is mandatory
  by construction (there is no other install entry point).
- No change to `MAX_BODY`: only the review endpoint reads up to
  4 MB, via its own reader; every other endpoint keeps the 64 KB
  cap.
- No credential fields anywhere: both new POST bodies go through
  the `REJECTED_FIELDS` check, like the rest.
- The install step does NOT create live characters: staging a
  pack and installing it are the same gated step here, but the
  characters inside it stay staged (no character files, no loader
  entry) until the separate character-install step exists.
- Enabling a connection or resuming a routine after install is a
  separate explicit action (not built in this patch): the
  connection stays `enabled: false` in the manifest and
  `install-state.json`; the routine stays `enabled: false` in
  the routine store (`POST /api/routines/enable` from the
  automation patch resumes a routine).

## 8. Observation for the coordinator (not my code to fix)

`install_reviewed_template` takes its routine store from
`state.routines`, which only exists once
`automation/server-wiring.patch.md` is applied. If that patch is
not applied yet, `state.routines` does not exist and the install
handler above raises AttributeError — apply the automation patch
first, or pass `routine_store=None` (falls back to a default
`RoutineStore()` on `data/routines.json`, same store, just not
the shared AppState instance).
