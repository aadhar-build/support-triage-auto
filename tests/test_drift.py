"""Tests for triage.drift.

Every thread and tweet here is made up by hand. Nothing reads data/raw/, the real
data/split.json or the owner's evals/drift.json (except the opt-in conformance test).
"""

import unittest
from datetime import datetime, timedelta, timezone

import triage.drift as drift
from triage.cheap_tier import CheapTier
from triage.threads import Thread, Turn

INTENTS7 = ("delivery", "billing", "account_access", "outage_technical", "complaint", "praise",
            "other")


UTC = timezone.utc
BASE = 7300000  # made-up ids are BASE + small number, so they never collide with a count
# 2016-03-07 is the Monday of ISO week 2016-W10. All fixture weeks are made-up test weeks.
MON = datetime(2016, 3, 7, tzinfo=UTC)
DELIVERY_TEXT = "where is my parcel"  # cheap tier: delivery, action ask, confidence 0.6
PRAISE_TEXT = "thanks love it"  # cheap tier: praise, action answer, confidence 0.75


def week_label(i):
    return "2016-W%02d" % (10 + i)


def make_thread(root, at, text, more=()):
    """A thread whose first turn is an inbound tweet at `at`. `more` holds
    (id, inbound, at, text) for later turns."""
    turns = [Turn(root, "cust_%d" % root, True, at, text)]
    for tid, inbound, t_at, t_text in more:
        turns.append(Turn(tid, "cust_%d" % root if inbound else "brandx", inbound, t_at, t_text))
    brand_ids = tuple(sorted({t.author_id for t in turns if not t.inbound}))
    return Thread(root_tweet_id=root, brand_ids=brand_ids, turns=tuple(turns),
                  brand_replied=bool(brand_ids))


def shares(**kw):
    return {i: kw.get(i, 0.0) for i in INTENTS7}


class TestPsi(unittest.TestCase):
    def test_worked_example_a(self):
        ref = {i: 0.15 for i in INTENTS7}
        ref["delivery"] = 0.10
        win = {i: 0.10 for i in INTENTS7}
        win["delivery"] = 0.40
        self.assertAlmostEqual(drift.psi(ref, win), 0.537528, delta=0.001)

    def test_worked_example_b_zero_bins(self):
        ref = shares(delivery=0.5, billing=0.25, praise=0.25)
        win = shares(delivery=0.5, billing=0.5)
        self.assertAlmostEqual(drift.psi(ref, win), 2.128516, delta=0.001)

    def test_identical_is_zero(self):
        s = shares(delivery=0.3, praise=0.7)
        self.assertEqual(drift.psi(s, dict(s)), 0.0)


class TestCut(unittest.TestCase):
    def test_later_turns_and_replies_excluded(self):
        cutoff = MON + timedelta(days=7)  # Monday 00:00 UTC after week 2016-W10
        t = make_thread(BASE + 1, MON + timedelta(days=6, hours=23), "hello",
                        more=[(BASE + 2, True, cutoff - timedelta(seconds=1), "still there"),
                              (BASE + 3, False, cutoff, "sorry, DM us"),
                              (BASE + 4, True, cutoff + timedelta(hours=1), "my parcel")])
        before = (t.turns, t.brand_ids, t.brand_replied)
        cut = drift.cut_thread(t, cutoff)
        self.assertEqual([x.tweet_id for x in cut.turns], [BASE + 1, BASE + 2])
        self.assertFalse(cut.brand_replied)
        self.assertEqual(cut.brand_ids, ())
        self.assertEqual(cut.root_tweet_id, BASE + 1)
        self.assertEqual((t.turns, t.brand_ids, t.brand_replied), before)
        self.assertTrue(t.brand_replied)
        self.assertEqual(len(t.turns), 4)
        # cutoff before the first turn keeps nothing
        self.assertIsNone(drift.cut_thread(t, MON))

    def test_signals_use_cut_copy(self):
        # first turn in week W (no keyword); a later inbound turn in W+1 names a parcel
        w0 = make_thread(BASE + 10, MON + timedelta(days=2), "hello there",
                         more=[(BASE + 11, True, MON + timedelta(days=8), DELIVERY_TEXT)])
        w1 = make_thread(BASE + 20, MON + timedelta(days=9), PRAISE_TEXT)
        buckets = drift.replay_weeks([w0, w1])
        self.assertEqual([len(b.threads[0].turns) for b in buckets], [1, 1])
        signals = drift.weekly_signals(buckets, CheapTier())
        self.assertEqual(signals[0].intent_counts["delivery"], 0)
        self.assertEqual(signals[0].intent_counts["other"], 1)
        # the uncut thread would have been labelled delivery
        self.assertEqual(CheapTier().label(w0).intent, "delivery")


class TestReplay(unittest.TestCase):
    def test_order_iso_week_utc_gaps_partial(self):
        a = make_thread(BASE + 31, MON + timedelta(days=1), "a")             # W10
        b = make_thread(BASE + 32, MON + timedelta(days=0, hours=5), "b")    # W10, earlier
        # Sunday 2016-03-20 23:30 at UTC-05:00 is Monday 04:30 UTC: ISO week W12, not W11
        tz5 = timezone(timedelta(hours=-5))
        c = make_thread(BASE + 33, datetime(2016, 3, 20, 23, 30, tzinfo=tz5), "c")
        d = make_thread(BASE + 34, MON + timedelta(days=28), "d")            # W14
        empty = Thread(root_tweet_id=BASE + 35, brand_ids=(), turns=(), brand_replied=False)
        buckets = drift.replay_weeks([d, c, empty, a, b])
        self.assertEqual([x.week for x in buckets],
                         ["2016-W10", "2016-W11", "2016-W12", "2016-W13", "2016-W14"])
        self.assertEqual([[t.root_tweet_id for t in x.threads] for x in buckets],
                         [[BASE + 32, BASE + 31], [], [BASE + 33], [], [BASE + 34]])
        self.assertEqual([x.partial for x in buckets], [True, False, False, False, True])
        self.assertEqual(buckets[0].week_end, datetime(2016, 3, 14, tzinfo=UTC))
        self.assertEqual(drift.replay_weeks([]), [])
        only = drift.replay_weeks([a])
        self.assertEqual(len(only), 1)
        self.assertTrue(only[0].partial)


if __name__ == "__main__":
    unittest.main()
