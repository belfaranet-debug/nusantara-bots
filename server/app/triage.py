"""Inbox-to-action triage — pure classification + Kanban card drafting.

Standard library only. No network, no credentials, no sending, no
deleting: this module only READS a mail item dict and RETURNS data.
Anything external (reply, forward, delete) happens only through the
approval queue — see the inbox-triage skill pack — never here.

Mail item shape (fields are all optional except where noted)::

    {
      "id": "m-123",            # message id from the bridge, if known
      "message_id": "m-123",    # alias of "id"
      "from": "a@example.com",  # sender address or display name
      "sender": "a@example.com",# alias of "from"
      "subject": "...",
      "snippet": "...",         # short preview / first lines
      "body": "...",            # alias of "snippet" when no preview
      "character": "chief-officer",  # owning character (mail label)
    }

Classification (keyword rules, deterministic, honest about limits):
* "spam"   — subject/snippet matches a known junk lure. Suggested
  action is "delete", which the endpoint turns into an approval
  request; nothing is ever deleted by this module.
* "task"   — asks for something (request words, a question mark, a
  deadline). Suggested action is "send" when a reply looks wanted
  (question / please / tolong), otherwise "create_card".
* "info"   — everything else: FYI, newsletter, notification.
  Suggested action is "create_card" at low priority, so the board
  keeps a trace without noise.

``mail_to_card_draft`` turns a task/info item into a Kanban card
dict (``origin: {"kind": "mail", "ref": <message id or sender>}``).
For spam it returns None: junk never becomes a card.
"""

from __future__ import annotations

from datetime import datetime, timezone

MAX_TITLE_CHARS = 250
MAX_BODY_CHARS = 4000
MAX_REF_CHARS = 256

# Junk lures: short, distinctive phrases. Kept small on purpose —
# a newsletter that merely says "unsubscribe" is NOT spam here.
_SPAM_MARKERS = (
    "you won",
    "claim your prize",
    "claim your reward",
    "lottery winner",
    "crypto giveaway",
    "double your bitcoin",
    "double your crypto",
    "verify your account urgently",
    "urgent verify your account",
    "inheritance fund",
    "wire transfer fee",
    "congratulations you have been selected",
    "act now and claim",
)

# Words that mark a mail as asking for something.
_TASK_MARKERS = (
    "please",
    "tolong",
    "mohon",
    "action required",
    "deadline",
    "review",
    "approve",
    "confirm",
    "meeting",
    "jadwal",
    "bisakah",
    "bisa tolong",
    "minta",
    "butuh",
    "perlu",
    "help",
    "urgent",
    "asap",
    "segera",
    "darurat",
    "?",
)

# Markers that a reply is probably wanted (question or direct plea).
_REPLY_MARKERS = (
    "?",
    "please",
    "tolong",
    "mohon",
    "could you",
    "bisakah",
    "bisa tolong",
)

# Markers that push a task to top priority.
_URGENT_MARKERS = (
    "urgent",
    "asap",
    "deadline",
    "segera",
    "darurat",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _item_text(item: dict) -> str:
    """Lowercased subject + snippet, for keyword matching."""
    subject = str(item.get("subject") or "")
    snippet = str(item.get("snippet") or item.get("body") or "")
    return ("%s %s" % (subject, snippet)).lower()


def _ref_for(item: dict) -> str:
    """Stable origin ref: message id when known, else the sender."""
    ref = item.get("id") or item.get("message_id")
    if ref:
        ref = str(ref)
    else:
        sender = item.get("from") or item.get("sender") or "unknown-sender"
        ref = "from:%s" % sender
    return ref[:MAX_REF_CHARS]


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def classify_mail_item(item: dict) -> dict:
    """Classify one mail item.

    Returns {"kind", "suggested_action", "priority", "draft_title",
    "draft_body", "ref", "reason"}. Never raises on bad input: a
    non-dict or empty item classifies as "info" with a note.
    """
    if not isinstance(item, dict):
        item = {}
    text = _item_text(item)
    subject = str(item.get("subject") or "").strip()
    sender = str(item.get("from") or item.get("sender") or "").strip()
    snippet = str(item.get("snippet") or item.get("body") or "").strip()
    ref = _ref_for(item)

    title = "Triage: %s" % (subject if subject else
                            ("mail dari %s" % sender if sender
                             else "mail tanpa subjek"))
    title = _truncate(title, MAX_TITLE_CHARS)

    if any(marker in text for marker in _SPAM_MARKERS):
        return {
            "kind": "spam",
            "suggested_action": "delete",
            "priority": 1,
            "draft_title": title,
            "draft_body": _truncate(
                "Mail ini diklasifikasi SPAM dan TIDAK dijadikan kartu.\n"
                "Dari: %s\nSubjek: %s\nRingkasan: %s\n\n"
                "Aksi yang disarankan: hapus — HANYA lewat approval "
                "queue (send/delete butuh persetujuan manusia). "
                "Skill ini tidak pernah menghapus sendiri."
                % (sender or "-", subject or "-",
                   snippet or "-"),
                MAX_BODY_CHARS),
            "ref": ref,
            "reason": "matches a known junk lure",
        }

    is_task = any(marker in text for marker in _TASK_MARKERS)
    wants_reply = any(marker in text for marker in _REPLY_MARKERS)
    urgent = any(marker in text for marker in _URGENT_MARKERS)

    if is_task:
        kind = "task"
        suggested = "send" if wants_reply else "create_card"
        priority = 3 if urgent else 2
        reason = ("asks for something%s"
                  % (" (reply looks wanted)" if wants_reply else ""))
        body = (
            "Tugas dari mail — draf kartu, belum aksi apa pun.\n"
            "Dari: %s\nSubjek: %s\nRingkasan: %s\n\n"
            "Klasifikasi: tugas%s.\n"
            % (sender or "-", subject or "-", snippet or "-",
               " (mendesak)" if urgent else ""))
        if wants_reply:
            body += (
                "\nBalasan draf (BELUM dikirim — kirim hanya lewat "
                "approval queue):\n"
                "---\n"
                "Terima kasih, mail Anda sudah kami terima dan dicatat "
                "sebagai tugas. Kami kabari lagi setelah ada tindak "
                "lanjut.\n"
                "---\n")
        body += ("\nAsal: mail %s (karakter: %s)."
                 % (ref, item.get("character") or "-"))
        return {
            "kind": kind,
            "suggested_action": suggested,
            "priority": priority,
            "draft_title": title,
            "draft_body": _truncate(body, MAX_BODY_CHARS),
            "ref": ref,
            "reason": reason,
        }

    return {
        "kind": "info",
        "suggested_action": "create_card",
        "priority": 1,
        "draft_title": title,
        "draft_body": _truncate(
            "Info dari mail — dicatat di papan sebagai arsip.\n"
            "Dari: %s\nSubjek: %s\nRingkasan: %s\n\n"
            "Klasifikasi: info (tidak meminta aksi). Kartu prioritas "
            "rendah, supaya jejaknya ada tanpa jadi noise.\n"
            "\nAsal: mail %s (karakter: %s)."
            % (sender or "-", subject or "-", snippet or "-",
               ref, item.get("character") or "-"),
            MAX_BODY_CHARS),
        "ref": ref,
        "reason": "no request markers; filed as info",
    }


def mail_to_card_draft(item: dict) -> dict | None:
    """Turn a mail item into a Kanban card dict (not yet created).

    Returns {"title", "body", "assignee", "priority",
    "origin": {"kind": "mail", "ref": ...}} for task/info items, and
    None for spam (junk never becomes a card — its path is an
    approval request to delete). The caller (endpoint) creates the
    card through the Kanban store; this function only drafts it.
    """
    result = classify_mail_item(item)
    if result["kind"] == "spam":
        return None
    assignee = item.get("character") if isinstance(item, dict) else None
    if assignee is not None:
        assignee = str(assignee).strip() or None
    return {
        "title": result["draft_title"],
        "body": result["draft_body"],
        "assignee": assignee,
        "priority": result["priority"],
        "origin": {"kind": "mail", "ref": result["ref"]},
        "classification": result["kind"],
        "suggested_action": result["suggested_action"],
    }
