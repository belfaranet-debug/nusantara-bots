# Memory service — Nusantara Bots (Phase 1)

**Headline: real memory with zero providers.** This service stores,
lists, and recalls from a local SQLite file using only Python's
standard library. No model API call, no network request, no vector
service, no embeddings. Keyword + recency recall is the floor, and
the floor always works — a character with no brain connection still
remembers yesterday. That is the deliberate differentiator over the
reference app, whose memory depends on its provider backend.
Embeddings, if ever added, are an optional accelerator, never a
dependency.

## Files

| File | What it is |
|---|---|
| `memory/store.py` | The service: `MemoryStore` + module-level functions |
| `memory/cli.py` | Terminal CLI (`remember` / `recall` / `list` / `forget`) |
| `memory/test_memory.py` | Stdlib `unittest` suite (temp DBs, TEST rows only) |
| `data/memory.db` | Default SQLite database (created on first use; test rows only unless a later phase adopts it as live) |

Nothing was deleted and nothing outside `server/`
was touched. No secrets, keys, or personal data anywhere in this code.

## Import path (for the later app-shell worker)

Run / import from `server/` (that directory must be
on `sys.path`; running the tests or the CLI from there handles it):

```python
from memory.store import MemoryStore, private_scope

store = MemoryStore()  # default: data/memory.db
# or: store = MemoryStore("/path/to/memory.db")
# or: store = MemoryStore() with GROKBOT_MEMORY_DB set

mid = store.remember("team", "Weekly theme is Sejarah Islam Nusantara", "chief")
store.remember("team", "Weekly theme is Wali Songo as method, not debate",
               "chief", supersedes_id=mid)
print(store.recall("team", "weekly theme"))   # -> only the newest row
print(store.list("team"))                     # -> both rows, old flagged
store.forget(mid)
```

Module-level convenience functions (same signatures, default store
`data/memory.db`) are also available:

```python
from memory import store
store.remember("team", "A fact", "chief")
store.recall("team", "fact")
store.list_memories("team")
store.forget(1)
```

## Function signatures

```python
remember(scope, text, author, supersedes_id=None) -> int
    # returns the new memory's id

recall(scope, keywords, limit=None, include_superseded=False) -> list[dict]
    # keywords: str ("wali songo") or iterable of str
    # each dict: id, scope, author, text, created_at, superseded_by,
    #            is_superseded, score (distinct keywords matched)

list(scope) -> list[dict]          # alias: list_memories(scope)
    # newest first; superseded rows included and flagged, never hidden

forget(id) -> bool                 # True if a row was deleted
```

On `MemoryStore`, the methods are `remember / recall / list /
forget` with the same signatures (plus an optional `db_path`
argument on the constructor only). `private_scope(name)` returns
`"private:<name>"`; the team scope is the literal string `"team"`.

## Scopes

* `"team"` — shared. Every character may read it.
* `"private:<character-name>"` — private to one character, e.g.
  `"private:chief"`. Character names may contain letters, digits,
  `.`, `_`, `-` only; anything else raises `ValueError`.

Isolation is structural: every query filters on an exact
`scope = ?` match and there is no cross-scope read operation, so a
private recall can never return another character's private rows.

## Ranking

1. Keyword match count (distinct query keywords found in the text,
   case-insensitive), descending.
2. Recency (`created_at`, then `id` as tie-breaker), descending.

Empty keywords return all live rows newest-first; keywords matching
nothing return `[]`.

## Supersede (newest wins)

`remember(..., supersedes_id=<old id>)` sets the old row's
`superseded_by` to the new row's id. The old id must exist, be in
the same scope, and not already be superseded, or `ValueError` is
raised and nothing is written. `recall()` hides superseded rows by
default (the newest correction is the answer); `list()` still shows
them flagged, so history is auditable, not erased. `forget()` on a
newer row clears pointers to it, reviving the older row.

## CLI

From `server/`:

```bash
python3 -m memory.cli remember --scope team --author tester \
    --text "TEST: weekly theme is Sejarah Islam Nusantara"
python3 -m memory.cli remember --scope team --author tester \
    --text "TEST: weekly theme is Wali Songo" --supersedes-id 1
python3 -m memory.cli recall --scope team --keywords "theme"
python3 -m memory.cli list --scope team
python3 -m memory.cli forget --id 1
# any command accepts --db PATH; otherwise data/memory.db
# (or $GROKBOT_MEMORY_DB) is used
```

## Tests

From `server/`:

```bash
python3 -m unittest memory.test_memory -v
```

The suite proves: store + recall round-trip, scope isolation,
list, forget (including revive-on-forget), newest-supersedes-old,
ranking (keyword count, then recency), validation, and fully
offline operation (a static check that `store.py`/`cli.py` import
no network/provider/vector libraries, plus all operations running
against a local temp SQLite file). Tests never touch
`data/memory.db` or any real user data.
