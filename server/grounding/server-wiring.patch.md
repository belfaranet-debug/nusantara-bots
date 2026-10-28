# Server wiring patch — grounding / Work IQ-lite (Fase 2)

**For the coordinator to paste into `app/server.py`. This file is
the patch; `server.py` itself was NOT edited by this builder.**

Module (already built, tested, in the repo root):

- `grounding/` — `gather(task_text, sources, per_source_cap=5)`,
  `render_bundle_text(bundle)`, plus adapters
  `make_memory_reader(store, bot)`, `make_kanban_reader(store)`,
  `make_mail_reader(items_fn)`.

Pure stdlib, deterministic (keyword/phrase match + recency),
no network, no model, no writes. Grounding never sends, never
creates cards, never edits memory — it only READS the three
sources and returns a bundle.

---

## Endpoint yang diusulkan

### `POST /api/tasks/ground`

**Request** (JSON):

```json
{
  "task": "Tulis naskah Wali Songo episode 3",
  "bot": "chief",
  "cap": 5
}
```

- `task` (wajib, string non-kosong): tugas yang mau di-ground.
- `bot` (opsional, string): karakter yang meminta grounding.
  Dipakai untuk membaca scope privat `private:<bot>` di memory
  (selain scope `team` yang selalu dibaca). Bila diisi, harus
  karakter yang valid (`state.find_character(bot)`); bila tidak
  dikenal, kembalikan 400 `unknown character` — pola yang sama
  dengan `POST /api/kanban/cards`.
- `cap` (opsional, int 1–10, default 5): batas item per sumber.

**Response** (200): context bundle, persis bentuk yang
dikembalikan `gather()`:

```json
{
  "kind": "context-bundle",
  "task": "Tulis naskah Wali Songo episode 3",
  "generated_at": "2026-10-25T06:40:00+00:00",
  "memory": [
    {
      "source": "memory",
      "ref": "memory:12",
      "score": 0.85,
      "reasons": [
        "kata kunci cocok: wali, songo (2/4)",
        "frasa cocok: \"wali songo\"",
        "baru: 2 hari lalu"
      ],
      "excerpt": "Tema minggu ini Wali Songo sebagai metode dakwah...",
      "created_at": "2026-10-23T10:00:00+00:00",
      "extra": {"scope": "team", "author": "chief"}
    }
  ],
  "mail": [],
  "kanban": [
    {
      "source": "kanban",
      "ref": "kanban:card-4",
      "score": 0.65,
      "reasons": ["kata kunci cocok: naskah, wali (2/4)"],
      "excerpt": "Naskah episode 2 Wali Songo — revisi closing...",
      "created_at": "2026-10-24T09:00:00+00:00",
      "extra": {"status": "doing", "assignee": "chief", "priority": 2,
                "origin": {"kind": "manual"}}
    }
  ],
  "counts": {"memory": 1, "mail": 0, "kanban": 1},
  "sources_available": {"memory": true, "mail": true, "kanban": true},
  "errors": {"memory": null, "mail": null, "kanban": null}
}
```

**Error:**

- 400 `need a non-empty task` — `task` kosong/bukan string.
- 400 `unknown character` — `bot` tidak dikenal.
- 400 `cap must be 1..10` — `cap` di luar rentang.
- Sumber yang gagal dibaca TIDAK membuat endpoint gagal:
  item sumber itu kosong, `sources_available[<sumber>]` = false,
  dan `errors[<sumber>]` berisi pesannya (pola "gagal harus
  terlihat gagal, bukan kosong" — sama seperti `/api/health`).

---

## 1. Impor (tambahkan di blok impor, setelah `from office.kanban import KanbanStore`)

```python
from grounding.context import (gather, make_kanban_reader,  # noqa: E402
                               make_mail_reader,  # noqa: E402
                               make_memory_reader)  # noqa: E402
from memory.store import MemoryStore  # noqa: E402
```

`MemoryStore` sudah diimpor di `server.py` hari ini (dipakai para
handler) — tambahkan hanya bila belum ada. `grounding/` duduk di
repo root seperti `memory/` dan `office/`, jadi impor ini bekerja
dengan `sys.path` yang sama seperti impor-impor existing.

## 2. `AppState.__init__` — simpan daftar item mail terakhir (opsional, seam mail)

Anchor — langsung setelah baris ini:

```python
        self.mail = mail if mail is not None else MailClient()
```

Paste:

```python
        # Grounding (Fase 2): mail items yang sudah pernah
        # diambil server untuk triage, supaya grounding bisa
        # menarik mail relevan tanpa mengambil jalur baru.
        # Bridge mail hanya memberi hitungan inbox (lihat
        # app/mail_client.py), bukan isi — jadi sumber mail di
        # grounding HANYA berisi item yang sudah ada di sini.
        # Kosong = sumber mail kosong, bukan error.
        self.recent_mail_items = []
```

Handler triage yang sudah ada (`POST /api/triage/...` dari patch
use-case) menambahkan tiap item mail yang ia proses ke
`state.recent_mail_items` (cap 200, FIFO, hapus duplikat per
`id`/`message_id`). Bila patch use-case belum mendarat, lewati
langkah ini — grounding tetap jalan dengan sumber mail kosong.

## 3. Handler `POST /api/tasks/ground` (di `do_POST`, sejajar handler POST lain)

Anchor — di dekat `elif path == "/api/kanban/cards":`. Paste:

```python
            elif path == "/api/tasks/ground":
                # Grounding Work IQ-lite (Fase 2): tarik konteks
                # relevan (memory tim + mail masuk + kartu Kanban
                # berjalan) SEBELUM rencana dibuat. Murni baca:
                # tidak mengirim, tidak membuat kartu, tidak
                # mengubah memory.
                data = self._allow(obj, ["task", "bot", "cap"])
                task = data.get("task")
                if not isinstance(task, str) or not task.strip():
                    self._send_json(
                        {"error": "need a non-empty task"}, status=400)
                    return
                bot = data.get("bot")
                if bot is not None:
                    bot = str(bot).strip() or None
                    if bot is not None and \
                            state.find_character(bot) is None:
                        self._send_json({"error": "unknown character"},
                                        status=400)
                        return
                cap = data.get("cap", 5)
                if not isinstance(cap, int) or isinstance(cap, bool) \
                        or cap < 1 or cap > 10:
                    self._send_json({"error": "cap must be 1..10"},
                                    status=400)
                    return
                # MemoryStore per-request: koneksi sqlite terikat
                # thread pembuatnya (pola yang sama dengan handler
                # lain di server ini).
                with MemoryStore(state.memory_db) as store:
                    bundle = gather(
                        task.strip(),
                        sources={
                            "memory": make_memory_reader(store, bot=bot),
                            "mail": make_mail_reader(
                                lambda: list(state.recent_mail_items)),
                            "kanban": make_kanban_reader(state.kanban),
                        },
                        per_source_cap=cap)
                self._send_json(bundle)
```

Catatan:

- `state.memory_db` sudah ada di `AppState` (Fase 1); tidak ada
  parameter `__init__` baru.
- `state.recent_mail_items` hanya dibaca di sini; bila atribut
  itu belum ada (langkah 2 dilewati), ganti lambda dengan
  `lambda: []` dan sumber mail selalu kosong.
- Tidak ada autentikasi baru: endpoint POST ini lewat gate token
  yang sama dengan endpoint POST lain di server.

---

## Integrasi dengan `plans/` (Fase 3) — grounding jalan SEBELUM rencana

Urutan yang dikunci di Fase 3 (modul `plans/` belum ada; ini
kontraknya):

1. **Ground dulu.** Plans/ memanggil `gather(task, sources,
   per_source_cap=cap)` dengan reader yang sama seperti handler
   di atas (atau memanggil `POST /api/tasks/ground` bila plans/
   berjalan sebagai klien HTTP — pilih satu, jangan dua-duanya).
2. **Tempel bundle ke prompt rencana.** `render_bundle_text(bundle)`
   menghasilkan blok teks `# Konteks tugas (grounding otomatis)`
   yang diletakkan DI ATAS teks tugas saat plans/ meminta draf
   rencana ke karakter. Rencana ditulis dengan konteks itu di
   depan mata — bukan dari nol.
3. **Simpan jejak grounding di rencana.** Record rencana menyimpan
   `grounding_refs`: daftar `ref` item bundle yang dipakai
   (mis. `["memory:12", "kanban:card-4"]`), supaya rencana bisa
   diaudit balik ke sumbernya. Item bundle yang TIDAK dipakai
   tidak perlu dicatat — yang dicatat hanya yang memengaruhi
   rencana.
4. **Grounding tidak menggantikan rencana.** Bila bundle kosong
   (tidak ada yang relevan), plans/ tetap jalan dengan tugas
   mentah dan mencatat `grounding_refs: []` + `grounded: false`
   — kosong itu fakta, bukan alasan menahan rencana.

## Integrasi dengan triage + pulse (Fase 2, sudah ada)

- **Inbox-triage (`app/triage.py` + pack `inbox-triage`):**
  grounding MEMAKAI bentuk item mail yang sama
  (`subject`/`snippet`/`from`/`id`) dan TIDAK mengubah
  `classify_mail_item` / `mail_to_card_draft`. Arah datanya
  satu arah: triage mengisi `state.recent_mail_items` (langkah
  2), grounding membacanya. Kartu Kanban yang lahir dari triage
  (`origin.kind == "mail"`) otomatis ikut ter-ground lewat
  reader Kanban — mail yang sudah jadi tugas tidak hilang dari
  konteks, ia muncul sebagai kartu berjalan.
- **Weekly pulse (`app/pulse.py` + pack `weekly-pulse`):**
  pulse TIDAK memakai grounding (draf mingguan adalah ringkasan
  data, bukan rencana). Yang dibagi hanya sumbernya: Kanban
  board, hitungan memory, ringkasan mail. Bila suatu saat pulse
  mau menyertakan "tugas apa yang relevan dengan tema minggu
  ini", ia memanggil `gather()` yang sama — bukan salinan logika
  baru.
- **Tidak ada yang berubah di modul lama.** `memory/store.py`,
  `app/triage.py`, `app/pulse.py`, `office/kanban.py`, dan
  `app/server.py` (sampai patch ini ditempel koordinator) tidak
  disentuh builder grounding.

## Yang TIDAK dibangun (sengaja)

- Integrasi suite produktivitas / direktori enterprise / tagihan
  kredit: tidak ada di build ini (BYO — biaya di tangan pemilik
  kunci).
- Embedding / vector search / model eksternal: relevansi tetap
  kata kunci + frasa + recency, supaya bisa dites tanpa model.
- Pengambilan isi mail penuh dari bridge: bridge hanya memberi
  hitungan; grounding tidak menambah jalur fetch mail.
- Cron/scheduler grounding: grounding jalan saat diminta
  (endpoint / sebelum rencana), bukan otomatis berkala.
