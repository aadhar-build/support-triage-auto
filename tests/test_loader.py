"""Tests for triage.loader. All fixtures are made up by hand; no real dataset rows."""

import io
import os
import tempfile
import types
import unittest
from collections import Counter
from datetime import datetime, timezone

from triage.loader import (
    EXPECTED_COLUMNS,
    HeaderError,
    LoadStats,
    Tweet,
    iter_tweets,
)

HEADER = "tweet_id,author_id,inbound,created_at,text,response_tweet_id,in_response_to_tweet_id\n"
DATE = "Tue Oct 31 22:10:47 +0000 2017"
DATE_DT = datetime(2017, 10, 31, 22, 10, 47, tzinfo=timezone.utc)


def row(tweet_id="1", author_id="cust_a", inbound="True", created_at=DATE,
        text="SENTINEL_TEXT_X hello", response="", in_response_to=""):
    return ",".join([tweet_id, author_id, inbound, created_at, text, response, in_response_to]) + "\n"


def new_stats():
    return LoadStats(rows_read=0, rows_parsed=0, rows_skipped=0, skip_reasons=Counter())


def load(csv_text, stats=None):
    return list(iter_tweets(io.StringIO(csv_text, newline=""), stats))


class TestHeader(unittest.TestCase):
    def test_expected_columns(self):
        self.assertEqual(
            EXPECTED_COLUMNS,
            ("tweet_id", "author_id", "inbound", "created_at", "text",
             "response_tweet_id", "in_response_to_tweet_id"),
        )

    def test_missing_column_raises_before_any_row(self):
        text = "tweet_id,author_id,inbound,created_at,text,response_tweet_id\n" \
               "1,cust_a,True," + DATE + ",SENTINEL_TEXT_1,,\n"
        gen = iter_tweets(io.StringIO(text, newline=""))
        with self.assertRaises(HeaderError) as ctx:
            next(gen)
        self.assertIn("in_response_to_tweet_id", str(ctx.exception))

    def test_message_names_every_missing_column(self):
        with self.assertRaises(HeaderError) as ctx:
            load("tweet_id,author_id,inbound,created_at,text\n")
        msg = str(ctx.exception)
        self.assertIn("response_tweet_id", msg)
        self.assertIn("in_response_to_tweet_id", msg)

    def test_empty_file_raises(self):
        with self.assertRaises(HeaderError):
            load("")

    def test_header_error_is_value_error(self):
        self.assertTrue(issubclass(HeaderError, ValueError))

    def test_extra_and_reordered_columns_accepted(self):
        text = (
            "extra,in_response_to_tweet_id,text,tweet_id,response_tweet_id,created_at,inbound,author_id\n"
            "zzz,7,SENTINEL_TEXT_R,3,4,\"" + DATE + "\",false,BrandOne\n"
        )
        tweets = load(text)
        self.assertEqual(len(tweets), 1)
        t = tweets[0]
        self.assertEqual(t.tweet_id, 3)
        self.assertEqual(t.author_id, "BrandOne")
        self.assertFalse(t.inbound)
        self.assertEqual(t.text, "SENTINEL_TEXT_R")
        self.assertEqual(t.response_tweet_ids, (4,))
        self.assertEqual(t.in_response_to_tweet_id, 7)


class TestParsing(unittest.TestCase):
    def test_valid_row_fields(self):
        stats = new_stats()
        tweets = load(HEADER + row("10", "cust_a", "True", DATE, "SENTINEL_TEXT_1 hi", "11,12", "9"), stats)
        self.assertEqual(len(tweets), 1)
        t = tweets[0]
        self.assertIsInstance(t, Tweet)
        self.assertEqual(t.tweet_id, 10)
        self.assertIsInstance(t.tweet_id, int)
        self.assertEqual(t.author_id, "cust_a")
        self.assertIsInstance(t.author_id, str)
        self.assertIs(t.inbound, True)
        self.assertEqual(t.created_at, DATE_DT)
        self.assertIsNotNone(t.created_at.tzinfo)
        self.assertEqual(t.text, "SENTINEL_TEXT_1 hi")
        self.assertEqual(t.response_tweet_ids, (11, 12))
        self.assertIsInstance(t.response_tweet_ids, tuple)
        self.assertEqual(t.in_response_to_tweet_id, 9)
        self.assertEqual(stats.rows_parsed, 1)
        with self.assertRaises(Exception):
            t.tweet_id = 5  # frozen

    def test_numeric_author_id_stays_string(self):
        (t,) = load(HEADER + row(author_id="115712"))
        self.assertEqual(t.author_id, "115712")

    def test_inbound_parsing(self):
        cases = [("True", True), ("False", False), ("true", True), ("FALSE", False), (" True ", True)]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                (t,) = load(HEADER + row(inbound=raw))
                self.assertIs(t.inbound, expected)
        stats = new_stats()
        self.assertEqual(load(HEADER + row(inbound="yes"), stats), [])
        self.assertEqual(stats.skip_reasons["inbound"], 1)

    def test_created_at(self):
        (t,) = load(HEADER + row(created_at=DATE))
        self.assertEqual(t.created_at, datetime(2017, 10, 31, 22, 10, 47, tzinfo=timezone.utc))
        (t2,) = load(HEADER + row(created_at="Wed Nov 01 01:02:03 -0500 2017"))
        self.assertEqual(t2.created_at, datetime(2017, 11, 1, 6, 2, 3, tzinfo=timezone.utc))
        for bad in ["2017-10-31 22:10:47", "Tue Foo 31 22:10:47 +0000 2017", "", "Tue Oct 31 25:10:47 +0000 2017",
                    "Tue Oct 31 22:10:47 2017"]:
            with self.subTest(bad=bad):
                stats = new_stats()
                self.assertEqual(load(HEADER + row(created_at=bad), stats), [])
                self.assertEqual(dict(stats.skip_reasons), {"created_at": 1})

    def test_response_ids(self):
        for raw, expected in [("", ()), ("5", (5,)), ('"5,6"', (5, 6)), ('" 5 , 6 "', (5, 6)), ("5.0", (5,))]:
            with self.subTest(raw=raw):
                (t,) = load(HEADER + row(response=raw))
                self.assertEqual(t.response_tweet_ids, expected)
        for bad in ['"5,abc"', '"5,"', "-5", "5.5"]:
            with self.subTest(bad=bad):
                stats = new_stats()
                self.assertEqual(load(HEADER + row(response=bad), stats), [])
                self.assertEqual(dict(stats.skip_reasons), {"response_tweet_id": 1})

    def test_in_response_to(self):
        for raw, expected in [("", None), ("7", 7), ("7.0", 7)]:
            with self.subTest(raw=raw):
                (t,) = load(HEADER + row(in_response_to=raw))
                self.assertEqual(t.in_response_to_tweet_id, expected)
        for bad in ["7.5", "abc", "1e3", "-7", "nan"]:
            with self.subTest(bad=bad):
                stats = new_stats()
                self.assertEqual(load(HEADER + row(in_response_to=bad), stats), [])
                self.assertEqual(dict(stats.skip_reasons), {"in_response_to_tweet_id": 1})

    def test_multiline_quoted_text(self):
        written = 'SENTINEL_TEXT_2 one, two "quoted"\nsecond line'
        quoted = '"' + written.replace('"', '""') + '"'
        tweets = load(HEADER + row("1", text=quoted) + row("2", text="SENTINEL_TEXT_3"))
        self.assertEqual([t.tweet_id for t in tweets], [1, 2])
        self.assertEqual(tweets[0].text, written)

    def test_empty_text_allowed(self):
        (t,) = load(HEADER + row(text=""))
        self.assertEqual(t.text, "")


class TestSkipping(unittest.TestCase):
    FIXTURE = (
        HEADER
        + row("1", "cust_a", text="SENTINEL_TEXT_1")
        + "2,cust_b,True," + DATE + ",SENTINEL_TEXT_2,,,extra\n"   # field_count (too many)
        + "3,cust_b,True\n"                                         # field_count (too few)
        + row("x4", "cust_b", text="SENTINEL_TEXT_4")               # tweet_id
        + row("", "cust_b", text="SENTINEL_TEXT_5")                 # tweet_id (empty)
        + row("6", "", text="SENTINEL_TEXT_6")                      # author_id
        + row("7", "BrandOne", "False", text="SENTINEL_TEXT_7")
    )

    def test_bad_rows_skipped_and_counted(self):
        stats = new_stats()
        tweets = load(self.FIXTURE, stats)
        self.assertEqual([t.tweet_id for t in tweets], [1, 7])
        self.assertEqual(stats.rows_read, 7)
        self.assertEqual(stats.rows_parsed, 2)
        self.assertEqual(stats.rows_skipped, 5)
        self.assertEqual(dict(stats.skip_reasons), {"field_count": 2, "tweet_id": 2, "author_id": 1})
        self.assertEqual(stats.rows_read, stats.rows_parsed + stats.rows_skipped)
        self.assertEqual(sum(stats.skip_reasons.values()), stats.rows_skipped)

    def test_first_failing_rule_wins(self):
        stats = new_stats()
        load(HEADER + row("bad", "", "maybe", "nope", response="x", in_response_to="y"), stats)
        self.assertEqual(dict(stats.skip_reasons), {"tweet_id": 1})

    def test_stats_optional(self):
        self.assertEqual(len(load(self.FIXTURE)), 2)

    def test_stats_updated_during_iteration(self):
        stats = new_stats()
        gen = iter_tweets(io.StringIO(self.FIXTURE, newline=""), stats)
        next(gen)
        self.assertEqual(stats.rows_parsed, 1)
        next(gen)
        self.assertEqual(stats.rows_parsed, 2)
        self.assertEqual(stats.rows_skipped, 5)


class CountingLines:
    """File-like iterator that counts how many lines it has served."""

    def __init__(self, text):
        self._lines = io.StringIO(text, newline="").readlines()
        self.served = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.served >= len(self._lines):
            raise StopIteration
        line = self._lines[self.served]
        self.served += 1
        return line


class TestStreaming(unittest.TestCase):
    def test_is_lazy(self):
        text = HEADER + "".join(row(str(i), text=f"SENTINEL_TEXT_{i}") for i in range(1, 1001))
        src = CountingLines(text)
        gen = iter_tweets(src)
        self.assertIsInstance(gen, types.GeneratorType)
        self.assertEqual(src.served, 0)
        first = next(gen)
        self.assertEqual(first.tweet_id, 1)
        self.assertLessEqual(src.served, 5)
        gen.close()

    def test_path_and_file_object(self):
        text = HEADER + row("1", text="SENTINEL_TEXT_1") + row("2", text="SENTINEL_TEXT_2")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "fixture.csv")
            with open(path, "w", newline="", encoding="utf-8") as f:
                f.write(text)
            from_str = list(iter_tweets(path))
            import pathlib
            from_pathlike = list(iter_tweets(pathlib.Path(path)))
            with open(path, newline="", encoding="utf-8") as f:
                from_file = list(iter_tweets(f))
                self.assertFalse(f.closed)  # caller's file is left open
            self.assertEqual([t.tweet_id for t in from_str], [1, 2])
            self.assertEqual(from_str, from_pathlike)
            self.assertEqual(from_str, from_file)

            opened = []
            import builtins
            real_open = builtins.open

            def spy_open(*args, **kwargs):
                fh = real_open(*args, **kwargs)
                opened.append(fh)
                return fh

            builtins.open = spy_open
            try:
                list(iter_tweets(path))
                gen = iter_tweets(path)
                next(gen)
                gen.close()
            finally:
                builtins.open = real_open
            self.assertEqual(len(opened), 2)
            self.assertTrue(all(fh.closed for fh in opened))


if __name__ == "__main__":
    unittest.main()
