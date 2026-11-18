"""Tests for python3 -m triage.pick_hard_cases.

All rows are made up by hand and written to a temp dir at run time (.gitignore ignores *.csv).
Nothing here reads data/raw/ or the real data/split.json.
"""

import unittest
from datetime import datetime, timezone

from triage.pick_hard_cases import escalation_signals
from triage.threads import Thread, Turn

_T0 = datetime(2017, 10, 31, 22, 10, tzinfo=timezone.utc)


def _thread(*turns):
    """Build a Thread from (inbound, text) pairs."""
    built = tuple(Turn(tweet_id=i + 1, author_id="cust_x" if inbound else "brand_x",
                       inbound=inbound, created_at=_T0, text=text)
                  for i, (inbound, text) in enumerate(turns))
    brands = ("brand_x",) if any(not t.inbound for t in built) else ()
    return Thread(root_tweet_id=1, brand_ids=brands, turns=built, brand_replied=bool(brands))


class TestEscalationSignals(unittest.TestCase):
    def test_one_example_per_signal(self):
        cases = [
            ("I will call my solicitor", ("legal",)),
            ("there is a scam on my card", ("fraud_or_hacked",)),
            ("the heater caught fire", ("safety",)),
            ("this is the third time I ask", ("repeated_contact",)),
            ("this is appalling", ("strong_negative",)),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(escalation_signals(_thread((True, text))), expected)

    def test_brand_turns_ignored(self):
        thread = _thread((True, "can you help me"),
                         (False, "sorry this is terrible, our lawyer was hacked"))
        self.assertEqual(escalation_signals(thread), ())

    def test_whole_tokens_only(self):
        self.assertEqual(escalation_signals(_thread((True, "fireworks tonight, I pursue suede"))),
                         ())

    def test_phrase_needs_consecutive_tokens(self):
        self.assertEqual(escalation_signals(_thread((True, "taking legal action now"))),
                         ("legal",))
        self.assertEqual(escalation_signals(_thread((True, "the legal team took action"))), ())

    def test_typographic_apostrophe(self):
        self.assertEqual(escalation_signals(_thread((True, "I didn’t authorise this"))),
                         ("fraud_or_hacked",))

    def test_fixed_order_and_inbound_turns_joined(self):
        thread = _thread((True, "this is useless"), (False, "please DM us"),
                         (True, "my house is on fire and I am calling the ombudsman"))
        self.assertEqual(escalation_signals(thread), ("legal", "safety", "strong_negative"))


if __name__ == "__main__":
    unittest.main()
