"""Tests for the health assessment (stdlib unittest only).

Run from ~/workspace/grokbot-own/:
    python3 -m unittest discover -s office/tests -v
"""

import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from office.health import assess_health


def check(ok=True, severity="core", detail=""):
    return {"ok": ok, "severity": severity, "detail": detail or "d"}


class TestAssessHealth(unittest.TestCase):
    def test_all_ok(self):
        result = assess_health({"memory": check(), "chat": check()},
                               started_at=1000, now=1060, version="v1")
        self.assertEqual(result["level"], "ok")
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["uptime_seconds"], 60)
        self.assertEqual(result["version"], "v1")

    def test_optional_failure_is_warn(self):
        result = assess_health({
            "memory": check(),
            "mail": check(ok=False, severity="optional",
                          detail="mail bridge unreachable"),
        })
        self.assertEqual(result["level"], "warn")
        self.assertEqual(result["reasons"], ["mail bridge unreachable"])

    def test_core_failure_is_bad(self):
        result = assess_health({
            "memory": check(ok=False, severity="core",
                            detail="memory db unreadable"),
            "mail": check(ok=False, severity="optional", detail="mail down"),
        })
        self.assertEqual(result["level"], "bad")
        self.assertEqual(result["reasons"],
                         ["memory db unreadable", "mail down"])

    def test_empty_checks_is_ok(self):
        result = assess_health({})
        self.assertEqual(result["level"], "ok")
        self.assertEqual(result["reasons"], [])

    def test_missing_severity_fails_closed(self):
        # A check without a severity is treated as core.
        result = assess_health({"mystery": {"ok": False}})
        self.assertEqual(result["level"], "bad")

    def test_malformed_check_counts_as_failed(self):
        result = assess_health({"weird": "not-a-dict"})
        self.assertEqual(result["level"], "bad")
        self.assertIn("weird", result["checks"])
        self.assertFalse(result["checks"]["weird"]["ok"])

    def test_unknown_severity_defaults_to_core(self):
        result = assess_health(
            {"x": {"ok": False, "severity": "cosmic", "detail": "x down"}})
        self.assertEqual(result["level"], "bad")

    def test_missing_detail_gets_a_default(self):
        result = assess_health({"mail": {"ok": False, "severity": "optional"}})
        self.assertEqual(result["reasons"], ["mail: failed"])

    def test_uptime_none_without_start(self):
        result = assess_health({})
        self.assertIsNone(result["uptime_seconds"])

    def test_uptime_never_negative(self):
        result = assess_health({}, started_at=2000, now=1000)
        self.assertEqual(result["uptime_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
