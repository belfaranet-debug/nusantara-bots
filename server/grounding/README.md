# Grounding — Work IQ-lite (Fase 2)

**Headline: bot merencana dengan konteks, bukan dari nol.** Sebelum
bot menyusun rencana untuk suatu tugas, `gather()` menarik konteks
relevan dari tiga sumber yang sudah ada — memory tim, mail masuk,
kartu Kanban yang sedang berjalan — dan mengembalikannya sebagai
"context bundle" dengan skor relevansi + alasan per item.

## Files

| File | What it is |
|---|---|
| `grounding/context.py` | Modul murni: `gather`, skor, adapter reader |
| `grounding/server-wiring.patch.md` | Endpoint yang diusulkan + integrasi plans/ (server TIDAK diedit) |
| `grounding/tests/test_grounding.py` | Suite stdlib `unittest` (fixture temp, tanpa jaringan/model) |

## Aturan

- **Stdlib saja, deterministik.** Skor = kecocokan kata kunci/frasa
  + recency sederhana. Tanpa embedding, tanpa model eksternal,
  tanpa jaringan, tanpa tulis apa pun.
- **Sumber di-inject.** `gather(task_text, sources)` menerima
  reader per sumber (callable atau list polos) — tes memakai
  fixture, server memakai adapter `make_*_reader`.
- **Kendaraan yang sudah ada.** Memory via `memory/store.py`
  (`recall` sebagai pra-filter), mail item bentuk `app/triage.py`,
  Kanban via `office/kanban.py` (`list_cards`, kartu non-done).
  Tidak ada jalur baru, tidak ada integrasi enterprise apa pun.
- **Sumber kosong/rusak = bundle kosong, bukan error.** Item skor
  0 (tidak ada kata kunci/frasa cocok) tidak pernah masuk bundle.

## Skor (maks 1.0)

| Komponen | Bobot |
|---|---|
| Kata kunci: proporsi kata kunci tugas yang cocok | 0.60 |
| Frasa: 0.15 per frasa 2-kata yang cocok, maks 2 | 0.30 |
| Recency: ≤7 hari +0.10, ≤30 hari +0.05 | 0.10 |

Urutan dalam tiap sumber: skor turun, lalu recency turun, lalu
`ref` naik (deterministik penuh). Cap default: 5 item per sumber.

## Bentuk bundle

```python
from grounding.context import gather

bundle = gather("Tulis naskah Wali Songo episode 3", sources={
    "memory": memory_reader,   # callable(task) -> [dict, ...]
    "mail": mail_reader,
    "kanban": kanban_reader,
})
# bundle = {
#   "kind": "context-bundle", "task": "...", "generated_at": "...",
#   "memory": [{"source": "memory", "ref": "memory:12", "score": 0.85,
#               "reasons": ['kata kunci cocok: wali, songo (2/4)',
#                           'frasa cocok: "wali songo"',
#                           "baru: 2 hari lalu"],
#               "excerpt": "Tema minggu ini Wali Songo ...",
#               "created_at": "...", "extra": {...}}, ...],
#   "mail": [...], "kanban": [...],
#   "counts": {"memory": 1, "mail": 0, "kanban": 2},
#   "sources_available": {"memory": True, "mail": True, "kanban": True},
#   "errors": {"memory": None, "mail": None, "kanban": None},
# }
```

`render_bundle_text(bundle)` mengubah bundle menjadi blok teks
siap tempel di atas teks tugas saat plans/ meminta draf rencana.

## Adapter (server memakai ini; tes tidak)

```python
from grounding.context import (make_memory_reader, make_kanban_reader,
                               make_mail_reader)
from memory.store import MemoryStore

store = MemoryStore()  # per-request, seperti pola server.py
sources = {
    "memory": make_memory_reader(store, bot="chief"),
    "mail": make_mail_reader(lambda: mail_items_yang_sudah_ada),
    "kanban": make_kanban_reader(kanban_store),
}
```

Catatan mail: bridge mail (`app/mail_client.py`) hanya memberi
hitungan inbox, bukan isi — server memasok item mail yang sudah
ia punya (mis. dari run triage); bila tidak ada, sumber mail
kosong dan bundle tetap valid.

## Tes

Dari `server/`:

```bash
python3 -m unittest discover -s grounding/tests -v
```

Tes memakai fixture temp (termasuk `MemoryStore` + `KanbanStore`
di file temp) dan tidak pernah menyentuh `data/` asli, jaringan,
atau model.
