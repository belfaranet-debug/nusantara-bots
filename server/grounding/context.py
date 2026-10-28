"""Work IQ-lite grounding — context bundle sebelum bot merencana.

Pola yang diadopsi (ditulis 100% dari nol, stdlib saja): sebelum
bot menyusun rencana untuk suatu tugas, konteks relevan ditarik
otomatis ke dalam konteks tugas — memory tim, mail masuk, dan
kartu Kanban yang sedang berjalan — sehingga bot bekerja "dengan
pemahaman seperti karyawan", bukan menjawab dari nol.

Kendaraan yang dipakai (bukan jalur baru):

* Memory tim: ``memory/store.py`` — ``MemoryStore.recall`` (keyword
  + recency) dipakai sebagai pra-filter lewat
  ``make_memory_reader``; grounding memberi skor ulang yang seragam
  untuk ketiga sumber.
* Mail masuk: item mail bentuk yang sama dengan ``app/triage.py``
  (``subject``/``snippet``/``from``/``id``) — ditarik lewat
  ``make_mail_reader``; klasifikasi triage sendiri tidak diubah.
* Kanban: ``office/kanban.py`` — kartu yang sedang berjalan
  (status bukan ``done``) lewat ``make_kanban_reader``; aturan
  verdict kartu tidak diubah.

Relevansi deterministik dan bisa dites — skor kecocokan kata
kunci/frasa + recency sederhana. TIDAK ada embedding, TIDAK ada
model eksternal, TIDAK ada jaringan, TIDAK ada panggilan model
sungguhan di mana pun di modul ini.

Fungsi utama: ``gather(task_text, sources, per_source_cap=5)``.
Sumber di-inject (callable atau list polos per sumber) supaya tes
bisa memakai fixture temp tanpa menyentuh store asli.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

__all__ = [
    "DEFAULT_CAP",
    "MAX_EXCERPT_CHARS",
    "gather",
    "make_kanban_reader",
    "make_mail_reader",
    "make_memory_reader",
    "render_bundle_text",
    "score_item",
    "task_keywords",
    "task_phrases",
    "tokenize",
]

DEFAULT_CAP = 5
MAX_EXCERPT_CHARS = 280

# Bobot skor (jumlah maksimal 1.0):
#   kecocokan kata kunci  0.60  (proporsi kata kunci tugas yang cocok)
#   kecocokan frasa       0.30  (0.15 per frasa, maksimal 2 frasa)
#   recency               0.10  (<=7 hari: 0.10; <=30 hari: 0.05)
_KEYWORD_WEIGHT = 0.60
_PHRASE_WEIGHT = 0.15
_PHRASE_CAP = 0.30
_RECENCY_FRESH_DAYS = 7
_RECENCY_FRESH_BONUS = 0.10
_RECENCY_RECENT_DAYS = 30
_RECENCY_RECENT_BONUS = 0.05

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Kata pengisi (ID + EN) yang tidak dihitung sebagai kata kunci:
# terlalu umum untuk membedakan satu item dari item lain.
_STOPWORDS = frozenset((
    # Indonesia
    "yang", "dan", "di", "ke", "dari", "untuk", "dengan", "pada",
    "dalam", "adalah", "atau", "ini", "itu", "ada", "tidak", "tak",
    "saya", "kamu", "kita", "kami", "dia", "mereka", "akan", "sudah",
    "belum", "jangan", "bisa", "dapat", "harus", "perlu", "mau",
    "saja", "juga", "lebih", "paling", "sangat", "banget", "nya",
    "sebagai", "tentang", "agar", "supaya", "kalau", "jika", "saat",
    "ketika", "oleh", "kepada", "antara", "setelah", "sebelum",
    "tolong", "mohon",
    # English
    "the", "and", "for", "with", "from", "this", "that", "are",
    "was", "were", "been", "have", "has", "will", "would", "should",
    "can", "could", "please", "about", "into", "over", "under",
    "your", "yours", "our", "ours", "their", "theirs", "its",
))

_SOURCES = ("memory", "mail", "kanban")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value):
    """Parse an ISO-8601 string to an aware datetime, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def tokenize(text) -> list:
    """Lower-cased alphanumeric tokens of a string (or joined list)."""
    if text is None:
        return []
    if isinstance(text, (list, tuple, set)):
        text = " ".join(str(t) for t in text)
    return _TOKEN_RE.findall(str(text).lower())


def task_keywords(task_text) -> list:
    """Distinct content keywords of the task, in first-seen order."""
    seen = []
    for token in tokenize(task_text):
        if len(token) < 2:
            continue
        if token in _STOPWORDS:
            continue
        if token not in seen:
            seen.append(token)
    return seen


def task_phrases(task_text) -> list:
    """Adjacent 2-token phrases of the content keywords, in order."""
    keywords = task_keywords(task_text)
    return ["%s %s" % (a, b) for a, b in zip(keywords, keywords[1:])]


def _age_days(created_at, now):
    parsed = _parse_iso(created_at)
    if parsed is None or now is None:
        return None
    delta = now - parsed
    return delta.total_seconds() / 86400.0


def score_item(keywords, phrases, item_text, created_at=None,
               now=None):
    """Deterministic relevance score for one item against a task.

    Returns ``(score, reasons)`` where ``score`` is a float in
    [0.0, 1.0] and ``reasons`` is a list of short human-readable
    strings (empty when nothing matched). Score 0.0 means "not
    relevant at all" — the item is dropped by ``gather``.
    """
    haystack_tokens = set(tokenize(item_text))
    haystack_text = " ".join(tokenize(item_text))
    matched = [kw for kw in keywords if kw in haystack_tokens]
    phrase_hits = [ph for ph in phrases if ph in haystack_text]

    score = 0.0
    reasons = []
    if keywords:
        coverage = len(matched) / len(keywords)
        score += _KEYWORD_WEIGHT * coverage
    if matched:
        reasons.append("kata kunci cocok: %s (%d/%d)"
                       % (", ".join(matched), len(matched),
                          len(keywords)))
    if phrase_hits:
        score += min(_PHRASE_CAP, _PHRASE_WEIGHT * len(phrase_hits))
        reasons.append("frasa cocok: %s"
                       % ", ".join('"%s"' % p for p in phrase_hits))
    age = _age_days(created_at, now)
    # Recency is only a BOOST for an item that already matched:
    # a fresh but unrelated item scores 0.0, never 0.10.
    if (matched or phrase_hits) and age is not None and age >= 0:
        if age <= _RECENCY_FRESH_DAYS:
            score += _RECENCY_FRESH_BONUS
            reasons.append("baru: %d hari lalu" % int(age))
        elif age <= _RECENCY_RECENT_DAYS:
            score += _RECENCY_RECENT_BONUS
            reasons.append("cukup baru: %d hari lalu" % int(age))
    return round(min(1.0, score), 3), reasons


def _excerpt(text, limit=MAX_EXCERPT_CHARS) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _normalize_item(source, raw):
    """Raw source row -> uniform dict, tolerant of missing fields.

    Uniform shape: {"source", "ref", "text", "created_at", "extra"}.
    Non-dict rows and rows with no searchable text are dropped
    (return None) — a source that returns junk never breaks the
    bundle, it just contributes nothing.
    """
    if not isinstance(raw, dict):
        return None
    if source == "memory":
        text = raw.get("text") or ""
        ref = "memory:%s" % raw.get("id", "?")
        created_at = raw.get("created_at")
        extra = {"scope": raw.get("scope"), "author": raw.get("author")}
    elif source == "mail":
        subject = raw.get("subject") or ""
        snippet = raw.get("snippet") or raw.get("body") or ""
        text = ("%s %s" % (subject, snippet)).strip()
        ref = raw.get("id") or raw.get("message_id")
        if ref:
            ref = "mail:%s" % ref
        else:
            sender = raw.get("from") or raw.get("sender") or "unknown"
            ref = "mail:from:%s" % sender
        created_at = raw.get("date") or raw.get("created_at")
        extra = {"from": raw.get("from") or raw.get("sender"),
                 "subject": subject or None,
                 "classification": raw.get("classification")}
    elif source == "kanban":
        title = raw.get("title") or ""
        body = raw.get("body") or ""
        text = ("%s %s" % (title, body)).strip()
        ref = "kanban:%s" % raw.get("id", "?")
        created_at = raw.get("updated_at") or raw.get("created_at")
        extra = {"status": raw.get("status"),
                 "assignee": raw.get("assignee"),
                 "priority": raw.get("priority"),
                 "origin": raw.get("origin")}
    else:
        return None
    if not str(text).strip():
        return None
    return {"source": source, "ref": str(ref), "text": str(text),
            "created_at": created_at, "extra": extra}


def _read_source(source, reader, task_text):
    """Call one injected source reader; never raises.

    Returns ``(items, available, error)``. ``reader`` may be a
    callable taking ``task_text`` and returning a list of raw
    dicts, a plain list of raw dicts, or None (source disabled).
    """
    if reader is None:
        return [], True, None
    try:
        raw_items = reader(task_text) if callable(reader) else reader
    except Exception as exc:  # a broken source must not kill grounding
        return [], False, "source %s gagal dibaca: %s" % (source, exc)
    if not isinstance(raw_items, (list, tuple)):
        return [], False, "source %s mengembalikan bukan-list" % source
    items = []
    for raw in raw_items:
        normalized = _normalize_item(source, raw)
        if normalized is not None:
            items.append(normalized)
    return items, True, None


def gather(task_text, sources=None, per_source_cap=DEFAULT_CAP,
           now=None):
    """Build the context bundle for one task.

    ``task_text``: the task to ground (string).
    ``sources``: dict {"memory": reader, "mail": reader,
    "kanban": reader} — each reader is a callable(task_text) ->
    list[dict] or a plain list[dict]; missing/None = source kosong.
    ``per_source_cap``: max items kept per source (default 5).
    ``now``: aware datetime for recency (default: current UTC);
    injectable so tests are deterministic.

    Returns a bundle dict::

        {"kind": "context-bundle", "task": ..., "generated_at": ...,
         "memory": [item, ...], "mail": [...], "kanban": [...],
         "counts": {"memory": n, "mail": n, "kanban": n},
         "sources_available": {"memory": bool, ...},
         "errors": {"memory": str|None, ...}}

    Each item: {"source", "ref", "score", "reasons", "excerpt",
    "created_at", "extra"}. Items with score 0 (no keyword/phrase
    match) are never included; empty sources give empty lists,
    never an error. Pure function apart from reading the injected
    sources — no network, no model, no writes.
    """
    task = str(task_text or "")
    moment = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    keywords = task_keywords(task)
    phrases = task_phrases(task)
    cap = per_source_cap if isinstance(per_source_cap, int) \
        and per_source_cap >= 0 else DEFAULT_CAP
    sources = sources if isinstance(sources, dict) else {}

    bundle = {
        "kind": "context-bundle",
        "task": task,
        "generated_at": moment.isoformat(timespec="seconds"),
        "memory": [],
        "mail": [],
        "kanban": [],
        "counts": {},
        "sources_available": {},
        "errors": {},
    }
    for source in _SOURCES:
        items, available, error = _read_source(
            source, sources.get(source), task)
        bundle["sources_available"][source] = available
        bundle["errors"][source] = error
        scored = []
        for item in items:
            score, reasons = score_item(
                keywords, phrases, item["text"],
                created_at=item["created_at"], now=moment)
            if score <= 0.0:
                continue
            scored.append({
                "source": source,
                "ref": item["ref"],
                "score": score,
                "reasons": reasons,
                "excerpt": _excerpt(item["text"]),
                "created_at": item["created_at"],
                "extra": item["extra"],
            })
        # Score desc, then recency desc, then ref asc — one key,
        # fully deterministic. Undated items sort last on recency.
        def _sort_key(d):
            parsed = _parse_iso(d["created_at"])
            ts = parsed.timestamp() if parsed is not None \
                else float("-inf")
            return (-d["score"], -ts, d["ref"])

        scored.sort(key=_sort_key)
        bundle[source] = scored[:cap]
        bundle["counts"][source] = len(bundle[source])
    return bundle


def render_bundle_text(bundle) -> str:
    """Render a bundle as a short text block for a plan prompt.

    The plans/ module (Fase 3) pastes this ABOVE the task text when
    asking a character to draft a plan, so the plan is written with
    the grounded context in view. Empty sections render as
    "(tidak ada)" — never invented.
    """
    if not isinstance(bundle, dict):
        return ""
    lines = ["# Konteks tugas (grounding otomatis)"]
    task = bundle.get("task") or ""
    if task:
        lines.append("Tugas: %s" % task)
    labels = {"memory": "Memory tim", "mail": "Mail masuk",
              "kanban": "Kartu Kanban berjalan"}
    for source in _SOURCES:
        items = bundle.get(source) or []
        lines.append("## %s (%d)" % (labels[source], len(items)))
        if not items:
            lines.append("- (tidak ada)")
            continue
        for item in items:
            reasons = "; ".join(item.get("reasons") or [])
            lines.append("- [%s | skor %.2f] %s%s"
                         % (item.get("ref"), item.get("score", 0),
                            item.get("excerpt") or "",
                            (" — " + reasons) if reasons else ""))
    return "\n".join(lines)


# -- adapters: wrap the existing stores into source readers --------
# These are the ONLY places that touch the real modules. The
# server (see grounding/server-wiring.patch.md) passes these
# callables into gather(); tests pass fixtures instead.

def make_memory_reader(store, bot=None, limit=50):
    """Reader for team memory (+ the bot's private scope, if given).

    Uses the existing ``MemoryStore.recall`` (keyword + recency)
    as a pre-filter, then grounding re-scores uniformly against
    the other sources. ``store`` is a ``MemoryStore`` instance.
    Scope isolation is inherited: only "team" and (when ``bot``
    is given) "private:<bot>" are ever read.
    """
    from memory.store import TEAM_SCOPE, private_scope

    scopes = [TEAM_SCOPE]
    if bot:
        scopes.append(private_scope(bot))

    def reader(task_text):
        keywords = task_keywords(task_text)
        rows = []
        for scope in scopes:
            try:
                rows.extend(store.recall(scope, keywords, limit=limit))
            except Exception:
                continue
        return rows

    return reader


def make_kanban_reader(store):
    """Reader for Kanban cards that are still running.

    Returns non-done cards (todo/doing/review/blocked) via the
    existing ``KanbanStore.list_cards``; done cards are history,
    not running work, so they are not grounding for a new plan.
    """
    def reader(task_text):
        return [c for c in store.list_cards()
                if c.get("status") != "done"]

    return reader


def make_mail_reader(items_fn):
    """Reader for mail items from a supplier callable.

    ``items_fn`` is a zero-arg callable returning a list of mail
    item dicts in the ``app/triage.py`` shape (subject/snippet/
    from/id). The mail bridge itself only exposes counts
    (``app/mail_client.py``), so the server supplies the items it
    has — e.g. items already fetched for a triage run — and an
    empty list when it has none. Grounding never fetches mail
    itself and never fails when the list is empty.
    """
    def reader(task_text):
        items = items_fn()
        return list(items) if isinstance(items, (list, tuple)) else []

    return reader
