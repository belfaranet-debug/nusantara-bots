"""Push hooks — what gets notified, and to whom.

Two events, both best-effort and honest:
* an approval is requested  -> the human is pinged to review it;
* a Kanban card moves to done -> the owner is told the task finished.

Each hook builds its payload (pure functions), fans out to every
registered token, and returns a result dict::

    {"attempted": 2, "sent": 2, "failed": [],
     "skipped_reason": None}

When there is nothing to send to, the hook is an HONEST no-op:
``sent`` stays 0 and ``skipped_reason`` says why — "no
registered tokens" or "kredensial FCM belum dikonfigurasi".
A hook never raises into its caller: a failed push must never
break the approval request or the card move that triggered it.
(The server wiring calls these inside try/except as well — see
``push/server-wiring.patch.md``.)

``registry`` and ``sender`` are injectable so tests can pass a
fake sender and a temp-dir registry; defaults are the real ones.
"""

from __future__ import annotations

from .send import FcmSender, PushNotConfigured
from .tokens import PushTokenRegistry


def approval_payload(approval: dict) -> dict:
    """Pure: title/body/data for an approval-requested push."""
    action = (approval or {}).get("action") or "?"
    target = (approval or {}).get("target") or "?"
    approval_id = (approval or {}).get("id") or ""
    return {
        "title": "Persetujuan diminta",
        "body": "%s → %s" % (action, target),
        "data": {
            "type": "approval_requested",
            "approval_id": approval_id,
            "action": action,
            "target": target,
        },
    }


def task_done_payload(card: dict) -> dict:
    """Pure: title/body/data for a task-done push."""
    title = (card or {}).get("title") or "Tugas"
    card_id = (card or {}).get("id") or ""
    assignee = (card or {}).get("assignee") or ""
    return {
        "title": "Tugas selesai",
        "body": title,
        "data": {
            "type": "task_done",
            "card_id": card_id,
            "assignee": assignee,
        },
    }


def _fan_out(payload, tokens, sender):
    result = {"attempted": len(tokens), "sent": 0, "failed": [],
              "skipped_reason": None}
    if not tokens:
        result["skipped_reason"] = "no registered tokens"
        return result
    try:
        configured, reason = sender.configured()
    except Exception:
        configured, reason = False, "kredensial FCM belum dikonfigurasi"
    if not configured:
        result["skipped_reason"] = (
            reason or "kredensial FCM belum dikonfigurasi")
        return result
    for token in tokens:
        try:
            sender.send_notification(
                token, payload["title"], payload["body"],
                data=payload["data"])
            result["sent"] += 1
        except PushNotConfigured as e:
            result["failed"].append({"token": token[:12] + "…",
                                     "error": str(e)})
        except Exception as e:
            result["failed"].append({"token": token[:12] + "…",
                                     "error": str(e)[:200]})
    return result


def notify_approval_requested(approval, registry=None, sender=None) -> dict:
    """Notify all registered devices that an approval is waiting.

    ``approval`` is the record dict from ``approvals/queue.py``.
    Returns the fan-out result (never raises).
    """
    try:
        payload = approval_payload(approval)
        registry = registry or PushTokenRegistry()
        sender = sender or FcmSender()
        return _fan_out(payload, registry.token_values(), sender)
    except Exception as e:
        return {"attempted": 0, "sent": 0, "failed": [],
                "skipped_reason": "push hook error: %s" % str(e)[:200]}


def notify_task_done(card, registry=None, sender=None) -> dict:
    """Notify all registered devices that a card reached done.

    ``card`` is the card dict from ``office/kanban.py``. Returns
    the fan-out result (never raises).
    """
    try:
        payload = task_done_payload(card)
        registry = registry or PushTokenRegistry()
        sender = sender or FcmSender()
        return _fan_out(payload, registry.token_values(), sender)
    except Exception as e:
        return {"attempted": 0, "sent": 0, "failed": [],
                "skipped_reason": "push hook error: %s" % str(e)[:200]}
