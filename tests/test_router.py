"""Tests for triage.router. All threads are made up by hand; no real dataset rows."""

import unittest
from datetime import datetime, timezone
from pathlib import Path

from triage.cheap_tier import CheapTier
from triage.policy_check import load_escalate_topics
from triage.router import (
    CASCADE_CODES,
    CHEAP_CONFIDENT,
    ESCALATE_ALWAYS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    HIGH_RISK_TO_LARGER,
    LOW_CONFIDENCE_TO_LARGER,
    REASON_CODES,
    RoutedThread,
    TierStep,
    is_high_risk,
    is_known_reason_code,
    route_thread,
    route_threads,
)
from triage.threads import Thread, Turn
from triage.tiers import AgentTier, FakeAgentTier, FakeLargerTier, Labels, LabellingTier

T0 = datetime(2017, 10, 31, 22, 0, 0, tzinfo=timezone.utc)
TOPICS = load_escalate_topics(Path(__file__).resolve().parent / "fixtures" / "policy_rules.json")


def thread(root_id, text="SENTINEL_TEXT"):
    turn = Turn(root_id, "cust_%d" % root_id, True, T0, text)
    return Thread(root_tweet_id=root_id, brand_ids=(), turns=(turn,), brand_replied=False)


class StubCheap(LabellingTier):
    name = "cheap"
    model_id = "stub"
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
    model_id = "stub"
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
        r = route_thread(thread(5), cheap, larger, agent, threshold=threshold, topics=())
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
        r = route_thread(t, CheapTier(), FakeLargerTier(), agent, topics=())
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
        r = route_thread(t, CheapTier(), FakeLargerTier(overrides={9: escalate}), agent, topics=())
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
        r = route_thread(thread(1), cheap, larger, agent, topics=())
        self.assertEqual(r.tiers, ("c2", "l2", "a2"))


class TestRouteThreads(unittest.TestCase):
    def test_order_and_ids(self):
        ids = [30, 10, 20]
        threads = [thread(i, "package SENTINEL_TEXT_%d" % i) for i in ids]
        out = route_threads(threads, CheapTier(), FakeLargerTier(), FakeAgentTier(), topics=())
        self.assertEqual([r.thread_id for r in out], ids)
        for r, t in zip(out, threads):
            self.assertIsInstance(r, RoutedThread)
            self.assertEqual(r.thread_id, t.root_tweet_id)
        self.assertEqual(route_threads([], CheapTier(), FakeLargerTier(), FakeAgentTier(), topics=()), [])

    def test_bad_threshold(self):
        t = thread(1)
        for bad in (-0.01, 1.01, float("nan")):
            with self.subTest(threshold=bad):
                with self.assertRaises(ValueError):
                    route_thread(t, CheapTier(), FakeLargerTier(), FakeAgentTier(), threshold=bad, topics=())
                with self.assertRaises(ValueError):
                    route_threads([t], CheapTier(), FakeLargerTier(), FakeAgentTier(),
                                  threshold=bad, topics=())
        route_thread(t, CheapTier(), FakeLargerTier(), FakeAgentTier(), threshold=0.0, topics=())
        route_thread(t, CheapTier(), FakeLargerTier(), FakeAgentTier(), threshold=1.0, topics=())


def route(text, threshold=DEFAULT_CONFIDENCE_THRESHOLD, larger=None, root_id=11):
    return route_thread(thread(root_id, text), CheapTier(), larger or FakeLargerTier(),
                        FakeAgentTier(), threshold=threshold, topics=())


class TestReasonCodes(unittest.TestCase):
    def test_registry(self):
        self.assertEqual(CASCADE_CODES, ("cheap_confident", "low_confidence_to_larger",
                                         "high_risk_to_larger"))
        for code in CASCADE_CODES:
            with self.subTest(code=code):
                self.assertIn(code, REASON_CODES)
                self.assertTrue(is_known_reason_code(code))
        for bad in ("no_such_code", None, 3, b"cheap_confident", "", "cheap_confident:x"):
            with self.subTest(bad=bad):
                self.assertFalse(is_known_reason_code(bad))
        self.assertTrue(is_known_reason_code("x", codes=("x",)))
        self.assertFalse(is_known_reason_code("cheap_confident", codes=("x",)))

    def assert_codes(self, r, first):
        self.assertEqual(r.reason_codes[0], first)
        self.assertEqual(sum(c in CASCADE_CODES for c in r.reason_codes), 1)
        for code in r.reason_codes:
            self.assertTrue(is_known_reason_code(code), code)

    def test_confident_not_high_risk(self):
        r = route("package delivery SENTINEL_TEXT")  # delivery, 0.75, normal, ask
        self.assertEqual(r.cheap_labels.confidence, 0.75)
        self.assert_codes(r, CHEAP_CONFIDENT)
        self.assertNotIn("larger", r.tiers)
        self.assertNotIn("larger", [s.tier for s in r.steps])

    def test_low_confidence_not_high_risk(self):
        r = route("hello SENTINEL_TEXT")  # other, 0.2, normal, ask
        self.assertFalse(is_high_risk(r.cheap_labels))
        self.assert_codes(r, LOW_CONFIDENCE_TO_LARGER)

    def test_high_risk_high_confidence(self):
        r = route("password login locked out SENTINEL_TEXT")  # account_access, 0.9
        self.assertEqual(r.cheap_labels.confidence, 0.9)
        self.assert_codes(r, HIGH_RISK_TO_LARGER)

    def test_high_risk_low_confidence(self):
        r = route("urgent hello SENTINEL_TEXT")  # other, 0.2, high urgency
        self.assertLess(r.cheap_labels.confidence, DEFAULT_CONFIDENCE_THRESHOLD)
        self.assertTrue(is_high_risk(r.cheap_labels))
        self.assert_codes(r, HIGH_RISK_TO_LARGER)

    def test_threshold_decides_confident(self):
        self.assert_codes(route("package delivery", threshold=0.8), LOW_CONFIDENCE_TO_LARGER)
        self.assert_codes(route("package delivery", threshold=0.75), CHEAP_CONFIDENT)


class TestSteps(unittest.TestCase):
    def test_steps_record_each_tier(self):
        override = L(intent="billing", urgency="normal", action="ask", confidence=1.0)
        r = route("hello SENTINEL_TEXT", larger=FakeLargerTier(overrides={11: override}))
        self.assertEqual(r.tiers, ("cheap", "larger", "agent"))
        self.assertEqual(tuple(s.tier for s in r.steps), r.tiers)
        self.assertEqual(tuple(s.model_id for s in r.steps),
                         ("keywords", "fake-larger", "fake-agent"))
        self.assertNotEqual(r.cheap_labels, override)
        self.assertEqual(r.steps[0], TierStep("cheap", "keywords", r.cheap_labels))
        self.assertEqual(r.steps[1].labels, override)
        self.assertEqual(r.steps[2].labels, override)

    def test_agent_step_records_act_result(self):
        final = L(action="escalate")
        cheap = StubCheap(L(action="ask"))
        r = route_thread(thread(5), cheap, StubLarger(L()), StubAgent(final), topics=())
        self.assertEqual([s.tier for s in r.steps], ["cheap", "agent"])
        self.assertEqual(r.steps[1].labels, final)

    def test_missing_model_id_refused(self):
        class NoIdCheap(LabellingTier):
            name = "nocheap"
            price_per_call = 0.0

            def label(self, thread):
                return L()

        class NoIdAgent(AgentTier):
            name = "noagent"
            price_per_call = 0.0

            def act(self, thread, labels):
                return labels

        empty_larger = StubLarger(L())
        empty_larger.name = "emptylarger"
        empty_larger.model_id = ""
        none_cheap = StubCheap(L())
        none_cheap.name = "nonecheap"
        none_cheap.model_id = None
        ok = (StubCheap(L()), StubLarger(L()), StubAgent())
        cases = {
            "nocheap": (NoIdCheap(), ok[1], ok[2]),
            "nonecheap": (none_cheap, ok[1], ok[2]),
            "emptylarger": (ok[0], empty_larger, ok[2]),
            "noagent": (ok[0], ok[1], NoIdAgent()),
        }
        for name, (c, l, a) in cases.items():
            with self.subTest(tier=name):
                with self.assertRaisesRegex(ValueError, name):
                    route_thread(thread(1), c, l, a, topics=())
                with self.assertRaisesRegex(ValueError, name):
                    route_threads([thread(1)], c, l, a, topics=())
                with self.assertRaisesRegex(ValueError, name):
                    route_threads([], c, l, a, topics=())
        self.assertEqual(ok[0].calls, 0)

    def test_routed_fields_unchanged(self):
        cases = [
            ("package delivery", ("cheap", "agent"), L("delivery", "normal", "ask", 0.75),
             L("delivery", "normal", "ask", 0.75)),
            ("hello", ("cheap", "larger", "agent"), L("other", "normal", "ask", 0.2),
             L("other", "normal", "ask", 1.0)),
            ("password login locked out", ("cheap", "larger"),
             L("account_access", "normal", "route", 0.9),
             L("account_access", "normal", "route", 1.0)),
            ("thanks, love it", ("cheap", "agent"), L("praise", "low", "answer", 0.75),
             L("praise", "low", "answer", 0.75)),
        ]
        for text, tiers, cheap_labels, labels in cases:
            with self.subTest(text=text):
                r = route(text + " SENTINEL_TEXT", root_id=42)
                self.assertEqual(r.thread_id, 42)
                self.assertEqual(r.tiers, tiers)
                self.assertEqual(r.cheap_labels, cheap_labels)
                self.assertEqual(r.labels, labels)

    def test_defaults_keep_old_constructions(self):
        r = RoutedThread(thread_id=1, labels=L(), cheap_labels=L(), tiers=("cheap",))
        self.assertEqual((r.steps, r.reason_codes, r.topic_id), ((), (), ()))


class RaisingLarger(StubLarger):
    def label(self, thread):
        raise AssertionError("the larger tier must not run on a forced thread")


class RecordingAgent(StubAgent):
    def act(self, thread, labels):
        self.seen = labels
        return super().act(thread, labels)


def brand_thread(root_id, customer_text, brand_text):
    turns = (Turn(root_id, "cust", True, T0, customer_text),
             Turn(root_id + 1, "brand", False, T0, brand_text))
    return Thread(root_tweet_id=root_id, brand_ids=("brand",), turns=turns, brand_replied=True)


class TestEscalateAlways(unittest.TestCase):
    def test_forced_over_confident_cheap(self):
        cheap = StubCheap(L("praise", "low", "answer", 0.9))
        r = route_thread(thread(3, "hi, quuxfrob"), cheap, RaisingLarger(L()), StubAgent(),
                         topics=TOPICS)
        self.assertEqual(r.labels, Labels("praise", "low", "escalate", 0.9))
        self.assertEqual(r.cheap_labels, Labels("praise", "low", "answer", 0.9))

    def test_larger_skipped_agent_drafts(self):
        agent = RecordingAgent()
        r = route_thread(thread(3, "a zorblax claim"), StubCheap(L("billing", "high", "route", 0.3)),
                         RaisingLarger(L()), agent, topics=TOPICS)
        self.assertEqual(r.tiers, ("cheap", "agent"))
        self.assertEqual(len(r.steps), 2)
        self.assertEqual(agent.calls, 1)
        self.assertEqual(agent.seen, Labels("billing", "high", "escalate", 0.3))
        self.assertEqual(r.labels, Labels("billing", "high", "escalate", 0.3))

    def test_agent_cannot_unforce(self):
        draft = Labels("delivery", "normal", "answer", 0.99)
        r = route_thread(thread(3, "quuxfrob"), StubCheap(L("praise", "low", "answer", 0.9)),
                         RaisingLarger(L()), StubAgent(draft), topics=TOPICS)
        self.assertEqual(r.labels.action, "escalate")
        self.assertEqual(r.labels, Labels("praise", "low", "escalate", 0.9))
        self.assertEqual(r.steps[1], TierStep("agent", "stub", draft))

    def test_reason_code_and_topic_id(self):
        cheap = StubCheap(L("praise", "low", "answer", 0.9))
        r = route_thread(thread(3, "quuxfrob"), cheap, RaisingLarger(L()), StubAgent(),
                         topics=TOPICS)
        self.assertIn(ESCALATE_ALWAYS, r.reason_codes)
        self.assertIn(ESCALATE_ALWAYS, REASON_CODES)
        for code in r.reason_codes:
            self.assertTrue(is_known_reason_code(code), code)
        self.assertEqual(r.topic_id, ("t-zorblax",))
        both = route_thread(thread(4, "can't florp, quuxfrob"), cheap, RaisingLarger(L()),
                            StubAgent(), topics=TOPICS)
        self.assertEqual(both.topic_id, ("t-zorblax", "t-florp"))
        self.assertEqual(both.reason_codes.count(ESCALATE_ALWAYS), 1)

    def test_registry_appends_code(self):
        self.assertEqual(ESCALATE_ALWAYS, "escalate_always")
        self.assertEqual(REASON_CODES, CASCADE_CODES + ("escalate_always",))

    def test_brand_turn_never_forces(self):
        cheap = StubCheap(L("praise", "low", "answer", 0.9))
        r = route_thread(brand_thread(7, "thanks", "quuxfrob"), cheap, StubLarger(L()),
                         StubAgent(), topics=TOPICS)
        self.assertEqual(r.labels.action, "answer")
        self.assertEqual(r.topic_id, ())

    def test_no_match_is_unchanged(self):
        for topics in ((), TOPICS):
            with self.subTest(topics=len(topics)):
                r = route_thread(thread(5, "thanks, love it"), CheapTier(), FakeLargerTier(),
                                 FakeAgentTier(), topics=topics)
                self.assertEqual(r.reason_codes, (CHEAP_CONFIDENT,))
                self.assertEqual(r.topic_id, ())
                self.assertEqual(r.labels, L("praise", "low", "answer", 0.75))

    def test_route_threads_forces_too(self):
        out = route_threads([thread(1, "hello"), thread(2, "quuxfrob")], CheapTier(),
                            FakeLargerTier(), FakeAgentTier(), topics=TOPICS)
        self.assertEqual([r.topic_id for r in out], [(), ("t-zorblax",)])
        self.assertEqual(out[1].labels.action, "escalate")

    def test_topics_is_required_keyword(self):
        with self.assertRaises(TypeError):
            route_thread(thread(1), CheapTier(), FakeLargerTier(), FakeAgentTier())
        with self.assertRaises(TypeError):
            route_threads([], CheapTier(), FakeLargerTier(), FakeAgentTier())


if __name__ == "__main__":
    unittest.main()
