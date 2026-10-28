"""Weekly project pulse — pure draft composer (stdlib only).

Reads three plain inputs and RETURNS a draft dict. It never sends,
publishes, or writes anything: the draft is shown in the deck and a
human decides whether to send/publish it (through the approval
queue if it is to be sent at all).

Inputs
------
* ``kanban_board`` — the dict from ``KanbanStore.board()``:
  {"columns": {"backlog": [...], "doing": [...], "review": [...],
  "done": [...]}, "counts": {...}, "unproven_done": n}. Cards carry
  "status", "evidence" and (usually) "verdict"; the verdict rule is
  re-applied here so a board without verdicts still composes
  honestly: done + >=1 evidence -> proven, done + 0 evidence ->
  unproven, anything else -> open.
* ``memory_stats`` — {"team_count": int, "scopes": {scope: count}}
  (extra keys are ignored; missing keys count as zero).
* ``mail_summaries`` — list of mail summary dicts as returned by
  ``AppState.mail_summary``: {"character", "inbox_count", "unread",
  "available", "error"}.

Output
------
A dict with "kind" == "weekly-pulse-draft", "sent" == False, the
grouped card titles (done_proven, done_unproven, blocked,
in_review, doing), "needs_decision" (blocked + unproven-done),
counts, the memory/mail numbers, and a markdown "body" ready to
display. Titles are listed, never invented: a card with no title
shows as "(tanpa judul)".
"""

from __future__ import annotations

from datetime import datetime, timezone


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _verdict(card: dict) -> str:
    """Evidence verdict for one card dict (same rule as office/kanban)."""
    if not isinstance(card, dict):
        return "open"
    marked = card.get("verdict")
    if marked in ("proven", "unproven", "open"):
        return marked
    if card.get("status") != "done":
        return "open"
    evidence = card.get("evidence")
    if isinstance(evidence, list) and evidence:
        return "proven"
    return "unproven"


def _title(card: dict) -> str:
    title = card.get("title") if isinstance(card, dict) else None
    title = str(title).strip() if isinstance(title, str) else ""
    return title or "(tanpa judul)"


def _section(heading: str, titles: list) -> str:
    lines = ["## %s" % heading]
    if not titles:
        lines.append("- (tidak ada)")
    else:
        lines.extend("- %s" % t for t in titles)
    return "\n".join(lines)


def compose_pulse(kanban_board, memory_stats, mail_summaries) -> dict:
    """Compose the weekly pulse draft. Pure function; sends nothing."""
    board = kanban_board if isinstance(kanban_board, dict) else {}
    columns = board.get("columns") if isinstance(
        board.get("columns"), dict) else {}
    done_cards = list(columns.get("done") or [])
    review_cards = list(columns.get("review") or [])
    doing_cards = list(columns.get("doing") or [])

    done_proven = [_title(c) for c in done_cards
                   if _verdict(c) == "proven"]
    done_unproven = [_title(c) for c in done_cards
                     if _verdict(c) == "unproven"]
    blocked = [_title(c) for c in review_cards
               if isinstance(c, dict) and c.get("status") == "blocked"]
    in_review = [_title(c) for c in review_cards
                 if isinstance(c, dict) and c.get("status") == "review"]
    doing = [_title(c) for c in doing_cards]

    needs_decision = blocked + done_unproven

    stats = memory_stats if isinstance(memory_stats, dict) else {}
    team_count = stats.get("team_count", 0)
    if not isinstance(team_count, int):
        team_count = 0
    scopes = stats.get("scopes")
    if not isinstance(scopes, dict):
        scopes = {"team": team_count}
    memory_total = sum(v for v in scopes.values()
                       if isinstance(v, int))

    mail_rows = mail_summaries if isinstance(mail_summaries, list) else []
    mail_lines = []
    mail_total = 0
    mail_unread = 0
    for row in mail_rows:
        if not isinstance(row, dict):
            continue
        char = str(row.get("character") or row.get("label") or "?")
        inbox = row.get("inbox_count", 0)
        unread = row.get("unread", 0)
        inbox = inbox if isinstance(inbox, int) else 0
        unread = unread if isinstance(unread, int) else 0
        mail_total += inbox
        mail_unread += unread
        if not row.get("available"):
            mail_lines.append("- %s: inbox tidak tersedia (%s)"
                              % (char, row.get("error") or "unknown"))
        else:
            mail_lines.append("- %s: %d mail, %d belum dibaca"
                              % (char, inbox, unread))

    counts = {
        "done": len(done_cards),
        "done_proven": len(done_proven),
        "done_unproven": len(done_unproven),
        "blocked": len(blocked),
        "in_review": len(in_review),
        "doing": len(doing),
        "needs_decision": len(needs_decision),
        "memory_team": team_count,
        "memory_total": memory_total,
        "mail_total": mail_total,
        "mail_unread": mail_unread,
    }

    body = "\n\n".join([
        "# Weekly Pulse — draf mingguan",
        "Dibuat: %s (UTC). Draf ini TIDAK dikirim ke mana pun; "
        "manusia yang memutuskan kirim/publish, lewat approval "
        "bila mau dikirim." % _utcnow(),
        _section("Selesai — terbukti (%d)" % len(done_proven),
                 done_proven),
        _section("Selesai — tanpa bukti (%d, butuh keputusan)"
                 % len(done_unproven), done_unproven),
        _section("Tertahan / blocked (%d)" % len(blocked), blocked),
        _section("Dalam review (%d)" % len(in_review), in_review),
        _section("Sedang dikerjakan (%d)" % len(doing), doing),
        _section("Butuh keputusan (%d)" % len(needs_decision),
                 needs_decision),
        "## Memory\n- team: %d fakta\n- total semua scope: %d"
        % (team_count, memory_total),
        "## Mail\n" + ("\n".join(mail_lines) if mail_lines
                       else "- (tidak ada karakter mail)"),
    ])

    return {
        "kind": "weekly-pulse-draft",
        "generated_at": _utcnow(),
        "sent": False,
        "counts": counts,
        "done_proven": done_proven,
        "done_unproven": done_unproven,
        "blocked": blocked,
        "in_review": in_review,
        "doing": doing,
        "needs_decision": needs_decision,
        "memory": {"team_count": team_count, "scopes": scopes,
                   "total": memory_total},
        "mail": [{"character": str(r.get("character")
                                   or r.get("label") or "?"),
                  "inbox_count": r.get("inbox_count", 0),
                  "unread": r.get("unread", 0),
                  "available": bool(r.get("available"))}
                 for r in mail_rows if isinstance(r, dict)],
        "body": body,
    }
