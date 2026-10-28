/* Grok Bot own build — browser code (Phase 3).
 *
 * BYO-API invariant: the credential lives ONLY in this browser's
 * localStorage. When BYO-API is configured, this file fetches the
 * model API DIRECTLY (fetch(baseUrl + "/chat/completions")) and
 * then persists only the finished messages to the local server via
 * POST /api/messages — a body of {room, character, text, role} with
 * no credential in it. The local server has no endpoint that
 * accepts a credential, so there is nowhere for it to leak to.
 */
let characters = [], rooms = [], currentRoom = "general", currentChar = null;

function byo() {
  try { return JSON.parse(localStorage.getItem("grokbot_byo") || "null"); }
  catch (e) { return null; }
}
function byoStatus() {
  const b = byo(), el = document.getElementById("byo-status");
  el.textContent = (b && b.baseUrl && b.credential)
    ? "BYO-API configured in this browser only (credential never sent to the local server). Replies call your model API directly from this browser."
    : "No BYO-API configured — using the clearly-labelled MOCK model (server-side demo text, no real API call).";
  if (b) {
    document.getElementById("byo-base").value = b.baseUrl || "";
    document.getElementById("byo-model").value = b.model || "";
  }
}
document.getElementById("byo-save").onclick = () => {
  const baseUrl = document.getElementById("byo-base").value.trim().replace(/\/$/, "");
  const model = document.getElementById("byo-model").value.trim();
  const credential = document.getElementById("byo-credential").value;
  if (!baseUrl || !credential) { alert("Base URL and credential are both needed."); return; }
  localStorage.setItem("grokbot_byo", JSON.stringify({ baseUrl, model, credential }));
  document.getElementById("byo-credential").value = "";
  byoStatus();
};
document.getElementById("byo-clear").onclick = () => {
  localStorage.removeItem("grokbot_byo"); byoStatus();
};

/* Access token (Phase 5 upgrade): the same localStorage-only
 * pattern as BYO-API. Sent as the X-Grokbot-Token header on every
 * local /api/* request, never in a body, never to the model API.
 * The static page stays public; the gate lives on /api/*, so a
 * 401 here only ever means "save the server token first". */
function authToken() {
  try { return localStorage.getItem("grokbot_auth") || ""; }
  catch (e) { return ""; }
}
function authHeaders() {
  const t = authToken();
  return t ? { "X-Grokbot-Token": t } : {};
}
function authStatus() {
  const el = document.getElementById("auth-status");
  if (!el) return;
  el.textContent = authToken()
    ? "Access token saved in this browser only (sent as a header on local /api/* requests)."
    : "No access token saved — loopback use needs none; a token-bound server answers 401 until you save its token.";
}
function noteAuthNeeded() {
  const el = document.getElementById("auth-status");
  if (el) el.textContent = "Server answered 401 — save its access token above, then retry.";
}
async function apiFetch(url, opts) {
  opts = opts || {};
  opts.headers = Object.assign({}, authHeaders(), opts.headers || {});
  const r = await fetch(url, opts);
  if (r.status === 401) noteAuthNeeded();
  return r;
}
/* apiJson: like apiFetch, but a 401 yields null instead of an
 * error body — so an unsaved token NEVER renders as empty data
 * ("No characters yet", "Bridge unavailable: no bridge CLI
 * found"). Callers return early on null and leave prior content
 * untouched; the auth note above says what to do. */
async function apiJson(url, opts) {
  const r = await apiFetch(url, opts);
  if (r.status === 401) return null;
  try { return await r.json(); } catch (e) { return null; }
}
document.getElementById("auth-save").onclick = () => {
  const t = document.getElementById("auth-token").value;
  if (!t) { alert("Paste the server token first."); return; }
  localStorage.setItem("grokbot_auth", t);
  document.getElementById("auth-token").value = "";
  authStatus();
};
document.getElementById("auth-clear").onclick = () => {
  localStorage.removeItem("grokbot_auth"); authStatus();
};

function esc(s) { return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

/* ---- Skills chips + email line on bot cards ----
 * Skills come from the characters payload (c.skills[]); the mail
 * line comes from GET /api/mail, contract:
 *   /api/mail?all=1  and  /api/mail?character=<stem>
 *   -> {character, label, address, address_present,
 *       inbox_count, unread, available, error}
 * Both fetches are defensive: a 404, an error body, a 401, or a
 * missing endpoint must NEVER break rendering — the mail line
 * simply stays "email: unavailable". Mail data is cached in
 * mailByChar keyed by character stem (c.id); re-renders read
 * from that cache, so a late mail response fills both the
 * sidebar character list and the deck's own-build list. */
let mailByChar = {};
let lastDeckCharacters = [];
function renderSkillChips(skills) {
  const list = Array.isArray(skills) ? skills.filter(s => s !== null && s !== undefined && String(s).trim() !== "") : [];
  if (!list.length) return `<span class="skill-empty">no skills listed</span>`;
  return list.map(s => `<span class="skill-chip">${esc(s)}</span>`).join("");
}
function renderRoomSkillChips(skills) {
  // Room chips are legacy bots: they carry no skills[] today, so
  // an absent/empty list renders nothing (no invented chips).
  // If a room entry ever carries skills[], they render as chips
  // inside the chip — which still dims with its parent .vo-agent.
  const list = Array.isArray(skills) ? skills.filter(s => s !== null && s !== undefined && String(s).trim() !== "") : [];
  if (!list.length) return "";
  return `<div class="skill-chips room-skills">${list.map(s => `<span class="skill-chip">${esc(s)}</span>`).join("")}</div>`;
}
function mailLineFor(charId) {
  const e = mailByChar[charId];
  if (!e || e.available === false || e.error) return "email: unavailable";
  const addr = (typeof e.address === "string" && e.address.trim()) ? e.address.trim() : "";
  if (!addr || e.address_present === false) return "email: unavailable";
  let line = "email: " + esc(addr);
  if (typeof e.inbox_count === "number") line += ` · inbox: ${e.inbox_count}`;
  if (typeof e.unread === "number") line += `, unread: ${e.unread}`;
  return line;
}
function mailLineHtml(charId) {
  return `<div class="mail-line">${mailLineFor(charId)}</div>`;
}
function roomMailLineHtml(botId) {
  // Same rule as room skills: no mail entry for this bot means
  // no line at all (legacy bots are not characters, so no mail
  // is invented for them); a matched entry renders its line.
  if (!mailByChar[botId]) return "";
  return `<div class="mail-line">${mailLineFor(botId)}</div>`;
}
function collectMailEntries(d) {
  if (!d || d.error) return [];
  if (Array.isArray(d)) return d;
  if (Array.isArray(d.mail)) return d.mail;
  if (Array.isArray(d.inboxes)) return d.inboxes;
  if (Array.isArray(d.characters)) return d.characters;
  if (d.character) return [d];
  // Mapping shape: {<stem>: {label, address, ...}, ...}
  const vals = Object.values(d).filter(v => v && typeof v === "object" && ("address" in v || "address_present" in v || "label" in v));
  if (vals.length) return Object.entries(d).filter(([, v]) => v && typeof v === "object" && ("address" in v || "address_present" in v || "label" in v)).map(([k, v]) => Object.assign({ character: k }, v));
  return [];
}
async function loadMail() {
  try {
    const all = await apiJson("/api/mail?all=1");
    const entries = collectMailEntries(all);
    entries.forEach(e => { if (e && e.character) mailByChar[e.character] = e; });
    if (entries.length) return;
    // Fallback: bulk endpoint missing/empty — try per-character.
    for (const c of characters) {
      try {
        const one = await apiJson("/api/mail?character=" + encodeURIComponent(c.id));
        const found = collectMailEntries(one);
        found.forEach(e => { if (e && e.character) mailByChar[e.character] = e; });
      } catch (e) { /* this character stays "email: unavailable" */ }
    }
  } catch (e) { /* mail endpoint unavailable: lines stay unavailable */ }
}
function renderCharacterItem(c, duty) {
  const status = c.status === "valid" ? "✓" : "INVALID: " + esc((c.errors || []).map(e => e.code).join(","));
  const dutyTag = duty ? ` <span class="duty-tag">duty: ${esc(duty)}</span>` : "";
  return `<li class="char-card"><span class="char-avatar">${esc(voInitials(c.name))}</span><div class="char-body"><div class="char-head"><strong>${esc(c.name)}</strong> <span class="char-status">${status}</span>${dutyTag}</div><div class="char-meta">${esc(c.role)} · ${esc(c.memory_scope)}</div><div class="skill-chips">${renderSkillChips(c.skills)}</div>${mailLineHtml(c.id)}</div></li>`;
}
function renderCharacterList() {
  const list = document.getElementById("character-list");
  if (!list) return;
  list.innerHTML = characters.map(c => renderCharacterItem(c)).join("") || "<li>No characters yet.</li>";
  // Tap a card to talk to that character: select it, load its
  // memory, and open its chat.
  list.querySelectorAll(".char-card").forEach((el, i) => {
    el.addEventListener("click", () => {
      const c = characters[i];
      if (!c) return;
      openChatForChar(c);
    });
  });
  renderBotDirectory();
}
/* Duty comes from the deck payload (d.duty, keyed by character stem);
 * the sidebar list has no duty data, so it renders without the tag. */
let lastDeckDuty = {};
function renderDeckCharacters(chars, dutyMap) {
  lastDeckCharacters = chars || [];
  if (dutyMap) lastDeckDuty = dutyMap;
  // The Bot tab mirrors this list (deck-characters no longer
  // exists in the page); render it before the guard below.
  renderBotDirectory();
  const el = document.getElementById("deck-characters");
  if (!el) return;
  el.innerHTML = lastDeckCharacters.map(c => renderCharacterItem(c, lastDeckDuty[c.id])).join("") || "<li>No characters yet.</li>";
}

/* Bot tab: the character directory. Prefers the deck's own-build
 * list (with duty tags); falls back to the sidebar characters.
 * Tapping a card selects that character and opens the Chat tab. */
function renderBotDirectory() {
  const dir = document.getElementById("bot-directory");
  if (!dir) return;
  const src = lastDeckCharacters.length ? lastDeckCharacters : characters;
  dir.innerHTML = src.map(c => renderCharacterItem(c, lastDeckDuty[c.id])).join("") || "<li>No characters yet.</li>";
  dir.querySelectorAll(".char-card").forEach((el, i) => {
    el.addEventListener("click", () => {
      const c = src[i];
      if (!c) return;
      openChatForChar(c);
    });
  });
}

/* 0.6.0 navigation: NO bottom nav, NO list screen. The chat IS
 * the workspace — view-chat is the landing screen and the bot
 * list lives in the drawer beside it (openDrawer/closeDrawer
 * below). showScreen is the only view switcher: "chat" (home),
 * "rooms" (Virtual Office deck), "settings". There is no tab bar
 * to keep in sync. Switching screens always closes the drawer,
 * so there is never a dead end: rooms and settings each have a
 * ← Chat button, and the drawer is one tap away from chat. */
function showScreen(name) {
  closeDrawer();
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + name));
  if (name === "rooms") { openDeck(); startDeckTimer(); }
}

/* ---- 0.6.0 Drawer: the bot list beside the chat ----
 * The drawer slides in from the left (CSS transform on #drawer).
 * It opens from the ☰ button in the chat header and closes by:
 * tapping the scrim, swiping left on the drawer, the Android
 * back button (a history entry is pushed on open, so back closes
 * the drawer instead of leaving the page), or navigating to
 * another screen (showScreen closes it). The drawer holds the
 * same bot list the old list screen had (renderBotList, same
 * ids) plus the only two navigation rows: Ruangan and Pengaturan. */
let drawerOpen = false;
let drawerHistoryPushed = false;
let drawerTouchStartX = null;
function setDrawerVisible(open) {
  document.getElementById("drawer").classList.toggle("open", open);
  document.getElementById("drawer-scrim").classList.toggle("hidden", !open);
}
function openDrawer() {
  if (drawerOpen) return;
  drawerOpen = true;
  setDrawerVisible(true);
  try {
    history.pushState({ drawer: true }, "");
    drawerHistoryPushed = true;
  } catch (e) { /* no history (file://, restricted): drawer still works */ }
  renderBotList();
  refreshBotPreviews();
}
function closeDrawer() {
  if (!drawerOpen) return;
  drawerOpen = false;
  setDrawerVisible(false);
  if (drawerHistoryPushed) {
    // Pop the state openDrawer pushed, so the next back press
    // leaves the page instead of closing an already-closed
    // drawer. The popstate handler below sees drawerOpen false
    // and does nothing.
    drawerHistoryPushed = false;
    try { history.back(); } catch (e) { /* history unavailable */ }
  }
}
window.addEventListener("popstate", () => {
  if (!drawerOpen) return;
  // A back press consumed the state openDrawer pushed.
  drawerHistoryPushed = false;
  drawerOpen = false;
  setDrawerVisible(false);
});
document.getElementById("drawer-open-btn").onclick = openDrawer;
document.getElementById("drawer-scrim").addEventListener("click", closeDrawer);
document.getElementById("drawer").addEventListener("touchstart", (e) => {
  drawerTouchStartX = e.changedTouches[0].clientX;
}, { passive: true });
document.getElementById("drawer").addEventListener("touchend", (e) => {
  if (drawerTouchStartX === null) return;
  const dx = e.changedTouches[0].clientX - drawerTouchStartX;
  drawerTouchStartX = null;
  // Swipe left (a clear horizontal flick) closes the drawer.
  if (dx < -50) closeDrawer();
}, { passive: true });
document.getElementById("drawer-rooms").onclick = () => showScreen("rooms");
document.getElementById("drawer-settings").onclick = () => showScreen("settings");
document.getElementById("rooms-back").onclick = () => showScreen("chat");
document.getElementById("settings-back").onclick = () => showScreen("chat");

/* ---- Working state (reply in flight) ----
 * One flag, two surfaces: the red Stop pill in the chat top bar
 * and the working row in the drawer bot list. setWorking is
 * called before a model call (chat-form submit, voice turn) and
 * clearWorking in its finally. The flag is client-side only:
 * POST /api/stop is what actually silences the room. The old
 * list working bar was deleted in 0.6.0 — its Stop lived there,
 * the chat pill covers it now. */
let workingState = null; // {room, charId} or null
function setWorking(room, charId) {
  workingState = { room, charId };
  updateWorkingUI();
}
function clearWorking() {
  workingState = null;
  updateWorkingUI();
}
function updateWorkingUI() {
  const pill = document.getElementById("chat-stop-pill");
  if (pill) pill.classList.toggle("hidden", !workingState);
  // The drawer bot rows read workingState live (see
  // botWorkingLabel), so re-render here: a row lights the moment
  // its bot starts working and goes still the moment it stops.
  renderBotList();
}
async function stopWorking() {
  // Stop the visible work first: abort the in-flight model fetch
  // and any typewriter, so the pill actually stops the reply —
  // not just the flag. The server-side /api/stop below silences
  // the room itself.
  stopLiveReply();
  const room = workingState ? workingState.room : currentRoom;
  try {
    await apiFetch("/api/stop", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room }) });
  } catch (e) { /* stop is best-effort; the room may already be silent */ }
  hideTyping();
  clearWorking();
  loadRoom();
}
document.getElementById("chat-stop-pill").onclick = stopWorking;

/* ---- Live presence: typing indicator + progressive replies ----
 * Everything here is driven by REAL state, never a fake animation:
 * - #typing shows only while a reply is in flight (the chat
 *   submit handler and the voice turn call showTyping; the
 *   finally / stopWorking call hideTyping).
 * - A fresh reply flows into a streaming bubble: chunk by chunk
 *   when the model API streams (SSE), or typewriter-animated
 *   from the full text when it answers plain JSON. Old messages
 *   and the intro are rendered statically by loadRoom() and are
 *   never re-animated — only the new reply streams.
 * - Stop cancels the in-flight fetch (AbortController) and the
 *   typewriter (replyToken), and whatever text already arrived
 *   is persisted so it is not lost.
 * BYO-API invariant kept: the credential stays in this browser's
 * localStorage and is sent only to the model API, directly from
 * this browser — streamByoReply is a plain fetch() to
 * baseUrl + "/chat/completions", never to the local server. */
let replyAbort = null;      // AbortController for the in-flight model call
let replyToken = 0;         // increments per reply; a stale typewriter bails
let typewriterTimer = null; // active typewriter timeout, if any

function typingName() {
  const c = currentChar && characters.find(x => x.id === currentChar);
  return c ? c.name : "Bot";
}
function showTyping(name) {
  const el = document.getElementById("typing");
  if (!el) return;
  // Fresh innerHTML each time so the dot animation restarts.
  el.innerHTML = `<span class="typing-name">${esc(name || typingName())}</span> mengetik<span class="typing-dots" aria-hidden="true"><span></span><span></span><span></span></span>`;
  el.classList.remove("hidden");
  syncBlobStates();
}
function hideTyping() {
  const el = document.getElementById("typing");
  if (el) el.classList.add("hidden");
  syncBlobStates();
}
/* ---- Living blobs (0.6.0, Builder B) ----
 * Each bot gets ONE organic blob — an odd, non-circle shape in
 * its own colour — derived DETERMINISTICALLY from the bot id,
 * so the same bot always renders the same blob and different
 * bots get different ones. No images, no library: CSS
 * border-radius morph + a per-bot hue (see style.css).
 * - blobStyleFor(botId) -> {hue 0-359, variant 0-4}: a small
 *   string hash of the id; hue = hash % 360, variant = hash
 *   % 5. No bot id (the "General chat" pill before a bot
 *   exists) -> the old amber, hue 38, variant 0.
 * - blobClass / blobStyle: the class + inline custom property
 *   the four render sites stamp onto the .bot-blob hook.
 * The initials STAY inside the blob: they are the name cue at
 * a glance, in every place, at every size — only the typing
 * state hides them, swapping in three dots.
 * Live states (syncBlobStates) come ONLY from real state —
 * never a timer or a guess:
 * - blob-working: workingState (setWorking/clearWorking, the
 *   same flag behind the red Stop pill) — or, for room chips,
 *   a run with status "running", baked at renderDeckBots
 *   time with the same trigger as .vo-agent.working.
 * - blob-typing: the #typing indicator is visible — the same
 *   showTyping/hideTyping trigger as the "mengetik…" line.
 * syncBlobStates is called from every place that changes
 * either trigger (showTyping, hideTyping, renderBotList,
 * setChatPill, createStreamingBubble), so a blob never keeps
 * a state its bot already left. Room chips are skipped here —
 * the deck render owns their state. */
function blobHash(id) {
  const s = String(id == null ? "" : id);
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return h >>> 0;
}
const BLOB_VARIANTS = 5;
function blobStyleFor(botId) {
  if (!botId) return { hue: 38, variant: 0 };
  const h = blobHash(botId);
  return { hue: h % 360, variant: h % BLOB_VARIANTS };
}
function blobClass(botId) {
  return "bot-blob blob-v" + blobStyleFor(botId).variant;
}
function blobStyle(botId) {
  return "--blob-hue:" + blobStyleFor(botId).hue;
}
function syncBlobStates() {
  const typingEl = document.getElementById("typing");
  const typingVisible = !!(typingEl && !typingEl.classList.contains("hidden"));
  const workingId = workingState ? workingState.charId : null;
  const typingId = typingVisible ? currentChar : null;
  document.querySelectorAll(".bot-blob").forEach(el => {
    if (el.closest("#deck")) return; // deck chips: renderDeckBots owns them
    const id = el.dataset.botId;
    if (!id) return;
    el.classList.toggle("blob-working", id === workingId);
    el.classList.toggle("blob-typing", id === typingId);
  });
}
function stopLiveReply() {
  // Invalidates any running typewriter and aborts the in-flight
  // model fetch. Safe to call when nothing is running. NOTE: the
  // typewriter timer is deliberately NOT cleared here — its next
  // tick sees the bumped token and resolves false, which is what
  // lets an awaiting chat submit finish (clearing the timer
  // would orphan that promise and the reply would hang forever).
  replyToken++;
  if (replyAbort) { try { replyAbort.abort(); } catch (e) { /* already gone */ } replyAbort = null; }
}
function nearBottom() {
  return window.innerHeight + window.scrollY >= document.body.scrollHeight - 200;
}
function scrollChatToBottom(force) {
  if (!force && !nearBottom()) return; // never yank a user reading history
  window.scrollTo({ top: document.body.scrollHeight, behavior: "auto" });
}
function createStreamingBubble(charId) {
  const box = document.getElementById("messages");
  if (!box) return null;
  const char = characters.find(c => c.id === charId);
  const name = char ? char.name : "Bot";
  const div = document.createElement("div");
  div.className = "msg assistant streaming";
  div.innerHTML = `<span class="msg-avatar ${blobClass(charId)}" style="${blobStyle(charId)}" data-bot-id="${esc(charId)}">${esc(voInitials(name))}</span><div class="msg-body"><span class="msg-label">${esc(name)}</span><div class="msg-text"></div></div>`;
  box.appendChild(div);
  scrollChatToBottom(true);
  syncBlobStates(); // the live reply is workingState: its blob pulses
  return div.querySelector(".msg-text");
}
/* Typewriter for a whole reply that arrived at once (a provider
 * that ignores stream:true, or the MOCK path). Resolves true when
 * the full text landed, false when Stop / a newer reply cut it
 * short — the caller then keeps the partial text. */
const TYPEWRITER_CHUNK = 4;  // chars per frame (~250 chars/s)
const TYPEWRITER_DELAY = 16; // ms between frames
function typewriterInto(el, text, token) {
  return new Promise(resolve => {
    let i = 0;
    const step = () => {
      if (token !== replyToken || !document.body.contains(el)) {
        typewriterTimer = null;
        resolve(false);
        return;
      }
      i = Math.min(text.length, i + TYPEWRITER_CHUNK);
      el.textContent = text.slice(0, i);
      scrollChatToBottom(false);
      if (i >= text.length) { typewriterTimer = null; resolve(true); return; }
      typewriterTimer = setTimeout(step, TYPEWRITER_DELAY);
    };
    step();
  });
}
/* Streaming BYO call. Returns {streamed:true, text, usage} when
 * the provider answers SSE (onDelta fires per chunk with the full
 * text so far), or {streamed:false, data} when it answers plain
 * JSON (the caller then typewriters data). A provider that rejects
 * stream:true (400/404/422) is retried once without it, so
 * streaming never breaks chat for a non-streaming provider. */
async function streamByoReply(b, content, onDelta) {
  const headers = { "Content-Type": "application/json", "Authorization": "Bearer " + b.credential };
  const body = stream => JSON.stringify({ model: b.model || "gpt-4o-mini",
    messages: [{ role: "user", content }], stream });
  const signal = replyAbort ? replyAbort.signal : undefined;
  let resp = await fetch(b.baseUrl + "/chat/completions",
    { method: "POST", headers, body: body(true), signal });
  if (!resp.ok && (resp.status === 400 || resp.status === 404 || resp.status === 422)) {
    resp = await fetch(b.baseUrl + "/chat/completions",
      { method: "POST", headers, body: body(false), signal });
  }
  if (!resp.ok) throw new Error("Model API returned HTTP " + resp.status);
  const ct = (resp.headers.get("content-type") || "").toLowerCase();
  if (!ct.includes("text/event-stream") || !resp.body || !resp.body.getReader) {
    return { streamed: false, data: await resp.json() };
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "", full = "", usage = null, finished = false;
  try {
    while (!finished) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (payload === "[DONE]") { finished = true; break; }
        let j = null;
        try { j = JSON.parse(payload); } catch (e) { /* ignore a malformed SSE line */ }
        if (!j) continue;
        if (j.usage) usage = j.usage;
        const delta = j.choices && j.choices[0] && j.choices[0].delta && j.choices[0].delta.content;
        if (delta) { full += delta; onDelta(full); }
      }
    }
  } finally {
    try { reader.releaseLock(); } catch (e) { /* reader already gone */ }
  }
  return { streamed: true, text: full, usage };
}

/* ---- 0.6.0 Drawer bot list ----
 * One chat-list row per VALID character, rendered into the
 * drawer: living blob (data-bot-id + hue/shape from
 * blobStyleFor, initials inside) + unread dot only when that bot
 * has unread mail (real mailByChar data), bold name, preview =
 * the last message where this character spoke (fetched per 1:1
 * room, capped at 12 parallel fetches, failures tolerated),
 * time = that message's created_at in local time (HH:MM today,
 * else short date). No message yet → preview is the
 * character's role text. Tap a row → open that bot's chat
 * (showScreen closes the drawer). */
let listSearchQuery = "";
let botListPreviews = {}; // charId -> {text, time}
/* Live "who is working" for the bot list, from REAL server data:
 * deckOperatingByChar is the /api/devices + /api/deck `operating`
 * map, keyed by character id — a bot listed there is operating a
 * device right now. A reply in flight (workingState) wins over
 * it, so the label is "mengetik…" while answering and
 * "bekerja…" while operating a device. Anything else is "" and
 * the row stays still — no invented activity. */
let deckOperatingByChar = {};
function botWorkingLabel(c) {
  if (workingState && workingState.charId === c.id) return "mengetik…";
  if (deckOperatingByChar[c.id]) return "bekerja…";
  return "";
}
function validCharacters() { return characters.filter(c => c.status === "valid"); }
function roomForChar(charId) {
  const own = rooms.filter(r => r.mode === "chat" && (r.members || []).includes(charId));
  if (!own.length) return null;
  return own.find(r => (r.members || []).length === 1) || own[0];
}
function formatListTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  if (sameDay) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}
function previewFromMessages(c, msgs) {
  const spoken = (msgs || []).filter(m => m.character === c.id);
  const last = spoken[spoken.length - 1];
  botListPreviews[c.id] = last
    ? { text: last.text, time: formatListTime(last.created_at) }
    : { text: c.role || "", time: "" };
}
async function refreshBotPreviews() {
  const jobs = [];
  for (const c of validCharacters().slice(0, 12)) {
    const room = roomForChar(c.id);
    if (!room) { botListPreviews[c.id] = { text: c.role || "", time: "" }; continue; }
    // Local history first; the server copy merges in after, when
    // reachable — a failed fetch keeps the local preview.
    previewFromMessages(c, LocalDB.messagesFor(room.id));
    if (!ServerLink.isOnline()) continue;
    jobs.push(apiFetch("/api/chat?room=" + encodeURIComponent(room.id))
      .then(r => r.json())
      .then(data => {
        if (data && Array.isArray(data.messages)) {
          LocalDB.mergeMessages(room.id, data.messages);
          previewFromMessages(c, LocalDB.messagesFor(room.id));
        }
      })
      .catch(() => { /* local preview stands */ }));
  }
  await Promise.all(jobs);
  renderBotList();
}
function renderBotList() {
  const list = document.getElementById("bot-list");
  const empty = document.getElementById("bot-list-empty");
  if (!list) return;
  const valid = validCharacters();
  const q = listSearchQuery.trim().toLowerCase();
  const shown = q ? valid.filter(c => (c.name + " " + (c.role || "")).toLowerCase().includes(q)) : valid;
  if (!valid.length) {
    empty.textContent = "Tap + to create a bot";
    empty.classList.remove("hidden");
  } else if (!shown.length) {
    empty.textContent = "No bots match your search.";
    empty.classList.remove("hidden");
  } else {
    empty.classList.add("hidden");
  }
  list.innerHTML = shown.map(c => {
    const prev = botListPreviews[c.id] || { text: c.role || "", time: "" };
    const unread = !!(mailByChar[c.id] && typeof mailByChar[c.id].unread === "number" && mailByChar[c.id].unread > 0);
    const working = botWorkingLabel(c);
    const preview = working
      ? `<span class="working-dot" aria-hidden="true"></span>${esc(working)}`
      : esc(prev.text);
    return `<li class="bot-row${working ? " working" : ""}" data-char="${esc(c.id)}"><span class="bot-avatar ${blobClass(c.id)}" style="${blobStyle(c.id)}" data-bot-id="${esc(c.id)}">${esc(voInitials(c.name))}${unread ? '<span class="bot-dot"></span>' : ""}</span><div class="bot-body"><div class="bot-head"><strong>${esc(c.name)}</strong>${prev.time ? `<span class="bot-time">${esc(prev.time)}</span>` : ""}</div><div class="bot-preview${working ? " bot-preview-working" : ""}">${preview}</div></div>${unread ? '<span class="bot-unread"></span>' : ""}</li>`;
  }).join("");
  list.querySelectorAll(".bot-row").forEach(el => {
    el.addEventListener("click", () => {
      const c = validCharacters().find(x => x.id === el.dataset.char);
      if (c) openChatForChar(c);
    });
  });
  syncBlobStates(); // re-rendered rows must not keep a stale state
}

/* ---- Drawer top bar: owner avatar, search, notifications ----
 * (0.6.0: the old list top bar moved into the drawer header —
 * same ids, same handlers, no behaviour change.) */
document.getElementById("owner-avatar").onclick = () => showScreen("settings");
function toggleListSearch() {
  const wrap = document.getElementById("list-search");
  const nowHidden = wrap.classList.toggle("hidden");
  if (!nowHidden) document.getElementById("list-search-input").focus();
  else {
    listSearchQuery = "";
    document.getElementById("list-search-input").value = "";
    renderBotList();
  }
}
document.getElementById("search-btn").onclick = toggleListSearch;
document.getElementById("list-search-input").addEventListener("input", (e) => {
  listSearchQuery = e.target.value;
  renderBotList();
});

/* ---- Notifications: pending approvals, real data only ----
 * GET /api/approvals?status=pending -> {approvals: [{id, action,
 * target, detail, requester, status, created_at, age_seconds,
 * stale}]}. Approve/Deny POST /api/approvals/approve|deny
 * {id, by, reason}. A failed fetch shows an honest empty state
 * ("Could not load notifications") — items are never invented. */
async function loadNotifications() {
  const statusEl = document.getElementById("notif-status");
  const list = document.getElementById("notif-list");
  const badge = document.getElementById("notif-badge");
  if (!list) return;
  let data = null;
  try { data = await apiJson("/api/approvals?status=pending"); }
  catch (e) { data = null; }
  if (!data || !Array.isArray(data.approvals)) {
    if (statusEl) statusEl.textContent = "Could not load notifications.";
    list.innerHTML = "<li>No notifications.</li>";
    if (badge) badge.classList.add("hidden");
    return;
  }
  const items = data.approvals;
  if (badge) {
    badge.textContent = String(items.length);
    badge.classList.toggle("hidden", items.length === 0);
  }
  if (statusEl) statusEl.textContent = items.length
    ? items.length + " pending approval" + (items.length === 1 ? "" : "s") + "."
    : "";
  list.innerHTML = items.map(a => {
    const age = (typeof a.age_seconds === "number") ? Math.round(a.age_seconds) + "s ago" : "";
    const stale = a.stale ? " — stale" : "";
    return `<li class="notif-item"><div class="notif-body"><strong>${esc(a.action || "")}</strong> — ${esc(a.target || "")}${a.detail ? `<div class="notif-detail">${esc(a.detail)}</div>` : ""}<div class="notif-meta">${esc(a.requester || "")}${age ? " · " + age : ""}${stale}</div></div><div class="notif-actions"><button type="button" data-approve="${esc(a.id)}">Approve</button><button type="button" data-deny="${esc(a.id)}">Deny</button></div></li>`;
  }).join("") || "<li>No notifications.</li>";
  list.querySelectorAll("[data-approve]").forEach(b => b.onclick = () => decideApproval(b.dataset.approve, "approve"));
  list.querySelectorAll("[data-deny]").forEach(b => b.onclick = () => decideApproval(b.dataset.deny, "deny"));
}
async function decideApproval(id, verdict) {
  const statusEl = document.getElementById("notif-status");
  try {
    const r = await apiFetch("/api/approvals/" + verdict, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, by: "Sam" }) });
    const data = await r.json().catch(() => null);
    if (!r.ok) throw new Error((data && data.error) || ("HTTP " + r.status));
  } catch (err) {
    if (statusEl) statusEl.textContent = "Could not " + verdict + ": " + err.message;
    return;
  }
  loadNotifications();
}
document.getElementById("notif-btn").onclick = () => {
  const panel = document.getElementById("notif-panel");
  const nowHidden = panel.classList.toggle("hidden");
  if (!nowHidden) loadNotifications();
};

/* ---- Create dialog: floating centred card, not a sheet ----
 * Dismiss by tapping the scrim outside the card; there is no X
 * button. NEW BOT asks for a name FIRST (the one deliberate
 * deviation from the video, where the bot starts unnamed — a
 * character file needs a name, so the bot is not created until
 * a name exists). NEW GROUP is a real form -> POST /api/rooms.
 * NEW SPACE opens the Ruangan view. */
let pendingBotName = null; // name typed, bot not created yet
function openCreate() {
  document.getElementById("create-actions").classList.remove("hidden");
  document.getElementById("create-name-step").classList.add("hidden");
  document.getElementById("create-group-step").classList.add("hidden");
  document.getElementById("create-scrim").classList.remove("hidden");
}
function closeCreate() {
  document.getElementById("create-scrim").classList.add("hidden");
}
document.getElementById("create-scrim").addEventListener("click", (e) => {
  if (e.target.id === "create-scrim") closeCreate();
});
document.getElementById("create-btn").onclick = openCreate;
document.getElementById("create-new-bot").onclick = () => {
  document.getElementById("create-actions").classList.add("hidden");
  document.getElementById("create-name-step").classList.remove("hidden");
  document.getElementById("create-bot-name-error").textContent = "";
  document.getElementById("create-bot-name").focus();
};
document.getElementById("create-new-group").onclick = () => {
  document.getElementById("create-actions").classList.add("hidden");
  document.getElementById("create-group-step").classList.remove("hidden");
  document.getElementById("create-group-error").textContent = "";
  renderGroupMembers();
  document.getElementById("create-group-name").focus();
};
document.getElementById("create-new-space").onclick = () => {
  closeCreate();
  showScreen("rooms");
};
function renderGroupMembers() {
  const wrap = document.getElementById("create-group-members");
  const valid = validCharacters();
  wrap.innerHTML = valid.map(c => `<label class="group-member"><input type="checkbox" value="${esc(c.id)}" checked> ${esc(c.name)} <span class="note">${esc(c.role || "")}</span></label>`).join("")
    || "<p class=\"note\">No bots yet — create a bot first.</p>";
}
document.getElementById("create-bot-name-submit").onclick = () => {
  const name = document.getElementById("create-bot-name").value.trim();
  const err = document.getElementById("create-bot-name-error");
  if (!name) { err.textContent = "Give the bot a name first."; return; }
  pendingBotName = name;
  closeCreate();
  openNewBotChat(name);
};
document.getElementById("create-bot-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("create-bot-name-submit").click();
});
document.getElementById("create-group-submit").onclick = async () => {
  const name = document.getElementById("create-group-name").value.trim();
  const err = document.getElementById("create-group-error");
  if (!name) { err.textContent = "Give the group a name first."; return; }
  const members = Array.from(document.querySelectorAll("#create-group-members input[type=checkbox]:checked")).map(cb => cb.value);
  if (!members.length) { err.textContent = "Pick at least one member."; return; }
  err.textContent = "Creating…";
  let room = null;
  if (ServerLink.isOnline()) {
    try {
      const r = await apiFetch("/api/rooms", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, members, mode: "meeting" }) });
      const data = await r.json().catch(() => null);
      if (r.ok && data && data.room) {
        LocalDB.mergeRooms([data.room]);
        rooms = LocalDB.rooms.slice();
        room = data.room;
      }
    } catch (e) { /* fall through to a local group */ }
  }
  if (!room) {
    // Offline, or the server call failed: the group lives on
    // the device and syncs when the server is back.
    room = LocalDB.addRoom(LocalDB.buildRoom(name, members, "meeting"));
    rooms = LocalDB.rooms.slice();
  }
  closeCreate();
  currentRoom = room.id;
  pendingBotName = null;
  hideOnboarding();
  await loadRoom();
  showScreen("chat");
};

/* ---- New bot chat + onboarding ----
 * The chat opens with the typed name in the header pill and the
 * onboarding card; the bot does NOT exist yet. Tapping a focus
 * creates it (see createBotWithFocus). Dismissing the card
 * creates nothing — nothing is saved. */
const FOCUS_LABELS = {
  "day-to-day": "day-to-day work",
  "inbox": "inbox & email",
  "research": "research & writing",
  "everything": "a bit of everything"
};
function setChatDate() {
  const el = document.getElementById("chat-date");
  if (!el) return;
  const now = new Date();
  const date = now.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric", year: "numeric" });
  const time = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  el.textContent = "Today, " + date + " · " + time;
}
/* The pill avatar carries the bot-blob hook (data-bot-id) for
 * Builder B's living blob: the large variant, its own hue +
 * shape from blobStyleFor. A room (no single bot) leaves it
 * empty — the old amber, hue 38. */
function setChatPill(name, botId) {
  document.getElementById("chat-pill-name").textContent = name;
  const avatar = document.getElementById("chat-pill-avatar");
  avatar.textContent = voInitials(name);
  const blob = blobStyleFor(botId);
  avatar.className = "chat-avatar bot-blob bot-blob--lg blob-v" + blob.variant;
  avatar.style.setProperty("--blob-hue", blob.hue);
  if (botId) avatar.dataset.botId = botId;
  else delete avatar.dataset.botId;
  document.getElementById("message-input").placeholder = "Message " + name;
  syncBlobStates();
}
function hideOnboarding() {
  document.getElementById("onboarding-card").classList.add("hidden");
  document.getElementById("onboarding-error").textContent = "";
  document.getElementById("onboarding-rename").classList.add("hidden");
}
function openNewBotChat(name) {
  currentChar = null;
  currentRoom = null;
  pendingBotName = name;
  setChatPill(name);
  setChatDate();
  document.getElementById("messages").innerHTML = "";
  document.getElementById("presence").innerHTML = "";
  document.getElementById("typing").classList.add("hidden");
  document.getElementById("meeting-panel").classList.add("hidden");
  document.getElementById("stopped-banner").classList.add("hidden");
  document.getElementById("onboarding-card").classList.remove("hidden");
  document.getElementById("onboarding-error").textContent = "";
  document.getElementById("onboarding-rename").classList.add("hidden");
  updateComposerButtons();
  showScreen("chat");
}
function openChatForChar(c) {
  pendingBotName = null;
  hideOnboarding();
  currentChar = c.id;
  const sel = document.getElementById("character-select");
  if (sel) sel.value = c.id;
  const room = roomForChar(c.id);
  currentRoom = room ? room.id : "general";
  setChatPill(c.name, c.id);
  setChatDate();
  loadMemory();
  loadRoom();
  updateComposerButtons();
  showScreen("chat");
}
document.querySelectorAll(".onboarding-option").forEach(btn => {
  btn.addEventListener("click", () => createBotWithFocus(btn.dataset.focus));
});
document.getElementById("onboarding-dismiss").onclick = () => {
  // Choosing nothing leaves the bot uncreated — nothing is saved.
  // The chat is the landing screen now, so there is no list to
  // go back to: dismissing just stays on the chat.
  hideOnboarding();
  pendingBotName = null;
  showScreen("chat");
};
document.getElementById("onboarding-rename").onclick = () => {
  hideOnboarding();
  pendingBotName = null;
  openCreate();
  document.getElementById("create-new-bot").click();
};

/* ---- Bot creation (on focus tap) ----
 * Server first when reachable: POST /api/characters {name,
 * focus} — 201 {character}, 400/409 {error}; the server's error
 * text is shown honestly and the user can pick a different name.
 * A 409 adopts the existing bot with that name (an earlier
 * attempt may have created it before the room step failed)
 * instead of failing. When the server is unreachable — or its
 * call fails for a transport reason — the bot is created ON THE
 * DEVICE instead (same role/skills the server would assign, see
 * LocalDB.buildCharacter), so creating a bot never needs the
 * server. A 400 validation error is still shown and returns:
 * an invalid name is not created locally either. The same
 * server-first/local-fallback shape applies to the 1:1 room,
 * and the intro message is saved by persistMessage (local
 * first). A locally created bot is pushed to the server on the
 * next sync (see pushLocalToServer). */
async function createBotWithFocus(focus) {
  const name = pendingBotName;
  const errEl = document.getElementById("onboarding-error");
  const renameBtn = document.getElementById("onboarding-rename");
  if (!name) { errEl.textContent = "No name was entered — start again from +."; return; }
  const stem = localSlugify(name);
  if (!stem || stem.length > 40) {
    errEl.textContent = "That name cannot be used for a bot (it needs 1–40 letters/numbers).";
    renameBtn.classList.remove("hidden");
    return;
  }
  errEl.textContent = "Creating " + name + "…";
  renameBtn.classList.add("hidden");
  let char = null;
  let validationError = null;
  if (ServerLink.isOnline()) {
    try {
      const r = await apiFetch("/api/characters", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, focus }) });
      const data = await r.json().catch(() => null);
      if (r.status === 409) {
        const all = await apiJson("/api/characters");
        char = (all && all.characters || []).find(x => x.name.toLowerCase() === name.toLowerCase());
        if (!char) validationError = (data && data.error) || "a bot with this name already exists";
      } else if (r.status === 400) {
        validationError = (data && data.error) || ("HTTP " + r.status);
      } else if (r.ok && data && data.character) {
        char = data.character;
        LocalDB.mergeCharacters([char]);
      }
      // Any other non-OK answer (401/500/…) is a server failure,
      // not a validation error: fall through to local creation.
    } catch (e) {
      // Transport failure (fetch threw): fall through to local
      // creation. A 409-adopt failure above already set
      // validationError instead of throwing.
      char = null;
    }
  }
  if (validationError) {
    errEl.textContent = "Could not create the bot: " + validationError;
    renameBtn.classList.remove("hidden");
    return;
  }
  if (!char) {
    // Offline, or the server call failed: create on the device.
    char = LocalDB.addCharacter(LocalDB.buildCharacter(name, focus));
    errEl.textContent = ServerLink.isOnline()
      ? "Server did not answer — " + name + " was created on this device and will sync when the server is back."
      : name + " created on this device (offline) — it will sync when the server is back.";
  }
  if (!characters.some(c => c.id === char.id)) characters.push(char);
  renderCharacterList();
  renderBotList();
  const sel = document.getElementById("character-select");
  if (sel) {
    sel.innerHTML = validCharacters().map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
    sel.value = char.id;
  }
  currentChar = char.id;
  let roomCreated = false;
  if (ServerLink.isOnline()) {
    try {
      const r = await apiFetch("/api/rooms", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: char.name, members: [char.id], mode: "chat" }) });
      const data = await r.json().catch(() => null);
      if (r.ok && data && data.room) {
        LocalDB.mergeRooms([data.room]);
        rooms = LocalDB.rooms.slice();
        currentRoom = data.room.id;
        roomCreated = true;
      }
    } catch (e) { /* fall through to a local room */ }
  }
  if (!roomCreated) {
    const room = LocalDB.addRoom(LocalDB.buildRoom(char.name, [char.id], "chat"));
    rooms = LocalDB.rooms.slice();
    currentRoom = room.id;
  }
  const intro = await composeIntro(char, focus);
  try {
    await persistMessage(currentRoom, char.id, intro, "assistant");
  } catch (e) {
    errEl.textContent = "Bot and room are ready, but the intro message could not be saved: " + e.message;
    // Still open the chat — the bot exists and works.
  }
  pendingBotName = null;
  hideOnboarding();
  setChatPill(char.name, char.id);
  setChatDate();
  loadMemory();
  loadRoom();
  renderBotList();
  refreshBotPreviews();
  updateComposerButtons();
  showScreen("chat");
}

/* ---- Intro message, composed client-side from REAL data ----
 * Own words only — never the video's bot wording. Greeting +
 * focus, what the bot can see right now (real counts from the
 * deck + its own memory), three setup questions in our own
 * words, and the real current date/time from the client clock.
 * Any API that fails says so honestly in its line; no number
 * is ever invented. Persisted as a normal chat message. */
async function composeIntro(char, focus) {
  const focusLabel = FOCUS_LABELS[focus] || focus;
  const now = new Date();
  const when = now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric", year: "numeric" })
    + ", " + now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  let seenParts = [];
  let deckFailed = false, memoryFailed = false, myMemories = null;
  const totalBots = characters.length; // the new bot is already in the list
  try {
    const d = await apiJson("/api/deck");
    if (!d) throw new Error("deck unavailable");
    const counts = (d.legacy && d.legacy.counts) || {};
    seenParts.push(totalBots + (totalBots === 1 ? " bot" : " bots") + " on the team");
    const teamCount = d.memory && d.memory.team_count;
    if (typeof teamCount === "number") seenParts.push(teamCount + (teamCount === 1 ? " fact" : " facts") + " in team memory");
    if (typeof counts.threads === "number") seenParts.push(counts.threads + " threads in the live office");
    if (typeof counts.runs === "number") seenParts.push(counts.runs + " runs");
  } catch (e) { deckFailed = true; }
  try {
    const m = await apiJson("/api/memory?character=" + encodeURIComponent(char.id));
    if (!m || !Array.isArray(m.memories)) throw new Error("memory unavailable");
    myMemories = m.memories.length;
  } catch (e) { memoryFailed = true; }
  const lines = [];
  lines.push("Hi, I'm " + char.name + " — your new bot, set up for " + focusLabel + ".");
  lines.push("");
  if (deckFailed) {
    lines.push("I couldn't reach the deck just now, so I can't see the live office yet.");
  } else if (seenParts.length) {
    lines.push("Here's what I can see right now: " + seenParts.join(", ") + ".");
  } else {
    lines.push("I can see the team, but the deck gave me no counts to report.");
  }
  if (memoryFailed) {
    lines.push("I couldn't load my own memory just now.");
  } else if (myMemories === 0) {
    lines.push("I don't have any memories saved for me yet — anything lasting you tell me, I'll keep.");
  } else {
    lines.push("I already have " + myMemories + (myMemories === 1 ? " memory" : " memories") + " saved for me.");
  }
  lines.push("");
  lines.push("Before I start, three things I need from you:");
  lines.push("1. What exactly should my role cover?");
  lines.push("2. What access do I need to do that work?");
  lines.push("3. What's the first task you'd like me to take on?");
  lines.push("");
  lines.push("It's " + when + " here.");
  return lines.join("\n");
}

/* ---- Chat composer: voice circle vs send arrow ----
 * Empty input → white circle is the voice button (#voice-ptt-btn,
 * existing VoicePTT wiring). Text present → the circle becomes
 * the send arrow (submits #chat-form). There is NO far-right ■
 * button: its function in the video is unknown, so no button
 * with no real function is included. */
function updateComposerButtons() {
  const input = document.getElementById("message-input");
  const voiceBtn = document.getElementById("voice-ptt-btn");
  const sendBtn = document.getElementById("composer-send");
  const hasText = input.value.trim().length > 0;
  voiceBtn.classList.toggle("hidden", hasText);
  sendBtn.classList.toggle("hidden", !hasText);
}
document.getElementById("message-input").addEventListener("input", updateComposerButtons);

/* [+] action sheet — every action is real. */
document.getElementById("composer-plus").onclick = () => {
  document.getElementById("composer-sheet").classList.toggle("hidden");
};
document.getElementById("sheet-memory").onclick = () => {
  document.getElementById("composer-sheet").classList.add("hidden");
  showMemoryPanel();
};
document.getElementById("sheet-look").onclick = () => {
  document.getElementById("composer-sheet").classList.add("hidden");
  document.getElementById("devices-look").click();
};
document.getElementById("sheet-new-group").onclick = () => {
  document.getElementById("composer-sheet").classList.add("hidden");
  openCreate();
  document.getElementById("create-new-group").click();
};
function showMemoryPanel() {
  document.getElementById("memory-panel").classList.remove("hidden");
  document.getElementById("memory-input").focus();
}

/* Chat top bar: ☰ opens the drawer + ••• menu. There is no
 * back arrow in 0.6.0 — the chat is the landing screen, so
 * there is no list to go back to. */
document.getElementById("chat-menu-btn").onclick = (e) => {
  e.stopPropagation();
  document.getElementById("chat-menu").classList.toggle("hidden");
};
document.addEventListener("click", (e) => {
  const menu = document.getElementById("chat-menu");
  if (menu && !menu.classList.contains("hidden") && !menu.contains(e.target) && e.target.id !== "chat-menu-btn") menu.classList.add("hidden");
});
document.getElementById("chat-menu-memory").onclick = () => {
  document.getElementById("chat-menu").classList.add("hidden");
  showMemoryPanel();
};
document.getElementById("chat-menu-stop").onclick = () => {
  document.getElementById("chat-menu").classList.add("hidden");
  document.getElementById("stop-btn").click();
};
document.getElementById("chat-menu-settings").onclick = () => {
  document.getElementById("chat-menu").classList.add("hidden");
  showScreen("settings");
};

function renderPresence(events) {
  const box = document.getElementById("presence");
  box.innerHTML = (events || []).map(ev => {
    if (ev.type === "typing") return `<div class="typing">${esc(ev.character)} is typing…</div>`;
    if (ev.type === "status") return `<div class="status">${esc(ev.character)}: ${esc(ev.text)}</div>`;
    if (ev.type === "turn") return `<div class="turn">Turn: ${esc(ev.speaker)}${ev.next ? " — next: " + esc(ev.next) : ""}</div>`;
    if (ev.type === "handoff") return `<div class="handoff">Handoff: ${esc(ev.from)} → ${esc(ev.to)} — ${esc(ev.note || "")}</div>`;
    if (ev.type === "done") return `<div class="done">Done: ${esc(ev.note || "")}</div>`;
    if (ev.type === "stopped") return `<div class="stopped">${esc(ev.note || "")}</div>`;
    return "";
  }).join("");
}

/* Fill #character-select from the in-memory characters (shared by
 * loadAll and by the post-sync re-render). */
function fillCharacterSelect() {
  const sel = document.getElementById("character-select");
  if (!sel) return;
  const valid = characters.filter(c => c.status === "valid");
  sel.innerHTML = valid.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
  if (valid.length && !currentChar) currentChar = valid[0].id;
  if (currentChar) sel.value = currentChar;
  sel.onchange = () => { currentChar = sel.value; loadMemory(); };
}
function renderRoomList() {
  const el = document.getElementById("room-list");
  if (!el) return;
  el.innerHTML = rooms.map(r =>
    `<li><button data-room="${esc(r.id)}">${esc(r.name)} (${esc(r.mode)})</button></li>`).join("");
  el.querySelectorAll("[data-room]").forEach(b => b.onclick = () => { currentRoom = b.dataset.room; loadRoom(); });
}
/* After a sync adopts local ids as server ids, the selection
 * follows the new id (LocalDB already remapped rooms/messages). */
function applyIdRemaps(remaps) {
  if (!remaps) return;
  if (remaps.charRemap && remaps.charRemap[currentChar]) currentChar = remaps.charRemap[currentChar];
  if (remaps.roomRemap && remaps.roomRemap[currentRoom]) currentRoom = remaps.roomRemap[currentRoom];
}

/* ---- Offline-first boot (Goal 2) ----
 * 1. Local first: characters/rooms/messages render from the
 * on-device store before any network, so the list and chat work
 * with no server at all.
 * 2. Probe the server for real (/api/health); when it answers,
 * merge its data in (LocalDB never deletes local records) and
 * push anything created offline, then re-render. When it does
 * not answer, the local copy simply stays on screen — mail and
 * previews are skipped, not faked. */
async function loadAll() {
  LocalDB.load();
  characters = LocalDB.characters.slice();
  rooms = LocalDB.rooms.slice();
  renderCharacterList();
  renderBotList();
  fillCharacterSelect();
  renderRoomList();
  loadRoom();
  await ServerLink.probeAndRender();
  if (ServerLink.isOnline()) {
    const synced = await syncFromServer();
    applyIdRemaps(synced);
    characters = LocalDB.characters.slice();
    rooms = LocalDB.rooms.slice();
    renderCharacterList();
    renderBotList();
    fillCharacterSelect();
    renderRoomList();
    loadRoom();
    // Mail fills in afterwards without blocking the list: on any
    // failure the lines simply stay "email: unavailable".
    loadMail().then(() => {
      renderCharacterList();
      if (lastDeckCharacters.length) renderDeckCharacters(lastDeckCharacters, lastDeckDuty);
      renderBotList();
    });
  }
  renderBotList();
  refreshBotPreviews();
}

function renderMessages(msgs) {
  document.getElementById("messages").innerHTML = (msgs || []).map(m => renderMessage(m)).join("");
  const last = [...(msgs || [])].reverse().find(m => m.presence && m.presence.length);
  if (last) renderPresence(last.presence);
}

async function loadRoom() {
  const room = rooms.find(r => r.id === currentRoom) || rooms[0];
  if (!room) return;
  currentRoom = room.id;
  document.getElementById("room-title").textContent = room.name + (room.mode === "meeting" ? " — meeting room" : "");
  const panel = document.getElementById("meeting-panel");
  if (room.mode === "meeting") {
    panel.classList.remove("hidden");
    // Local rooms carry no server budget fields: derive them
    // from the local message count (budget 40, same as server).
    const used = LocalDB.messagesFor(room.id).length;
    const budget = (typeof room.budget === "number") ? room.budget : LOCAL_MEETING_BUDGET;
    const remaining = (typeof room.budget_remaining === "number") ? room.budget_remaining : Math.max(0, budget - used);
    document.getElementById("budget").textContent = `${remaining} of ${budget} messages left (${used} used)`;
    document.getElementById("turn-order").textContent = (room.members || []).join(" → ") || "no members yet";
    document.getElementById("next-speaker").textContent = room.next_speaker || "—";
    document.getElementById("decision-list").innerHTML =
      (room.decisions || []).map(d => `<li>${esc(d.decision)}${d.disputed ? " — disputed: " + esc(d.disputed) : ""}</li>`).join("") || "<li>No decisions recorded yet.</li>";
  } else panel.classList.add("hidden");
  const banner = document.getElementById("stopped-banner");
  if (room.stopped) { banner.classList.remove("hidden"); banner.textContent = "STOPPED — room is silent until the next message."; }
  else banner.classList.add("hidden");
  const char = (room.mode !== "meeting" && currentChar) ? characters.find(x => x.id === currentChar) : null;
  setChatPill(char ? char.name : room.name, char ? char.id : null);
  // Local messages render first (offline-first); when the server
  // is reachable its copy merges in and the view re-renders.
  renderMessages(LocalDB.messagesFor(currentRoom));
  if (ServerLink.isOnline()) {
    try {
      const data = await apiFetch("/api/chat?room=" + encodeURIComponent(currentRoom)).then(r => r.json());
      if (data && Array.isArray(data.messages)) {
        LocalDB.mergeMessages(currentRoom, data.messages);
        renderMessages(LocalDB.messagesFor(currentRoom));
      }
    } catch (e) { /* the local copy stays on screen */ }
  }
  loadMemory();
}

/* 0.4.0 message rendering: bot messages have NO bubble —
 * transparent background, white text, small avatar at left +
 * sender-name label above the first line. User messages keep a
 * dark-grey rounded bubble, right-aligned. */
function renderMessage(m) {
  if (m.role === "user") {
    return `<div class="msg user">${esc(m.text)}</div>`;
  }
  if (m.role === "assistant") {
    const char = characters.find(c => c.id === m.character);
    const name = char ? char.name : (m.character || "Bot");
    return `<div class="msg assistant"><span class="msg-avatar ${blobClass(m.character || "")}" style="${blobStyle(m.character || "")}" data-bot-id="${esc(m.character || "")}">${esc(voInitials(name))}</span><div class="msg-body"><span class="msg-label">${esc(name)}</span><div class="msg-text">${esc(m.text)}</div></div></div>`;
  }
  return `<div class="msg system"><strong>${esc(m.character || "system")}:</strong> ${esc(m.text)}</div>`;
}

async function loadMemory() {
  if (!currentChar) return;
  // Memory is server-only; offline the panel keeps its previous
  // content instead of throwing an unhandled rejection.
  let data = null;
  try {
    data = await apiFetch("/api/memory?character=" + encodeURIComponent(currentChar)).then(r => r.json());
  } catch (e) { return; }
  if (!data) return;
  document.getElementById("memory-list").innerHTML = (data.memories || []).map(m =>
    `<li>[${esc(m.scope)}] ${esc(m.text)}</li>`).join("") || "<li>No memories yet.</li>";
}

/* Local-first persist: the message is on the device even when
 * the server is unreachable; the server copy is best-effort
 * after that (and only when the probe says online). Returns the
 * server's {message} shape when the server took it, else the
 * local message in the same shape — callers only read
 * .message.id, which a local id satisfies. */
async function persistMessage(room, character, text, role) {
  const msg = LocalDB.addMessage(room, { room, character, text, role });
  if (ServerLink.isOnline()) {
    try {
      const r = await apiFetch("/api/messages", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ room, character, text, role }) });
      const data = await r.json().catch(() => null);
      if (r.ok && data && data.message) {
        LocalDB.adoptServerMessage(room, msg.id, data.message);
        return data;
      }
    } catch (e) { /* server copy failed; the local copy stands */ }
  }
  return { message: msg };
}

/* Cost ledger (FinOps-lite): laporkan pemakaian ke server.
 * usage datang dari respons provider (panggilan BYO yang sudah
 * terjadi, Bearer HANYA ke URL provider). Ke server lokal hanya
 * angka + nama model + id tugas/bot — TIDAK PERNAH kunci. */
async function reportUsage(bot, taskId, taskKind, providerResponse) {
  var usage = (providerResponse && providerResponse.usage) || null;
  if (!usage) return null; // provider tidak melaporkan usage: tidak dicatat
  try {
    var resp = await fetch("/api/costs/record", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        bot: bot,
        task_id: taskId,
        task_kind: taskKind || "chat",
        model: providerResponse.model || "",
        prompt_tokens: usage.prompt_tokens || 0,
        completion_tokens: usage.completion_tokens || 0
      })
    });
    if (!resp.ok) return null; // ledger gagal: jangan gagalkan chat
    return (await resp.json()).record || null;
  } catch (e) { return null; }
}

/* Gate pre-flight: panggil SEBELUM mengirim tugas ke provider.
 * Bila cap tercapai, tolak di deck dengan pesan server. Ledger
 * down / gagal fetch = jangan blokir tugas (server tetap
 * menjaga gerbangnya sendiri di POST /api/chat). */
async function checkCapBeforeTask(bot) {
  try {
    var resp = await fetch("/api/costs/cap-check?bot=" + encodeURIComponent(bot));
    if (!resp.ok) return { allowed: true };
    return await resp.json();
  } catch (e) { return { allowed: true }; }
}

document.getElementById("chat-form").onsubmit = async (e) => {
  e.preventDefault();
  const text = document.getElementById("message-input").value.trim();
  if (!text) return;
  // A bot that is still on its onboarding card does not exist
  // yet — sending would go nowhere. Say so on the card.
  if (!currentChar && pendingBotName) {
    document.getElementById("onboarding-error").textContent = "Choose what this bot should focus on first — the card above creates the bot.";
    return;
  }
  if (!currentChar) return;
  document.getElementById("message-input").value = "";
  updateComposerButtons();
  // Show the user's message at once (it is replaced by the
  // persisted copy when loadRoom() runs in the finally below),
  // then the typing indicator: a reply is in flight from here.
  document.getElementById("messages").insertAdjacentHTML("beforeend", renderMessage({ role: "user", text }));
  scrollChatToBottom(true);
  showTyping();
  try {
    // Cost gate pre-flight (FinOps-lite): cap tercapai = tugas
    // baru ditolak di deck sebelum provider dipanggil.
    const gate = await checkCapBeforeTask(currentChar);
    if (!gate.allowed) throw new Error(gate.message || "cap harian tercapai");
    // A reply is in flight from here: the Stop pill (chat top
    // bar) and the drawer row for the working bot show until the
    // finally below.
    setWorking(currentRoom, currentChar);
    const b = byo();
    if (b && b.baseUrl && b.credential) {
      // DIRECT browser -> model API call. Credential never touches /api/*.
      const savedUser = await persistMessage(currentRoom, currentChar, text, "user");
      const taskId = (savedUser && savedUser.message && savedUser.message.id) || currentRoom;
      // Vision (slice 3, BYO path): shots ride along ONLY because the
      // user pressed Look first; they go browser -> model API only.
      let content = text;
      if (pendingVision && pendingVision.images) {
        content = [{ type: "text", text }].concat(pendingVision.images.map(url => ({ type: "image_url", image_url: { url } })));
      }
      replyAbort = new AbortController();
      const token = ++replyToken;
      let bubble = null;
      let partial = "";
      let stopped = false;
      try {
        const res = await streamByoReply(b, content, (full) => {
          partial = full;
          // First token: the typing indicator hands over to the
          // streaming bubble — the reply is visibly flowing now.
          if (!bubble) { hideTyping(); bubble = createStreamingBubble(currentChar); }
          if (bubble) { bubble.textContent = full; scrollChatToBottom(false); }
        });
        if (res.streamed) {
          if (!partial) throw new Error("Model API returned no reply text.");
          // A streamed reply usually carries no usage (providers
          // only send it with stream_options); reportUsage skips
          // a null usage, so the ledger simply records nothing.
          if (res.usage) await reportUsage(currentChar, taskId, "chat", { model: b.model || "", usage: res.usage });
        } else {
          const data = res.data;
          await reportUsage(currentChar, taskId, "chat", data);
          const reply = (data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content) || "";
          if (!reply) throw new Error("Model API returned no reply text.");
          hideTyping();
          bubble = createStreamingBubble(currentChar);
          const done = bubble ? await typewriterInto(bubble, reply, token) : true;
          partial = done ? reply : (bubble ? bubble.textContent : "");
          if (!done) stopped = true; // Stop pressed mid-typewriter
        }
      } catch (err) {
        if (err && err.name === "AbortError") stopped = true; // Stop pressed mid-stream
        else throw err;
      } finally { replyAbort = null; }
      // Whatever arrived before a Stop is kept, not lost.
      if (partial) await persistMessage(currentRoom, currentChar, partial, "assistant");
      if (stopped) return; // skip the error popup below: a Stop is not a failure
    } else {
      // MOCK reply path: needs the server. Offline with no
      // BYO-API there is no reply to give — say so plainly
      // instead of failing on a network error.
      if (!ServerLink.isOnline()) throw new Error("server unreachable — the mock reply needs the server; configure BYO-API in Settings to chat offline");
      const data = await apiFetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ room: currentRoom, character: currentChar, text, vision: !!pendingVision }) }).then(r => r.json());
      if (data.error) throw new Error(data.error);
      // The mock reply arrived whole — flow it in with the
      // typewriter instead of popping it in at once. The text
      // comes from the server's own assistant_message, so what
      // streams is exactly what gets saved.
      const mockReply = (data.assistant_message && data.assistant_message.text) || "";
      if (mockReply) {
        hideTyping();
        const token = ++replyToken;
        const bubble = createStreamingBubble(currentChar);
        if (bubble) await typewriterInto(bubble, mockReply, token);
      } else {
        hideTyping();
      }
      renderPresence(data.presence);
    }
  } catch (err) {
    if (err && err.name === "AbortError") {
      // Stopped by the user (no partial text arrived) — not a
      // failure, so no error popup.
    } else {
      alert("Reply failed: " + err.message + " (BYO failures never fall back silently to mock; your credential was not sent to the local server.)");
    }
  } finally { clearWorking(); hideTyping(); pendingVision = null; document.getElementById("devices-vision-note").textContent = ""; loadRoom(); }
};

document.getElementById("stop-btn").onclick = () => {
  // Same path as the red Stop pill: abort the in-flight model
  // fetch / typewriter, hide the typing indicator, silence the
  // room, and reload — the chat menu's Stop clicks this button.
  stopWorking();
};
document.getElementById("memory-form").onsubmit = async (e) => {
  e.preventDefault();
  const text = document.getElementById("memory-input").value.trim();
  if (!text || !currentChar) return;
  document.getElementById("memory-input").value = "";
  // Memory is server-only; offline the save fails honestly
  // instead of an unhandled rejection.
  try {
    await apiFetch("/api/memory", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ character: currentChar, text }) });
  } catch (err) {
    alert("Could not save the memory: the server is unreachable.");
    return;
  }
  loadMemory();
};
document.getElementById("record-decision").onclick = async () => {
  const decision = document.getElementById("decision-text").value.trim();
  const disputed = document.getElementById("disputed-text").value.trim();
  if (!decision) return;
  try {
    await apiFetch("/api/decisions", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room: currentRoom, decision, disputed }) });
  } catch (e) {
    alert("Could not record the decision: the server is unreachable.");
    return;
  }
  loadRoom();
};
/* Room creation from the legacy controls: server first when
 * reachable, local room when offline (same fallback as the
 * create dialog). */
async function createRoomLocalFirst(name, mode) {
  if (ServerLink.isOnline()) {
    try {
      const r = await apiFetch("/api/rooms", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, members: characters.filter(c => c.status === "valid").map(c => c.id), mode }) });
      const data = await r.json().catch(() => null);
      if (r.ok && data && data.room) {
        LocalDB.mergeRooms([data.room]);
        rooms = LocalDB.rooms.slice();
        return;
      }
    } catch (e) { /* fall through to a local room */ }
  }
  LocalDB.addRoom(LocalDB.buildRoom(name, characters.filter(c => c.status === "valid").map(c => c.id), mode));
  rooms = LocalDB.rooms.slice();
}
document.getElementById("new-room").onclick = async () => {
  const name = document.getElementById("new-room-name").value.trim(); if (!name) return;
  await createRoomLocalFirst(name, "chat");
  loadAll();
};
document.getElementById("new-meeting-room").onclick = async () => {
  const name = document.getElementById("new-room-name").value.trim(); if (!name) return;
  await createRoomLocalFirst(name, "meeting");
  loadAll();
};

/* ---- Devices view (slices 2-4, character-centered) ----
 * Shots are fetched fresh from /api/devices/shot and never stored.
 * Vision rule: screenshots go to a model ONLY when BYO-API is
 * configured in this browser AND the user pressed Look before
 * sending. The local server never sees the model call or any
 * credential; for the MOCK path the server records shot metadata
 * (never image bytes) on the character's turn. */
let pendingVision = null; // {images: [dataUrl]} for BYO, {mock:true} for MOCK

async function refreshShots() {
  // Blob fetch, not <img src>: an <img> tag cannot send the token
  // header, and phone screenshots must never ride an unauthenticated
  // request. Frames are object URLs only — never stored anywhere.
  const t = Date.now();
  for (const kind of ["phone", "display"]) {
    try {
      const r = await apiFetch("/api/devices/shot?kind=" + kind + "&t=" + t);
      if (!r.ok) throw new Error("shot " + kind + ": HTTP " + r.status);
      const blob = await r.blob();
      const img = document.getElementById(kind === "phone" ? "shot-phone" : "shot-display");
      if (img._obj) URL.revokeObjectURL(img._obj);
      img._obj = URL.createObjectURL(blob);
      img.src = img._obj;
    } catch (e) { /* keep the previous frame; 401 is noted by apiFetch */ }
  }
}

async function loadDevices() {
  try {
    const d = await apiJson("/api/devices");
    if (!d) return;
    // Real working data for the bot list: `operating` is keyed by
    // character id — a bot listed there is operating a device.
    deckOperatingByChar = d.operating || {};
    renderBotList();
    const ops = Object.values(d.operating || {});
    document.getElementById("devices-operating").textContent = ops.length
      ? ops.map(o => `${o.character_name} (${o.character_role}) operating ${o.device} — ${o.action}`).join("; ")
      : "no character operating a device yet";
    const views = Object.values(d.viewing || {});
    document.getElementById("devices-viewing").textContent = views.length
      ? views.map(v => `${v.character_name} (${v.character_role}) looked at both screens`).join("; ")
      : "no look yet";
    if (!d.available) document.getElementById("devices-vision-note").textContent = "Bridge unavailable: " + (d.status && d.status.error ? d.status.error : "no bridge CLI found") + " — shots and actions need the PC bridge.";
  } catch (e) { /* devices panel stays empty offline */ }
  try {
    const f = await apiJson("/api/devices/feed");
    if (!f) return;
    document.getElementById("devices-feed").innerHTML = (f.attributed || []).map(a =>
      `<li>${esc(a.character_name)} (${esc(a.character_role)}) — ${esc(a.device)} ${esc(a.action)} ${a.ok ? "✓" : "✗ " + esc(a.error || "")}</li>`).join("") || "<li>No character device actions yet.</li>";
    document.getElementById("devices-bridge-log").innerHTML = (f.bridge_raw || []).slice(0, 20).map(e =>
      `<li>${esc(e.ts || "")} ${esc(e.action)} — direct CLI use, no character</li>`).join("") || "<li>No bridge log events yet.</li>";
  } catch (e) { /* feed stays empty offline */ }
}

const PHONE_ACTIONS = ["tap", "swipe", "keyevent", "text", "launch"];
const BROWSER_ACTIONS = ["key", "click", "mousemove", "type"];
const ACTION_PLACEHOLDERS = { tap: "tap: x y", swipe: "swipe: x1 y1 x2 y2 [ms]", keyevent: "keyevent: KEYCODE_HOME", text: "text: hello", launch: "launch: com.example.app", key: "key: Return", click: "click: x y", mousemove: "mousemove: x y", type: "type: hello" };
function refreshActionOptions() {
  const dev = document.getElementById("action-device").value;
  const acts = dev === "phone" ? PHONE_ACTIONS : BROWSER_ACTIONS;
  const sel = document.getElementById("action-name");
  sel.innerHTML = acts.map(a => `<option value="${a}">${a}</option>`).join("");
  document.getElementById("action-params").placeholder = ACTION_PLACEHOLDERS[acts[0]];
}
document.getElementById("action-device").onchange = refreshActionOptions;
document.getElementById("action-name").onchange = () => {
  document.getElementById("action-params").placeholder = ACTION_PLACEHOLDERS[document.getElementById("action-name").value] || "";
};
refreshActionOptions();

function parseActionParams(device, action, raw) {
  const t = raw.trim();
  if (["tap", "click", "mousemove"].includes(action)) {
    const p = t.split(/\s+/).map(Number); return { x: p[0], y: p[1] };
  }
  if (action === "swipe") {
    const p = t.split(/\s+/).map(Number); return { x1: p[0], y1: p[1], x2: p[2], y2: p[3], ms: p[4] || 300 };
  }
  if (action === "keyevent" || action === "key") return { key: t };
  if (action === "text" || action === "type") return { text: t };
  if (action === "launch") return { component: t };
  return {};
}

document.getElementById("devices-refresh").onclick = () => { refreshShots(); loadDevices(); };
document.getElementById("devices-look").onclick = async () => {
  if (!currentChar) { alert("Pick a character first."); return; }
  const note = document.getElementById("devices-vision-note");
  try {
    const b = byo();
    const armed = b && b.baseUrl && b.credential;
    const data = await apiFetch("/api/devices/look", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room: currentRoom, character: currentChar, model_path: armed ? "byo-armed" : "mock" }) }).then(r => r.json());
    if (data.error) throw new Error(data.error);
    const turn = data.vision_turn;
    const okShots = Object.entries(turn.shots).filter(([, s]) => s.ok).map(([k]) => k).join(" + ") || "none (bridge unavailable)";
    note.textContent = `${turn.character_name} (${turn.character_role}) looked at both screens — shots attached: ${okShots}. ${armed ? "They will ride along with your next BYO model call (browser to model API directly)." : "Recorded on this character's turn (mock path, metadata only)."}`;
    refreshShots();
    if (armed) {
      const imgs = [];
      for (const kind of ["phone", "display"]) {
        const blob = await apiFetch("/api/devices/shot?kind=" + kind + "&t=" + Date.now()).then(r => { if (!r.ok) throw new Error("shot " + kind + " failed"); return r.blob(); });
        imgs.push(await new Promise(res => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.readAsDataURL(blob); }));
      }
      pendingVision = { images: imgs };
    } else {
      pendingVision = { mock: true };
    }
    loadDevices();
  } catch (err) { note.textContent = "Look failed: " + err.message; }
};
document.getElementById("devices-act").onclick = async () => {
  if (!currentChar) { alert("Pick a character first."); return; }
  const device = document.getElementById("action-device").value;
  const action = document.getElementById("action-name").value;
  const params = parseActionParams(device, action, document.getElementById("action-params").value);
  const out = document.getElementById("devices-action-result");
  try {
    const data = await apiFetch("/api/devices/action", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ character: currentChar, device, action, params }) }).then(r => r.json());
    if (data.error) throw new Error(data.error);
    const a = data.attribution;
    out.textContent = `${a.character_name} (${a.character_role}) — ${a.device} ${a.action}: ${a.ok ? "ok" : "failed: " + (a.error || "")}`;
    refreshShots(); loadDevices();
  } catch (err) { out.textContent = "Action failed: " + err.message; }
};

/* ---- Virtual Office deck (read-only legacy feed) ----
 * The DECK button opens the live Virtual Office view: legacy
 * office state (bot names/titles, thread counts, run statuses,
 * timestamps — metadata only, never message text), devices panes,
 * presence feed, memory counts, and own-build characters. The
 * legacy feed is a subprocess SELECT-only bridge, authorized
 * 2026-10-24; this page never writes to the old system. The
 * deck polls only while it is visible, and an empty office is
 * shown as empty-but-live, never filled with invented bots. */
let deckTimer = null;
let deckAuto = false, deckAutoTimer = null;
function stopDeckAuto() {
  if (deckAutoTimer) { clearInterval(deckAutoTimer); deckAutoTimer = null; }
  deckAuto = false;
  const b = document.getElementById("deck-auto-btn");
  if (b) b.textContent = "AUTO — Live Deck";
}
function openDeck() {
  const s = document.getElementById("deck");
  s.classList.remove("hidden");
  s.scrollIntoView();
  loadDeck(); refreshDeckShots();
}
function startDeckTimer() {
  if (!deckAuto && !deckTimer) deckTimer = setInterval(() => {
    if (!document.getElementById("deck").classList.contains("hidden")) loadDeck();
  }, 20000);
}
document.getElementById("deck-btn").onclick = () => {
  openDeck();
  startDeckTimer();
};
/* AUTO button: one tap opens the live deck, forces the legacy
 * feed on, and keeps deck data AND device shots auto-refreshing
 * every 20s — no manual refresh needed. A second tap stops it. */
document.getElementById("deck-auto-btn").onclick = async () => {
  if (deckAuto) { stopDeckAuto(); return; }
  openDeck();
  await apiFetch("/api/deck/feed", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ active: true }) });
  document.getElementById("deck-feed-switch").checked = true;
  if (deckTimer) { clearInterval(deckTimer); deckTimer = null; }
  deckAuto = true;
  document.getElementById("deck-auto-btn").textContent = "AUTO ● ON — Live Deck";
  deckAutoTimer = setInterval(() => {
    if (!document.getElementById("deck").classList.contains("hidden")) { loadDeck(); refreshDeckShots(); }
  }, 20000);
};
document.getElementById("deck-refresh").onclick = () => { loadDeck(); refreshDeckShots(); };
document.getElementById("deck-feed-switch").onchange = async (e) => {
  await apiFetch("/api/deck/feed", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ active: e.target.checked }) });
  loadDeck();
};
async function refreshDeckShots() {
  const t = Date.now();
  for (const kind of ["phone", "display"]) {
    try {
      const r = await apiFetch("/api/devices/shot?kind=" + kind + "&t=" + t);
      if (!r.ok) throw new Error("shot " + kind + ": HTTP " + r.status);
      const blob = await r.blob();
      const img = document.getElementById(kind === "phone" ? "deck-shot-phone" : "deck-shot-display");
      if (img._obj) URL.revokeObjectURL(img._obj);
      img._obj = URL.createObjectURL(blob);
      img.src = img._obj;
    } catch (e) { /* keep the previous frame; 401 is noted by apiFetch */ }
  }
}
/* Status tags map REAL run statuses only — never invented
 * labels. The room avatars are living blobs with initials. */
function voTag(status) {
  switch (status) {
    case "running": return ["run", "JALAN"];
    case "queued": return ["queue", "ANTRE"];
    case "completed": return ["done", "SELESAI"];
    case "failed": return ["fail", "GAGAL"];
    case "cancelled": return ["none", "BATAL"];
    default: return ["none", "TANPA RUN"];
  }
}
function voInitials(name) {
  const parts = String(name).trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}
let deckFilter = "ALL";
function renderDeckBots(bots) {
  const list = bots || [];
  const room = document.getElementById("vo-room");
  const filters = document.getElementById("vo-filters");
  const agents = document.getElementById("vo-agents");
  filters.innerHTML = `<button data-f="ALL" class="${deckFilter === "ALL" ? "active" : ""}">SEMUA</button>` +
    list.map(b => `<button data-f="${esc(b.id)}" class="${deckFilter === b.id ? "active" : ""}">${esc(b.name.toUpperCase())}</button>`).join("");
  filters.querySelectorAll("button").forEach(btn => btn.onclick = () => { deckFilter = btn.dataset.f; renderDeckBots(list); });
  room.innerHTML = list.map((b, i) => {
    const [cls, label] = voTag(b.latest_run ? b.latest_run.status : null);
    const dim = (deckFilter !== "ALL" && deckFilter !== b.id) ? " dim" : "";
    // Live presence, real data only: a run with status "running"
    // in the deck payload means this agent is working right now —
    // the chip glows/pulses (see .vo-agent.working) and its blob
    // takes blob-working (same trigger, baked here because the
    // deck re-renders per payload). Any other status (queued,
    // completed, failed, none) stays still.
    const isWorking = !!(b.latest_run && b.latest_run.status === "running");
    return `<div class="vo-agent pos-${i % 6}${dim}${isWorking ? " working" : ""}"><div class="vo-avatar ${blobClass(b.id)}${isWorking ? " blob-working" : ""}" style="${blobStyle(b.id)}" data-bot-id="${esc(b.id)}">${esc(voInitials(b.name))}</div><div class="vo-agent-name">${esc(b.name)}</div><div class="vo-agent-title">${esc(b.title || "")}</div><span class="vo-tag ${cls}">${label}</span>${renderRoomSkillChips(b.skills)}${roomMailLineHtml(b.id)}</div>`;
  }).join("") || "<p>No live bots — the office is empty, but live.</p>";
  const shown = deckFilter === "ALL" ? list : list.filter(b => b.id === deckFilter);
  agents.innerHTML = shown.map(b => {
    const th = b.thread ? `thread: ${b.thread.messages} messages, last ${b.thread.last_activity || "—"}` : "no thread yet";
    const lr = b.latest_run ? `latest run: ${b.latest_run.status} (${b.latest_run.trigger || ""}, ${b.latest_run.updated || ""})` : "no runs yet";
    const workingMark = (b.latest_run && b.latest_run.status === "running") ? ` · <span class="vo-working-mark">● bekerja</span>` : "";
    return `<li><strong>${esc(b.name)}</strong> — ${esc(b.title || "")} · ${th} · ${lr}${workingMark}</li>`;
  }).join("") || "<li>No live bots — the office is empty, but live.</li>";
}
/* ---- Next-level Phase 1: health, data ages, Kanban, duty ----
 * Health line, per-source ages, and the Kanban board render from
 * the deck payload (d.health, d.ages, d.kanban). Ages are honest:
 * null means "no cached copy to age" (shown as —), and anything
 * older than the stale window is labelled basi (stale). The
 * verdict shown per card is the server's (c.verdict), never a
 * second copy of the rule in this file. */
function ageText(seconds, staleAfter) {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.round(seconds);
  let label;
  if (s < 60) label = s + " dtk lalu";
  else if (s < 3600) label = Math.floor(s / 60) + " mnt lalu";
  else label = Math.floor(s / 3600) + " jam lalu";
  if (staleAfter !== undefined && staleAfter !== null && seconds > staleAfter) label += " (basi)";
  return label;
}
function renderDeckHealth(health) {
  const el = document.getElementById("deck-health");
  if (!el) return;
  if (!health) { el.textContent = "Kesehatan: —"; return; }
  const level = String(health.level || "—").toUpperCase();
  const reasons = (health.reasons || []).join("; ");
  const uptime = (health.uptime_seconds !== null && health.uptime_seconds !== undefined)
    ? ` · uptime ${Math.floor(health.uptime_seconds / 60)} mnt` : "";
  el.textContent = `Kesehatan: ${level}` + (reasons ? ` — ${reasons}` : "") + uptime;
}
function renderDeckAges(ages, staleAfter) {
  const el = document.getElementById("deck-ages");
  if (!el) return;
  if (!ages) { el.textContent = "Umur data: —"; return; }
  el.textContent = "Umur data — " + Object.entries(ages).map(([k, v]) => `${k}: ${ageText(v, staleAfter)}`).join(" · ");
}
/* ---- Fase 2/3: rencana + checkpoint (plans) ----
 * GET /api/plans -> {plans, summaries}; aksi POST ->
 * {plan, summary} atau {error}. Defensive seperti panel lain:
 * endpoint belum ada / 401 / error tidak pernah merusak deck —
 * panel hanya menampilkan "belum tersedia". Status langkah
 * dirender dari server (pending/running/done/failed), tidak
 * ada salinan aturan status di file ini. */
let plansCache = [];
const PLAN_STEP_LABELS = {
  pending: "menunggu", running: "berjalan",
  done: "selesai", failed: "gagal",
};
const PLAN_STATUS_LABELS = {
  active: "aktif", paused: "jeda", done: "selesai", failed: "gagal",
};
async function planAction(url, body) {
  const d = await apiJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!d) return null;
  if (d.error) { alert(d.error); return null; }
  return d;
}
function renderPlans() {
  const wrap = document.getElementById("deck-plans");
  const note = document.getElementById("deck-plans-note");
  if (!wrap) return;
  apiJson("/api/plans").then(d => {
    if (!d || d.error) {
      if (note) note.textContent = "Rencana: belum tersedia.";
      wrap.innerHTML = "";
      return;
    }
    plansCache = d.plans || [];
    const summaries = {};
    (d.summaries || []).forEach(s => { summaries[s.id] = s; });
    if (note) {
      const active = plansCache.filter(p => p.status === "active").length;
      const paused = plansCache.filter(p => p.status === "paused").length;
      note.textContent = plansCache.length
        ? `${plansCache.length} rencana — ${active} aktif, ${paused} jeda.`
        : "Belum ada rencana.";
    }
    wrap.innerHTML = plansCache.map(p => renderPlanCard(p, summaries[p.id])).join("")
      || "<p>Belum ada rencana — buat dari tugas panjang, atau dari routine yang berjalan.</p>";
    plansCache.forEach(bindPlanCard);
  });
}
function renderPlanCard(p, s) {
  const sum = s || {};
  const pct = sum.percent_done ?? 0;
  const steps = (p.steps || []).map(st => {
    const label = PLAN_STEP_LABELS[st.status] || st.status;
    const card = st.card_id ? ` · kartu ${esc(st.card_id)}` : "";
    const note = st.note ? ` — ${esc(st.note)}` : "";
    const stepBtn = (p.status === "active" && st.status === "pending")
      ? ` <button id="plan-${esc(p.id)}-start-${esc(st.id)}">Mulai</button>` : "";
    const doneBtn = (p.status === "active" && st.status === "running")
      ? ` <button id="plan-${esc(p.id)}-done-${esc(st.id)}">Selesai</button>`
        + ` <button id="plan-${esc(p.id)}-fail-${esc(st.id)}">Gagal</button>` : "";
    return `<li><strong>${esc(st.title)}</strong> — <span class="plan-step-${esc(st.status)}">${label}</span>${card}${note}${stepBtn}${doneBtn}</li>`;
  }).join("");
  const steering = (p.steering || []).map(en =>
    `<li>${esc(en.by)}: ${esc(en.direction)} (${esc(en.ts || "")})</li>`).join("");
  const pauseBtn = p.status === "active"
    ? `<button id="plan-${esc(p.id)}-pause">Pause</button>` : "";
  const resumeBtn = p.status === "paused"
    ? `<button id="plan-${esc(p.id)}-resume">Lanjutkan</button>` : "";
  const steerBtn = (p.status === "active" || p.status === "paused" || p.status === "failed")
    ? `<button id="plan-${esc(p.id)}-steer-toggle">Steer</button>` : "";
  return `<div class="plan-card" id="plan-card-${esc(p.id)}">
    <h4>${esc(p.title)} — ${PLAN_STATUS_LABELS[p.status] || p.status}</h4>
    <p>${esc(p.assignee || "")} · ${sum.done ?? 0}/${sum.total ?? 0} langkah selesai (${pct}%)</p>
    <ol>${steps}</ol>
    ${steering ? `<h5>Arahan manusia</h5><ul>${steering}</ul>` : ""}
    <p>${pauseBtn}${resumeBtn}${steerBtn}</p>
    <div id="plan-${esc(p.id)}-steer-form" class="plan-steer hidden">
      <label>Arahan baru <input id="plan-${esc(p.id)}-steer-direction" placeholder="cth: tambah pengecekan fakta sebelum VO"></label>
      <label>Langkah baru (satu per baris, disisipkan setelah langkah berjalan) <textarea id="plan-${esc(p.id)}-steer-steps" rows="2" placeholder="Cek fakta naskah"></textarea></label>
      <button id="plan-${esc(p.id)}-steer-send">Kirim arahan</button>
    </div>
  </div>`;
}
function bindPlanCard(p) {
  const byId = id => document.getElementById(id);
  const pause = byId(`plan-${p.id}-pause`);
  if (pause) pause.onclick = async () => {
    const d = await planAction("/api/plans/pause", { plan_id: p.id });
    if (d) renderPlans();
  };
  const resume = byId(`plan-${p.id}-resume`);
  if (resume) resume.onclick = async () => {
    const d = await planAction("/api/plans/resume", { plan_id: p.id });
    if (d) renderPlans();
  };
  const toggle = byId(`plan-${p.id}-steer-toggle`);
  const form = byId(`plan-${p.id}-steer-form`);
  if (toggle && form) toggle.onclick = () => form.classList.toggle("hidden");
  const send = byId(`plan-${p.id}-steer-send`);
  if (send) send.onclick = async () => {
    const direction = byId(`plan-${p.id}-steer-direction`).value.trim();
    const steps = byId(`plan-${p.id}-steer-steps`).value
      .split("\n").map(s => s.trim()).filter(Boolean);
    if (!direction) { alert("Tulis arahannya dulu."); return; }
    if (!steps.length) { alert("Tambah minimal satu langkah baru, atau ganti judul langkah dari server."); return; }
    const d = await planAction("/api/plans/steer",
      { plan_id: p.id, direction, by: "Sam", new_steps: steps });
    if (d) renderPlans();
  };
  (p.steps || []).forEach(st => {
    const start = byId(`plan-${p.id}-start-${st.id}`);
    if (start) start.onclick = async () => {
      const d = await planAction("/api/plans/step/start",
        { plan_id: p.id, step_id: st.id });
      if (d) renderPlans();
    };
    const done = byId(`plan-${p.id}-done-${st.id}`);
    if (done) done.onclick = async () => {
      const note = prompt("Catatan penyelesaian (opsional, jadi bukti di kartu Kanban):", "") || "";
      const d = await planAction("/api/plans/step/complete",
        { plan_id: p.id, step_id: st.id, note });
      if (d) renderPlans();
    };
    const fail = byId(`plan-${p.id}-fail-${st.id}`);
    if (fail) fail.onclick = async () => {
      const note = prompt("Kenapa gagal? (dicatat di rencana + kartu Kanban):", "") || "";
      const d = await planAction("/api/plans/step/fail",
        { plan_id: p.id, step_id: st.id, note });
      if (d) renderPlans();
    };
  });
}
/* ---- Cost ledger panel (FinOps-lite, §7) ----
 * Satu fetch GET /api/costs/summary, render dari server.
 * Defensive seperti panel lain: 401 / error tidak pernah
 * merusak deck — panel hanya menampilkan "belum tersedia". */
async function renderCostsSummary() {
  const wrap = document.getElementById("deck-costs");
  const note = document.getElementById("deck-costs-note");
  if (!wrap) return;
  const s = await apiJson("/api/costs/summary");
  if (!s) {
    if (note) note.textContent = "Spend hari ini: belum tersedia.";
    wrap.innerHTML = "";
    return;
  }
  const bots = s.bots || {};
  const totals = s.totals || {};
  const rows = Object.entries(bots).map(([bot, a]) =>
    `<li>${esc(bot)} — $${Number(a.cost_usd || 0).toFixed(4)} · ${a.tasks || 0} tugas · ${(a.total_tokens || 0).toLocaleString()} token${a.price_unknown_tasks ? " · harga tak diketahui" : ""}</li>`).join("");
  const top = s.most_expensive_task;
  const topLine = top
    ? `<p>Tugas termahal: ${esc(top.bot)} / ${esc(top.task_id)} · ${esc(top.model)} · $${Number(top.cost_usd || 0).toFixed(4)}${top.price_known ? "" : " (harga tak diketahui)"}</p>`
    : "<p>Belum ada pemakaian tercatat hari ini.</p>";
  if (note) note.textContent = `Total hari ini: $${Number(totals.cost_usd || 0).toFixed(4)} · ${totals.tasks || 0} tugas · ${(totals.total_tokens || 0).toLocaleString()} token.`;
  wrap.innerHTML = (rows ? `<ul>${rows}</ul>` : "<p>Belum ada pemakaian tercatat hari ini.</p>")
    + topLine
    + (s.estimate_note ? `<p class="vo-note">${esc(s.estimate_note)}</p>` : "");
}
function renderDeckKanban(board) {
  const wrap = document.getElementById("deck-kanban");
  const note = document.getElementById("deck-kanban-note");
  if (!wrap) return;
  if (!board || !board.columns) {
    wrap.innerHTML = "";
    if (note) note.textContent = "Kanban: —";
    return;
  }
  const titles = { backlog: "Backlog", doing: "Dikerjakan", review: "Review", done: "Selesai" };
  if (note) note.textContent = board.unproven_done
    ? `${board.unproven_done} kartu selesai tanpa bukti (unproven).`
    : "Semua kartu selesai punya bukti.";
  wrap.innerHTML = Object.entries(board.columns).map(([col, cards]) => {
    const count = (board.counts && board.counts[col] !== undefined) ? board.counts[col] : cards.length;
    const items = cards.map(c => {
      const who = c.assignee ? ` — ${esc(c.assignee)}` : "";
      return `<li>${esc(c.title)}${who} <span class="kanban-verdict">${esc(c.verdict || "open")}</span></li>`;
    }).join("") || "<li>—</li>";
    return `<div class="kanban-col"><h4>${titles[col] || col} (${count})</h4><ul>${items}</ul></div>`;
  }).join("");
}
/* ---- Next-level Fase 2: computers as the core + always-approve ----
 * The computer is the product's core, so the deck headlines it: the
 * primary machine (first active), its live attached devices (phone +
 * display from the bridge, with last-shot ages), and pause/resume
 * per machine. GET /api/computers carries the merged view; a dead
 * bridge renders as "bridge unavailable + reason", never as empty.
 * The old small PERANGKAT sidebar is now a one-line summary of
 * this panel (renderComputers owns #vo-devices); the deck shot
 * panes moved INTO this panel (same ids, no duplicate).
 *
 * Always-approve toggle: contract GET/POST /api/approvals/settings
 * -> {global: bool, bots: {...}, actions: {...}}. Defensive: while
 * the endpoint does not exist (the approvals builder's part), the
 * toggle stays disabled with "belum tersedia" — never a fake on. */
function shotAgeText(shot) {
  if (!shot || shot.age_seconds === null || shot.age_seconds === undefined) return "belum pernah";
  return ageText(shot.age_seconds);
}
function renderComputers(data) {
  const primaryEl = document.getElementById("computers-primary");
  const devicesEl = document.getElementById("computers-devices");
  const listEl = document.getElementById("computers-list");
  const noteEl = document.getElementById("computers-note");
  const badgeEl = document.getElementById("computers-primary-badge");
  const sidebarEl = document.getElementById("vo-devices");
  if (!primaryEl || !devicesEl || !listEl) return;
  if (!data || !Array.isArray(data.machines)) {
    primaryEl.textContent = "Komputer: tidak bisa dibaca.";
    devicesEl.textContent = "";
    listEl.innerHTML = "";
    if (noteEl) noteEl.textContent = "GET /api/computers gagal — coba Refresh deck.";
    if (badgeEl) badgeEl.textContent = "—";
    return;
  }
  const machines = data.machines;
  const primary = machines.find(m => m.primary) || null;
  if (badgeEl) badgeEl.textContent = primary ? ("UTAMA: " + primary.label.toUpperCase()) : "TANPA MESIN AKTIF";
  if (!machines.length) {
    primaryEl.textContent = "Belum ada komputer terdaftar — daftarkan mesin pertama lewat POST /api/computers.";
    devicesEl.textContent = "";
    listEl.innerHTML = "<li>—</li>";
    if (noteEl) noteEl.textContent = "";
    if (sidebarEl) sidebarEl.textContent = "tidak ada mesin";
    return;
  }
  if (primary) {
    const age = primary.last_seen ? (Date.now() - Date.parse(primary.last_seen)) / 1000 : null;
    const caps = (primary.capabilities || []).join(", ") || "tanpa capability tercatat";
    primaryEl.innerHTML = `<strong>${esc(primary.label)}</strong> — status: ${esc(primary.status)} · terakhir terlihat: ${ageText(age)}<br>Capability: ${esc(caps)}${primary.notes ? "<br>Catatan: " + esc(primary.notes) : ""}`;
  } else {
    primaryEl.textContent = "Tidak ada mesin aktif — semua mesin dijeda atau dihapus.";
  }
  const view = primary ? primary.devices : (machines[0] ? machines[0].devices : null);
  const reason = primary ? primary.devices_reason : (machines[0] ? machines[0].devices_reason : null);
  if (!view) {
    devicesEl.textContent = "Device bridge: tidak tersedia — " + (reason || "alasan tidak diketahui") + ". Phone/display tidak bisa dibaca; bukan berarti tidak ada perangkat.";
    if (sidebarEl) sidebarEl.textContent = (primary ? primary.label + " (" + primary.status + ")" : "—") + " · bridge mati";
  } else {
    const phone = view.phone || {};
    const display = view.display || {};
    const lastShot = view.last_shot || {};
    devicesEl.innerHTML = `Phone: ${phone.connected ? "terhubung" + (phone.serial ? " (" + esc(phone.serial) + ")" : "") : "tidak terhubung"} · Display :100: ${display.running ? "jalan" : "mati"} · Mirror: ${view.mirror_running ? "jalan" : "mati"}<br>Shot terakhir — phone: ${shotAgeText(lastShot.phone)} · display: ${shotAgeText(lastShot.display)}`;
    if (sidebarEl) sidebarEl.textContent = (primary ? primary.label + " (" + primary.status + ")" : "—") + ` · phone: ${phone.connected ? "ya" : "tidak"} · display: ${display.running ? "ya" : "tidak"}`;
  }
  listEl.innerHTML = machines.map(m => {
    const tag = m.primary ? " <span class='vo-tag run'>UTAMA</span>" : "";
    const btn = (m.status === "removed") ? "" : (m.status === "paused"
      ? ` <button data-computer-resume="${esc(m.id)}">Resume</button>`
      : ` <button data-computer-pause="${esc(m.id)}">Pause</button>`);
    return `<li><strong>${esc(m.label)}</strong> — ${esc(m.status)}${tag}${btn}</li>`;
  }).join("");
  listEl.querySelectorAll("[data-computer-pause]").forEach(b => b.onclick = () => setComputerStatus(b.dataset.computerPause, "paused"));
  listEl.querySelectorAll("[data-computer-resume]").forEach(b => b.onclick = () => setComputerStatus(b.dataset.computerResume, "active"));
  if (noteEl) noteEl.textContent = machines.length + " mesin terdaftar · perangkat di atas adalah perangkat owner yang dipakai bersama (shared), bukan per-mesin.";
}
async function loadComputers() {
  try {
    const d = await apiJson("/api/computers");
    if (!d) return; // 401: apiFetch already noted the token; leave panel as-is.
    renderComputers(d);
  } catch (e) {
    const noteEl = document.getElementById("computers-note");
    if (noteEl) noteEl.textContent = "Komputer gagal dimuat: " + e.message;
  }
}
async function setComputerStatus(machineId, status) {
  try {
    const r = await apiFetch("/api/computers/status", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ machine_id: machineId, status }) });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    loadComputers();
  } catch (e) {
    const noteEl = document.getElementById("computers-note");
    if (noteEl) noteEl.textContent = "Gagal mengubah status mesin: " + e.message;
  }
}
/* Approval level: ask / edits / auto / full / custom for the
 * approval queue (replaces the old binary always-approve
 * checkbox). Contract GET/POST /api/approvals/settings
 * {mode, bots, actions, alwaysAllow, engineCaps}. Until the
 * endpoint answers, the picker is disabled and says so. A POST
 * failure reverts the select — the UI never claims a state the
 * server did not confirm. */
const APPROVAL_LEVELS = ["ask", "edits", "auto", "full", "custom"];
const APPROVAL_LEVEL_TEXT = {
  ask: "ASK — selalu tanya: tiap aksi eksternal butuh persetujuan manusia.",
  edits: "EDITS — auto hanya untuk edit file; sisanya tetap tanya.",
  auto: "AUTO — auto untuk aksi aman (baca, cari web, edit file); sisanya tetap tanya.",
  full: "FULL — auto semua aksi, seperti always-approve dulu. Antrean tetap mencatat tiap keputusan.",
  custom: "CUSTOM — per-aturan: level per-bot / per-aksi yang menentukan.",
};
let approvalSettingsAvailable = false;
let currentApprovalLevel = "ask";
async function loadApprovalSettings() {
  const select = document.getElementById("approval-level-select");
  const statusEl = document.getElementById("always-approve-status");
  const indicator = document.getElementById("always-approve-indicator");
  if (!select || !statusEl) return;
  let settings = null;
  try {
    const r = await apiFetch("/api/approvals/settings");
    if (r.status === 401) return; // token note already shown; retry later.
    if (!r.ok) throw new Error("HTTP " + r.status);
    settings = await r.json();
  } catch (e) { settings = null; }
  if (!settings || typeof settings.mode !== "string" || !APPROVAL_LEVELS.includes(settings.mode)) {
    approvalSettingsAvailable = false;
    select.disabled = true;
    select.value = "ask";
    if (indicator) indicator.classList.add("hidden");
    statusEl.textContent = "Pengaturan persetujuan belum tersedia — endpoint /api/approvals/settings belum aktif. Pemilih level nonaktif sampai builder approvals menyelesaikannya.";
    return;
  }
  approvalSettingsAvailable = true;
  currentApprovalLevel = settings.mode;
  select.disabled = false;
  select.value = settings.mode;
  if (indicator) {
    indicator.classList.remove("hidden");
    indicator.textContent = "LEVEL: " + settings.mode.toUpperCase();
  }
  statusEl.textContent = APPROVAL_LEVEL_TEXT[settings.mode] || "";
}
document.getElementById("approval-level-select").onchange = async (e) => {
  const select = e.target;
  const statusEl = document.getElementById("always-approve-status");
  const indicator = document.getElementById("always-approve-indicator");
  const previous = currentApprovalLevel;
  const want = select.value;
  try {
    const r = await apiFetch("/api/approvals/settings", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: want }) });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const settings = await r.json();
    if (typeof settings.mode !== "string" || !APPROVAL_LEVELS.includes(settings.mode)) throw new Error("unexpected response");
    currentApprovalLevel = settings.mode;
    select.value = settings.mode;
    if (indicator) {
      indicator.classList.remove("hidden");
      indicator.textContent = "LEVEL: " + settings.mode.toUpperCase();
    }
    statusEl.textContent = APPROVAL_LEVEL_TEXT[settings.mode] || "";
  } catch (err) {
    select.value = previous; // revert: the server did not confirm.
    if (indicator) {
      indicator.classList.remove("hidden");
      indicator.textContent = "LEVEL: " + previous.toUpperCase();
    }
    statusEl.textContent = "Gagal menyimpan pengaturan: " + err.message + " — level dikembalikan.";
  }
};
async function loadDeck() {
  const statusEl = document.getElementById("deck-status");
  let d;
  try {
    d = await apiJson("/api/deck");
    if (!d) { statusEl.textContent = "Save the server token above to load the deck."; return; }
    if (d.error) throw new Error(d.error);
  } catch (e) { statusEl.textContent = "Deck load failed: " + e.message; return; }
  const leg = d.legacy || {};
  const counts = leg.counts || {};
  document.getElementById("deck-feed-switch").checked = !!d.deck_active;
  document.getElementById("deck-updated").textContent = `DIPERBARUI: ${leg.fetched_at || "—"} · bots ${counts.bots ?? "—"}, threads ${counts.threads ?? "—"}, runs ${counts.runs ?? "—"}, messages ${counts.messages ?? "—"}`;
  if (leg.paused) {
    statusEl.textContent = "Live legacy feed paused — flip the switch to resume.";
    document.getElementById("vo-room").innerHTML = "<p>Feed paused.</p>";
    document.getElementById("vo-filters").innerHTML = "";
    document.getElementById("vo-agents").innerHTML = "<li>Feed paused.</li>";
    document.getElementById("deck-runs").innerHTML = "<li>Feed paused.</li>";
  } else if (!leg.ok) {
    statusEl.textContent = "Legacy feed unavailable: " + (leg.error || "unknown error");
    document.getElementById("vo-room").innerHTML = "<p>Legacy feed unavailable.</p>";
    document.getElementById("vo-filters").innerHTML = "";
    document.getElementById("vo-agents").innerHTML = "<li>Legacy feed unavailable.</li>";
    document.getElementById("deck-runs").innerHTML = "<li>Legacy feed unavailable.</li>";
  } else {
    const byId = {};
    (leg.bots || []).forEach(b => { byId[b.id] = b; });
    statusEl.textContent = `Live — fetched ${leg.fetched_at || ""} — bots ${counts.bots ?? "—"}, threads ${counts.threads ?? "—"}, runs ${counts.runs ?? "—"}, messages ${counts.messages ?? "—"}.`;
    renderDeckBots(leg.bots || []);
    document.getElementById("deck-runs").innerHTML = (leg.runs || []).map(r => {
      const bn = byId[r.botId] ? byId[r.botId].name : r.botId;
      return `<li>${esc(bn)} — ${esc(r.status)} (${esc(r.trigger || "")}, ${esc(r.updatedAt || "")})${r.error ? " — " + esc(r.error) : ""}</li>`;
    }).join("") || "<li>No runs yet.</li>";
  }
  const dev = d.devices || {};
  const ops = Object.values(dev.operating || {});
  // Same operating map as /api/devices — refresh the bot list's
  // working state from this deck load too.
  if (dev.operating) { deckOperatingByChar = dev.operating; renderBotList(); }
  const devText = (dev.available ? "Bridge up" : "Bridge unavailable")
    + (ops.length ? " — operating: " + ops.map(o => `${o.character_name} (${o.character_role}) on ${o.device}`).join("; ") : " — no character operating a device");
  document.getElementById("deck-devices-status").textContent = devText;
  // #vo-devices is owned by renderComputers (the main computers
  // panel above) — no second copy of the device line here.
  document.getElementById("vo-system").textContent = `Feed: ${d.deck_active ? "ON" : "OFF"} · fetched ${leg.fetched_at || "—"} · bridge: ${dev.available ? "up" : "down"}`;
  const activeCount = (leg.bots || []).filter(b => b.latest_run && (b.latest_run.status === "running" || b.latest_run.status === "queued")).length;
  document.getElementById("vo-stats").innerHTML = [["BOT", counts.bots], ["THREAD", counts.threads], ["RUN", counts.runs], ["PESAN", counts.messages], ["AKTIF", leg.ok ? activeCount : null]].map(([k, v]) => `<li>${k}: <strong>${v ?? "—"}</strong></li>`).join("");
  const pres = d.presence || {};
  document.getElementById("deck-feed").innerHTML = (pres.attributed || []).map(a =>
    `<li>${esc(a.character_name)} (${esc(a.character_role)}) — ${esc(a.device)} ${esc(a.action)} ${a.ok ? "✓" : "✗"}</li>`).join("") || "<li>No character device actions yet.</li>";
  document.getElementById("deck-memory").textContent = (d.memory && d.memory.team_count !== null && d.memory.team_count !== undefined)
    ? `Own-build team memory: ${d.memory.team_count} facts. Legacy memory text is never read by design.`
    : "Memory unavailable.";
  renderDeckHealth(d.health);
  renderDeckAges(d.ages, d.stale_after_seconds);
  renderDeckKanban(d.kanban);
  renderPlans();
  renderCostsSummary();
  renderDeckCharacters(d.characters || [], d.duty || {});
  loadComputers();
  // Retry the approval settings until the endpoint exists; once
  // it answers, the picker owns its own state (no re-render).
  if (!approvalSettingsAvailable) loadApprovalSettings();
  maybeRegisterPush();
}

/* ---- Push FCM (Fase 2h, PUSH-CLIENT.md) ----
 * Registrasi push — dipanggil sekali setelah deck termuat
 * (maybeRegisterPush, di akhir loadDeck). VAPID key + web config
 * diambil dari /api/push/config, bukan dari file. Token FCM
 * tidak pernah ditampilkan di UI. */
async function registerPush(characterId) {
  if (!('serviceWorker' in navigator) || !('Notification' in window)) {
    console.warn('push: browser tidak mendukung service worker/notifikasi');
    return { ok: false, reason: 'unsupported' };
  }
  if (typeof firebase === "undefined") {
    console.warn('push: Firebase SDK belum dimuat');
    return { ok: false, reason: 'firebase-sdk-missing' };
  }
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    return { ok: false, reason: 'permission-' + permission };
  }
  const configRes = await fetch('/api/push/config');
  if (!configRes.ok) {
    return { ok: false, reason: 'push config belum disajikan server' };
  }
  const config = await configRes.json();
  const vapidKey = config.vapidKey;
  if (!vapidKey) {
    return { ok: false, reason: 'VAPID key belum dikonfigurasi' };
  }
  // Compat SDK butuh default app sebelum firebase.messaging()
  // bisa dipakai; web config dari server adalah FirebaseOptions
  // untuk initializeApp (vapidKey bukan bagiannya).
  if (firebase.apps && !firebase.apps.length) {
    const appConfig = Object.assign({}, config);
    delete appConfig.vapidKey;
    firebase.initializeApp(appConfig);
  }
  const registration = await navigator.serviceWorker.register(
    '/static/firebase-messaging-sw.js'
  );
  const messaging = firebase.messaging();
  const pushToken = await firebase.messaging.getToken(messaging, {
    vapidKey: vapidKey,
    serviceWorkerRegistration: registration,
  });
  // PENTING: nama field-nya "push_token", BUKAN "token" — server
  // menolak body yang berisi field bernama "token"
  // (REJECTED_FIELDS di app/server.py).
  const res = await fetch('/api/push/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      character_id: characterId,
      device_label: navigator.userAgent.slice(0, 128),
      platform: 'web',
      push_token: pushToken,
    }),
  });
  if (!res.ok) {
    return { ok: false, reason: 'register gagal: ' + res.status };
  }
  return { ok: true };
}

// Unregister (mis. tombol "matikan notifikasi" di deck):
async function unregisterPush() {
  const registration = await navigator.serviceWorker.getRegistration(
    '/static/firebase-messaging-sw.js'
  );
  if (!registration) return { ok: true };
  const messaging = firebase.messaging();
  const pushToken = await firebase.messaging.getToken(messaging, {
    serviceWorkerRegistration: registration,
  }).catch(() => null);
  if (pushToken) {
    await fetch('/api/push/unregister', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ push_token: pushToken }),
    });
  }
  await firebase.messaging.deleteToken(messaging);
  return { ok: true };
}

/* Sekali per pemuatan halaman, sesudah deck termuat. Tanpa SDK
 * Firebase atau tanpa karakter terpilih, diam-diam dilewati —
 * push tidak pernah merusak deck. */
let pushRegisterAttempted = false;
function maybeRegisterPush() {
  if (pushRegisterAttempted) return;
  pushRegisterAttempted = true;
  if (typeof firebase === "undefined" || !currentChar) return;
  try {
    const p = registerPush(currentChar);
    if (p && typeof p.catch === "function") p.catch(e => console.warn('push: register gagal', e));
  } catch (e) { console.warn('push: register gagal', e); }
}

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

byoStatus(); authStatus(); ServerLink.startAutoProbe(); loadAll(); loadDevices(); refreshShots();
updateComposerButtons();

/* Deep link: opening with #deck jumps straight into the live
 * room scene (used by the :100 display and by bookmarks). */
if (location.hash === "#deck") showScreen("rooms");
