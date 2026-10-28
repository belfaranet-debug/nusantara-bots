# plans/ — Rencana + checkpoint untuk tugas panjang (Fase 2/3)

Setiap tugas panjang yang didelegasikan ke bot punya RENCANA:
langkah-langkah berstatus `pending | running | done | failed`,
dengan plan `active | paused | done | failed`. Manusia bisa
PAUSE di tengah jalan (langkah berjalan kembali ke `pending`,
bisa dilanjutkan) dan STEER (sisipkan/ganti langkah berikut +
arahan tercatat dengan siapa yang mengarahkan).

## Modul

| File | Apa |
|---|---|
| `plan.py` | `PlanStore` — rencana + langkah + log steer di `data/plans.json` (env `GROKBOT_PLANS_FILE`); tulis atomik di bawah lock. `plan_summary(plan)` — ringkasan progres (pure). `plan_for_routine_run(routine, card, ...)` — bungkus satu run routine dalam rencana. |
| `server-wiring.patch.md` | Endpoint yang diusulkan untuk koordinator (jangan edit `app/server.py` dari sini). |
| `deck.patch.md` | Snippet HTML/JS panel rencana untuk koordinator. |

## Aturan

- **Kartu mengikuti langkah.** Setiap langkah memetakan ke satu
  kartu Kanban (`pending->todo`, `running->doing`, `done->done`,
  `failed->blocked`) lewat `KanbanStore` saja — modul ini tidak
  pernah menulis `data/kanban.json` sendiri.
- **Selesai butuh bukti.** Langkah selesai dengan `note` menempelkan
  note itu sebagai evidence `summary` di kartunya (aturan verdict
  Kanban tetap berlaku: selesai tanpa bukti = `unproven`).
- **Gagal terlihat gagal.** `fail_step` menandai plan `failed`;
  plan gagal hanya dibuka lagi oleh steer yang menambah langkah.
  Langkah gagal tetap tercatat sebagai riwayat.
- **Satu langkah berjalan.** Plan menjalankan satu langkah dalam
  satu waktu; `start_step` butuh plan `active` + langkah `pending`.
- **Steer tercatat.** Setiap steer menyimpan `by`, `direction`,
  `ts`, dan id langkah yang disisipkan/diganti — deck menampilkan
  seluruh riwayat arahan manusia.
- **Routine memakai rencana.** Tiap run routine membungkus
  kartunya dalam rencana (`plan_for_routine_run`); wiring-nya di
  `server-wiring.patch.md` §5, tanpa mengubah
  `routines/scheduler.py`.

## Tes

```bash
python3 -m unittest discover -s plans/tests -v
```

Temp dir saja; tanpa jaringan, tanpa model, tanpa kredensial.
