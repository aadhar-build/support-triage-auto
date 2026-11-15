"""Tests for triage.profile. All fixtures are made up by hand; no real dataset rows."""

import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone

from triage.profile import build_profile, main

HEADER = "tweet_id,author_id,inbound,created_at,text,response_tweet_id,in_response_to_tweet_id\n"

SENTINELS = [f"SENTINEL_TEXT_{i}" for i in range(1, 10)]

FIXTURE = (
    HEADER
    + '1,cust_a,True,Tue Oct 31 22:10:47 +0000 2017,"SENTINEL_TEXT_1 hello, world",2,\n'
    + '2,BrandOne,False,Tue Oct 31 22:15:00 +0000 2017,SENTINEL_TEXT_2 sorry,3,1\n'
    + '3,cust_a,True,Wed Nov 01 08:00:00 +0000 2017,"SENTINEL_TEXT_3 ""still""\nbroken",,2\n'
    + '4,cust_b,true,Mon Oct 30 07:00:00 +0000 2017,SENTINEL_TEXT_4,5,\n'
    + '5,BrandTwo,false,Thu Nov 02 12:30:00 +0000 2017,SENTINEL_TEXT_5,,4.0\n'
    + '6,BrandOne,FALSE,Tue Oct 31 23:00:00 +0000 2017,SENTINEL_TEXT_6,,1\n'
    + 'x7,cust_c,True,Tue Oct 31 23:00:00 +0000 2017,SENTINEL_TEXT_7,,\n'      # tweet_id
    + '8,cust_c,maybe,Tue Oct 31 23:00:00 +0000 2017,SENTINEL_TEXT_8,,\n'      # inbound
    + '9,cust_c,True,not a date,SENTINEL_TEXT_9,,\n'                           # created_at
)


def write(dirname, name, text):
    path = os.path.join(dirname, name)
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(text)
    return path


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class TestBuildProfile(unittest.TestCase):
    def test_fixture_counts(self):
        p = build_profile(io.StringIO(FIXTURE, newline=""))
        self.assertEqual(p.rows_read, 9)
        self.assertEqual(p.rows_parsed, 6)
        self.assertEqual(p.rows_skipped, 3)
        self.assertEqual(dict(p.skip_reasons), {"tweet_id": 1, "inbound": 1, "created_at": 1})
        self.assertEqual(p.inbound_rows, 3)
        self.assertEqual(p.brand_rows, 3)
        self.assertEqual(p.distinct_brands, 2)
        self.assertEqual(p.earliest_created_at, datetime(2017, 10, 30, 7, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(p.latest_created_at, datetime(2017, 11, 2, 12, 30, 0, tzinfo=timezone.utc))

    def test_from_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = build_profile(write(d, "twcs.csv", FIXTURE))
        self.assertEqual(p.rows_parsed, 6)

    def test_header_only(self):
        p = build_profile(io.StringIO(HEADER, newline=""))
        self.assertEqual(
            (p.rows_read, p.rows_parsed, p.rows_skipped, p.inbound_rows, p.brand_rows, p.distinct_brands),
            (0, 0, 0, 0, 0, 0),
        )
        self.assertEqual(sum(p.skip_reasons.values()), 0)
        self.assertIsNone(p.earliest_created_at)
        self.assertIsNone(p.latest_created_at)


class TestMain(unittest.TestCase):
    def test_output_aggregates_only(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = run_main([write(d, "twcs.csv", FIXTURE)])
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                "rows_read: 9",
                "rows_parsed: 6",
                "inbound_rows: 3",
                "brand_rows: 3",
                "distinct_brands: 2",
                "earliest_created_at: 2017-10-30T07:00:00+00:00",
                "latest_created_at: 2017-11-02T12:30:00+00:00",
                "rows_skipped: 3",
                "skipped.created_at: 1",
                "skipped.inbound: 1",
                "skipped.tweet_id: 1",
            ],
        )
        for s in SENTINELS + ["cust_a", "BrandOne", "BrandTwo"]:
            self.assertNotIn(s, out)
            self.assertNotIn(s, err)

    def test_header_only_output(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = run_main([write(d, "twcs.csv", HEADER)])
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                "rows_read: 0",
                "rows_parsed: 0",
                "inbound_rows: 0",
                "brand_rows: 0",
                "distinct_brands: 0",
                "earliest_created_at: none",
                "latest_created_at: none",
                "rows_skipped: 0",
            ],
        )

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "nope.csv")
            code, out, err = run_main([missing])
        self.assertEqual(code, 2)
        self.assertIn(f"file not found: {missing}", err)
        self.assertIn("scripts/download_data.sh", err)
        self.assertEqual(out, "")

    def test_bad_header(self):
        bad = "tweet_id,author_id,text\n1,cust_a,SENTINEL_TEXT_1\n"
        with tempfile.TemporaryDirectory() as d:
            code, out, err = run_main([write(d, "bad.csv", bad)])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)
        self.assertIn("inbound", err)
        self.assertNotIn("SENTINEL_TEXT_1", err + out)

    def test_default_path(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "data", "raw"))
            write(os.path.join(d, "data", "raw"), "twcs.csv", HEADER)
            old = os.getcwd()
            os.chdir(d)
            try:
                code, out, _ = run_main([])
            finally:
                os.chdir(old)
        self.assertEqual(code, 0)
        self.assertIn("rows_read: 0", out)


if __name__ == "__main__":
    unittest.main()
