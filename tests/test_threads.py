"""Tests for triage.threads. All fixtures are made up by hand; no real dataset rows."""

import dataclasses
import io
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import triage.threads
from triage.loader import HeaderError, LoadStats, Tweet
from triage.threads import Thread, ThreadStats, Turn, build_threads, threads_from_tweets

BASE = datetime(2017, 10, 31, 22, 0, 0, tzinfo=timezone.utc)

ODD_COUNTERS = (
    "duplicate_ids",
    "broken_links",
    "cycles",
    "cycle_tweets",
    "brand_started",
    "parent_link_disagreements",
    "response_id_fallbacks",
)


def tw(tweet_id, author_id="cust_a", inbound=True, minute=0, text=None, responses=(),
       in_response_to=None):
    return Tweet(
        tweet_id=tweet_id,
        author_id=author_id,
        inbound=inbound,
        created_at=BASE + timedelta(minutes=minute),
        text=text if text is not None else "SENTINEL_TEXT_%d" % tweet_id,
        response_tweet_ids=tuple(responses),
        in_response_to_tweet_id=in_response_to,
    )


def brand(tweet_id, author_id="BrandA", **kw):
    return tw(tweet_id, author_id=author_id, inbound=False, **kw)


HEADER = "tweet_id,author_id,inbound,created_at,text,response_tweet_id,in_response_to_tweet_id\n"
DATE = "Tue Oct 31 22:10:47 +0000 2017"


def row(tweet_id="1", author_id="cust_a", inbound="True", created_at=DATE,
        text="SENTINEL_TEXT_X hello", response="", in_response_to=""):
    return ",".join([tweet_id, author_id, inbound, created_at, text, response, in_response_to]) + "\n"


def turn_ids(thread):
    return [t.tweet_id for t in thread.turns]


class OddCaseMixin:
    def assert_only(self, stats, **expected):
        """Assert the named odd-case counters and that every other one is 0."""
        for name in ODD_COUNTERS:
            self.assertEqual(getattr(stats, name), expected.get(name, 0), name)

    def assert_totals(self, threads, stats):
        self.assertEqual(stats.threads_built, len(threads))
        self.assertEqual(stats.tweets_placed, sum(len(t.turns) for t in threads))
        self.assertEqual(stats.tweets_placed + stats.tweets_not_placed, stats.tweets_seen)


class TestRecords(unittest.TestCase):
    def test_fields(self):
        self.assertEqual([f.name for f in dataclasses.fields(Turn)],
                         ["tweet_id", "author_id", "inbound", "created_at", "text"])
        self.assertEqual([f.name for f in dataclasses.fields(Thread)],
                         ["root_tweet_id", "brand_ids", "turns", "brand_replied"])
        self.assertEqual(
            [f.name for f in dataclasses.fields(ThreadStats)],
            ["tweets_seen", "duplicate_ids", "threads_built", "tweets_placed",
             "tweets_not_placed", "broken_links", "cycles", "cycle_tweets", "brand_started",
             "parent_link_disagreements", "response_id_fallbacks", "load"],
        )

    def test_stats_defaults(self):
        s = ThreadStats()
        for f in dataclasses.fields(ThreadStats):
            if f.name == "load":
                self.assertIsNone(s.load)
            else:
                self.assertEqual(getattr(s, f.name), 0, f.name)

    def test_frozen(self):
        turn = Turn(tweet_id=1, author_id="cust_a", inbound=True, created_at=BASE,
                    text="SENTINEL_TEXT_1")
        thread = Thread(root_tweet_id=1, brand_ids=(), turns=(turn,), brand_replied=False)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            turn.text = "x"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            thread.brand_replied = True


class TestBasic(OddCaseMixin, unittest.TestCase):
    def test_simple_exchange(self):
        threads, stats = threads_from_tweets([
            tw(1, minute=0, responses=(2,)),
            brand(2, minute=1, in_response_to=1),
        ])
        self.assertEqual(len(threads), 1)
        t = threads[0]
        self.assertEqual(t.root_tweet_id, 1)
        self.assertEqual(turn_ids(t), [1, 2])
        self.assertEqual(t.brand_ids, ("BrandA",))
        self.assertTrue(t.brand_replied)
        self.assertEqual(t.turns[0], Turn(1, "cust_a", True, BASE, "SENTINEL_TEXT_1"))
        self.assertFalse(t.turns[1].inbound)
        self.assert_only(stats)
        self.assertEqual(stats.tweets_seen, 2)
        self.assertEqual(stats.tweets_placed, 2)
        self.assert_totals(threads, stats)

    def test_deep_and_branching(self):
        tweets = [
            tw(10, minute=0, responses=(11, 14)),
            brand(11, minute=2, in_response_to=10, responses=(12,)),
            tw(12, minute=5, in_response_to=11, responses=(13,)),
            brand(13, minute=9, in_response_to=12),
            # second branch off the root, between the others in time
            brand(14, minute=3, in_response_to=10),
            # an unrelated thread
            tw(20, minute=1),
        ]
        threads, stats = threads_from_tweets(tweets)
        self.assertEqual([t.root_tweet_id for t in threads], [10, 20])
        self.assertEqual(turn_ids(threads[0]), [10, 11, 14, 12, 13])
        self.assertEqual(turn_ids(threads[1]), [20])
        self.assert_only(stats)
        self.assertEqual(stats.tweets_placed, 6)
        self.assert_totals(threads, stats)

    def test_multiple_brands(self):
        threads, stats = threads_from_tweets([
            tw(1, minute=0),
            brand(2, author_id="BrandB", minute=1, in_response_to=1),
            brand(3, author_id="BrandA", minute=2, in_response_to=1),
            brand(4, author_id="BrandB", minute=3, in_response_to=3),
        ])
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0].brand_ids, ("BrandA", "BrandB"))
        self.assertTrue(threads[0].brand_replied)
        self.assert_only(stats)

    def test_no_brand_reply(self):
        threads, stats = threads_from_tweets([
            tw(1, minute=0),
            tw(2, author_id="cust_b", minute=1),
            tw(3, author_id="cust_b", minute=2, in_response_to=2),
        ])
        self.assertEqual([t.root_tweet_id for t in threads], [1, 2])
        for t in threads:
            self.assertFalse(t.brand_replied)
            self.assertEqual(t.brand_ids, ())
        self.assertEqual(turn_ids(threads[0]), [1])
        self.assertEqual(turn_ids(threads[1]), [2, 3])
        self.assert_only(stats)


class TestOddCases(OddCaseMixin, unittest.TestCase):
    def test_broken_link(self):
        threads, stats = threads_from_tweets([
            tw(1, minute=0),
            brand(2, minute=1, in_response_to=999, responses=(3,)),
            tw(3, minute=2, in_response_to=2),
        ])
        self.assertEqual([t.root_tweet_id for t in threads], [1])
        self.assertEqual(turn_ids(threads[0]), [1])
        self.assert_only(stats, broken_links=1)
        self.assertEqual(stats.tweets_not_placed, 2)
        self.assert_totals(threads, stats)

    def test_two_tweet_cycle(self):
        threads, stats = threads_from_tweets([
            tw(1, minute=0),
            tw(100, minute=1, in_response_to=101),
            brand(101, minute=2, in_response_to=100),
            tw(102, minute=3, in_response_to=101),  # hangs below the loop
            tw(200, minute=4, in_response_to=200),  # self-reply
        ])
        self.assertEqual([t.root_tweet_id for t in threads], [1])
        self.assertEqual(turn_ids(threads[0]), [1])
        self.assert_only(stats, cycles=2, cycle_tweets=3)
        self.assertEqual(stats.tweets_not_placed, 4)
        self.assert_totals(threads, stats)

    def test_self_reply_cycle(self):
        threads, stats = threads_from_tweets([
            tw(5, minute=0, in_response_to=5, responses=(5,)),
            brand(6, minute=1, in_response_to=5),
        ])
        self.assertEqual(threads, [])
        self.assert_only(stats, cycles=1, cycle_tweets=1)
        self.assertEqual(stats.tweets_not_placed, 2)
        self.assert_totals(threads, stats)

    def test_brand_started(self):
        threads, stats = threads_from_tweets([
            brand(1, minute=0, responses=(2,)),
            tw(2, minute=1, in_response_to=1),
            tw(3, minute=2),
        ])
        self.assertEqual([t.root_tweet_id for t in threads], [3])
        self.assert_only(stats, brand_started=1)
        self.assertEqual(stats.tweets_not_placed, 2)
        self.assert_totals(threads, stats)

    def test_duplicate_id(self):
        threads, stats = threads_from_tweets([
            tw(1, minute=0, text="SENTINEL_TEXT_FIRST"),
            tw(2, minute=1),
            tw(1, minute=0, text="SENTINEL_TEXT_SECOND", responses=(2,)),
        ])
        self.assertEqual([t.root_tweet_id for t in threads], [1, 2])
        self.assertEqual(threads[0].turns[0].text, "SENTINEL_TEXT_FIRST")
        # the second copy's response link to 2 is ignored, so 2 stays a root
        self.assertEqual(turn_ids(threads[1]), [2])
        self.assert_only(stats, duplicate_ids=1)
        self.assertEqual(stats.tweets_seen, 2)
        self.assert_totals(threads, stats)

    def test_parent_link_disagreement(self):
        threads, stats = threads_from_tweets([
            tw(1, minute=0, responses=(3,)),
            tw(5, author_id="cust_b", minute=1, responses=(3,)),
            brand(3, minute=2, in_response_to=1),
        ])
        self.assertEqual([t.root_tweet_id for t in threads], [1, 5])
        self.assertEqual(turn_ids(threads[0]), [1, 3])
        self.assertEqual(turn_ids(threads[1]), [5])
        self.assert_only(stats, parent_link_disagreements=1)

    def test_response_id_fallback(self):
        threads, stats = threads_from_tweets([
            tw(1, minute=0, responses=(2, 777)),  # 777 is missing: ignored
            brand(2, minute=1),
        ])
        self.assertEqual(len(threads), 1)
        self.assertEqual(turn_ids(threads[0]), [1, 2])
        self.assert_only(stats, response_id_fallbacks=1)

    def test_fallback_multiple_listers(self):
        threads, stats = threads_from_tweets([
            tw(7, author_id="cust_b", minute=0, responses=(9,)),
            tw(4, minute=1, responses=(9,)),
            brand(9, minute=2),
        ])
        self.assertEqual([t.root_tweet_id for t in threads], [7, 4])
        self.assertEqual(turn_ids(threads[1]), [4, 9])
        self.assertEqual(turn_ids(threads[0]), [7])
        self.assert_only(stats, response_id_fallbacks=1, parent_link_disagreements=1)


def mixed_fixture():
    return [
        tw(1, minute=0, responses=(2,)),
        brand(2, minute=1, in_response_to=1, responses=(3,)),
        tw(3, minute=1, in_response_to=2),
        tw(4, minute=1, responses=(5,)),
        brand(5, author_id="BrandB", minute=1),  # fallback
        brand(6, minute=2, in_response_to=999),  # broken
        tw(7, minute=3, in_response_to=6),
        tw(8, minute=4, in_response_to=9),  # cycle
        brand(9, minute=5, in_response_to=8),
        tw(10, minute=6, in_response_to=10),  # self-reply
        brand(11, minute=7),  # brand-started
        tw(12, minute=8, in_response_to=11),
        tw(1, minute=9, text="SENTINEL_TEXT_DUP"),  # duplicate
        tw(13, minute=10, responses=(14,)),
        tw(15, minute=10, responses=(14,)),
        brand(14, minute=11, in_response_to=13),  # disagreement
        tw(16, minute=1),
    ]


class TestDeterminism(OddCaseMixin, unittest.TestCase):
    def test_tie_break_and_input_order(self):
        threads, stats = threads_from_tweets(mixed_fixture())
        # 2, 3, 4, 5 and 16 share minute 1; the ordering is by tweet_id
        self.assertEqual(turn_ids(threads[0]), [1, 2, 3])
        self.assertEqual([t.root_tweet_id for t in threads], [1, 4, 16, 13, 15])
        # the duplicate is last in the fixture, so the first copy is kept whatever the
        # order of the other tweets
        base = [t for t in mixed_fixture() if t.text != "SENTINEL_TEXT_DUP"]
        dup = [t for t in mixed_fixture() if t.text == "SENTINEL_TEXT_DUP"]
        rng = random.Random(1234)
        for _ in range(20):
            shuffled = list(base)
            rng.shuffle(shuffled)
            t2, s2 = threads_from_tweets(shuffled + dup)
            self.assertEqual(t2, threads)
            self.assertEqual(s2, stats)


class TestTotals(OddCaseMixin, unittest.TestCase):
    def test_invariants(self):
        threads, stats = threads_from_tweets(mixed_fixture())
        self.assert_totals(threads, stats)
        self.assert_only(stats, duplicate_ids=1, broken_links=1, cycles=2, cycle_tweets=3,
                         brand_started=1, parent_link_disagreements=1,
                         response_id_fallbacks=1)
        self.assertEqual(stats.tweets_seen, 16)
        self.assertEqual(stats.tweets_placed, 9)
        self.assertEqual(stats.tweets_not_placed, 7)
        self.assertEqual(stats.threads_built, 5)
        self.assertEqual(threads[1].brand_ids, ("BrandB",))


class TestScale(OddCaseMixin, unittest.TestCase):
    N = 5000

    def test_deep_chain_no_recursion(self):
        tweets = [tw(1, minute=0)]
        for i in range(2, self.N + 1):
            maker = brand if i % 2 == 0 else tw
            tweets.append(maker(i, minute=i, in_response_to=i - 1))
        tweets.reverse()  # children before parents
        threads, stats = threads_from_tweets(tweets)
        self.assertEqual(len(threads), 1)
        self.assertEqual(turn_ids(threads[0]), list(range(1, self.N + 1)))
        self.assert_only(stats)

    def test_long_cycle_terminates(self):
        tweets = [tw(i, minute=i, in_response_to=(i % self.N) + 1)
                  for i in range(1, self.N + 1)]
        threads, stats = threads_from_tweets(tweets)
        self.assertEqual(threads, [])
        self.assert_only(stats, cycles=1, cycle_tweets=self.N)
        self.assertEqual(stats.tweets_not_placed, self.N)


class TestApi(OddCaseMixin, unittest.TestCase):
    def test_one_shot_iterable(self):
        pulls = []

        def gen():
            for t in [tw(1, minute=0), brand(2, minute=1, in_response_to=1)]:
                pulls.append(t.tweet_id)
                yield t

        g = gen()
        threads, stats = threads_from_tweets(g)
        self.assertEqual(pulls, [1, 2])
        self.assertEqual(list(g), [])
        self.assertEqual(turn_ids(threads[0]), [1, 2])
        self.assertIsNone(stats.load)

    def _csv(self):
        return (HEADER
                + row("1", response="2")
                + row("2", author_id="BrandA", inbound="False", in_response_to="1")
                + row("3", inbound="maybe"))  # malformed: skipped by the loader

    def test_build_threads_from_csv(self):
        f = io.StringIO(self._csv(), newline="")
        threads, stats = build_threads(f)
        self.assertFalse(f.closed)
        self.assertEqual(len(threads), 1)
        self.assertEqual(turn_ids(threads[0]), [1, 2])
        self.assertIsInstance(stats.load, LoadStats)
        self.assertEqual(stats.load.rows_skipped, 1)
        self.assertEqual(stats.load.rows_parsed, 2)
        self.assertEqual(stats.load.rows_parsed, stats.tweets_seen + stats.duplicate_ids)

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "fixture.csv")
            with open(path, "w", newline="", encoding="utf-8") as out:
                out.write(self._csv())
            for src in (path, __import__("pathlib").Path(path)):
                t2, s2 = build_threads(src)
                self.assertEqual(t2, threads)
                self.assertEqual(s2.load.rows_skipped, 1)

    def test_header_error_propagates(self):
        f = io.StringIO("tweet_id,author_id\n1,cust_a\n", newline="")
        with self.assertRaises(HeaderError):
            build_threads(f)
        self.assertFalse(f.closed)

    def test_docstring_memory_note(self):
        doc = (triage.threads.__doc__ or "").lower()
        self.assertIn("memory", doc)
        self.assertIn("linear", doc)


if __name__ == "__main__":
    unittest.main()
