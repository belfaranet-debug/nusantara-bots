"""Cost ledger tests — stdlib unittest, no network, temp dirs.

Covers: record -> daily aggregates are correct; cap reached ->
new tasks refused; no cap set -> unlimited; unknown model ->
cost 0 + price_known False, tokens still recorded; price
estimates match the table (incl. dated-variant prefix match);
summary (per-bot split, totals, most expensive task); cap
config from file + env; validation errors; zero-trace check
(forbidden product/fork names, written as split literals).
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from costs.config import CostConfig  # noqa: E402
from costs.ledger import CAP_REACHED_MESSAGE, CostLedger  # noqa: E402
from costs.prices import (  # noqa: E402
    DEFAULT_PRICES,
    estimate_cost,
    lookup_price,
)


def _tmp_ledger(testcase, caps=None, prices=None, env=None):
    """A ledger on temp files, with an isolated CostConfig.

    caps: {"bot": cap} written to the runtime settings file.
    prices: price table written to the runtime settings file.
    env: extra env vars (restored by the test's addCleanup).
    """
    tmp = tempfile.mkdtemp()
    testcase.addCleanup(
        lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
    settings_path = os.path.join(tmp, "cost_settings.json")
    defaults_path = os.path.join(tmp, "defaults.json")
    runtime = {}
    if caps is not None:
        runtime["caps"] = caps
    if prices is not None:
        runtime["prices"] = prices
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(runtime, f)
    with open(defaults_path, "w", encoding="utf-8") as f:
        json.dump({"caps": {}, "global_cap_usd": None, "prices": {}},
                  f)
    # Env overrides must not leak in from the real environment.
    saved = {}
    for key in ("GROKBOT_DAILY_CAP_USD",
                "GROKBOT_DAILY_CAP_USD_YT_SCRIPTWRITER",
                "GROKBOT_DAILY_CAP_USD_CHIEF",
                "GROKBOT_COST_PRICES_JSON"):
        saved[key] = os.environ.pop(key, None)
    testcase.addCleanup(
        lambda: [os.environ.__setitem__(k, v) if v is not None
                 else os.environ.pop(k, None)
                 for k, v in saved.items()])
    for key, value in (env or {}).items():
        os.environ[key] = value
    config = CostConfig(settings_path=settings_path,
                        defaults_path=defaults_path)
    ledger = CostLedger(path=os.path.join(tmp, "costs.json"),
                        config=config)
    return ledger, config, tmp


class PriceTest(unittest.TestCase):
    def test_estimate_matches_table(self):
        # gpt-4o-mini: $0.15 in / $0.60 out per 1M.
        # 1M in + 1M out = $0.75 exactly.
        cost, known = estimate_cost("gpt-4o-mini", 1_000_000,
                                    1_000_000)
        self.assertTrue(known)
        self.assertEqual(cost, 0.75)
        # 1200 in + 300 out = 1200*0.15e-6 + 300*0.60e-6 = 0.00036.
        cost, known = estimate_cost("gpt-4o-mini", 1200, 300)
        self.assertTrue(known)
        self.assertEqual(cost, 0.00036)

    def test_unknown_model_cost_zero_and_flagged(self):
        cost, known = estimate_cost("model-yang-tidak-ada", 1000, 500)
        self.assertEqual(cost, 0.0)
        self.assertFalse(known)

    def test_dated_variant_prices_like_base_model(self):
        cost, known = estimate_cost("gpt-4o-mini-2024-07-18",
                                    1_000_000, 0)
        self.assertTrue(known)
        self.assertEqual(cost, 0.15)
        self.assertEqual(lookup_price("gpt-4o-2024-11-20"),
                         lookup_price("gpt-4o"))

    def test_negative_tokens_rejected(self):
        with self.assertRaises(ValueError):
            estimate_cost("gpt-4o-mini", -1, 0)
        with self.assertRaises(ValueError):
            estimate_cost("gpt-4o-mini", 0, -5)

    def test_default_table_has_input_and_output(self):
        for model, entry in DEFAULT_PRICES.items():
            self.assertIn("input_per_1m", entry, model)
            self.assertIn("output_per_1m", entry, model)
            self.assertGreaterEqual(entry["input_per_1m"], 0)
            self.assertGreaterEqual(entry["output_per_1m"], 0)


class RecordAndSummaryTest(unittest.TestCase):
    def test_record_then_daily_aggregate_correct(self):
        ledger, _, _ = _tmp_ledger(self)
        day = "2026-10-25"
        when = datetime(2026, 10, 25, 6, 40, tzinfo=timezone.utc)
        r1 = ledger.record("yt-scriptwriter", "card-12", "gpt-4o-mini",
                           1200, 300, task_kind="chat",
                           recorded_at=when)
        r2 = ledger.record("yt-scriptwriter", "card-13", "gpt-4o-mini",
                           800, 200, task_kind="chat",
                           recorded_at=when)
        ledger.record("chief", "task-1", "gpt-4o", 100, 50,
                      task_kind="chat", recorded_at=when)
        self.assertEqual(r1["total_tokens"], 1500)
        self.assertEqual(r1["cost_usd"], 0.00036)
        self.assertTrue(r1["price_known"])
        self.assertEqual(r1["day"], day)
        summary = ledger.daily_summary(day=day)
        self.assertEqual(summary["day"], day)
        sw = summary["bots"]["yt-scriptwriter"]
        self.assertEqual(sw["prompt_tokens"], 2000)
        self.assertEqual(sw["completion_tokens"], 500)
        self.assertEqual(sw["total_tokens"], 2500)
        self.assertEqual(sw["cost_usd"], 0.0006)
        self.assertEqual(sw["tasks"], 2)
        self.assertEqual(sw["price_unknown_tasks"], 0)
        self.assertEqual(summary["totals"]["tasks"], 3)
        self.assertEqual(summary["totals"]["total_tokens"], 2650)
        # gpt-4o 100 in + 50 out = 0.00025 + 0.0005 = 0.00075.
        self.assertEqual(summary["totals"]["cost_usd"],
                         round(0.0006 + 0.00075, 8))
        # Most expensive single task: chief's gpt-4o call ($0.00075).
        most = summary["most_expensive_task"]
        self.assertEqual(most["bot"], "chief")
        self.assertEqual(most["task_id"], "task-1")
        self.assertEqual(most["cost_usd"], 0.00075)

    def test_summary_empty_day(self):
        ledger, _, _ = _tmp_ledger(self)
        summary = ledger.daily_summary(day="2026-10-25")
        self.assertEqual(summary["bots"], {})
        self.assertEqual(summary["totals"]["tasks"], 0)
        self.assertEqual(summary["totals"]["cost_usd"], 0.0)
        self.assertIsNone(summary["most_expensive_task"])

    def test_summary_filters_by_bot_and_day(self):
        ledger, _, _ = _tmp_ledger(self)
        t25 = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)
        t26 = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)
        ledger.record("yt-scriptwriter", "c1", "gpt-4o-mini", 100, 0,
                      recorded_at=t25)
        ledger.record("yt-scriptwriter", "c2", "gpt-4o-mini", 100, 0,
                      recorded_at=t26)
        self.assertEqual(
            ledger.daily_summary(day="2026-10-25")["totals"]["tasks"], 1)
        self.assertEqual(
            ledger.daily_summary(day="2026-10-26")["totals"]["tasks"], 1)
        self.assertEqual(
            ledger.daily_summary(day="2026-10-25",
                                 bot="chief")["totals"]["tasks"], 0)

    def test_unknown_model_recorded_with_zero_cost_and_flag(self):
        ledger, _, _ = _tmp_ledger(self)
        when = datetime(2026, 10, 25, 6, 40, tzinfo=timezone.utc)
        rec = ledger.record("chief", "t1", "model-tanpa-harga",
                            1000, 500, recorded_at=when)
        self.assertEqual(rec["cost_usd"], 0.0)
        self.assertFalse(rec["price_known"])
        self.assertEqual(rec["total_tokens"], 1500)
        summary = ledger.daily_summary(day="2026-10-25")
        self.assertEqual(summary["totals"]["total_tokens"], 1500)
        self.assertEqual(summary["totals"]["cost_usd"], 0.0)
        self.assertEqual(summary["bots"]["chief"]["price_unknown_tasks"], 1)
        # Most expensive task still exists (cost 0, most tokens).
        self.assertEqual(summary["most_expensive_task"]["id"], rec["id"])

    def test_record_validation(self):
        ledger, _, _ = _tmp_ledger(self)
        with self.assertRaises(ValueError):
            ledger.record("", "t1", "gpt-4o-mini", 1, 1)
        with self.assertRaises(ValueError):
            ledger.record("chief", "t1", "gpt-4o-mini", -1, 1)
        with self.assertRaises(ValueError):
            ledger.record("chief", "t1", "gpt-4o-mini", 1.5, 1)
        with self.assertRaises(ValueError):
            ledger.record("chief", "t1", "gpt-4o-mini", True, 1)

    def test_list_records_newest_first(self):
        ledger, _, _ = _tmp_ledger(self)
        t1 = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)
        ledger.record("chief", "t1", "gpt-4o-mini", 10, 0,
                      recorded_at=t1)
        ledger.record("chief", "t2", "gpt-4o-mini", 10, 0,
                      recorded_at=t2)
        records = ledger.list_records(bot="chief")
        self.assertEqual([r["task_id"] for r in records], ["t2", "t1"])
        self.assertEqual(len(ledger.list_records(bot="chief",
                                                 limit=1)), 1)


class CapTest(unittest.TestCase):
    def test_no_cap_means_unlimited(self):
        ledger, config, _ = _tmp_ledger(self)
        self.assertIsNone(config.cap_for("yt-scriptwriter"))
        self.assertIsNone(config.global_cap())
        gate = ledger.check_cap("yt-scriptwriter", day="2026-10-25")
        self.assertTrue(gate["allowed"])
        self.assertIsNone(gate["cap_usd"])
        self.assertIsNone(gate["remaining_usd"])
        self.assertEqual(gate["spent_usd"], 0.0)

    def test_cap_reached_refuses_new_task(self):
        # Cap $0.01; one gpt-4o call of 1000 in + 1000 out costs
        # 0.0025 + 0.01 = $0.0125 -> over the cap.
        ledger, _, _ = _tmp_ledger(self, caps={"chief": 0.01})
        when = datetime(2026, 10, 25, 6, 40, tzinfo=timezone.utc)
        before = ledger.check_cap("chief", day="2026-10-25")
        self.assertTrue(before["allowed"])
        self.assertEqual(before["cap_usd"], 0.01)
        ledger.record("chief", "t1", "gpt-4o", 1000, 1000,
                      recorded_at=when)
        gate = ledger.check_cap("chief", day="2026-10-25")
        self.assertFalse(gate["allowed"])
        self.assertEqual(gate["cap_usd"], 0.01)
        self.assertEqual(gate["spent_usd"], 0.0125)
        self.assertEqual(gate["remaining_usd"], 0.0)
        self.assertIn(CAP_REACHED_MESSAGE, gate["message"])
        self.assertIn("chief", gate["message"])
        # A different bot is unaffected; tomorrow is unaffected.
        self.assertTrue(ledger.check_cap("yt-scriptwriter",
                                         day="2026-10-25")["allowed"])
        self.assertTrue(ledger.check_cap("chief",
                                         day="2026-10-26")["allowed"])

    def test_cap_exactly_reached_refuses(self):
        # 4000 in + 1000 out of gpt-4o-mini = 0.0006 + 0.0006 = $0.0012.
        ledger, _, _ = _tmp_ledger(self,
                                   caps={"yt-scriptwriter": 0.0012})
        when = datetime(2026, 10, 25, 6, 40, tzinfo=timezone.utc)
        ledger.record("yt-scriptwriter", "c1", "gpt-4o-mini", 4000,
                      1000, recorded_at=when)
        gate = ledger.check_cap("yt-scriptwriter", day="2026-10-25")
        self.assertFalse(gate["allowed"])  # spent == cap counts as reached

    def test_cap_not_yet_reached_allows(self):
        ledger, _, _ = _tmp_ledger(self, caps={"chief": 1.00})
        when = datetime(2026, 10, 25, 6, 40, tzinfo=timezone.utc)
        ledger.record("chief", "t1", "gpt-4o-mini", 1000, 1000,
                      recorded_at=when)
        gate = ledger.check_cap("chief", day="2026-10-25")
        self.assertTrue(gate["allowed"])
        self.assertEqual(gate["spent_usd"], 0.00075)
        self.assertEqual(gate["remaining_usd"], round(1.0 - 0.00075, 8))

    def test_recording_never_refuses_on_cap(self):
        # A record is a fact about spend that already happened.
        ledger, _, _ = _tmp_ledger(self, caps={"chief": 0.001})
        when = datetime(2026, 10, 25, 6, 40, tzinfo=timezone.utc)
        rec = ledger.record("chief", "t1", "gpt-4o", 10000, 10000,
                            recorded_at=when)
        self.assertEqual(rec["cost_usd"], 0.125)
        self.assertFalse(ledger.check_cap("chief",
                                          day="2026-10-25")["allowed"])

    def test_global_cap_from_env(self):
        ledger, config, _ = _tmp_ledger(
            self, env={"GROKBOT_DAILY_CAP_USD": "0.5"})
        self.assertEqual(config.global_cap(), 0.5)
        self.assertEqual(config.cap_for("any-bot"), 0.5)
        gate = ledger.check_cap("any-bot", day="2026-10-25")
        self.assertEqual(gate["cap_usd"], 0.5)

    def test_per_bot_env_cap_beats_global(self):
        _, config, _ = _tmp_ledger(
            self, env={"GROKBOT_DAILY_CAP_USD": "5",
                       "GROKBOT_DAILY_CAP_USD_YT_SCRIPTWRITER": "2"})
        self.assertEqual(config.cap_for("yt-scriptwriter"), 2.0)
        self.assertEqual(config.cap_for("chief"), 5.0)

    def test_file_cap_beats_env_cap(self):
        _, config, _ = _tmp_ledger(
            self, caps={"yt-scriptwriter": 3},
            env={"GROKBOT_DAILY_CAP_USD_YT_SCRIPTWRITER": "2"})
        self.assertEqual(config.cap_for("yt-scriptwriter"), 3.0)

    def test_invalid_cap_values_mean_unlimited(self):
        _, config, _ = _tmp_ledger(
            self, env={"GROKBOT_DAILY_CAP_USD": "not-a-number"})
        self.assertIsNone(config.global_cap())
        ledger2, config2, _ = _tmp_ledger(self, caps={"chief": 0})
        self.assertIsNone(config2.cap_for("chief"))
        ledger3, config3, _ = _tmp_ledger(self, caps={"chief": -5})
        self.assertIsNone(config3.cap_for("chief"))

    def test_set_and_clear_cap(self):
        ledger, config, _ = _tmp_ledger(self)
        self.assertIsNone(config.cap_for("chief"))
        caps = config.set_cap("chief", 2.5)
        self.assertEqual(caps, {"chief": 2.5})
        self.assertEqual(config.cap_for("chief"), 2.5)
        # A fresh config object reading the same file sees it too.
        fresh = CostConfig(settings_path=config.settings_path,
                           defaults_path=config.defaults_path)
        self.assertEqual(fresh.cap_for("chief"), 2.5)
        self.assertEqual(ledger.check_cap("chief",
                                          day="2026-10-25")["cap_usd"],
                         2.5)
        caps = config.clear_cap("chief")
        self.assertEqual(caps, {})
        self.assertIsNone(config.cap_for("chief"))
        with self.assertRaises(ValueError):
            config.set_cap("chief", 0)
        with self.assertRaises(ValueError):
            config.set_cap("chief", -1)
        with self.assertRaises(ValueError):
            config.set_cap("", 1)

    def test_custom_prices_override_table(self):
        ledger, _, _ = _tmp_ledger(
            self, prices={"model-custom": {"input_per_1m": 1.0,
                                           "output_per_1m": 3.0}})
        when = datetime(2026, 10, 25, 6, 40, tzinfo=timezone.utc)
        rec = ledger.record("chief", "t1", "model-custom", 1_000_000,
                            1_000_000, recorded_at=when)
        self.assertTrue(rec["price_known"])
        self.assertEqual(rec["cost_usd"], 4.0)


class ZeroTraceTest(unittest.TestCase):
    def test_no_forbidden_product_names_in_costs_files(self):
        banned = ["rak" + "azo", "octo" + "bot", "open" + "dots"]
        here = Path(__file__).resolve()
        files = [here, REPO / "costs" / "__init__.py",
                 REPO / "costs" / "ledger.py",
                 REPO / "costs" / "prices.py",
                 REPO / "costs" / "config.py",
                 REPO / "costs" / "README.md",
                 REPO / "costs" / "server-wiring.patch.md",
                 REPO / "costs" / "defaults.json"]
        for path in files:
            text = path.read_text(encoding="utf-8").lower()
            for name in banned:
                self.assertNotIn(name, text, "%s found in %s" % (name, path))


if __name__ == "__main__":
    unittest.main()
