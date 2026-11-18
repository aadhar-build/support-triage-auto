"""Tests for triage.evaluate. Gold rows and threads are made up by hand; no real dataset rows.

.gitignore ignores *.csv, so every gold fixture is an inline string written to a temp dir.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from triage.cheap_tier import CheapTier
from triage.evaluate import (
    GoldFileError,
    GoldRow,
    Report,
    load_gold,
    report_to_json,
    report_to_markdown,
    score,
    wilson_interval,
)
from triage.router import RoutedThread, route_threads
from triage.threads import Thread, Turn
from triage.tiers import PLACEHOLDER_PRICES, FakeAgentTier, FakeLargerTier, Labels

T0 = datetime(2017, 10, 31, 22, 0, 0, tzinfo=timezone.utc)
HEADER = "thread_id,intent,action,should_escalate\n"


def rt(thread_id, intent, action, tiers, urgency="normal", confidence=0.9):
    lab = Labels(intent, urgency, action, confidence)
    return RoutedThread(thread_id=thread_id, labels=lab, cheap_labels=lab, tiers=tuple(tiers))


def g(intent, action, esc):
    return GoldRow(intent=intent, action=action, should_escalate=esc)


C, CL, CA, CLA = ("cheap",), ("cheap", "larger"), ("cheap", "agent"), ("cheap", "larger", "agent")


class GoldFileMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def write(self, text, name="gold.csv"):
        path = os.path.join(self._tmp.name, name)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return path


class TestLoadGold(GoldFileMixin, unittest.TestCase):
    def test_load_gold_valid(self):
        path = self.write(
            "note,thread_id,intent,action,should_escalate,extra\n"
            "a,1,delivery,ask,false,x\n"
            "b,2,billing,route,TRUE,y\n"
            "c, 3 ,complaint,escalate, 1 ,z\n"
            "d,4,other,answer,0,w\n"
            "e,5,praise,answer,False,v\n"
        )
        gold = load_gold(path)
        self.assertEqual(gold, {
            1: g("delivery", "ask", False),
            2: g("billing", "route", True),
            3: g("complaint", "escalate", True),
            4: g("other", "answer", False),
            5: g("praise", "answer", False),
        })
        self.assertEqual(load_gold(self.write(HEADER, "header_only.csv")), {})
        self.assertEqual(load_gold(self.write("﻿" + HEADER + "7,billing,ask,0\n", "bom.csv")),
                         {7: g("billing", "ask", False)})

    def assert_gold_error(self, text, *needles):
        with self.assertRaises(GoldFileError) as cm:
            load_gold(self.write(text))
        msg = str(cm.exception)
        for needle in needles:
            self.assertIn(needle, msg)
        return msg

    def test_gold_missing_column(self):
        self.assert_gold_error("thread_id,intent,action\n1,delivery,ask\n", "should_escalate")

    def test_gold_bad_thread_id(self):
        for bad in ("abc", "-1", "1.5", ""):
            with self.subTest(thread_id=bad):
                self.assert_gold_error(HEADER + "1,delivery,ask,0\n%s,delivery,ask,0\n" % bad,
                                       "thread_id", "line 3")

    def test_gold_duplicate(self):
        self.assert_gold_error(HEADER + "1,delivery,ask,0\n1,billing,ask,0\n",
                               "duplicate", "line 3")

    def test_gold_unknown_intent(self):
        self.assert_gold_error(HEADER + "1,weather,ask,0\n", "intent", "weather", "line 2")

    def test_gold_unknown_action(self):
        self.assert_gold_error(HEADER + "1,delivery,ignore,0\n", "action", "ignore", "line 2")

    def test_gold_bad_bool(self):
        for bad in ("yes", "", "2", "t"):
            with self.subTest(value=bad):
                self.assert_gold_error(HEADER + "1,delivery,ask,%s\n" % bad,
                                       "should_escalate", "line 2")

    def test_gold_empty_file(self):
        self.assert_gold_error("", "empty")

    def test_gold_short_row(self):
        self.assert_gold_error(HEADER + "1,delivery\n", "line 2")

    def test_gold_not_found(self):
        with self.assertRaises(FileNotFoundError):
            load_gold(os.path.join(self._tmp.name, "nope.csv"))


class TestWilsonInterval(unittest.TestCase):
    # Expected bounds are worked by hand from the Wilson score formula with z = 1.96.
    def test_zero_successes(self):
        low, high = wilson_interval(0, 37)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, 0.094, delta=0.001)

    def test_all_successes(self):
        low, high = wilson_interval(37, 37)
        self.assertAlmostEqual(low, 0.906, delta=0.001)
        self.assertEqual(high, 1.0)
        for n in (1, 2, 5, 6, 300):
            with self.subTest(n=n):
                self.assertEqual(wilson_interval(0, n)[0], 0.0)
                self.assertEqual(wilson_interval(n, n)[1], 1.0)

    def test_zero_denominator(self):
        self.assertIsNone(wilson_interval(0, 0))

    def test_interior_value_and_bounds(self):
        low, high = wilson_interval(8, 10)
        self.assertAlmostEqual(low, 0.4902, delta=0.001)
        self.assertAlmostEqual(high, 0.9433, delta=0.001)
        for k, n in ((1, 3), (5, 5), (0, 1), (150, 300), (8, 10), (0, 37), (37, 37)):
            with self.subTest(k=k, n=n):
                low, high = wilson_interval(k, n)
                self.assertTrue(0.0 <= low <= k / n <= high <= 1.0, (low, high))


class TestScore(unittest.TestCase):
    def hand_fixture(self):
        routed = [
            rt(101, "delivery", "ask", CA),                # action ok, intent ok
            rt(102, "billing", "ask", CLA),                # action mismatch
            rt(103, "complaint", "answer", CA),            # missed escalation
            rt(104, "account_access", "escalate", CL),     # false escalation, intent mismatch
            rt(105, "outage_technical", "escalate", CL),   # correct escalation
            rt(106, "other", "ask", CA),                   # no gold row
        ]
        gold = {
            101: g("delivery", "ask", False),
            102: g("billing", "route", False),
            103: g("complaint", "escalate", True),
            104: g("outage_technical", "route", False),
            105: g("outage_technical", "escalate", True),
            999: g("praise", "answer", False),             # no routed thread
        }
        return routed, gold

    def test_intervals_match_rates(self):
        r = score(*self.hand_fixture(), PLACEHOLDER_PRICES)
        expected = {
            "action_accuracy_ci": (2, 5),
            "intent_accuracy_ci": (4, 5),
            "missed_escalation_rate_ci": (1, 2),
            "false_escalation_rate_ci": (1, 3),
            "needs_person_share_ci": (2, 6),
        }
        for field, (k, n) in expected.items():
            with self.subTest(field):
                self.assertEqual(getattr(r, field), wilson_interval(k, n))
        self.assertEqual(r.tier_shares_ci, {"cheap": wilson_interval(6, 6),
                                            "larger": wilson_interval(3, 6),
                                            "agent": wilson_interval(4, 6)})
        # independent check: 2 of 5 worked by hand
        low, high = r.action_accuracy_ci
        self.assertAlmostEqual(low, 0.1176, delta=0.001)
        self.assertAlmostEqual(high, 0.7693, delta=0.001)

    def test_interval_null_when_rate_null(self):
        cases = {
            "no routed threads": ([], {1: g("delivery", "ask", True)}),
            "no scored threads": ([rt(1, "delivery", "ask", CA)], {}),
        }
        for name, (routed, gold) in cases.items():
            with self.subTest(name):
                r = score(routed, gold, PLACEHOLDER_PRICES)
                for field in ("action_accuracy", "intent_accuracy", "missed_escalation_rate",
                              "false_escalation_rate", "needs_person_share"):
                    self.assertEqual(getattr(r, field) is None,
                                     getattr(r, field + "_ci") is None, field)
                for field in ("action_accuracy_ci", "intent_accuracy_ci",
                              "missed_escalation_rate_ci", "false_escalation_rate_ci"):
                    self.assertIsNone(getattr(r, field), field)

        r = score([], {}, PLACEHOLDER_PRICES)
        self.assertIsNone(r.needs_person_share_ci)
        self.assertEqual(r.tier_shares_ci, {"cheap": None, "larger": None, "agent": None})

        r = score([rt(1, "delivery", "escalate", CL), rt(2, "delivery", "ask", CA)],
                  {1: g("delivery", "ask", False), 2: g("delivery", "ask", False)},
                  PLACEHOLDER_PRICES)
        self.assertIsNone(r.missed_escalation_rate_ci)
        self.assertEqual(r.false_escalation_rate_ci, wilson_interval(1, 2))

        r = score([rt(1, "delivery", "escalate", CL), rt(2, "delivery", "ask", CA)],
                  {1: g("delivery", "escalate", True), 2: g("delivery", "escalate", True)},
                  PLACEHOLDER_PRICES)
        self.assertIsNone(r.false_escalation_rate_ci)
        self.assertEqual(r.missed_escalation_rate_ci, wilson_interval(1, 2))

    def test_metrics_hand_computed(self):
        routed = [
            rt(101, "delivery", "ask", CA),                # action ok, intent ok
            rt(102, "billing", "ask", CLA),                # action mismatch
            rt(103, "complaint", "answer", CA),            # missed escalation
            rt(104, "account_access", "escalate", CL),     # false escalation, intent mismatch
            rt(105, "outage_technical", "escalate", CL),   # correct escalation
            rt(106, "other", "ask", CA),                   # no gold row
        ]
        gold = {
            101: g("delivery", "ask", False),
            102: g("billing", "route", False),
            103: g("complaint", "escalate", True),
            104: g("outage_technical", "route", False),
            105: g("outage_technical", "escalate", True),
            999: g("praise", "answer", False),             # no routed thread
        }
        r = score(routed, gold, PLACEHOLDER_PRICES)
        self.assertIsInstance(r, Report)
        self.assertEqual(r.threads_routed, 6)
        self.assertEqual(r.threads_scored, 5)
        self.assertEqual(r.threads_without_gold, 1)
        self.assertEqual(r.gold_rows_unmatched, 1)
        self.assertAlmostEqual(r.action_accuracy, 2 / 5)
        self.assertAlmostEqual(r.intent_accuracy, 4 / 5)
        self.assertEqual(r.missed_escalations, 1)
        self.assertAlmostEqual(r.missed_escalation_rate, 1 / 2)
        self.assertEqual(r.false_escalations, 1)
        self.assertAlmostEqual(r.false_escalation_rate, 1 / 3)
        self.assertAlmostEqual(r.needs_person_share, 2 / 6)
        self.assertEqual(set(r.tier_shares), {"cheap", "larger", "agent"})
        self.assertAlmostEqual(r.tier_shares["cheap"], 1.0)
        self.assertAlmostEqual(r.tier_shares["larger"], 3 / 6)
        self.assertAlmostEqual(r.tier_shares["agent"], 4 / 6)
        # agent 0.01 x4, larger 0.002 x3 = 0.046 over 6 threads
        self.assertAlmostEqual(r.cost_per_1000_threads, 1000 * 0.046 / 6)

    def test_zero_division(self):
        cases = {
            "no routed threads": ([], {1: g("delivery", "ask", True)}),
            "no scored threads": ([rt(1, "delivery", "ask", CA)], {}),
        }
        for name, (routed, gold) in cases.items():
            with self.subTest(name):
                r = score(routed, gold, PLACEHOLDER_PRICES)
                for field in ("action_accuracy", "intent_accuracy", "missed_escalation_rate",
                              "false_escalation_rate"):
                    self.assertIsNone(getattr(r, field), field)
                self.assertEqual((r.missed_escalations, r.false_escalations), (0, 0))
                data = json.loads(report_to_json(r))
                self.assertIsNone(data["action_accuracy"])
                self.assertIn("n/a", report_to_markdown(r))

        r = score([], {}, PLACEHOLDER_PRICES)
        self.assertIsNone(r.needs_person_share)
        self.assertIsNone(r.cost_per_1000_threads)
        self.assertEqual(set(r.tier_shares), {"cheap", "larger", "agent"})
        self.assertTrue(all(v is None for v in r.tier_shares.values()))
        data = json.loads(report_to_json(r))
        self.assertIsNone(data["cost_per_1000_threads"])
        self.assertIsNone(data["needs_person_share"])

        # no gold escalations: miss rate undefined, false rate defined
        r = score([rt(1, "delivery", "escalate", CL), rt(2, "delivery", "ask", CA)],
                  {1: g("delivery", "ask", False), 2: g("delivery", "ask", False)},
                  PLACEHOLDER_PRICES)
        self.assertIsNone(r.missed_escalation_rate)
        self.assertEqual(r.missed_escalations, 0)
        self.assertAlmostEqual(r.false_escalation_rate, 0.5)
        self.assertIsNone(json.loads(report_to_json(r))["missed_escalation_rate"])

        # no gold non-escalations: false rate undefined, miss rate defined
        r = score([rt(1, "delivery", "escalate", CL), rt(2, "delivery", "ask", CA)],
                  {1: g("delivery", "escalate", True), 2: g("delivery", "escalate", True)},
                  PLACEHOLDER_PRICES)
        self.assertIsNone(r.false_escalation_rate)
        self.assertEqual(r.false_escalations, 0)
        self.assertAlmostEqual(r.missed_escalation_rate, 0.5)
        self.assertIn("n/a", report_to_markdown(r))

    def test_cost_arithmetic(self):
        prices = {"cheap": 0.001, "larger": 0.003, "agent": 0.02}
        routed = [rt(1, "delivery", "route", C), rt(2, "delivery", "route", CL),
                  rt(3, "delivery", "ask", CA), rt(4, "delivery", "ask", CLA)]
        r = score(routed, {}, prices)
        expected = 1000 * ((0.001) + (0.001 + 0.003) + (0.001 + 0.02)
                           + (0.001 + 0.003 + 0.02)) / 4
        self.assertAlmostEqual(r.cost_per_1000_threads, expected)
        self.assertAlmostEqual(expected, 12.5)
        r = score(routed, {}, PLACEHOLDER_PRICES)
        self.assertAlmostEqual(r.cost_per_1000_threads,
                               1000 * ((0 + 0) + (0 + 0.002) + (0 + 0.01) + (0 + 0.002 + 0.01)) / 4)
        self.assertEqual(set(r.tier_shares), {"cheap", "larger", "agent"})
        self.assertEqual(r.tier_shares, {"cheap": 1.0, "larger": 0.5, "agent": 0.5})
        with self.assertRaises(ValueError):
            score(routed, {}, {"cheap": 0.0, "larger": 0.002})
        with self.assertRaises(ValueError):
            score(routed, {}, {"cheap": -1.0, "larger": 0.002, "agent": 0.01})

    def test_placeholder_flag(self):
        routed = [rt(1, "delivery", "route", C)]
        r = score(routed, {}, dict(PLACEHOLDER_PRICES))
        self.assertTrue(r.prices_are_placeholders)
        self.assertEqual(r.prices, PLACEHOLDER_PRICES)
        self.assertTrue(json.loads(report_to_json(r))["prices_are_placeholders"])
        self.assertIn("placeholder", report_to_markdown(r).lower())
        r = score(routed, {}, {"cheap": 0.0, "larger": 0.002, "agent": 0.02})
        self.assertFalse(r.prices_are_placeholders)
        self.assertNotIn("not real prices", report_to_markdown(r).lower())


class TestReportOutput(unittest.TestCase):
    def test_report_has_no_text_or_ids(self):
        ids = [987650001, 987650002, 987650003]
        texts = ["urgent my package SENTINEL_TEXT_A", "thanks love it SENTINEL_TEXT_B",
                 "hello SENTINEL_TEXT_C"]
        threads = []
        for i, (tid, text) in enumerate(zip(ids, texts)):
            turns = (Turn(tid, "SENTINELAUTHOR%d" % i, True, T0, text),
                     Turn(tid + 100, "SENTINELBRAND", False, T0 + timedelta(minutes=1),
                          "sorry SENTINEL_TEXT_REPLY"))
            threads.append(Thread(root_tweet_id=tid, brand_ids=("SENTINELBRAND",), turns=turns,
                                  brand_replied=True))
        routed = route_threads(threads, CheapTier(), FakeLargerTier(), FakeAgentTier())
        gold = {ids[0]: g("delivery", "escalate", True), ids[1]: g("praise", "answer", False),
                987659999: g("other", "ask", False)}
        r = score(routed, gold, PLACEHOLDER_PRICES, sample_size=3, seed=11, split="dev",
                  threshold=0.5)
        js = report_to_json(r)
        data = json.loads(js)
        self.assertEqual(js, json.dumps(data, sort_keys=True, indent=2) + "\n")
        self.assertEqual((data["sample_size"], data["seed"], data["split"], data["threshold"]),
                         (3, 11, "dev", 0.5))
        md = report_to_markdown(r)
        self.assertTrue(md.startswith("#"))
        self.assertIn("| ", md)
        for out in (js, md):
            self.assertNotIn("SENTINEL", out)
            for tid in ids + [987659999]:
                self.assertNotIn(str(tid), out)
            for t in threads:
                for turn in t.turns:
                    self.assertNotIn(str(turn.tweet_id), out)


if __name__ == "__main__":
    unittest.main()
