/* local-data.js — Goal 2 (0.5.0): LAPISAN DATA LOKAL.
 *
 * Why this file exists: the app used to read characters, rooms and
 * chat history ONLY from the PC server (/api/*). With no server the
 * list was empty, creating a bot failed, and chat could not run.
 * This layer makes the phone the source of truth for the core —
 * characters, rooms, messages — and treats the server as an
 * optional peer that enriches local data, never replaces it.
 *
 * STORAGE CHOICE — localStorage, one JSON document:
 *   key "grokbot_local_v1": {version, characters, rooms, messages}
 * - It works identically in a plain browser (Firefox) AND in the
 *   Capacitor WebView, with no plugin, no async API and no
 *   migration — the parent verifies in Firefox, so the store must
 *   not depend on native code. IndexedDB would also work in both,
 *   but its async API would have forced every render path to
 *   become async for no benefit at this size; Preferences alone
 *   would not work in Firefox at all.
 * - On Android the same key is ALSO mirrored into native
 *   Preferences by cap-bootstrap.js (MIRRORED_KEYS), so the
 *   durable copy survives a WebView data wipe; localStorage is
 *   the working copy that this file reads and writes.
 * - Bounded: at most 200 messages per room, and the whole
 *   document is trimmed to ~400 KB (oldest messages dropped
 *   first). BYO-API credentials are NOT stored here — they stay
 *   in their own localStorage key (see app.js) and are never
 *   sent to the server.
 *
 * WHAT IS LOCAL vs WHAT NEEDS THE SERVER:
 *   Local (works fully offline): bot list, create bot, create
 *   room/group, chat with a BYO-API model (browser -> model API
 *   directly), chat history, intro messages.
 *   Server-only (honest empty states offline): deck feed, live
 *   office runs, devices/shots, mail, approvals, memory, costs,
 *   plans, push. The mock reply also needs the server — without
 *   BYO-API and without a server, chat says so instead of
 *   inventing a reply.
 *
 * SYNC / MERGE RULES (syncFromServer + pushLocalToServer):
 * - Download: union by id. A server record is added when its id
 *   is unknown locally; a record the server already knows is
 *   refreshed from the server copy. A record created on this
 *   device (id starts "local-") is NEVER deleted or overwritten
 *   by a download — the server can only add to local data.
 * - Upload: a locally created character/room/message that the
 *   server has never seen is pushed when the server comes back
 *   (POST /api/characters, /api/rooms, /api/messages — the
 *   existing endpoints, unchanged). A 201 adopts the server id;
 *   a 409 adopts the existing server record with the same name.
 *   Adoption remaps the id in rooms, messages and the current
 *   selection, so nothing is duplicated and nothing is lost.
 * - Every network step is best-effort: a failed push or fetch
 *   leaves the local copy untouched and is retried on the next
 *   sync. No sync step ever throws into the UI.
 *
 * CONNECTION STATUS (ServerLink): determined by actually
 * probing GET /api/health with a 3s timeout — never guessed.
 * Any HTTP answer (even 401, which only means "reachable but
 * unauthenticated") counts as online; only a network failure
 * or timeout counts as offline. The pill is rendered by
 * renderStatus() into #conn-status (drawer) and
 * #conn-status-settings (Pengaturan). It starts as "unknown"
 * ("Memeriksa…") until the first probe answers.
 */

var LOCAL_STORE_KEY = "grokbot_local_v1";
var LOCAL_STORE_VERSION = 1;
var LOCAL_MAX_MSGS_PER_ROOM = 200;
var LOCAL_MAX_STORE_BYTES = 400 * 1024; // trim oldest messages past this
var LOCAL_MEETING_BUDGET = 40; // mirrors MEETING_BUDGET in app/chat_store.py

/* Focus -> (role, skills): the same defaults the server applies in
 * POST /api/characters (FOCUS_DEFAULTS in app/server.py), copied
 * here so a bot created offline gets the same role and skills a
 * server-created bot would. A server-created bot keeps the
 * server's own values; this table is only used for local creation. */
var LOCAL_FOCUS_DEFAULTS = {
  "day-to-day": ["Day-to-day assistant — handles the owner's daily work across the team", ["team-memory", "bot-live-presence"]],
  "inbox": ["Inbox & email assistant — triages the owner's inbox into actions", ["team-memory", "bot-live-presence"]],
  "research": ["Research & writing assistant — researches topics and drafts in the owner's voice", ["team-memory", "bot-live-presence"]],
  "everything": ["General assistant — day-to-day work, inbox, and research", ["team-memory", "bot-live-presence"]]
};

function localNowIso() { return new Date().toISOString(); }

function localSlugify(name) {
  // Mirrors _slugify_name in app/server.py.
  var stem = String(name || "").toLowerCase().replace(/ /g, "-").replace(/_/g, "-");
  stem = stem.replace(/[^a-z0-9-]/g, "").replace(/-+/g, "-").replace(/^-+|-+$/g, "");
  return stem;
}

function localNewId(prefix) {
  return prefix + "-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
}

function localIsLocalId(id) { return typeof id === "string" && id.indexOf("local-") === 0; }

/* ---- LocalDB: the on-device document + merge rules ---- */
var LocalDB = {
  characters: [],
  rooms: [],
  messages: {}, // roomId -> [message, ...] oldest first
  loaded: false,

  emptyDoc: function () {
    return { version: LOCAL_STORE_VERSION, characters: [], rooms: [], messages: {}, updatedAt: localNowIso() };
  },

  load: function () {
    var doc = null;
    try {
      var raw = localStorage.getItem(LOCAL_STORE_KEY);
      if (raw) doc = JSON.parse(raw);
    } catch (e) { doc = null; }
    if (!doc || doc.version !== LOCAL_STORE_VERSION
        || !Array.isArray(doc.characters) || !Array.isArray(doc.rooms)
        || !doc.messages || typeof doc.messages !== "object") {
      doc = this.emptyDoc();
    }
    this.characters = doc.characters;
    this.rooms = doc.rooms;
    this.messages = doc.messages;
    this.loaded = true;
    return doc;
  },

  save: function () {
    var doc = {
      version: LOCAL_STORE_VERSION,
      characters: this.characters,
      rooms: this.rooms,
      messages: this._trimmedMessages(),
      updatedAt: localNowIso()
    };
    try {
      localStorage.setItem(LOCAL_STORE_KEY, JSON.stringify(doc));
    } catch (e) {
      // Quota or unavailable storage: drop to half the messages
      // per room and retry once; a failed save never throws.
      try {
        Object.keys(doc.messages).forEach(function (rid) {
          doc.messages[rid] = doc.messages[rid].slice(-100);
        });
        localStorage.setItem(LOCAL_STORE_KEY, JSON.stringify(doc));
      } catch (e2) { /* storage unusable; in-memory copy stands */ }
    }
    this.messages = doc.messages;
  },

  _trimmedMessages: function () {
    var out = {};
    Object.keys(this.messages).forEach(function (rid) {
      var list = this.messages[rid] || [];
      out[rid] = list.slice(-LOCAL_MAX_MSGS_PER_ROOM);
    }, this);
    // Whole-document size guard: drop the oldest message anywhere
    // until the document fits, so one long room cannot evict the
    // others by itself.
    var guard = 0;
    while (JSON.stringify(out).length > LOCAL_MAX_STORE_BYTES && guard < 10000) {
      guard++;
      var oldestRoom = null, oldestAt = null;
      Object.keys(out).forEach(function (rid) {
        var list = out[rid];
        if (!list.length) return;
        var at = list[0].created_at || "";
        if (oldestAt === null || at < oldestAt) { oldestAt = at; oldestRoom = rid; }
      });
      if (!oldestRoom) break;
      out[oldestRoom] = out[oldestRoom].slice(1);
    }
    return out;
  },

  messagesFor: function (roomId) {
    var list = this.messages[roomId];
    return Array.isArray(list) ? list.slice() : [];
  },

  buildCharacter: function (name, focus) {
    var def = LOCAL_FOCUS_DEFAULTS[focus] || LOCAL_FOCUS_DEFAULTS["everything"];
    var stem = localSlugify(name);
    return {
      id: "local-" + (stem || "bot") + "-" + Math.random().toString(36).slice(2, 8),
      name: name,
      role: def[0],
      skills: def[1].slice(),
      tools: [],
      memory_scope: "private+team",
      status: "valid",
      errors: [],
      focus: focus,
      _origin: "local",
      _synced: false
    };
  },

  addCharacter: function (char) {
    if (!this.characters.some(function (c) { return c.id === char.id; })) {
      this.characters.push(char);
    }
    this.save();
    return char;
  },

  buildRoom: function (name, members, mode) {
    var room = {
      id: "local-" + (localSlugify(name) || "room") + "-" + Math.random().toString(36).slice(2, 8),
      name: name,
      mode: mode || "chat",
      members: (members || []).slice(),
      decisions: [],
      disputed: [],
      stopped: false,
      _origin: "local",
      _synced: false
    };
    if (room.mode === "meeting") {
      room.budget = LOCAL_MEETING_BUDGET;
      room.budget_remaining = LOCAL_MEETING_BUDGET;
      room.next_speaker = room.members[0] || null;
    }
    return room;
  },

  addRoom: function (room) {
    if (!this.rooms.some(function (r) { return r.id === room.id; })) {
      this.rooms.push(room);
    }
    this.save();
    return room;
  },

  addMessage: function (roomId, fields) {
    var msg = {
      id: localNewId("local-msg"),
      room: roomId,
      character: fields.character || null,
      role: fields.role,
      text: fields.text,
      created_at: localNowIso(),
      presence: fields.presence || [],
      _origin: "local",
      _synced: false
    };
    if (!Array.isArray(this.messages[roomId])) this.messages[roomId] = [];
    this.messages[roomId].push(msg);
    this.save();
    return msg;
  },

  /* A server copy of a locally saved message arrived (POST
   * /api/messages 200): adopt the server id so the next download
   * merge does not show the same message twice. */
  adoptServerMessage: function (roomId, localId, serverMsg) {
    var list = this.messages[roomId];
    if (!Array.isArray(list)) return;
    for (var i = 0; i < list.length; i++) {
      if (String(list[i].id) === String(localId)) {
        list[i].id = serverMsg.id;
        list[i].created_at = serverMsg.created_at || list[i].created_at;
        list[i]._origin = "server";
        list[i]._synced = true;
        break;
      }
    }
  },

  /* Download merge: union by id. Server records refresh or add;
   * locally created records are never touched by a download. */
  mergeCharacters: function (serverChars) {
    var changed = false;
    (serverChars || []).forEach(function (sc) {
      if (!sc || !sc.id) return;
      var idx = -1;
      for (var i = 0; i < this.characters.length; i++) {
        if (String(this.characters[i].id) === String(sc.id)) { idx = i; break; }
      }
      if (idx === -1) {
        var copy = Object.assign({}, sc, { _origin: "server", _synced: true });
        this.characters.push(copy);
        changed = true;
      } else if (this.characters[idx]._origin !== "local") {
        this.characters[idx] = Object.assign({}, sc, { _origin: "server", _synced: true });
        changed = true;
      }
    }, this);
    if (changed) this.save();
    return changed;
  },

  mergeRooms: function (serverRooms) {
    var changed = false;
    (serverRooms || []).forEach(function (sr) {
      if (!sr || !sr.id) return;
      var idx = -1;
      for (var i = 0; i < this.rooms.length; i++) {
        if (String(this.rooms[i].id) === String(sr.id)) { idx = i; break; }
      }
      if (idx === -1) {
        this.rooms.push(Object.assign({}, sr, { _origin: "server", _synced: true }));
        changed = true;
      } else if (this.rooms[idx]._origin !== "local") {
        var keepOrigin = this.rooms[idx]._origin;
        this.rooms[idx] = Object.assign({}, sr, { _origin: keepOrigin, _synced: true });
        changed = true;
      }
    }, this);
    if (changed) this.save();
    return changed;
  },

  mergeMessages: function (roomId, serverMsgs) {
    var list = Array.isArray(this.messages[roomId]) ? this.messages[roomId] : [];
    var seen = {};
    list.forEach(function (m) { seen[String(m.id)] = true; });
    var changed = false;
    (serverMsgs || []).forEach(function (sm) {
      if (!sm || sm.id === undefined || sm.id === null) return;
      if (seen[String(sm.id)]) return;
      seen[String(sm.id)] = true;
      list.push(Object.assign({}, sm, { room: roomId, _origin: "server", _synced: true }));
      changed = true;
    });
    if (changed) {
      list.sort(function (a, b) {
        return String(a.created_at || "") < String(b.created_at || "") ? -1
          : String(a.created_at || "") > String(b.created_at || "") ? 1 : 0;
      });
      this.messages[roomId] = list.slice(-LOCAL_MAX_MSGS_PER_ROOM);
      this.save();
    }
    return changed;
  },

  /* Id remap after a local record is adopted by the server:
   * rooms' member lists, messages' character fields, and the
   * message-room keys all follow the new id. Returns nothing;
   * the caller (app.js) remaps its own currentChar/currentRoom
   * from the remap object pushLocalToServer returns. */
  remapCharacterId: function (oldId, newId) {
    this.characters.forEach(function (c) { if (c.id === oldId) c.id = newId; });
    this.rooms.forEach(function (r) {
      r.members = (r.members || []).map(function (m) { return m === oldId ? newId : m; });
    });
    Object.keys(this.messages).forEach(function (rid) {
      this.messages[rid].forEach(function (m) { if (m.character === oldId) m.character = newId; });
    }, this);
    this.save();
  },

  remapRoomId: function (oldId, newId) {
    this.rooms.forEach(function (r) { if (r.id === oldId) r.id = newId; });
    if (this.messages[oldId]) {
      var moved = this.messages[oldId];
      delete this.messages[oldId];
      moved.forEach(function (m) { m.room = newId; });
      this.messages[newId] = (this.messages[newId] || []).concat(moved);
      this.messages[newId].sort(function (a, b) {
        return String(a.created_at || "") < String(b.created_at || "") ? -1 : 1;
      });
    }
    this.save();
  }
};

/* ---- ServerLink: honest connection state, probed for real ---- */
var ServerLink = {
  state: "unknown", // "unknown" | "online" | "offline"
  lastProbeAt: null,
  lastError: "",
  _timer: null,

  isOnline: function () { return this.state === "online"; },

  /* Any HTTP answer — even 401 (reachable, token missing) — is
   * online. Only a network failure or a timeout is offline. */
  probe: async function (timeoutMs) {
    var ms = timeoutMs || 3000;
    var ctrl = null, timer = null;
    try {
      if (typeof AbortController !== "undefined") {
        ctrl = new AbortController();
        timer = setTimeout(function () { try { ctrl.abort(); } catch (e) {} }, ms);
      }
      var r = await fetch("/api/health", ctrl ? { signal: ctrl.signal } : {});
      this.state = "online";
      this.lastError = "";
    } catch (e) {
      this.state = "offline";
      this.lastError = (e && e.name === "AbortError") ? "probe timed out" : String((e && e.message) || e);
    } finally {
      if (timer) clearTimeout(timer);
      this.lastProbeAt = localNowIso();
    }
    this.renderStatus();
    return this.state;
  },

  probeAndRender: async function () {
    this.renderStatus();
    await this.probe();
  },

  startAutoProbe: function () {
    var self = this;
    if (this._timer) return;
    if (typeof window !== "undefined" && window.addEventListener) {
      window.addEventListener("online", function () { self.probe(); });
      window.addEventListener("offline", function () { self.state = "offline"; self.renderStatus(); });
    }
    if (typeof setInterval !== "undefined") {
      this._timer = setInterval(function () { self.probe(); }, 60000);
    }
  },

  statusText: function () {
    if (this.state === "online") return "● Terhubung ke server";
    if (this.state === "offline") return "● Lokal — server tidak terjangkau";
    return "● Memeriksa koneksi…";
  },

  renderStatus: function () {
    var text = this.statusText();
    var ids = ["conn-status", "conn-status-settings"];
    for (var i = 0; i < ids.length; i++) {
      var el = null;
      try { el = document.getElementById(ids[i]); } catch (e) { el = null; }
      if (!el) continue;
      el.textContent = text;
      if (el.classList) {
        el.classList.remove("online", "offline", "unknown");
        el.classList.add(this.state);
      }
    }
  }
};

/* ---- Sync: download-merge + push-up of local creations ----
 * Uses apiFetch (app.js) for the auth header when it exists, and
 * falls back to plain fetch. Never throws: every step is guarded
 * so a failed sync leaves local data exactly as it was. */
async function syncFromServer() {
  if (!ServerLink.isOnline()) return { ok: false, reason: "offline", charRemap: {}, roomRemap: {} };
  var remaps = { charRemap: {}, roomRemap: {} };
  try {
    var fetchJson = (typeof apiJson === "function")
      ? apiJson
      : async function (url, opts) {
          var r = await fetch(url, opts);
          try { return await r.json(); } catch (e) { return null; }
        };
    var ch = await fetchJson("/api/characters");
    if (ch && Array.isArray(ch.characters)) LocalDB.mergeCharacters(ch.characters);
    var rm = await fetchJson("/api/rooms");
    if (rm && Array.isArray(rm.rooms)) LocalDB.mergeRooms(rm.rooms);
    // Messages, per room known locally (server rooms included).
    var roomIds = LocalDB.rooms.map(function (r) { return r.id; });
    for (var i = 0; i < roomIds.length; i++) {
      try {
        var data = await fetchJson("/api/chat?room=" + encodeURIComponent(roomIds[i]));
        if (data && Array.isArray(data.messages)) LocalDB.mergeMessages(roomIds[i], data.messages);
      } catch (e) { /* this room's server copy stays unmerged */ }
    }
    var pushed = await pushLocalToServer(fetchJson);
    remaps.charRemap = pushed.charRemap;
    remaps.roomRemap = pushed.roomRemap;
    LocalDB.save();
    return { ok: true, charRemap: remaps.charRemap, roomRemap: remaps.roomRemap };
  } catch (e) {
    return { ok: false, reason: String((e && e.message) || e), charRemap: remaps.charRemap, roomRemap: remaps.roomRemap };
  }
}

async function pushLocalToServer(fetchJson) {
  var charRemap = {}, roomRemap = {};
  var postJson = (typeof apiFetch === "function") ? apiFetch : fetch;
  async function post(url, body) {
    var r = await postJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    var data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    return { status: r.status, ok: r.ok, data: data };
  }

  // 1. Locally created characters first (rooms reference them).
  var localChars = LocalDB.characters.filter(function (c) { return c._origin === "local" && !c._synced; });
  for (var i = 0; i < localChars.length; i++) {
    var lc = localChars[i];
    try {
      var res = await post("/api/characters", { name: lc.name, focus: lc.focus || "everything" });
      var adopted = null;
      if (res.ok && res.data && res.data.character) {
        adopted = res.data.character;
      } else if (res.status === 409) {
        var all = await fetchJson("/api/characters");
        var found = (all && all.characters || []).find(function (x) {
          return String(x.name || "").toLowerCase() === String(lc.name || "").toLowerCase();
        });
        if (found) adopted = found;
      }
      if (adopted) {
        var oldId = lc.id;
        LocalDB.characters = LocalDB.characters.filter(function (c) { return c.id !== oldId; });
        LocalDB.mergeCharacters([adopted]);
        LocalDB.remapCharacterId(oldId, adopted.id);
        charRemap[oldId] = adopted.id;
      }
    } catch (e) { /* stays unsynced; retried on the next sync */ }
  }

  // 2. Locally created rooms (member ids already remapped above).
  var localRooms = LocalDB.rooms.filter(function (r) { return r._origin === "local" && !r._synced; });
  for (var j = 0; j < localRooms.length; j++) {
    var lr = localRooms[j];
    try {
      var rr = await post("/api/rooms", {
        name: lr.name,
        members: (lr.members || []).map(function (m) { return charRemap[m] || m; }),
        mode: lr.mode || "chat"
      });
      if (rr.ok && rr.data && rr.data.room) {
        var oldRoomId = lr.id;
        LocalDB.rooms = LocalDB.rooms.filter(function (r) { return r.id !== oldRoomId; });
        LocalDB.mergeRooms([rr.data.room]);
        LocalDB.remapRoomId(oldRoomId, rr.data.room.id);
        roomRemap[oldRoomId] = rr.data.room.id;
      }
    } catch (e) { /* stays unsynced; retried on the next sync */ }
  }

  // 3. Locally created messages in rooms the server now knows.
  var serverRoomIds = {};
  LocalDB.rooms.forEach(function (r) { if (r._origin === "server" || r._synced) serverRoomIds[r.id] = true; });
  var roomIds = Object.keys(LocalDB.messages);
  for (var k = 0; k < roomIds.length; k++) {
    var roomId = roomIds[k];
    var targetRoom = roomRemap[roomId] || roomId;
    if (!serverRoomIds[targetRoom]) continue;
    var list = LocalDB.messages[roomId] || [];
    for (var m = 0; m < list.length; m++) {
      var msg = list[m];
      if (msg._origin !== "local" || msg._synced) continue;
      try {
        var mr = await post("/api/messages", {
          room: targetRoom,
          character: charRemap[msg.character] || msg.character,
          text: msg.text,
          role: msg.role
        });
        if (mr.ok && mr.data && mr.data.message) {
          LocalDB.adoptServerMessage(roomId, msg.id, mr.data.message);
        }
      } catch (e) { /* stays unsynced; retried on the next sync */ }
    }
  }
  LocalDB.save();
  return { charRemap: charRemap, roomRemap: roomRemap };
}
