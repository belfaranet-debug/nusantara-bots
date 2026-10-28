"""Real memory service for the Grok Bot own build.

Python standard library only: ``sqlite3`` + ``re`` + ``datetime`` +
``os``. No third-party packages, no network, no model provider, no
vector service, no embeddings. Recall is keyword match + recency.

Storage
-------
One SQLite database per server. Default location (relative to this
repo, ``~/workspace/grokbot-own/``)::

    data/memory.db

Schema (table ``memories``)::

    id            INTEGER PRIMARY KEY AUTOINCREMENT
    scope         TEXT NOT NULL
    author        TEXT NOT NULL   -- character name that wrote it
    text          TEXT NOT NULL
    created_at    TEXT NOT NULL   -- UTC ISO-8601, for recency order
    superseded_by INTEGER NULL REFERENCES memories(id)

Indexes: ``(scope, created_at DESC, id DESC)`` for scoped,
newest-first reads.

Scopes
------
* ``"team"`` -- shared team memory. Every character may read it.
* ``"private:<character-name>"`` -- private to one character, e.g.
  ``"private:chief"``, ``"private:scriptwriter"``. Only that
  character's scope string ever returns those rows.

Scope isolation is enforced by construction: every query filters on
an exact ``scope = ?`` match. There is no "recall everything"
operation. A private recall can therefore never leak another
character's private memories, because rows from other scopes are
never selected.

Supersede mechanism (newest wins)
---------------------------------
``remember(..., supersedes_id=<old id>)`` links the new row to the
old one: the old row's ``superseded_by`` is set to the new row's id.
Rules:

* The superseded row must exist, be in the SAME scope, and not
  already be superseded; otherwise ``ValueError`` is raised and
  nothing is written.
* ``recall()`` excludes superseded rows by default, so the newest
  correction is what comes back. Pass ``include_superseded=True``
  only for audit/history views.
* ``list()`` always shows every row in the scope, with
  ``superseded_by`` / ``is_superseded`` flags, so history is never
  silently hidden -- it is just no longer the live answer.
* ``forget()`` on a row clears ``superseded_by`` pointers that
  pointed at it, so deleting a newer correction revives the older
  row instead of leaving it orphaned-and-hidden.

Ranking (``recall``)
--------------------
1. Keyword match count, descending: number of distinct query
   keywords found (case-insensitive, tokenised on non-alphanumeric
   characters) in the memory text.
2. Recency, descending: ``created_at`` then ``id`` (id is a
   monotonic tie-breaker for rows created in the same instant).

Empty ``keywords`` returns all live rows in the scope, newest first.
Keywords that match nothing return ``[]`` (never the whole store).

Only test rows belong in ``data/memory.db`` unless a later phase
explicitly adopts it as the live store. No personal data, contact
details, credentials, or private files are ever to be copied into
team memory by callers; reference them, do not duplicate them (per
``../MEMORY.md``).
"""

import os
import re
import sqlite3
from datetime import datetime, timezone

__all__ = [
    "MemoryStore",
    "DEFAULT_DB_PATH",
    "TEAM_SCOPE",
    "private_scope",
    "validate_scope",
    "remember",
    "recall",
    "list_memories",
    "forget",
]

# Default DB: <repo>/data/memory.db, i.e. sibling of this package dir.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(_REPO_ROOT, "data", "memory.db")

TEAM_SCOPE = "team"

_SCOPE_RE = re.compile(r"^(team|private:[A-Za-z0-9][A-Za-z0-9._-]*)$")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def private_scope(character_name):
    """Return the private scope string for a character.

    >>> private_scope("chief")
    'private:chief'
    """
    name = (character_name or "").strip()
    scope = "private:%s" % name
    validate_scope(scope)
    return scope


def validate_scope(scope):
    """Raise ValueError unless scope is 'team' or 'private:<name>'."""
    if not isinstance(scope, str) or not _SCOPE_RE.match(scope.strip()):
        raise ValueError(
            "scope must be 'team' or 'private:<character-name>' "
            "(letters, digits, '.', '_' or '-'), got %r" % (scope,)
        )
    return scope.strip()


def _tokens(text):
    """Lower-cased alphanumeric tokens of a string."""
    if text is None:
        return []
    if isinstance(text, (list, tuple, set)):
        text = " ".join(str(t) for t in text)
    return _TOKEN_RE.findall(str(text).lower())


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _row_to_dict(row):
    return {
        "id": row[0],
        "scope": row[1],
        "author": row[2],
        "text": row[3],
        "created_at": row[4],
        "superseded_by": row[5],
        "is_superseded": row[5] is not None,
    }


class MemoryStore:
    """A scoped, persistent memory store backed by one SQLite file."""

    def __init__(self, db_path=None):
        self.db_path = db_path or os.environ.get(
            "GROKBOT_MEMORY_DB", DEFAULT_DB_PATH
        )
        directory = os.path.dirname(os.path.abspath(self.db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = None
        self._init_schema()

    def _init_schema(self):
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                author TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                superseded_by INTEGER REFERENCES memories(id)
            );
            CREATE INDEX IF NOT EXISTS idx_memories_scope_created
                ON memories (scope, created_at DESC, id DESC);
            """
        )
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- spec API ---------------------------------------------------

    def remember(self, scope, text, author, supersedes_id=None):
        """Save a fact. Returns the new memory's integer id.

        ``supersedes_id`` (optional): id of an older memory in the
        SAME scope that this new memory replaces. See module docstring.
        """
        scope = validate_scope(scope)
        text = (text or "").strip()
        author = (author or "").strip()
        if not text:
            raise ValueError("text must not be empty")
        if not author:
            raise ValueError("author must not be empty")
        cur = self._conn.cursor()
        if supersedes_id is not None:
            old = cur.execute(
                "SELECT scope, superseded_by FROM memories WHERE id = ?",
                (supersedes_id,),
            ).fetchone()
            if old is None:
                raise ValueError("supersedes_id %r does not exist" % (supersedes_id,))
            if old[0] != scope:
                raise ValueError(
                    "supersedes_id %r is in scope %r, not %r"
                    % (supersedes_id, old[0], scope)
                )
            if old[1] is not None:
                raise ValueError(
                    "supersedes_id %r is already superseded by %r"
                    % (supersedes_id, old[1])
                )
        cur.execute(
            "INSERT INTO memories (scope, author, text, created_at, superseded_by)"
            " VALUES (?, ?, ?, ?, NULL)",
            (scope, author, text, _utcnow()),
        )
        new_id = cur.lastrowid
        if supersedes_id is not None:
            cur.execute(
                "UPDATE memories SET superseded_by = ? WHERE id = ?",
                (new_id, supersedes_id),
            )
        self._conn.commit()
        return new_id

    def recall(self, scope, keywords, limit=None, include_superseded=False):
        """Keyword + recency ranked memories in one scope.

        ``keywords``: a string (``"wali songo dakwah"``) or an iterable
        of strings. Returns a list of dicts (see ``_row_to_dict``),
        each with an extra ``score`` = distinct keywords matched.
        Superseded rows are excluded unless ``include_superseded``.
        """
        scope = validate_scope(scope)
        wanted = set(_tokens(keywords))
        query = (
            "SELECT id, scope, author, text, created_at, superseded_by"
            " FROM memories WHERE scope = ?"
            " ORDER BY created_at DESC, id DESC"
        )
        rows = self._conn.execute(query, (scope,)).fetchall()
        results = []
        for row in rows:
            d = _row_to_dict(row)
            if d["is_superseded"] and not include_superseded:
                continue
            haystack = set(_tokens(d["text"]))
            score = len(wanted & haystack) if wanted else 0
            if wanted and score == 0:
                continue
            d["score"] = score
            results.append(d)
        # Match count first, then recency (rows already newest-first;
        # a stable sort keeps that order within equal scores).
        results.sort(key=lambda d: -d["score"])
        if limit is not None:
            results = results[:limit]
        return results

    def list(self, scope):
        """All memories in one scope, newest first, including superseded.

        Superseded rows are flagged, never silently dropped.
        """
        scope = validate_scope(scope)
        rows = self._conn.execute(
            "SELECT id, scope, author, text, created_at, superseded_by"
            " FROM memories WHERE scope = ?"
            " ORDER BY created_at DESC, id DESC",
            (scope,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ``list`` shadows the builtin only inside this class; keep an
    # explicit alias so callers can be unambiguous.
    list_memories = list

    def forget(self, id):
        """Delete one memory. Returns True if a row was deleted.

        Pointers to the deleted row are cleared, so a superseded
        older row becomes live again instead of staying hidden.
        """
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE memories SET superseded_by = NULL WHERE superseded_by = ?",
            (id,),
        )
        cur.execute("DELETE FROM memories WHERE id = ?", (id,))
        deleted = cur.rowcount > 0
        self._conn.commit()
        return deleted

    def forget_scope(self, scope):
        """Delete ALL memories in a scope. Returns the row count.

        Pointers (superseded_by) from surviving rows that pointed at
        a deleted row are cleared first, so no dangling references
        remain.
        """
        scope = validate_scope(scope)
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE memories SET superseded_by = NULL "
            "WHERE superseded_by IN "
            "(SELECT id FROM memories WHERE scope = ?)",
            (scope,),
        )
        cur.execute("DELETE FROM memories WHERE scope = ?", (scope,))
        deleted = cur.rowcount
        self._conn.commit()
        return deleted


# -- module-level convenience API (default store: data/memory.db) ---

_default_store = None


def _default():
    global _default_store
    if _default_store is None:
        _default_store = MemoryStore()
    return _default_store


def remember(scope, text, author, supersedes_id=None, db_path=None):
    store = MemoryStore(db_path) if db_path else _default()
    try:
        return store.remember(scope, text, author, supersedes_id=supersedes_id)
    finally:
        if db_path:
            store.close()


def recall(scope, keywords, limit=None, include_superseded=False, db_path=None):
    store = MemoryStore(db_path) if db_path else _default()
    try:
        return store.recall(scope, keywords, limit=limit,
                            include_superseded=include_superseded)
    finally:
        if db_path:
            store.close()


def list_memories(scope, db_path=None):
    store = MemoryStore(db_path) if db_path else _default()
    try:
        return store.list(scope)
    finally:
        if db_path:
            store.close()


def forget(id, db_path=None):
    store = MemoryStore(db_path) if db_path else _default()
    try:
        return store.forget(id)
    finally:
        if db_path:
            store.close()
