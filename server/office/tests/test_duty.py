"""Tests for the duty state machine (stdlib unittest only).

Run from ~/workspace/grokbot-own/:
    python3 -m unittest discover -s office/tests -v
"""

import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from office.duty import CHAT_LIVE_MS, chat_is_live, duty_of


class TestDutyOf(unittest.TestCase):
    def test_idle_by_default(self):
        self.assertEqual(duty_of(), "idle")

    def test_meeting_beats_everything(self):
        self.assertEqual(duty_of(in_meeting=True, has_active_card=True,
                                 chat_live=True, has_review_card=True),
                         "meeting")

    def test_active_card_is_desk(self):
        self.assertEqual(duty_of(has_active_card=True), "desk")

    def test_live_chat_is_desk(self):
        self.assertEqual(duty_of(chat_live=True), "desk")

    def test_desk_beats_review(self):
        self.assertEqual(duty_of(has_active_card=True, has_review_card=True),
                         "desk")
        self.assertEqual(duty_of(chat_live=True, has_review_card=True), "desk")

    def test_review_card_is_review(self):
        self.assertEqual(duty_of(has_review_card=True), "review")


class TestChatIsLive(unittest.TestCase):
    def test_none_timestamps_are_not_live(self):
        self.assertFalse(chat_is_live(None, 1000))
        self.assertFalse(chat_is_live(1000, None))

    def test_inside_window_is_live(self):
        self.assertTrue(chat_is_live(1000, 1000 + CHAT_LIVE_MS))
        self.assertTrue(chat_is_live(1000, 1000 + CHAT_LIVE_MS - 1))

    def test_outside_window_is_not_live(self):
        self.assertFalse(chat_is_live(1000, 1000 + CHAT_LIVE_MS + 1))

    def test_window_is_120_seconds(self):
        self.assertEqual(CHAT_LIVE_MS, 120_000)


if __name__ == "__main__":
    unittest.main()
