"""Duty state machine for characters (office piece).

Adapted in SHAPE from hermes-virtual-office's duty.ts (MIT) — see
office/SOURCE.md. No code was copied; this is a fresh Python version
fitted to this build.

Duty states, highest priority first:

    meeting  — the character is in a live meeting-room turn
               (member of the meeting room with budget left and
               activity inside the live window).
    desk     — the character has work in hand: a card in doing/blocked,
               or a chat turn inside the live window.
    review   — the character has a card waiting in review.
    idle     — nothing of the above.

Differences from the upstream machine, all deliberate:

* There is NO "a2a" state. This build has no autonomous agent-to-agent
  conversation (hard limit); handoffs are presence events, not chats,
  so the state is omitted rather than relabelled.
* There is NO "cron" input. A cron run cannot be attributed to one
  character (the same honesty rule the upstream code states in its own
  comments), so it never feeds a single character's duty.
* The live window is the same honest idea as upstream: one chat turn
  counts as "live" for CHAT_LIVE_MS after the last message.
"""

from __future__ import annotations

DUTIES = ("meeting", "desk", "review", "idle")

#: A chat turn counts as live for 2 minutes after the last message.
CHAT_LIVE_MS = 120_000


def chat_is_live(last_message_at_ms, now_ms) -> bool:
    """True when a last-message timestamp is inside the live window."""
    if last_message_at_ms is None or now_ms is None:
        return False
    return (now_ms - last_message_at_ms) <= CHAT_LIVE_MS


def duty_of(in_meeting: bool = False, has_active_card: bool = False,
            chat_live: bool = False, has_review_card: bool = False) -> str:
    """Duty for one character, priority meeting > desk > review > idle."""
    if in_meeting:
        return "meeting"
    if has_active_card or chat_live:
        return "desk"
    if has_review_card:
        return "review"
    return "idle"
