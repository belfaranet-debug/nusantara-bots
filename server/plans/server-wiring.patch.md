# server-wiring.patch.md — plans (rencana + checkpoint), untuk koordinator

Patch doc: JANGAN edit `app/server.py` dari dokumen ini.
Koordinator yang menempel potongan di bawah ke `server.py`
(builder lain sedang mengedit file itu).

Modul: `plans/plan.py` — `PlanStore`, `plan_summary`,
`plan_for_routine_run`. 100% kode sendiri; tidak ada jejak
implementasi referensi. Server tidak pernah menerima atau
menyimpan kredensial di endpoint mana pun di bawah ini
(invariant BYO: kredensial hanya di localStorage browser dan
dikirim browser langsung ke API model).

## Kontrak rencana (ringkas)

* Plan: `active | paused | done | failed`.
  Langkah: `pending | running | done | failed`.
* Satu langkah berjalan dalam satu waktu; `start_step` butuh
  plan `active` + langkah `pending`.
* `complete_step` (dengan `note` opsional) menempelkan note
  sebagai evidence `summary` di kartu Kanban langkah itu.
  Plan jadi `done` saat semua langkah `done`.
* `fail_step` menandai plan `failed` (kartu langkah jadi
  `blocked`); plan gagal hanya dibuka lagi oleh `steer` yang
  menambah langkah baru.
* `pause_plan`: `active -> paused`; langkah yang sedang berjalan
  kembali ke `pending` (kartunya kembali ke `todo`) — bisa
  dilanjutkan dari langkah yang sama lewat `resume` +
  `start_step`.
* `steer`: sisipkan langkah baru (`new_steps`, setelah
  `after_step_id` — default setelah langkah berjalan, kalau
  tidak ada di akhir) dan/atau ganti judul langkah `pending`
  (`replace`); setiap steer dicatat di `plan.steering` dengan
  `by` + `direction` + `ts` + id langkah yang disisipkan/diganti.
* Setiap langkah memetakan ke satu kartu Kanban
  (`pending->todo`, `running->doing`, `done->done`,
  `failed->blocked`); kartu dibuat lewat `KanbanStore` saja.

Bentuk plan yang dikembalikan (GET / list / semua POST):

```json
{
  "id": "plan-1",
  "title": "Produksi Short",
  "assignee": "chief-officer",
  "origin": {"kind": "manual"},
  "status": "active",
  "steps": [
    {"id": "step-1", "title": "Riset topik", "status": "done",
     "card_id": "card-1", "note": "riset selesai, 3 sumber",
     "created_at": "2026-10-25T06:00:00+00:00",
     "updated_at": "2026-10-25T06:05:00+00:00"},
    {"id": "step-2", "title": "Tulis naskah", "status": "running",
     "card_id": "card-2", "note": "", "...": "..."}
  ],
  "steering": [
    {"by": "the owner", "direction": "Tambah pengecekan fakta sebelum VO",
     "ts": "2026-10-25T06:10:00+00:00", "after_step_id": "step-2",
     "inserted_step_ids": ["step-5"], "replaced_step_ids": []}
  ],
  "created_at": "...", "updated_at": "...",
  "paused_at": null, "completed_at": null
}
```

Ringkasan progres (`summary`): `{id, title, status, assignee,
total, done, failed, running, pending, percent_done,
current_step_id, steering_count}`.

## 1. Impor (di dekat impor `office` / `approvals`)

```python
from plans.plan import PlanStore, plan_for_routine_run  # noqa: E402
```

## 2. State (di `AppState.__init__`, dekat `self.approvals`)

`plans` injectable seperti `kanban` — tes bisa menyuntik store
temp; produksi memakai default (`data/plans.json`, atau env
`GROKBOT_PLANS_FILE`). WAJIB memakai `self.kanban` yang sama,
supaya kartu langkah dan papan Kanban tidak terbelah dua file.

```python
# Next-level Fase 2/3: rencana + checkpoint (plans/plan).
# Satu PlanStore memakai KanbanStore yang sama dengan state,
# jadi status kartu selalu mengikuti status langkah.
self.plans = (plans if plans is not None
              else PlanStore(kanban=self.kanban))
```

Dan tambahkan `plans=None` ke parameter `__init__` (di samping
`approvals=None`).

## 3. Endpoint GET baru (di `do_GET`, dekat `/api/kanban`)

```python
elif path == "/api/plans":
    # Daftar rencana + ringkasannya. ?status= / ?assignee=
    # memfilter; status yang tidak dikenal -> 400.
    status_filter = (query.get("status") or [None])[0]
    assignee = (query.get("assignee") or [None])[0]
    try:
        plans = state.plans.list_plans(status=status_filter,
                                       assignee=assignee)
    except ValueError as e:
        self._send_json({"error": str(e)}, status=400)
        return
    self._send_json({"plans": plans,
                     "summaries": [plan_summary(p) for p in plans]})
elif path.startswith("/api/plans/"):
    # GET /api/plans/<plan-id> — satu rencana lengkap.
    plan_id = path[len("/api/plans/"):]
    plan = state.plans.get_plan(plan_id)
    if plan is None:
        self._send_json({"error": "unknown plan"}, status=404)
        return
    self._send_json({"plan": plan, "summary": plan_summary(plan)})
```

## 4. Endpoint POST baru (di `do_POST`, dekat `/api/kanban/*`)

Semua POST mengembalikan `{"plan": <plan>, "summary": {...}}`
kecuali dinyatakan lain. `ValueError` dari store -> 400,
`KeyError` (plan/langkah tidak dikenal) -> 404.

```python
elif path == "/api/plans":
    # Buat rencana. assignee, bila diisi, harus karakter valid
    # (pola yang sama dengan POST /api/kanban/cards).
    data = self._allow(obj, ["title", "steps", "assignee", "origin"])
    assignee = data.get("assignee")
    if assignee is not None and \
            state.find_character(assignee) is None:
        self._send_json({"error": "unknown character"}, status=400)
        return
    try:
        plan = state.plans.create_plan(
            data.get("title"), data.get("steps"),
            assignee=assignee, origin=data.get("origin"))
    except ValueError as e:
        self._send_json({"error": str(e)}, status=400)
        return
    self._send_json({"plan": plan, "summary": plan_summary(plan)})
elif path == "/api/plans/step/start":
    # {"plan_id", "step_id"} -> langkah pending menjadi running.
    data = self._allow(obj, ["plan_id", "step_id"])
    try:
        plan = state.plans.start_step(data.get("plan_id"),
                                      data.get("step_id"))
    except KeyError:
        self._send_json({"error": "unknown plan or step"}, status=404)
        return
    except ValueError as e:
        self._send_json({"error": str(e)}, status=400)
        return
    self._send_json({"plan": plan, "summary": plan_summary(plan)})
elif path == "/api/plans/step/complete":
    # {"plan_id", "step_id", "note"?} -> running menjadi done.
    data = self._allow(obj, ["plan_id", "step_id", "note"])
    try:
        plan = state.plans.complete_step(
            data.get("plan_id"), data.get("step_id"),
            note=data.get("note"))
    except KeyError:
        self._send_json({"error": "unknown plan or step"}, status=404)
        return
    except ValueError as e:
        self._send_json({"error": str(e)}, status=400)
        return
    self._send_json({"plan": plan, "summary": plan_summary(plan)})
elif path == "/api/plans/step/fail":
    # {"plan_id", "step_id", "note"?} -> running menjadi failed,
    # plan menjadi failed.
    data = self._allow(obj, ["plan_id", "step_id", "note"])
    try:
        plan = state.plans.fail_step(
            data.get("plan_id"), data.get("step_id"),
            note=data.get("note"))
    except KeyError:
        self._send_json({"error": "unknown plan or step"}, status=404)
        return
    except ValueError as e:
        self._send_json({"error": str(e)}, status=400)
        return
    self._send_json({"plan": plan, "summary": plan_summary(plan)})
elif path == "/api/plans/pause":
    # {"plan_id"} -> active menjadi paused; langkah berjalan
    # kembali ke pending.
    data = self._allow(obj, ["plan_id"])
    try:
        plan = state.plans.pause_plan(data.get("plan_id"))
    except KeyError:
        self._send_json({"error": "unknown plan"}, status=404)
        return
    except ValueError as e:
        self._send_json({"error": str(e)}, status=400)
        return
    self._send_json({"plan": plan, "summary": plan_summary(plan)})
elif path == "/api/plans/resume":
    # {"plan_id"} -> paused menjadi active.
    data = self._allow(obj, ["plan_id"])
    try:
        plan = state.plans.resume_plan(data.get("plan_id"))
    except KeyError:
        self._send_json({"error": "unknown plan"}, status=404)
        return
    except ValueError as e:
        self._send_json({"error": str(e)}, status=400)
        return
    self._send_json({"plan": plan, "summary": plan_summary(plan)})
elif path == "/api/plans/steer":
    # {"plan_id", "direction", "by", "after_step_id"?,
    #  "new_steps"?, "replace"?}
    # new_steps: [judul, ...] disisipkan setelah after_step_id
    # (default: setelah langkah berjalan, kalau tidak ada di
    # akhir). replace: {step_id: judul_baru} untuk langkah pending.
    # Minimal satu dari new_steps / replace harus diisi.
    data = self._allow(obj, ["plan_id", "direction", "by",
                             "after_step_id", "new_steps", "replace"])
    try:
        plan = state.plans.steer(
            data.get("plan_id"), data.get("direction"),
            data.get("by"), after_step_id=data.get("after_step_id"),
            new_steps=data.get("new_steps"),
            replace=data.get("replace"))
    except KeyError:
        self._send_json({"error": "unknown plan or step"}, status=404)
        return
    except ValueError as e:
        self._send_json({"error": str(e)}, status=400)
        return
    self._send_json({"plan": plan, "summary": plan_summary(plan)})
```

Contoh request steer:

```json
POST /api/plans/steer
{"plan_id": "plan-1", "direction": "Tambah pengecekan fakta sebelum VO",
 "by": "the owner", "new_steps": ["Cek fakta naskah"]}
```

## 5. Integrasi routine (routines/scheduler.py)

Tiap run routine membuat rencana — TANPA mengubah
`routines/scheduler.py`. Di `RoutineStore.run_routine`, setelah
kartu dibuat (dan sebelum `last_run`/`next_run` dimajukan),
bungkus kartu itu dalam rencana lewat helper:

```python
# Di run_routine, setelah `card = kanban.create_card(...)`:
try:
    plan_for_routine_run(snapshot, card, plan_store=...,
                         kanban=kanban)
except Exception:
    pass  # rencana gagal dibuat tidak boleh menggagalkan run
```

Catatan untuk koordinator: `RoutineStore` hari ini tidak
mengenal `PlanStore` — `plan_store` perlu diteruskan dari
server (mis. `state.plans`) lewat argumen baru di
`run_routine`/`tick`, atau lewat satu pemanggilan
`plan_for_routine_run` di handler server setelah `tick`
mengembalikan `card_id` tiap routine. Pilihan kedua lebih
kecil risikonya: `tick` sudah mengembalikan `card_id` per
routine, jadi handler bisa memanggil helper itu dengan
`kanban=state.kanban, plan_store=state.plans` dan kartu yang
diambil lewat `state.kanban.get_card(card_id)`. Langkah default
rencana routine: "Laksanakan: <judul>", "Lampirkan bukti ke
kartu <id>", "Serahkan ke review manusia" — atau `steps`
eksplisit bila routine punya langkah sendiri.
