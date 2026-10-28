"""Meeting-room circuit breaker (own build, written from scratch).

A second safety layer that sits ABOVE the 40-message meeting budget
(``app/chat_store.py`` ``MEETING_BUDGET``): the budget counts messages,
the breaker watches HOW the bots post. It only ever gates bot posts in
rooms whose mode is ``"meeting"``; human posts always pass.

Rules, in the order they are checked for a bot post:

1. ``cooldown`` — after a ring trip, every bot post is refused for
   5 minutes.
2. ``duplicate`` — the same text from the same bot within 60 seconds
   is refused.
3. ``ring`` — if the reply chain closes a loop of 3+ bots
   (A replies to B, B replies to C, C replies to A) the breaker TRIPS:
   the post is refused and the 5-minute cooldown starts. A 2-bot
   ping-pong (A replies to B, B replies to A) is normal conversation
   and does NOT trip.
4. ``rate`` — more than 10 accepted posts per minute from one bot;
   the 11th is refused.

Escalation (a signal, NOT a refusal): when the same bot posts twice
in a row within 5 minutes and nobody else — human or another bot —
has posted in between, the second post is still accepted but the
verdict carries ``reason="escalate"`` and ``escalate=True``. The
breaker records ``escalated=True`` until a post from someone else
arrives. The breaker never sends a notification itself; it only
marks the state and returns the signal to the caller.

Refusals are never recorded: a refused post does not enter the room
log, does not count toward the rate limit, does not update the
duplicate window, and does not add a reply edge — so a blocked bot
cannot dig itself deeper.

Pure module: stdlib only, no I/O, no wall-clock reads of its own.
Time comes from an injectable ``clock`` callable (default
``time.monotonic``); ``check()`` also accepts an explicit ``now`` so
tests are fully deterministic. Thread-safe: one lock guards
``check()``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

#: Same text from the same bot inside this window is a duplicate.
DUPLICATE_WINDOW_SECONDS = 60

#: Accepted posts per bot inside this window before the rate rule fires.
RATE_LIMIT_PER_MINUTE = 10
RATE_WINDOW_SECONDS = 60

#: Cooldown after a ring trip: all bot posts refused this long.
COOLDOWN_SECONDS = 300

#: Two consecutive unanswered bot posts inside this window escalate.
ESCALATE_WINDOW_SECONDS = 300
ESCALATE_UNANSWERED_COUNT = 2

#: Longest reply chain the ring walk follows (a ring longer than
#: this is still a ring; the cap only bounds the walk).
MAX_RING_WALK = 10

#: Refusal reasons. ``"escalate"`` is the odd one out: it is a
#: signal on an ALLOWED post, not a refusal.
REASONS = ("duplicate", "ring", "rate", "cooldown", "escalate")


@dataclass(frozen=True)
class Post:
    """One room post as the breaker sees it.

    ``sender`` is the bot id (or ``"human"`` for a human post).
    ``reply_to`` is the bot id this post replies to, or None.
    ``is_human`` marks a human post: always allowed, resets the
    unanswered-bot run, clears a raised escalation.
    """

    sender: str
    text: str
    reply_to: str | None = None
    is_human: bool = False


@dataclass(frozen=True)
class Verdict:
    """Result of ``CircuitBreaker.check``.

    ``allowed`` False means the post must NOT be persisted.
    ``reason`` is None for a plain allow, one of REASONS otherwise.
    ``escalate`` is True exactly when ``reason == "escalate"``: the
    post is allowed, but a human should look at the room.
    """

    allowed: bool
    reason: str | None = None
    escalate: bool = False


class CircuitBreaker:
    """In-memory circuit breaker for one meeting room.

    One instance per room; state is never written anywhere. A
    refused post leaves no trace in the state except a ring trip,
    which starts the cooldown.
    """

    def __init__(self, clock=None):
        self._clock = clock if clock is not None else time.monotonic
        self._lock = threading.Lock()
        # Accepted bot posts only: sender -> [ts, ...] inside the
        # rate window (refused posts never land here).
        self._posts_by_sender: dict[str, list[float]] = {}
        # Accepted posts only: sender -> (normalized text, ts).
        self._texts_by_sender: dict[str, list[tuple[str, float]]] = {}
        # Latest accepted reply edge per bot: sender -> replied-to.
        self._reply_edge: dict[str, str] = {}
        # Ring trip time (monotonic seconds) or None.
        self._tripped_at: float | None = None
        # Unanswered-bot run: sender of the run + first-post time +
        # count. Reset by any post from a different sender.
        self._run_sender: str | None = None
        self._run_started_at: float | None = None
        self._run_count: int = 0
        # True once an escalation fired, until someone else posts.
        self._escalated: bool = False

    # -- public state -------------------------------------------------

    @property
    def escalated(self) -> bool:
        """True after an escalation fired and nobody has replied yet."""
        with self._lock:
            return self._escalated

    def in_cooldown(self, now=None) -> bool:
        """True while a ring-trip cooldown is running."""
        now = self._now(now)
        with self._lock:
            return self._tripped_at is not None and (
                now < self._tripped_at + COOLDOWN_SECONDS)

    def status(self, now=None) -> dict:
        """Small read-only snapshot for callers/debugging."""
        now = self._now(now)
        with self._lock:
            return {
                "escalated": self._escalated,
                "in_cooldown": self._tripped_at is not None
                and now < self._tripped_at + COOLDOWN_SECONDS,
                "cooldown_remaining_seconds": (
                    max(0.0, self._tripped_at + COOLDOWN_SECONDS - now)
                    if self._tripped_at is not None else 0.0),
                "unanswered_run": {
                    "sender": self._run_sender,
                    "count": self._run_count,
                },
            }

    # -- the check ----------------------------------------------------

    def check(self, post: Post, now=None) -> Verdict:
        """Judge one post. Human posts are always allowed.

        Accepted bot posts update the state (rate window, duplicate
        window, reply edge, unanswered run). Refused bot posts update
        nothing — except a ring refusal, which starts the cooldown.
        """
        now = self._now(now)
        with self._lock:
            if post.is_human:
                self._reset_run_locked()
                self._escalated = False
                return Verdict(allowed=True)

            if self._in_cooldown_locked(now):
                return Verdict(allowed=False, reason="cooldown")

            text = post.text.strip()
            if self._is_duplicate_locked(post.sender, text, now):
                return Verdict(allowed=False, reason="duplicate")

            if (post.reply_to is not None
                    and self._closes_ring_locked(post.sender,
                                                 post.reply_to)):
                self._tripped_at = now
                return Verdict(allowed=False, reason="ring")

            if self._over_rate_locked(post.sender, now):
                return Verdict(allowed=False, reason="rate")

            # Accepted: record it, then judge the unanswered run.
            self._record_locked(post, text, now)
            if self._unanswered_run_hits_escalation_locked(post.sender,
                                                           now):
                self._escalated = True
                return Verdict(allowed=True, reason="escalate",
                               escalate=True)
            return Verdict(allowed=True)

    # -- internals (called with the lock held) ------------------------

    def _now(self, now):
        return self._clock() if now is None else now

    def _in_cooldown_locked(self, now) -> bool:
        return (self._tripped_at is not None
                and now < self._tripped_at + COOLDOWN_SECONDS)

    def _is_duplicate_locked(self, sender, text, now) -> bool:
        seen = self._texts_by_sender.get(sender) or []
        seen = [(t, ts) for (t, ts) in seen
                if now - ts < DUPLICATE_WINDOW_SECONDS]
        self._texts_by_sender[sender] = seen
        return any(t == text for (t, _ts) in seen)

    def _closes_ring_locked(self, sender, reply_to) -> bool:
        """True if adding edge sender -> reply_to closes a 3+ cycle.

        Walk from reply_to following latest accepted edges. When the
        walk reaches ``sender`` on iteration ``steps``, the path
        reply_to -> ... -> sender has ``steps - 1`` edges, so the
        loop the new edge closes has ``steps`` bots. ``steps >= 3``
        is a 3+ bot ring; ``steps == 2`` is a 2-bot ping-pong
        (A replies to B, B replies to A), which is normal
        conversation and is allowed.
        """
        node = reply_to
        for steps in range(1, MAX_RING_WALK + 1):
            if node == sender:
                return steps >= 3
            nxt = self._reply_edge.get(node)
            if nxt is None:
                return False
            node = nxt
        return False

    def _over_rate_locked(self, sender, now) -> bool:
        stamps = self._posts_by_sender.get(sender) or []
        stamps = [ts for ts in stamps
                  if now - ts < RATE_WINDOW_SECONDS]
        self._posts_by_sender[sender] = stamps
        return len(stamps) >= RATE_LIMIT_PER_MINUTE

    def _record_locked(self, post, text, now) -> None:
        self._posts_by_sender.setdefault(post.sender, []).append(now)
        self._texts_by_sender.setdefault(post.sender, []).append(
            (text, now))
        if post.reply_to is not None:
            self._reply_edge[post.sender] = post.reply_to

    def _unanswered_run_hits_escalation_locked(self, sender, now) -> bool:
        """Update the consecutive-same-bot run; True on escalation.

        A post from a different sender resets the run (it counts as
        engagement with the previous post). A same-sender post more
        than ESCALATE_WINDOW_SECONDS after the run started also
        restarts the run: the earlier post timed out alone, and one
        unanswered post on its own never escalates.
        """
        if (self._run_sender == sender and self._run_started_at is not None
                and now - self._run_started_at <= ESCALATE_WINDOW_SECONDS):
            self._run_count += 1
        else:
            if sender != self._run_sender:
                # A post from someone else is engagement with the
                # previous run: the raised escalation is resolved.
                self._escalated = False
            self._run_sender = sender
            self._run_started_at = now
            self._run_count = 1
        return self._run_count >= ESCALATE_UNANSWERED_COUNT

    def _reset_run_locked(self) -> None:
        self._run_sender = None
        self._run_started_at = None
        self._run_count = 0
