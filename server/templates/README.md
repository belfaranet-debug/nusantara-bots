# Templates — format + import flow

Our answer to the official app's template gallery. A template is a
**pack** (kind `"template"`) that bundles several characters into
one installable crew. The format is our own; nothing is copied from
the official gallery.

## 1. Link format

```
x.ai/bot/<id>
```

* Host: `x.ai` (optional `www.`, optional `https://` scheme).
* Path: `/bot/<id>` — a trailing path, query string, or fragment
  after the id is tolerated.
* Id: **20–21 base64url characters** — `^[A-Za-z0-9_-]{20,21}$`.
  The RE report says 20, but the example link the owner clicked,
  `https://x.ai/bot/Ub3T7usX-c6yRQibQq83P`, is **21** characters
  (counted, not eyeballed — NanoID default length, same alphabet).
  The parser accepts 20–21 so it never rejects the very link it
  was built for.

`templates/resolve.py::parse_template_link` enforces this shape;
anything else is a `ValueError` that names the problem.

## 2. What SHOULD happen on import (from the RE report)

The official backend (gRPC package `server.v1`, RE report §4)
exposes this template surface:

| Official method | What it should do | Our equivalent |
|---|---|---|
| `ListPublicGrokBotMarketplaceListings` | list public templates in the gallery | the pack shelf (`grokbot/skill-pack/`) — no gallery UI yet |
| `GetGrokBotTemplateForSourceAgent` | fetch the template behind a source agent | `resolve_template(url)` — fetch the public page, extract the definition |
| `GetGrokBotTemplateImportDetails` | show what the template contains before installing | `import_template` validation errors + the staged `template.json` |
| `GetGrokBotTemplateVersion` | check the template's version | `template.json.version` |
| `GetGrokBotTemplateExportPolicy` | whether the template may be re-shared | not modelled — our templates are own-build or staged as published |
| `CreateGrokBotAgentFromTemplate` | create a live agent from the template | **not built** — import only *stages* the pack; installing characters is a separate, explicit step |

Expected flow for a link click, in our build:

```
x.ai/bot/<id>  →  parse_template_link  →  resolve_template (fetch page)
→  import_template (strict validation)  →  staged pack on the shelf
→  (separate step, not built yet) install characters as live characters
```

## 3. Why the owner's click failed in the old app — diagnosis

**This is an inference, not a verified fact.** The old app was
never instrumented, and its backend is a vendor service we cannot
inspect. What is known:

* The old app's manifest declares the App Link
  `x.ai/bot/<20 karakter>/…` (RE report §3), so the OS handed the
  link to the app — the click *reached* the app.
* Inside the app, the template opened nothing. The RE report shows
  the template methods exist as gRPC stubs
  (`GetGrokBotTemplateForSourceAgent`, `GetGrokBotTemplateImportDetails`,
  …), i.e. the client *knows the words* for import.

The most likely explanations, in order:

1. **Import was never implemented past the stub.** The deep-link
   handler exists (the link opens the app), but the screen that
   should render the template was never wired to
   `GetGrokBotTemplateImportDetails` / `CreateGrokBotAgentFromTemplate`,
   so the id goes nowhere.
2. **The backend does not know that id.** Template ids are
   vendor-issued; a gallery link can point at a template that was
   removed, made private, or never published, and the import call
   then fails server-side with nothing to show.
3. **A routing mismatch.** The App Link pattern is
   `x.ai/bot/<id>/…` (with a trailing segment); a bare
   `x.ai/bot/<id>` may fall through the router to a generic screen.

Our resolver is built for explanation 1 and 2: it validates the
link shape locally, fetches the public page without auth, and
says plainly when the page carries no recognisable definition —
instead of opening an empty screen.

## 4. Resolver behaviour (defensive by design)

* Fetch: one GET via `urllib`, no auth, no cookies, 8 s timeout,
  `User-Agent: grokbot-own-template-resolver/1.0`.
* Extraction order: embedded JSON (`__NEXT_DATA__`, then
  `<script type="application/json">`, then JSON-LD) walked for a
  template-shaped object (a `characters` list of objects with
  `name`); fallback to meta tags (`og:title` / `og:description`)
  for name + description only, flagged `partial: True`.
* **Nothing is invented.** An unrecognised page raises
  `TemplateResolveError` with a clear message. A partial
  definition cannot be staged (`template_partial`).
* The fetcher is injectable: `resolve_template(url, fetcher=...)`.
  Tests pass fixture HTML and never touch the network.

Caveat: the official page format has **not** been verified against
a live gallery page — the example page's contents are being read
separately, and if its real format differs (e.g. the definition
lives in a different JSON blob or behind auth), the extractor's
walk order must be updated. The resolver fails loudly in that
case rather than staging a guess.

## 5. Template pack format

```
<name>/                      # folder name == template name
  template.json             # the definition (required)
  manifest.json             # kind "template" (required)
  README.md                 # crew description (required)
  SOURCE.md                 # required when imported (origin != own-build)
```

`template.json`:

```json
{
  "format": "grokbot.team",
  "format_version": 1,
  "name": "daily-briefing-crew",
  "version": "1.0.0",
  "description": "…",
  "characters": [
    {"name": "Bina", "role": "Peringkas berita pagi",
     "skills": ["inbox-triage"], "prompt": "…"}
  ],
  "skills": ["inbox-triage", "weekly-pulse"],
  "routines": [
    {"title": "Morning pulse", "character": "Bina",
     "schedule": {"kind": "daily", "time": "07:00"}}
  ],
  "connections": [
    {"name": "notes-mcp", "type": "http",
     "url": "https://example.invalid/mcp"}
  ],
  "source": {"origin": "own-build"}
}
```

Rules (enforced by `templates/importer.py` and by the pack
validator, kind `"template"` in `packs/pack.py` — the same rules
in both places, so they can never drift apart):

* One template may hold several characters (a crew template);
  at most 16.
* Every character needs `name`, `role`, and a non-empty `prompt`;
  `skills` is a list of shelf skill names (may be empty).
* `manifest.json` mirrors the definition: `characters` = the
  character names, `skills` = the union of all character skills,
  `mcp.servers` = the template's connections, each with
  `"enabled": false` (see §8).
* The six default `review.required_before` actions are mandatory,
  exactly as for skill/crew packs.
* The no-secrets rule applies to `template.json` itself, not just
  to the manifest: any credential-like key anywhere fails
  validation. Credentials stay BYO (browser localStorage only).
* Import refuses to overwrite an existing pack folder.
* Package caps (§8): at most 4 MB of serialised JSON, at most 60
  distinct skills per package.
* A routine's `character` must be one of the template's own
  characters, and its `schedule` must pass the routine
  scheduler's validation (`interval` / `daily` / `cron`).
* A connection is `{"name", "type": "http"|"stdio",
  "url"|"command", "tools"?}` — an MCP server the template
  brings. At most 16 per package.

## 6. Staging vs installing

`import_template` writes the pack to the shelf and stops there.
The staged pack is **not** live characters: no character files are
created, no loader entry appears, nothing runs. Installing a
template is a separate, explicit, **reviewed** step — see §8.

## 7. Our own template

`daily-briefing-crew` (on the shelf, `grokbot/skill-pack/`): a
small crew built from scratch for this build — **not** a copy of
any official gallery template:

* **Bina** — peringkas berita pagi (skill: `inbox-triage`).
* **Jaka** — penjadwal harian (skill: `weekly-pulse`).

It passes the pack validator (`python3 -m packs.convert --shelf
../grokbot/skill-pack --check` and `validate_shelf`).

## 8. Package security rules — the `grokbot.team` format

The template package format is our own, named `grokbot.team`
(a `grokbot.package`). These rules apply to every template
package, whether it arrives from a link, an inline upload, or a
file — enforced identically by `templates/importer.py` and by
the pack validator for kind `"template"`:

| Rule | Behaviour |
|---|---|
| Strict schema | Required fields + types; anything else fails validation with a clear error. A failed package is refused, never staged half-valid. |
| Forbidden fields | Chat history, secrets, model choice, computer, visibility — if one of those fields is present anywhere in the package, import is **REJECTED** (never stripped silently). The error names the field. |
| Unknown fields | Any other unknown field is **stripped** and listed in `stripped_fields` on the review payload (and the import result), so nothing disappears without a trace. |
| Secrets | A secret-like value (known token prefixes, `key = <token>` assignments) anywhere rejects the package (`secret_value_detected`), and the value never appears in an error, a log, or a review payload — only the field path is named; redacted output shows `[REDACTED]`. |
| Size caps | At most **4 MB** of serialised JSON; at most **60** distinct skills per package. |
| Review before install | `review_template_package` validates and returns a review payload (what will be installed, stripped fields, warnings). It installs nothing. `install_reviewed_template` installs **only** with a valid `review_id` from that review — no review, no install. Reviews are single-use and expire after 30 minutes. |
| Connections disabled | A connection the template brings starts **DISABLED** (`enabled: false` in the manifest's `mcp.servers` and in `install-state.json`) until a human enables it explicitly. The package schema has no `enabled` field — a package cannot pre-enable anything. |
| Routines paused | A routine the template brings is created in the routine store with `enabled: false` (**paused**) — it never fires until a human resumes it. The package schema has no `enabled` field for routines either. |

Endpoints (server wiring, not yet pasted into `app/server.py`):
`POST /api/templates/review` and `POST /api/templates/install` —
see `templates/server-wiring.patch.md`. Review screen (deck
snippet, not yet pasted into `app/static/`): see
`templates/deck-review.patch.md`.

Install writes `install-state.json` into the pack folder,
recording the review id, the installed characters/skills, the
created routines (with their ids, paused), and the connections
(disabled) — the auditable record of what the install did.
