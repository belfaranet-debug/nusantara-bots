# deck.patch.md — panel Rencana + Checkpoint, untuk koordinator

Patch doc: JANGAN edit `app/static/index.html` atau
`app/static/app.js` dari dokumen ini. Koordinator yang menempel
potongan di bawah (builder lain sedang mengedit file itu).

Panel ini menampilkan rencana (`plans/plan.py`) di deck:
daftar langkah berstatus + tombol Pause / Resume / Steer di
tengah jalan. Semua id unik per rencana (`plan-<id>-...`).
Semua teks dari server di-escape lewat `esc()` yang sudah ada.
Bila endpoint `/api/plans` belum ada (server belum ditempel),
panel menampilkan "belum tersedia" — tidak pernah data palsu.

Kontrak endpoint: lihat `plans/server-wiring.patch.md`.
Ringkasan: `GET /api/plans` -> `{plans, summaries}`;
`POST /api/plans/pause|resume|steer`, `/api/plans/step/start`,
`/api/plans/step/complete`, `/api/plans/step/fail` ->
`{plan, summary}`; error -> `{error}` + 400/404.

## 1. HTML — di `index.html`, dalam `#deck .vo-main`, tepat setelah blok Kanban

```html
<h3 class="vo-h">Rencana — tugas panjang</h3>
<p id="deck-plans-note" class="status"></p>
<div id="deck-plans"></div>
```

(Penanda yang sudah ada untuk jangkar tempel: `<div id="deck-kanban"></div>`
di bawah `<h3 class="vo-h">Papan Kanban</h3>`.)

## 2. JS — di `app.js`, dekat `renderDeckKanban`

Tempel pemanggilan ini di akhir pemuatan deck, setelah
`renderDeckKanban(d.kanban);`:

```js
renderPlans();
```

Lalu tempel fungsi-fungsi di bawah. Pola fetch mengikuti
`apiJson`/`apiFetch` yang sudah ada (token lewat header,
401 -> null, render lama dibiarkan utuh).

```js
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
      { plan_id: p.id, direction, by: "the owner", new_steps: steps });
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
```

Catatan untuk koordinator:

* `by: "the owner"` pada steer di atas adalah placeholder pemilik
  deck — ganti dengan nama pengguna yang sebenarnya bila deck
  sudah mengenal identitas pengguna; jangan invent identitas
  lain.
* Tombol "Ganti judul langkah" (steer `replace`) sengaja
  tidak dibuatkan UI di patch ini: steer dari deck menambah
  langkah baru; penggantian judul dilakukan lewat API
  (`POST /api/plans/steer` dengan `replace`) sampai ada
  kebutuhan UI-nya. Kontraknya sudah ada di server.
* Kelas CSS yang dipakai: `.plan-card`, `.plan-steer`,
  `.plan-step-pending|running|done|failed` — belum ada di
  `style.css`; tambahkan pewarnaan status seperlunya, atau
  biarkan inherit dari `.vo-panel` bila koordinator mau
  panel polos dulu.
```

## 3. README singkat modul

Tempel juga ringkasan modul di `plans/README.md`? Sudah dibuat
oleh builder (file ini bagian dari paket plans/ — lihat
`plans/README.md`).
