"""Tests for triage.tiers. All threads are made up by hand; no real dataset rows."""

import ast
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from triage.cheap_tier import CheapTier
from triage.threads import Thread, Turn
from triage.tiers import (
    ACTIONABLE_ACTIONS,
    ACTIONS,
    INTENTS,
    PLACEHOLDER_PRICES,
    URGENCIES,
    AgentTier,
    FakeAgentTier,
    FakeLargerTier,
    Labels,
    LabellingTier,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
NEW_MODULES = ("tiers.py", "cheap_tier.py", "router.py", "evaluate.py", "run_eval.py")
T0 = datetime(2017, 10, 31, 22, 0, 0, tzinfo=timezone.utc)


def thread(root_id, text):
    turn = Turn(root_id, "cust_%d" % root_id, True, T0, text)
    return Thread(root_tweet_id=root_id, brand_ids=(), turns=(turn,), brand_replied=False)


class TestVocab(unittest.TestCase):
    def test_vocabularies(self):
        self.assertEqual(INTENTS, ("delivery", "billing", "account_access", "outage_technical",
                                   "complaint", "praise", "other"))
        self.assertEqual(URGENCIES, ("low", "normal", "high"))
        self.assertEqual(ACTIONS, ("answer", "ask", "route", "escalate"))
        self.assertEqual(ACTIONABLE_ACTIONS, ("answer", "ask"))


class TestLabels(unittest.TestCase):
    def test_valid(self):
        lab = Labels("delivery", "normal", "ask", 0.6)
        self.assertEqual((lab.intent, lab.urgency, lab.action, lab.confidence),
                         ("delivery", "normal", "ask", 0.6))
        Labels("other", "low", "answer", 0.0)
        Labels("praise", "high", "escalate", 1.0)

    def test_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            Labels("weather", "normal", "ask", 0.5)
        with self.assertRaises(ValueError):
            Labels("delivery", "critical", "ask", 0.5)
        with self.assertRaises(ValueError):
            Labels("delivery", "normal", "ignore", 0.5)
        with self.assertRaises(ValueError):
            Labels("delivery", "normal", "ask", -0.01)
        with self.assertRaises(ValueError):
            Labels("delivery", "normal", "ask", 1.01)
        with self.assertRaises(ValueError):
            Labels("delivery", "normal", "ask", float("nan"))

    def test_frozen(self):
        lab = Labels("delivery", "normal", "ask", 0.6)
        with self.assertRaises(Exception):
            lab.intent = "billing"


class TestFakeTiers(unittest.TestCase):
    def test_larger_override(self):
        override = Labels("billing", "high", "escalate", 0.95)
        larger = FakeLargerTier(overrides={7: override})
        self.assertEqual(larger.label(thread(7, "my package SENTINEL_TEXT_7")), override)

    def test_larger_default_is_cheap_with_full_confidence(self):
        larger = FakeLargerTier(overrides={7: Labels("billing", "high", "escalate", 0.95)})
        t = thread(8, "my package SENTINEL_TEXT_8")
        cheap = CheapTier().label(t)
        got = larger.label(t)
        self.assertEqual((got.intent, got.urgency, got.action),
                         (cheap.intent, cheap.urgency, cheap.action))
        self.assertEqual(got.confidence, 1.0)
        self.assertEqual(FakeLargerTier().label(t), got)

    def test_agent_passes_through_and_counts(self):
        agent = FakeAgentTier()
        self.assertEqual(agent.calls, 0)
        lab = Labels("praise", "low", "answer", 0.75)
        self.assertIs(agent.act(thread(1, "x"), lab), lab)
        self.assertEqual(agent.act(thread(2, "y"), lab), lab)
        self.assertEqual(agent.calls, 2)

    def test_names_and_default_prices(self):
        cheap, larger, agent = CheapTier(), FakeLargerTier(), FakeAgentTier()
        self.assertEqual((cheap.name, larger.name, agent.name), ("cheap", "larger", "agent"))
        self.assertEqual(cheap.price_per_call, PLACEHOLDER_PRICES["cheap"])
        self.assertEqual(larger.price_per_call, PLACEHOLDER_PRICES["larger"])
        self.assertEqual(agent.price_per_call, PLACEHOLDER_PRICES["agent"])
        self.assertEqual(PLACEHOLDER_PRICES, {"cheap": 0.0, "larger": 0.002, "agent": 0.01})
        self.assertIsInstance(cheap, LabellingTier)
        self.assertIsInstance(larger, LabellingTier)
        self.assertIsInstance(agent, AgentTier)

    def test_custom_prices(self):
        self.assertEqual(CheapTier(price_per_call=0.5).price_per_call, 0.5)
        self.assertEqual(FakeLargerTier(price_per_call=0.3).price_per_call, 0.3)
        self.assertEqual(FakeAgentTier(price_per_call=0.7).price_per_call, 0.7)


class TestNoNetwork(unittest.TestCase):
    def test_no_network_imports(self):
        banned = {"urllib", "http", "socket", "requests", "ssl", "ftplib", "smtplib"}
        stdlib = set(sys.stdlib_module_names)
        for name in NEW_MODULES:
            path = REPO_ROOT / "src" / "triage" / name
            src = path.read_text(encoding="utf-8")
            self.assertNotIn("os.environ", src, name)
            self.assertNotIn("getenv", src, name)
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        continue
                    mods = [node.module or ""]
                else:
                    continue
                for mod in mods:
                    top = mod.split(".")[0]
                    self.assertNotIn(top, banned, "%s imports %s" % (name, mod))
                    self.assertTrue(top == "triage" or top in stdlib,
                                    "%s imports non-stdlib %s" % (name, mod))


if __name__ == "__main__":
    unittest.main()
