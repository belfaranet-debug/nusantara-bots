# server-wiring.patch.md — costs (FinOps-lite), untuk koordinator

Patch doc: JANGAN edit `app/server.py` atau `app/static/` dari
dokumen ini. Koordinator yang menempel potongan di bawah ke
`server.py` / `app.js` (builder lain sedang mengedit file itu).

## 0. Invariant — kunci tidak pernah ke server

Client membaca `usage` dari respons provider (panggilan yang
browser SUDAH lakukan dengan kredensial BYO, Bearer HANYA ke
URL provider — pola yang sama dengan `app/static/voice.js`).
Yang dikirim ke `/api/costs/record` HANYA angka + nama model +
id tugas/bot. Body record TIDAK BOLEH berisi field kredensial
(`api_key`, `secret`, `token`, `authorization`, ...) — server
menolaknya lewat `REJECTED_FIELDS` sebelum disimpan.

## 1. Impor (di dekat impor `approvals` / `office`)

```python
from costs import CostLedger  # noqa: E402
```

## 2. State (di `AppState.__init__`, dekat `self.approvals`)

```python
# Next-level Phase 2/3: cost ledger / spend cap per bot.
self.costs = costs if costs is not None else CostLedger()
```

(`costs` sebagai parameter `__init__` opsional, seperti
`kanban`/`approvals`, supaya tes bisa menyuntik temp store.)

## 3. Endpoint GET baru (di `do_GET`, dekat `/api/approvals`)

### GET /api/costs/summary?bot=&day=

Ringkasan spend satu hari UTC (default hari ini). `bot`
opsional — bila diisi, hanya bot itu yang diringkas.

```python
elif path == "/api/costs/summary":
    bot = (query.get("bot") or [None])[0]
    day = (query.get("day") or [None])[0]
    self._send_json(state.costs.daily_summary(day=day, bot=bot))
```

Response 200:

```json
{
  "day": "2026-10-25",
  "bots": {
    "yt-scriptwriter": {
      "prompt_tokens": 1200, "completion_tokens": 300,
      "total_tokens": 1500, "cost_usd": 0.00036,
      "tasks": 1, "price_unknown_tasks": 0
    }
  },
  "totals": {
    "prompt_tokens": 1200, "completion_tokens": 300,
    "total_tokens": 1500, "cost_usd": 0.00036,
    "tasks": 1, "price_unknown_tasks": 0
  },
  "most_expensive_task": {
    "id": "cost-1", "bot": "yt-scriptwriter", "task_id": "card-12",
    "task_kind": "chat", "model": "gpt-4o-mini",
    "prompt_tokens": 1200, "completion_tokens": 300,
    "total_tokens": 1500, "cost_usd": 0.00036,
    "price_known": true, "day": "2026-10-25",
    "recorded_at": "2026-10-25T06:40:00+00:00"
  },
  "estimate_note": "ESTIMASI: harga per-1M token dari tabel konfigurabel (defaults: tarif publik per Okt 2026). Provider BYO bisa menagih berbeda — angka cost_usd di ledger adalah perkiraan, bukan tagihan."
}
```

`most_expensive_task` adalah record tunggal dengan `cost_usd`
tertinggi hari itu (seri → token terbanyak); `null` bila belum
ada record hari itu. Bila `price_known` false, deck tampilkan
"harga tak diketahui" di samping tokennya.

### GET /api/costs/cap-check?bot=

Gate pre-flight: bolehkah bot ini memulai tugas baru?

```python
elif path == "/api/costs/cap-check":
    bot = (query.get("bot") or [None])[0]
    if not bot:
        self._send_json({"error": "bot is required"}, status=400)
        return
    try:
        self._send_json(state.costs.check_cap(bot))
    except ValueError as e:
        self._send_json({"error": str(e)}, status=400)
```

Response 200 (selalu 200 — ini query, bukan error):

```json
{
  "bot": "yt-scriptwriter", "day": "2026-10-25",
  "cap_usd": 2.0, "spent_usd": 2.0, "remaining_usd": 0.0,
  "allowed": false,
  "message": "cap harian tercapai untuk yt-scriptwriter: sudah $2.0000 dari cap harian $2.0000"
}
```

Bila tidak ada cap: `"cap_usd": null, "remaining_usd": null,
"allowed": true, "message": "tidak ada cap harian untuk bot ini"`.

## 4. Endpoint POST baru (di `do_POST`)

### POST /api/costs/record

Body: `{bot, task_id, model, prompt_tokens, completion_tokens,
task_kind?}` — HANYA angka + nama model + id. `task_kind`
opsional (mis. `"chat"`, `"voice"`, `"stt"`, `"tts"`).

```python
elif path == "/api/costs/record":
    # Body: angka pemakaian + nama model + id tugas/bot SAJA.
    # Tidak pernah ada kredensial di sini (REJECTED_FIELDS).
    data = self._allow(obj, ["bot", "task_id", "model",
                             "prompt_tokens", "completion_tokens",
                             "task_kind"])
    try:
        record = state.costs.record(
            data.get("bot"), data.get("task_id"), data.get("model"),
            data.get("prompt_tokens"), data.get("completion_tokens"),
            task_kind=data.get("task_kind") or "")
    except ValueError as e:
        self._send_json({"error": str(e)}, status=400)
        return
    self._send_json({"record": record}, status=201)
```

Response 201: `{"record": {...record seperti di §3...}}`.
400 bila field wajib kosong / token negatif / bukan integer.
CATATAN: endpoint ini TIDAK menolak atas dasar cap — record
adalah fakta spend yang sudah terjadi (provider sudah
menagihnya). Gate cap ada di §5, SEBELUM tugas dimulai.

### POST /api/costs/caps — set / hapus cap (jalur manusia)

Body: `{bot, cap_usd}` — `cap_usd` angka > 0, atau `null`
untuk menghapus cap bot itu (kembali unlimited / global).

```python
elif path == "/api/costs/caps":
    data = self._allow(obj, ["bot", "cap_usd"])
    try:
        caps = state.costs.config.set_cap(
            data.get("bot"), data.get("cap_usd"))
    except ValueError as e:
        self._send_json({"error": str(e)}, status=400)
        return
    self._send_json({"caps": caps,
                     "global_cap_usd": state.costs.config.global_cap()})
```

Response 200: `{"caps": {"yt-scriptwriter": 2.0},
"global_cap_usd": null}`.

## 5. Hook: gate cap SEBELUM tugas dimulai

Di path mana pun tugas bot dimulai (pengiriman pesan deck /
eksekusi tugas), panggil gate SEBELUM memanggil provider — cap
yang tercapai menolak tugas baru dengan pesan jelas:

```python
gate = state.costs.check_cap(bot_id)
if not gate["allowed"]:
    self._send_json({"error": gate["message"]}, status=429)
    return
```

Status 429 (bukan 403): cap harian adalah batas laju
sementara — besok (hari UTC baru) tugas boleh jalan lagi.

## 6. Client deck — snippet untuk `app/static/app.js`

Tempel di dekat path kirim-pesan deck (sesudah respons
`chat/completions` provider diterima, SEBELUM / BERSAMAAN
dengan persist ke server lokal). TIDAK ADA kredensial di
snippet ini — hanya `usage` dari respons provider.

```js
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
 * Bila cap tercapai, tolak di deck dengan pesan server. */
async function checkCapBeforeTask(bot) {
  var resp = await fetch("/api/costs/cap-check?bot=" + encodeURIComponent(bot));
  if (!resp.ok) return { allowed: true }; // ledger down: jangan blokir tugas
  return await resp.json();
}
```

Contoh pemakaian di path kirim-pesan (sketsa, sesuaikan dengan
nama fungsi deck yang ada):

```js
var gate = await checkCapBeforeTask(botId);
if (!gate.allowed) { showError(gate.message); return; } // "cap harian tercapai ..."
var providerResp = await callProviderChat(...); // panggilan BYO yang sudah ada
await reportUsage(botId, taskId, "chat", providerResp);
```

Untuk voice (`voice.js`): `askModel` yang di-inject deck
adalah jalur yang sama — `reportUsage(botId, taskId, "voice",
providerResp)` dipanggil di sana, bukan di `voice.js`
(`voice.js` tidak fetch ke `/api/*` sama sekali).

## 7. Deck — panel ringkasan spend

Panel kecil "Spend hari ini" (di samping board/kanban).
Satu fetch, render dari `GET /api/costs/summary`:

```js
async function renderCostsSummary() {
  var resp = await fetch("/api/costs/summary");
  if (!resp.ok) return;
  var s = await resp.json();
  // s.bots: {bot: {total_tokens, cost_usd, tasks, price_unknown_tasks}}
  // s.totals: agregat semua bot; s.most_expensive_task: tugas termahal
  // s.estimate_note: tampilkan sebagai footnote "ESTIMASI ..."
}
```

Tampilkan: spend hari ini per bot (`$cost_usd`, `tasks` tugas),
total hari ini, dan tugas termahal (`bot` / `task_id` /
`model` / `$cost_usd`). Bila `price_unknown_tasks > 0`,
tampilkan "harga tak diketahui" untuk bot itu.

## 8. Verifikasi sesudah ditempel

```bash
python3 -m unittest discover -s costs/tests -v
python3 -m unittest discover -s app/tests -v
# Tanpa cap diatur: GET /api/costs/cap-check?bot=X -> allowed true,
# cap_usd null (unlimited).
# POST /api/costs/record dengan body berisi field "token" ->
# ditolak server (REJECTED_FIELDS), record tidak tersimpan.
```
