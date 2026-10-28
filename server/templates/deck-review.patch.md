# Deck patch — template review screen (Fase 2, item 4)

**For the coordinator to paste into `app/static/index.html` and
`app/static/app.js`. These files are the patch; the static files
themselves were NOT edited by this builder.**

What it adds: a "Template review" panel in the deck. The user
pastes a template link (`x.ai/bot/<id>`) or an inline
`grokbot.team` package, sees the review payload — what will be
installed (characters, skills, routines, connections), which
fields were stripped, and the warnings — and only then confirms.
Nothing installs from this screen until the **Install template**
button is pressed, and that button sends only the `review_id`:
the install happens server-side from the stored review.

Security notes for the deck builder:

* The review payload is rendered with the existing `esc()`
  helper everywhere — template content is untrusted text (names,
  roles, prompt previews, warnings) and must never be injected
  as raw HTML.
* The payload never contains a secret (the server redacts and
  rejects), but the panel must still never render a `prompt` in
  full — only `prompt_preview` (first 200 chars) plus the
  character count, as the payload provides.
* The install button is disabled while an install is in flight,
  and it is never re-enabled for the same `review_id` after a
  successful install (reviews are single-use server-side; a
  second click would only produce a 400, but the button should
  not invite it).
* The panel shows the two inert-by-default states prominently:
  routines carry a **PAUSED** badge, connections a **DISABLED**
  badge. Enabling/resuming them is a separate explicit action
  elsewhere, not a button on this screen.

---

## 1. HTML — the review panel

Anchor — inside `<section id="deck" class="panel hidden">`,
directly after this block:

```html
      <h3 class="vo-h">Own-build characters</h3>
      <ul id="deck-characters"></ul>
```

Paste:

```html
      <h3 class="vo-h">Template review — install a crew, reviewed first</h3>
      <p class="vo-note">Paste a template link or an inline grokbot.team package. Nothing installs until you review what it brings and press <strong>Install template</strong>. Routines start <strong>paused</strong>, connections start <strong>disabled</strong>.</p>
      <label>Template link <input id="template-link" placeholder="https://x.ai/bot/…" autocomplete="off"></label>
      <button id="template-review-link" type="button">Review link</button>
      <label>…or paste a package (JSON) <textarea id="template-json" rows="4" placeholder='{"name": "…", "version": "…", …}'></textarea></label>
      <button id="template-review-inline" type="button">Review package</button>
      <p id="template-review-status" class="status">No template under review.</p>
      <div id="template-review" class="template-review"></div>
```

## 2. JS — review + install

Anchor — in `app/static/app.js`, next to the other deck
renderers (after `renderCharacterList` or any nearby deck
function; the exact spot does not matter, the functions are
self-contained and use only the existing `apiJson`, `apiFetch`,
and `esc` helpers).

Paste:

```js
/* ---- Template review (grokbot.team) ----
 * Flow: POST /api/templates/review (link or inline package) ->
 * render the review payload -> POST /api/templates/install
 * with ONLY the review_id. Nothing installs before the confirm
 * button. All template text goes through esc(); prompts are
 * shown as prompt_preview only, never in full. A 401 yields null
 * from apiJson (see its contract) and leaves prior content
 * untouched. */
let pendingTemplateReviewId = null;

function templateReviewRows(review) {
  const pkg = review.package || {};
  const chars = (review.characters || []).map(c =>
    `<li><strong>${esc(c.name)}</strong> — ${esc(c.role)}` +
    ` <span class="skill-chips">${(c.skills || []).map(s =>
      `<span class="skill-chip">${esc(s)}</span>`).join("")}</span>` +
    `<br><span class="vo-note">prompt: ${esc(c.prompt_preview || "")}` +
    ` (${c.prompt_chars || 0} chars, preview only)</span></li>`).join("");
  const routines = (review.routines || []).map(r =>
    `<li><strong>${esc(r.title)}</strong> — ${esc(r.character)}` +
    ` <span class="vo-tag">PAUSED</span>` +
    `<br><span class="vo-note">${esc(r.schedule ? r.schedule.kind : "")}` +
    ` ${esc(r.schedule ? (r.schedule.time || r.schedule.expression || (r.schedule.seconds + "s")) : "")}</span></li>`).join("");
  const conns = (review.connections || []).map(c =>
    `<li><strong>${esc(c.name)}</strong> (${esc(c.type)})` +
    ` <span class="vo-tag">DISABLED</span>` +
    `<br><span class="vo-note">${esc(c.url || c.command || "")}</span></li>`).join("");
  const stripped = (review.stripped_fields || []).map(f =>
    `<li>${esc(f)}</li>`).join("");
  const warnings = (review.warnings || []).map(w =>
    `<li>${esc(w)}</li>`).join("");
  return { pkg, chars, routines, conns, stripped, warnings };
}

function renderTemplateReview(review) {
  const el = document.getElementById("template-review");
  const status = document.getElementById("template-review-status");
  if (!el || !review) return;
  pendingTemplateReviewId = review.review_id || null;
  const { pkg, chars, routines, conns, stripped, warnings } =
    templateReviewRows(review);
  el.innerHTML =
    `<h4>${esc(pkg.name || "?")} <span class="vo-note">v${esc(pkg.version || "?")} · ` +
    `${pkg.character_count || 0} characters · ${pkg.skill_count || 0} skills · ` +
    `${pkg.routine_count || 0} routines · ${pkg.connection_count || 0} connections · ` +
    `${pkg.size_bytes || 0} bytes</span></h4>` +
    `<p>${esc(pkg.description || "")}</p>` +
    `<h5>Characters</h5><ul>${chars || "<li>—</li>"}</ul>` +
    `<h5>Routines — start paused</h5><ul>${routines || "<li>none</li>"}</ul>` +
    `<h5>Connections — start disabled</h5><ul>${conns || "<li>none</li>"}</ul>` +
    (stripped ? `<h5>Fields stripped from the package (not installed)</h5><ul>${stripped}</ul>` : "") +
    (warnings ? `<h5>Warnings</h5><ul>${warnings}</ul>` : "") +
    `<p class="vo-note">Review expires in ${review.expires_in_seconds || 1800}s. ` +
    `Installing uses only the review id — the package itself is not re-sent.</p>` +
    `<button id="template-install" type="button">Install template</button> ` +
    `<button id="template-discard" type="button">Discard review</button>`;
  if (status) status.textContent =
    `Reviewing ${pkg.name || "?"} — check everything above, then install or discard.`;
  const installBtn = document.getElementById("template-install");
  if (installBtn) installBtn.onclick = installReviewedTemplate;
  const discardBtn = document.getElementById("template-discard");
  if (discardBtn) discardBtn.onclick = () => {
    pendingTemplateReviewId = null;
    el.innerHTML = "";
    if (status) status.textContent = "Review discarded. Nothing was installed.";
  };
}

async function requestTemplateReview(body) {
  const status = document.getElementById("template-review-status");
  const data = await apiJson("/api/templates/review", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!data) return; // 401: apiJson yields null; auth note says what to do.
  if (data.error) {
    if (status) status.textContent = "Review refused: " + data.error;
    return;
  }
  if (data.review) renderTemplateReview(data.review);
}

async function reviewTemplateLink() {
  const link = (document.getElementById("template-link") || {}).value || "";
  if (!link.trim()) {
    const status = document.getElementById("template-review-status");
    if (status) status.textContent = "Paste a template link first.";
    return;
  }
  await requestTemplateReview({ link: link.trim() });
}

async function reviewTemplateInline() {
  const raw = (document.getElementById("template-json") || {}).value || "";
  let template;
  try { template = JSON.parse(raw); }
  catch (e) {
    const status = document.getElementById("template-review-status");
    if (status) status.textContent = "That is not valid JSON: " + e.message;
    return;
  }
  await requestTemplateReview({ template });
}

async function installReviewedTemplate() {
  const status = document.getElementById("template-review-status");
  const btn = document.getElementById("template-install");
  if (!pendingTemplateReviewId) {
    if (status) status.textContent = "No review to install — review a template first.";
    return;
  }
  if (btn) btn.disabled = true;
  const r = await apiFetch("/api/templates/install", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ review_id: pendingTemplateReviewId }),
  });
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!data) {
    if (status) status.textContent = "Install failed: no response from the server.";
    if (btn) btn.disabled = false;
    return;
  }
  if (data.error) {
    if (status) status.textContent = "Install refused: " + data.error;
    if (btn) btn.disabled = false;
    return;
  }
  const installed = data.installed || {};
  pendingTemplateReviewId = null;
  if (status) status.textContent =
    `Installed ${installed.name || "?"}: ` +
    `${(installed.routines || []).length} routine(s) paused, ` +
    `${(installed.connections || []).length} connection(s) disabled. ` +
    `Enable or resume each one explicitly when you are ready.`;
  const el = document.getElementById("template-review");
  if (el) el.innerHTML = "";
}

document.getElementById("template-review-link").onclick = reviewTemplateLink;
document.getElementById("template-review-inline").onclick = reviewTemplateInline;
```

## 3. What the screen shows (contract with the review payload)

| Payload field | Rendered as |
|---|---|
| `package.name/version/description/size_bytes` + counts | Header line + description |
| `characters[]` (name, role, skills, `prompt_preview`, `prompt_chars`) | One row each; prompt as preview only |
| `routines[]` | One row each with a **PAUSED** badge + schedule |
| `connections[]` | One row each with a **DISABLED** badge + url/command |
| `stripped_fields[]` | "Fields stripped" list (only when non-empty) |
| `warnings[]` | Warnings list |
| `expires_in_seconds` | "Review expires in …s" note |
| `review_id` | Held in `pendingTemplateReviewId`; sent only on Install |

## 4. What is NOT in this patch (deliberately)

- No enable/resume buttons for connections or routines: those
  are separate explicit actions after install, not part of the
  review screen.
- No character-install action: the staged pack's characters are
  not live characters yet (that step does not exist in this
  phase); the screen installs the pack, it does not activate
  characters.
- No file upload input: inline review pastes JSON text (up to
  the server's 4 MB cap); a file-picker upload is a later step
  if the owner wants it.
- No styling beyond the existing `vo-note`, `vo-tag`,
  `skill-chip`, and `status` classes — the deck builder owns
  the look; this patch only adds structure and behaviour.
