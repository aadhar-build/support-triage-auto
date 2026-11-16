"""Tests for triage.cheap_tier. All threads are made up by hand; no real dataset rows."""

import unittest
from datetime import datetime, timedelta, timezone

from triage.cheap_tier import (
    CONFIDENCE_BY_HITS,
    INTENT_KEYWORDS,
    INTENT_PRIORITY,
    NO_MATCH_CONFIDENCE,
    TIE_CONFIDENCE,
    CheapTier,
)
from triage.threads import Thread, Turn
from triage.tiers import INTENTS

T0 = datetime(2017, 10, 31, 22, 0, 0, tzinfo=timezone.utc)


def thread(*turns, root_id=100):
    """turns: (inbound, text) pairs in order."""
    built = tuple(Turn(root_id + i, "cust_x" if inbound else "BrandX", inbound,
                       T0 + timedelta(minutes=i), text)
                  for i, (inbound, text) in enumerate(turns))
    brands = tuple(sorted({t.author_id for t in built if not t.inbound}))
    return Thread(root_tweet_id=root_id, brand_ids=brands, turns=built,
                  brand_replied=bool(brands))


def cust(text, root_id=100):
    return thread((True, text + " SENTINEL_TEXT_%d" % root_id), root_id=root_id)


LABEL = CheapTier().label


class TestRuleTables(unittest.TestCase):
    def test_tables(self):
        self.assertEqual(set(INTENT_KEYWORDS), set(INTENTS) - {"other"})
        self.assertEqual(INTENT_PRIORITY, ("account_access", "outage_technical", "billing",
                                           "delivery", "complaint", "praise"))
        self.assertEqual(NO_MATCH_CONFIDENCE, 0.2)
        self.assertEqual(TIE_CONFIDENCE, 0.4)
        self.assertEqual(CONFIDENCE_BY_HITS[1], 0.6)
        self.assertEqual(CONFIDENCE_BY_HITS[2], 0.75)
        self.assertEqual(CONFIDENCE_BY_HITS[3], 0.9)


class TestIntent(unittest.TestCase):
    def test_single_keyword_per_intent(self):
        cases = {
            "delivery": "where is my parcel",
            "billing": "please send the invoice",
            "account_access": "i forgot my password",
            "outage_technical": "is there an outage",
            "complaint": "the agent was rude",
            "praise": "i love it",
        }
        for intent, text in cases.items():
            with self.subTest(intent=intent):
                lab = LABEL(cust(text))
                self.assertEqual(lab.intent, intent)
                self.assertEqual(lab.confidence, 0.6)

    def test_no_match(self):
        lab = LABEL(cust("hello there"))
        self.assertEqual((lab.intent, lab.confidence, lab.action), ("other", 0.2, "ask"))
        empty = thread((False, "thanks for the refund"), (True, ""))
        self.assertEqual(LABEL(empty).intent, "other")
        brand_only = thread((False, "your package has shipped"))
        self.assertEqual(LABEL(brand_only).intent, "other")

    def test_tie_break_priority(self):
        lab = LABEL(cust("my package and my refund"))
        self.assertEqual((lab.intent, lab.confidence), ("billing", 0.4))
        lab = LABEL(cust("login problem with my package"))
        self.assertEqual((lab.intent, lab.confidence), ("account_access", 0.4))
        lab = LABEL(cust("awful but thanks"))
        self.assertEqual((lab.intent, lab.confidence), ("complaint", 0.4))

    def test_confidence_by_hits(self):
        self.assertEqual(LABEL(cust("package tracking")).confidence, 0.75)
        self.assertEqual(LABEL(cust("package tracking parcel")).confidence, 0.9)
        self.assertEqual(LABEL(cust("package tracking parcel shipping delivery")).confidence, 0.9)
        # A clear winner beats a smaller runner-up: no tie.
        lab = LABEL(cust("package tracking refund"))
        self.assertEqual((lab.intent, lab.confidence), ("delivery", 0.75))


class TestMatching(unittest.TestCase):
    def test_ignores_outbound(self):
        base = LABEL(cust("where is my package"))
        noisy = thread((True, "where is my package SENTINEL_TEXT_1"),
                       (False, "refund invoice password lawyer urgent outage"),
                       (False, "thank you terrible"))
        self.assertEqual(LABEL(noisy), base)

    def test_distinct_hits(self):
        self.assertEqual(LABEL(cust("package package package")).confidence, 0.6)
        two_turns = thread((True, "package"), (False, "ok"), (True, "package again"))
        self.assertEqual(LABEL(two_turns).confidence, 0.6)

    def test_token_matching(self):
        self.assertEqual(LABEL(cust("i am locked out")).intent, "account_access")
        self.assertEqual(LABEL(cust("i locked my bike out front")).intent, "other")
        self.assertEqual(LABEL(cust("the app is not working")).intent, "outage_technical")
        self.assertEqual(LABEL(cust("not sure it is working")).intent, "other")
        self.assertEqual(LABEL(cust("thank you")).intent, "praise")
        self.assertEqual(LABEL(cust("thank the team for you")).intent, "other")
        self.assertEqual(LABEL(cust("the download finished")).intent, "other")
        self.assertEqual(LABEL(cust("going DOWN again")).intent, "outage_technical")
        self.assertEqual(LABEL(cust("my 2fa code")).intent, "account_access")
        self.assertEqual(LABEL(cust("unpackaged")).intent, "other")


class TestUrgencyAndAction(unittest.TestCase):
    def test_urgency_rules(self):
        self.assertEqual(LABEL(cust("this is fraud")).urgency, "high")
        self.assertEqual(LABEL(cust("need my package asap")).urgency, "high")
        self.assertEqual(LABEL(cust("URGENT please")).urgency, "high")
        self.assertEqual(LABEL(cust("i love it")).urgency, "low")
        self.assertEqual(LABEL(cust("love it, urgent")).urgency, "high")
        self.assertEqual(LABEL(cust("where is my package")).urgency, "normal")
        self.assertEqual(LABEL(cust("hello there")).urgency, "normal")

    def test_action_rules(self):
        cases = [
            ("my account was hacked", "escalate", "high"),
            ("package stolen from porch", "escalate", "high"),
            ("i love it but call my lawyer", "escalate", "high"),
            ("cannot log in", "route", "normal"),
            ("app keeps showing an error", "route", "normal"),
            ("password reset urgent", "route", "high"),
            ("terrible service fix it immediately", "escalate", "high"),
            ("terrible service", "answer", "normal"),
            ("where is my package", "ask", "normal"),
            ("wrong bill", "ask", "normal"),
            ("hello there", "ask", "normal"),
            ("thanks so much", "answer", "low"),
        ]
        for text, action, urgency in cases:
            with self.subTest(text=text):
                lab = LABEL(cust(text))
                self.assertEqual((lab.action, lab.urgency), (action, urgency))

    def test_deterministic(self):
        t = thread((True, "my package is late and the agent was rude SENTINEL_TEXT_1"),
                   (False, "sorry"), (True, "urgent please"))
        self.assertEqual(CheapTier().label(t), CheapTier().label(t))
        self.assertEqual(LABEL(t), LABEL(t))


if __name__ == "__main__":
    unittest.main()
