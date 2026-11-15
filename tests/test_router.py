"""Tests for triage.router. All threads are made up by hand; no real dataset rows."""

import unittest
from datetime import datetime, timezone

from triage.cheap_tier import CheapTier
from triage.router import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    RoutedThread,
    is_high_risk,
    route_thread,
    route_threads,
)
from triage.threads import Thread, Turn
from triage.tiers import AgentTier, FakeAgentTier, FakeLargerTier, Labels, LabellingTier

T0 = datetime(2017, 10, 31, 22, 0, 0, tzinfo=timezone.utc)


def thread(root_id, text="SENTINEL_TEXT"):
    turn = Turn(root_id, "cust_%d" % root_id, True, T0, text)
    return Thread(root_tweet_id=root_id, brand_ids=(), turns=(turn,), brand_replied=False)


class StubCheap(LabellingTier):
    name = "cheap"
    price_per_call = 0.0

    def __init__(self, labels):
        self.labels = labels
        self.calls = 0

    def label(self, thread):
        self.calls += 1
        return self.labels


class StubLarger(StubCheap):
    name = "larger"


class StubAgent(AgentTier):
    name = "agent"
    price_per_call = 0.0

    def __init__(self, result=None):
        self.result = result
        self.calls = 0

    def act(self, thread, labels):
        self.calls += 1
        return self.result if self.result is not None else labels


def L(intent="delivery", urgency="normal", action="route", confidence=0.9):
    return Labels(intent, urgency, action, confidence)


class TestHighRisk(unittest.TestCase):
    def test_is_high_risk(self):
        self.assertFalse(is_high_risk(L()))
        self.assertTrue(is_high_risk(L(urgency="high")))
        self.assertTrue(is_high_risk(L(intent="account_access")))
        self.assertTrue(is_high_risk(L(intent="outage_technical")))
        self.assertTrue(is_high_risk(L(action="escalate")))
        self.assertFalse(is_high_risk(L(intent="complaint", urgency="low", action="answer")))

    def test_default_threshold(self):
        self.assertEqual(DEFAULT_CONFIDENCE_THRESHOLD, 0.5)


class TestRouteThread(unittest.TestCase):
    def run_route(self, cheap_labels, larger_labels=None, threshold=DEFAULT_CONFIDENCE_THRESHOLD,
                  agent_result=None):
        cheap = StubCheap(cheap_labels)
        larger = StubLarger(larger_labels if larger_labels is not None else cheap_labels)
        agent = StubAgent(agent_result)
        r = route_thread(thread(5), cheap, larger, agent, threshold=threshold)
        return r, cheap, larger, agent

    def test_plain(self):
        r, cheap, larger, agent = self.run_route(L(confidence=0.9))
        self.assertEqual(r.tiers, ("cheap",))
        self.assertEqual(r.labels, L(confidence=0.9))
        self.assertEqual(r.cheap_labels, L(confidence=0.9))
        self.assertEqual((cheap.calls, larger.calls, agent.calls), (1, 0, 0))

    def test_actionable(self):
        t = thread(6, "thanks, love it SENTINEL_TEXT_6")
        agent = FakeAgentTier()
        r = route_thread(t, CheapTier(), FakeLargerTier(), agent)
        self.assertEqual(r.cheap_labels.action, "answer")
        self.assertEqual(r.cheap_labels.confidence, 0.75)
        self.assertEqual(r.tiers, ("cheap", "agent"))
        self.assertEqual(agent.calls, 1)
        self.assertEqual(r.labels, r.cheap_labels)

    def test_agent_result_is_final(self):
        final = L(action="escalate")
        r, *_ = self.run_route(L(action="ask"), agent_result=final)
        self.assertEqual(r.tiers, ("cheap", "agent"))
        self.assertEqual(r.labels, final)

    def test_low_confidence(self):
        larger_labels = L(intent="billing", action="route", confidence=1.0)
        r, _, larger, agent = self.run_route(L(confidence=0.3), larger_labels)
        self.assertIn("larger", r.tiers)
        self.assertEqual(r.tiers, ("cheap", "larger"))
        self.assertEqual(r.labels, larger_labels)
        self.assertEqual(r.cheap_labels, L(confidence=0.3))
        self.assertEqual((larger.calls, agent.calls), (1, 0))

    def test_low_confidence_then_actionable(self):
        r, *_ = self.run_route(L(action="ask", confidence=0.2))
        self.assertEqual(r.tiers, ("cheap", "larger", "agent"))

    def test_each_high_risk_condition(self):
        for labels in (L(urgency="high"), L(intent="account_access"),
                       L(intent="outage_technical"), L(action="escalate")):
            with self.subTest(labels=labels):
                r, _, larger, _ = self.run_route(labels)
                self.assertEqual(r.tiers, ("cheap", "larger"))
                self.assertEqual(larger.calls, 1)

    def test_override_to_escalate_skips_agent(self):
        t = thread(9, "hello there SENTINEL_TEXT_9")
        escalate = L(intent="complaint", urgency="high", action="escalate", confidence=1.0)
        agent = FakeAgentTier()
        r = route_thread(t, CheapTier(), FakeLargerTier(overrides={9: escalate}), agent)
        self.assertEqual(r.cheap_labels.action, "ask")
        self.assertEqual(r.tiers, ("cheap", "larger"))
        self.assertEqual(r.labels, escalate)
        self.assertEqual(agent.calls, 0)

    def test_threshold_boundary(self):
        r, *_ = self.run_route(L(confidence=0.5))
        self.assertEqual(r.tiers, ("cheap",))
        r, *_ = self.run_route(L(confidence=0.49))
        self.assertEqual(r.tiers, ("cheap", "larger"))
        r, *_ = self.run_route(L(confidence=0.7), threshold=0.7)
        self.assertEqual(r.tiers, ("cheap",))
        r, *_ = self.run_route(L(confidence=0.7), threshold=0.71)
        self.assertEqual(r.tiers, ("cheap", "larger"))

    def test_tier_names_come_from_tiers(self):
        cheap = StubCheap(L(action="ask", confidence=0.1))
        cheap.name = "c2"
        larger = StubLarger(L(action="ask"))
        larger.name = "l2"
        agent = StubAgent()
        agent.name = "a2"
        r = route_thread(thread(1), cheap, larger, agent)
        self.assertEqual(r.tiers, ("c2", "l2", "a2"))


class TestRouteThreads(unittest.TestCase):
    def test_order_and_ids(self):
        ids = [30, 10, 20]
        threads = [thread(i, "package SENTINEL_TEXT_%d" % i) for i in ids]
        out = route_threads(threads, CheapTier(), FakeLargerTier(), FakeAgentTier())
        self.assertEqual([r.thread_id for r in out], ids)
        for r, t in zip(out, threads):
            self.assertIsInstance(r, RoutedThread)
            self.assertEqual(r.thread_id, t.root_tweet_id)
        self.assertEqual(route_threads([], CheapTier(), FakeLargerTier(), FakeAgentTier()), [])

    def test_bad_threshold(self):
        t = thread(1)
        for bad in (-0.01, 1.01, float("nan")):
            with self.subTest(threshold=bad):
                with self.assertRaises(ValueError):
                    route_thread(t, CheapTier(), FakeLargerTier(), FakeAgentTier(), threshold=bad)
                with self.assertRaises(ValueError):
                    route_threads([t], CheapTier(), FakeLargerTier(), FakeAgentTier(),
                                  threshold=bad)
        route_thread(t, CheapTier(), FakeLargerTier(), FakeAgentTier(), threshold=0.0)
        route_thread(t, CheapTier(), FakeLargerTier(), FakeAgentTier(), threshold=1.0)


if __name__ == "__main__":
    unittest.main()
