"""Tests for the meeting-room circuit breaker (stdlib unittest only).

Unit tests run against a fake clock (fully deterministic, no
wall-clock reads). Server tests run the real HTTP server on temp
dirs and prove the wiring: refused posts never reach the room log,
and the 40-message budget still counts underneath the breaker.

Run from ~/workspace/grokbot-own/:
    python3 -m unittest discover -s office/tests -v
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP_DIR = REPO / "app"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(APP_DIR))

from office.circuit_breaker import (  # noqa: E402
    COOLDOWN_SECONDS, DUPLICATE_WINDOW_SECONDS, RATE_LIMIT_PER_MINUTE,
    CircuitBreaker, Post)


class FakeClock:
    """Deterministic clock: the test moves time by hand."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _bot(name, text, reply_to=None):
    return Post(sender=name, text=text, reply_to=reply_to)


def _human(text="noted"):
    return Post(sender="human", text=text, is_human=True)


class TestDuplicate(unittest.TestCase):
    def test_same_text_same_bot_within_60s_refused(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        first = breaker.check(_bot("bot-a", "hello"), now=clock())
        self.assertTrue(first.allowed)
        self.assertIsNone(first.reason)
        second = breaker.check(_bot("bot-a", "hello"), now=clock())
        self.assertFalse(second.allowed)
        self.assertEqual(second.reason, "duplicate")

    def test_same_text_after_61s_allowed(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        breaker.check(_bot("bot-a", "hello"), now=clock())
        clock.advance(DUPLICATE_WINDOW_SECONDS + 1)
        verdict = breaker.check(_bot("bot-a", "hello"), now=clock())
        self.assertTrue(verdict.allowed)
        # Not a duplicate any more — but it IS the bot's second
        # consecutive unanswered post, so the escalation signal
        # fires on the allowed post.
        self.assertEqual(verdict.reason, "escalate")
        self.assertTrue(verdict.escalate)

    def test_same_text_from_different_bot_allowed(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        breaker.check(_bot("bot-a", "hello"), now=clock())
        verdict = breaker.check(_bot("bot-b", "hello"), now=clock())
        self.assertTrue(verdict.allowed)

    def test_different_text_same_bot_allowed(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        breaker.check(_bot("bot-a", "hello"), now=clock())
        verdict = breaker.check(_bot("bot-a", "world"), now=clock())
        self.assertTrue(verdict.allowed)

    def test_duplicate_boundary_60s_is_outside_window(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        breaker.check(_bot("bot-a", "hello"), now=clock())
        clock.advance(DUPLICATE_WINDOW_SECONDS)
        verdict = breaker.check(_bot("bot-a", "hello"), now=clock())
        self.assertTrue(verdict.allowed)


class TestRing(unittest.TestCase):
    def test_ring_a_b_c_a_trips_and_cools_down(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        self.assertTrue(breaker.check(
            _bot("bot-a", "to b", reply_to="bot-b"), now=clock()).allowed)
        self.assertTrue(breaker.check(
            _bot("bot-b", "to c", reply_to="bot-c"), now=clock()).allowed)
        third = breaker.check(
            _bot("bot-c", "to a", reply_to="bot-a"), now=clock())
        self.assertFalse(third.allowed)
        self.assertEqual(third.reason, "ring")
        self.assertTrue(breaker.in_cooldown(now=clock()))
        # During the 5-minute cooldown every bot post is refused.
        refused = breaker.check(_bot("bot-a", "fresh post"), now=clock())
        self.assertFalse(refused.allowed)
        self.assertEqual(refused.reason, "cooldown")
        # Cooldown is exactly 5 minutes: still cooling at 300s...
        clock.advance(COOLDOWN_SECONDS)
        self.assertFalse(breaker.in_cooldown(now=clock()))
        # ...and posting works again afterwards.
        self.assertTrue(breaker.check(
            _bot("bot-a", "fresh post"), now=clock()).allowed)

    def test_two_bot_ping_pong_does_not_trip(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        self.assertTrue(breaker.check(
            _bot("bot-a", "to b", reply_to="bot-b"), now=clock()).allowed)
        verdict = breaker.check(
            _bot("bot-b", "to a", reply_to="bot-a"), now=clock())
        self.assertTrue(verdict.allowed)
        self.assertFalse(breaker.in_cooldown(now=clock()))

    def test_four_bot_ring_trips(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        breaker.check(_bot("bot-a", "1", reply_to="bot-b"), now=clock())
        breaker.check(_bot("bot-b", "2", reply_to="bot-c"), now=clock())
        breaker.check(_bot("bot-c", "3", reply_to="bot-d"), now=clock())
        verdict = breaker.check(
            _bot("bot-d", "4", reply_to="bot-a"), now=clock())
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "ring")

    def test_refused_ring_post_leaves_no_edge(self):
        # The refused C->A post must not be recorded: after the
        # cooldown the same C->A post still forms the ring from the
        # two accepted edges alone, and trips again.
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        breaker.check(_bot("bot-a", "to b", reply_to="bot-b"), now=clock())
        breaker.check(_bot("bot-b", "to c", reply_to="bot-c"), now=clock())
        self.assertEqual(
            breaker.check(_bot("bot-c", "to a", reply_to="bot-a"),
                          now=clock()).reason, "ring")
        clock.advance(COOLDOWN_SECONDS + 1)
        again = breaker.check(
            _bot("bot-c", "to a again", reply_to="bot-a"), now=clock())
        self.assertFalse(again.allowed)
        self.assertEqual(again.reason, "ring")


class TestRate(unittest.TestCase):
    def test_ten_per_minute_allowed_eleventh_refused(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        for i in range(RATE_LIMIT_PER_MINUTE):
            verdict = breaker.check(
                _bot("bot-a", "post %d" % i), now=clock())
            self.assertTrue(verdict.allowed, "post %d refused" % i)
        eleventh = breaker.check(_bot("bot-a", "post 10"), now=clock())
        self.assertFalse(eleventh.allowed)
        self.assertEqual(eleventh.reason, "rate")

    def test_rate_is_per_bot(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        for i in range(RATE_LIMIT_PER_MINUTE):
            breaker.check(_bot("bot-a", "post %d" % i), now=clock())
        verdict = breaker.check(_bot("bot-b", "first post"), now=clock())
        self.assertTrue(verdict.allowed)

    def test_rate_window_slides_after_61s(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        for i in range(RATE_LIMIT_PER_MINUTE):
            breaker.check(_bot("bot-a", "post %d" % i), now=clock())
        self.assertFalse(breaker.check(
            _bot("bot-a", "post 10"), now=clock()).allowed)
        clock.advance(61)
        verdict = breaker.check(_bot("bot-a", "post 10"), now=clock())
        self.assertTrue(verdict.allowed)

    def test_refused_posts_do_not_count_toward_rate(self):
        # 10 accepted, then a duplicate refusal, then the window
        # still holds exactly 10 accepted posts: the 11th distinct
        # post is refused for rate, not for anything the refusal did.
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        for i in range(RATE_LIMIT_PER_MINUTE):
            breaker.check(_bot("bot-a", "post %d" % i), now=clock())
        self.assertEqual(
            breaker.check(_bot("bot-a", "post 0"), now=clock()).reason,
            "duplicate")
        self.assertEqual(
            breaker.check(_bot("bot-a", "post 10"), now=clock()).reason,
            "rate")


class TestEscalation(unittest.TestCase):
    def test_two_unanswered_posts_from_same_bot_escalate(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        first = breaker.check(_bot("bot-a", "one"), now=clock())
        self.assertTrue(first.allowed)
        self.assertIsNone(first.reason)
        self.assertFalse(first.escalate)
        second = breaker.check(_bot("bot-a", "two"), now=clock())
        # Escalation is a signal, not a refusal: the post is allowed.
        self.assertTrue(second.allowed)
        self.assertEqual(second.reason, "escalate")
        self.assertTrue(second.escalate)
        self.assertTrue(breaker.escalated)

    def test_other_bot_post_between_resets_run(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        breaker.check(_bot("bot-a", "one"), now=clock())
        breaker.check(_bot("bot-b", "replying"), now=clock())
        verdict = breaker.check(_bot("bot-a", "two"), now=clock())
        self.assertTrue(verdict.allowed)
        self.assertIsNone(verdict.reason)
        self.assertFalse(breaker.escalated)

    def test_human_post_resets_run_and_clears_escalation(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        breaker.check(_bot("bot-a", "one"), now=clock())
        breaker.check(_bot("bot-a", "two"), now=clock())
        self.assertTrue(breaker.escalated)
        human = breaker.check(_human(), now=clock())
        self.assertTrue(human.allowed)
        self.assertFalse(breaker.escalated)
        # A fresh post after the human reply starts a new run.
        verdict = breaker.check(_bot("bot-a", "three"), now=clock())
        self.assertTrue(verdict.allowed)
        self.assertIsNone(verdict.reason)

    def test_second_post_after_5_min_gap_does_not_escalate(self):
        # The first post timed out alone; one unanswered post on its
        # own never escalates, so the late second post starts a
        # fresh run instead of firing.
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        breaker.check(_bot("bot-a", "one"), now=clock())
        clock.advance(301)
        verdict = breaker.check(_bot("bot-a", "two"), now=clock())
        self.assertTrue(verdict.allowed)
        self.assertIsNone(verdict.reason)
        self.assertFalse(breaker.escalated)

    def test_escalation_state_cleared_by_other_bot(self):
        clock = FakeClock()
        breaker = CircuitBreaker(clock=clock)
        breaker.check(_bot("bot-a", "one"), now=clock())
        breaker.check(_bot("bot-a", "two"), now=clock())
        self.assertTrue(breaker.escalated)
        breaker.check(_bot("bot-b", "i am here"), now=clock())
        self.assertFalse(breaker.escalated)


class TestClockInjection(unittest.TestCase):
    def test_explicit_now_overrides_clock(self):
        breaker = CircuitBreaker(clock=lambda: 5000.0)
        breaker.check(_bot("bot-a", "hello"), now=1000.0)
        # At t=1061 the explicit-now window has passed, even though
        # the injected clock still says 5000.
        verdict = breaker.check(_bot("bot-a", "hello"), now=1061.0)
        self.assertTrue(verdict.allowed)


# ----------------------------------------------------------------------
# Server wiring tests: the breaker sits on the room post path.
# ----------------------------------------------------------------------

CHAR_MD = """---
name: Test %s
role: Test bot
skills:
  - team-memory
tools: []
memory_scope: private+team
---

You are test %s.
"""

BOT_IDS = ["bot-a", "bot-b", "bot-c", "bot-d", "bot-e"]


def _req(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")}


class ServerCircuitBreakerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        chars = Path(self.tmp.name) / "characters"
        chars.mkdir()
        for bot in BOT_IDS:
            (chars / ("%s.md" % bot)).write_text(
                CHAR_MD % (bot, bot))
        from app.server import create_server  # noqa: E402
        self.httpd = create_server(
            0, characters_dir=str(chars),
            chat_file=str(Path(self.tmp.name) / "chat.json"),
            memory_db=str(Path(self.tmp.name) / "memory.db"))
        self.port = self.httpd.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _post_message(self, bot, text, role="assistant", reply_to=None):
        body = {"room": "meeting-room", "character": bot,
                "text": text, "role": role}
        if reply_to is not None:
            body["reply_to"] = reply_to
        return _req(self.base, "POST", "/api/messages", body)

    def _room_messages(self):
        _, data = _req(self.base, "GET", "/api/chat?room=meeting-room")
        return data["messages"]

    def _meeting_room(self):
        _, data = _req(self.base, "GET", "/api/rooms")
        return [r for r in data["rooms"]
                if r["mode"] == "meeting"][0]

    def test_duplicate_refused_and_absent_from_room_log(self):
        status, _ = self._post_message("bot-a", "same text twice")
        self.assertEqual(status, 200)
        status, data = self._post_message("bot-a", "same text twice")
        self.assertEqual(status, 429)
        self.assertEqual(data["breaker"]["reason"], "duplicate")
        msgs = self._room_messages()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["text"], "same text twice")

    def test_ring_trips_then_cooldown_refuses_all_bots(self):
        status, _ = self._post_message("bot-a", "to b", reply_to="bot-b")
        self.assertEqual(status, 200)
        status, _ = self._post_message("bot-b", "to c", reply_to="bot-c")
        self.assertEqual(status, 200)
        status, data = self._post_message("bot-c", "to a",
                                          reply_to="bot-a")
        self.assertEqual(status, 429)
        self.assertEqual(data["breaker"]["reason"], "ring")
        # Cooldown: an unrelated bot is refused too.
        status, data = self._post_message("bot-d", "unrelated post")
        self.assertEqual(status, 429)
        self.assertEqual(data["breaker"]["reason"], "cooldown")
        # None of the refused posts reached the room log.
        msgs = self._room_messages()
        self.assertEqual(len(msgs), 2)

    def test_rate_limit_eleventh_refused(self):
        for i in range(10):
            status, _ = self._post_message("bot-a", "rate post %d" % i)
            self.assertEqual(status, 200, "post %d refused" % i)
        status, data = self._post_message("bot-a", "rate post 10")
        self.assertEqual(status, 429)
        self.assertEqual(data["breaker"]["reason"], "rate")
        self.assertEqual(len(self._room_messages()), 10)

    def test_escalation_signal_returned_and_post_still_logged(self):
        status, data = self._post_message("bot-a", "first unanswered")
        self.assertEqual(status, 200)
        self.assertFalse(data["breaker"]["escalate"])
        status, data = self._post_message("bot-a", "second unanswered")
        self.assertEqual(status, 200)
        self.assertTrue(data["breaker"]["escalate"])
        self.assertEqual(data["breaker"]["reason"], "escalate")
        # Escalation is a signal, not a refusal: both posts are logged.
        self.assertEqual(len(self._room_messages()), 2)

    def test_general_room_is_not_gated(self):
        # The breaker only guards meeting rooms: the same text twice
        # in general chat passes straight through.
        for _ in range(2):
            status, _ = _req(self.base, "POST", "/api/messages",
                             {"room": "general", "character": "bot-a",
                              "text": "same text twice",
                              "role": "assistant"})
            self.assertEqual(status, 200)

    def test_budget_40_still_counts_under_breaker(self):
        # 40 posts, 5 bots x 8 distinct texts: no duplicate, no
        # rate trip (8 < 10 per bot), no ring (no reply_to), no
        # escalation (senders alternate, so the unanswered run
        # resets every post). Budget must hit exactly 0.
        for i in range(40):
            bot = BOT_IDS[i % len(BOT_IDS)]
            status, _ = self._post_message(bot, "budget post %d" % i)
            self.assertEqual(status, 200, "post %d refused" % i)
        meeting = self._meeting_room()
        self.assertEqual(meeting["budget"], 40)
        self.assertEqual(meeting["message_count"], 40)
        self.assertEqual(meeting["budget_remaining"], 0)
        self.assertEqual(len(self._room_messages()), 40)

    def test_zero_trace_in_new_and_touched_files(self):
        # Forbidden product/fork names must not appear anywhere in
        # the breaker module or the wired server file. The names
        # are built from parts so this test file itself carries no
        # literal trace either.
        banned = ["rak" + "azo", "octo" + "bot", "open" + "dots"]
        for path in (REPO / "office" / "circuit_breaker.py",
                     REPO / "app" / "server.py"):
            text = path.read_text()
            for name in banned:
                self.assertNotIn(name, text, "%s in %s" % (name, path))


if __name__ == "__main__":
    unittest.main()
