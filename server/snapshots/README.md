# snapshots/ — snapshot / recovery (Fase 2)

A snapshot is a point-in-time copy of the two local JSON stores
that define the office's working state:

* the computer registry (`data/computers.json`), and
* the Kanban board (`data/kanban.json`),

plus an optional caller-supplied `device_state` dict (for
example a bridge status summary). The snapshot file lives in
`data/snapshots/<id>.json` and holds the copied state itself —
not a pointer to it — so restoring works even after the live
files have been overwritten.

## What this module deliberately does NOT do

* It does not import any device bridge and never touches a live
  device. `device_state` is data handed in by the caller; the
  snapshot records it, restore hands it back in the summary, and
  nothing here acts on it.
* It does not snapshot chat history, memory (SQLite), or mail
  drafts. Those are separate stores with their own formats; a
  snapshot that silently omitted them would be a lie, so the
  manifest lists exactly which files were copied
  (`file_names`).
* Restore is explicit and audited: every create / restore /
  delete appends a line to `data/snapshots/audit.jsonl`.
  Restore never deletes a snapshot, and a file that was absent
  at snapshot time is left alone on restore (reported as
  `skipped`, not silently created).
* Snapshot ids are never reused: a persistent counter
  (`data/snapshots/counter.json`) hands out `snap-1`, `snap-2`,
  … even after a snapshot is deleted.

## API

```python
store = SnapshotStore()  # or SnapshotStore(snapshots_dir=..., ...)
snap = store.create_snapshot(device_state={"bridge": "ok"})
store.list_snapshots()   # [{id, created_at, files, has_device_state, ok}]
store.get_snapshot("snap-1")
store.restore_snapshot("snap-1")
# -> {"id", "restored": ["computers.json", "kanban.json"],
#     "skipped": [], "device_state": {...}}
store.delete_snapshot("snap-1")
```

`restore_snapshot` on an unknown id raises `KeyError` with the
id in the message. A corrupt snapshot file raises `ValueError`.
Files are written atomically (tmp + replace), the same pattern
as `office/kanban.py` and `computers/registry.py`.

Env overrides: `GROKBOT_SNAPSHOTS_DIR`, `GROKBOT_COMPUTERS_FILE`,
`GROKBOT_KANBAN_FILE` (the last two are shared with the
registry / Kanban stores, so a snapshot always covers the same
files the server actually uses).

## Tests

```bash
python3 -m unittest discover -s snapshots/tests -v
```

Tests use temp files only (temp snapshots dir plus temp
registry + Kanban stores). The real `data/` files are never
touched.
