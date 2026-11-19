"""Tests for triage.spend_log. Every log lives in a temp dir; the clock is injected."""

import fcntl
import json
import math
import multiprocessing
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from triage import spend_log
from triage.spend_log import MonthlyCapError, SpendLog, SpendLogError, load_monthly_cap

REPO_ROOT = Path(__file__).resolve().parents[1]
NOV = datetime(2026, 11, 18, 10, 0, 0, tzinfo=timezone.utc)
OCT = datetime(2026, 10, 31, 23, 59, 0, tzinfo=timezone.utc)
FIXTURE_THRESHOLDS = REPO_ROOT / "tests" / "fixtures" / "thresholds_spend.json"


def line(**kw):
    return json.dumps(kw) + "\n"


OPEN = line(event="open", id="0" * 32, at=NOV.isoformat())


def reserve_line(rid, usd, at=NOV):
    return line(event="reserve", id=rid, at=at.isoformat(), run_id="r", model="m",
                split="dev", usd=usd)


def settle_line(rid, usd, at=NOV):
    return line(event="settle", id=rid, at=at.isoformat(), usd=usd, input_tokens=1,
                output_tokens=1, cache_write_tokens=0, cache_read_tokens=0)


def _child_reserve(path, cap, q):
    log = SpendLog(path, cap, clock=lambda: NOV)
    try:
        log.reserve("child", "m", "dev", 0.6)
        q.put("ok")
    except MonthlyCapError:
        q.put("cap")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "spend.jsonl"

    def log(self, cap=1.0, clock=NOV):
        return SpendLog(self.path, cap, clock=lambda: clock)


class TestSpendLog(Base):
    def test_create_and_reserve_settle(self):
        SpendLog.create(self.path)
        log = self.log()
        rid = log.reserve("run1", "m", "dev", 0.002)
        self.assertRegex(rid, r"^[0-9a-f]{32}$")
        self.assertAlmostEqual(log.month_to_date(), 0.002)
        log.settle(rid, 0.001, {"input_tokens": 10, "output_tokens": 2,
                                "cache_write_tokens": 0, "cache_read_tokens": 0})
        self.assertAlmostEqual(log.month_to_date(), 0.001)
        events = [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["event"] for e in events], ["open", "reserve", "settle"])
        self.assertEqual(events[1]["split"], "dev")

    def test_month_boundary(self):
        self.path.write_text(OPEN + reserve_line("a" * 32, 0.5, OCT) + settle_line("a" * 32, 0.5, OCT),
                             encoding="utf-8")
        self.assertEqual(self.log(clock=NOV).month_to_date(), 0.0)
        self.assertAlmostEqual(self.log(clock=OCT).month_to_date(), 0.5)

    def test_monthly_cap_refuses_without_writing(self):
        self.path.write_text(OPEN + reserve_line("a" * 32, 0.0095) + settle_line("a" * 32, 0.0095),
                             encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(MonthlyCapError):
            self.log(cap=0.01).reserve("r", "m", "dev", 0.001)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(issubclass(MonthlyCapError, SpendLogError))

    def test_corrupt_log_fails_closed(self):
        r, s = reserve_line("a" * 32, 0.1), settle_line("a" * 32, 0.1)
        cases = {
            "missing file": None,
            "no open line": r,
            "bad json": OPEN + "{nope\n",
            "partial last line": OPEN + r.rstrip("\n"),
            "unknown event": OPEN + line(event="refund", id="b" * 32, at=NOV.isoformat()),
            "missing field": OPEN + line(event="reserve", id="a" * 32, at=NOV.isoformat(),
                                         run_id="r", model="m", split="dev"),
            "negative usd": OPEN + reserve_line("a" * 32, -0.1),
            "nan usd": OPEN + r.replace("0.1", "NaN"),
            "settle without reserve": OPEN + s,
            "double settle": OPEN + r + s + s,
        }
        for name, content in cases.items():
            for op in ("reserve", "month_to_date"):
                with self.subTest(name, op=op):
                    if self.path.exists():
                        self.path.unlink()
                    if content is not None:
                        self.path.write_text(content, encoding="utf-8")
                    before = self.path.read_bytes() if content is not None else None
                    log = self.log()
                    with self.assertRaises(SpendLogError):
                        if op == "reserve":
                            log.reserve("r", "m", "dev", 0.001)
                        else:
                            log.month_to_date()
                    if content is None:
                        self.assertFalse(self.path.exists())
                    else:
                        self.assertEqual(self.path.read_bytes(), before)

    def test_unsettled_reservation_stays_charged(self):
        SpendLog.create(self.path)
        log = self.log()
        a = log.reserve("r", "m", "dev", 0.3)
        b = log.reserve("r", "m", "dev", 0.2)
        c = log.reserve("r", "m", "dev", 0.1)
        self.assertAlmostEqual(log.month_to_date(), 0.6)
        log.settle(a, 0.05, {"input_tokens": 1, "output_tokens": 1, "cache_write_tokens": 0,
                             "cache_read_tokens": 0})
        log.mark_unknown(b)
        # a at settled 0.05, b at reserved 0.2 (unknown), c at reserved 0.1 (unsettled)
        self.assertAlmostEqual(log.month_to_date(), 0.35)
        with self.assertRaises(SpendLogError):
            log.mark_unknown(b)

    def test_create_refuses_existing(self):
        SpendLog.create(self.path)
        before = self.path.read_bytes()
        with self.assertRaises(SpendLogError):
            SpendLog.create(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_path_inside_repo_refused(self):
        path = REPO_ROOT / "spend_should_not_exist.jsonl"
        with self.assertRaises(SpendLogError):
            SpendLog(path, 1.0)
        with self.assertRaises(SpendLogError):
            SpendLog.create(path)
        self.assertFalse(path.exists())

    def test_append_fsyncs(self):
        SpendLog.create(self.path)
        log = self.log()
        with mock.patch.object(spend_log.os, "fsync", wraps=os.fsync) as fs:
            rid = log.reserve("r", "m", "dev", 0.01)
            self.assertEqual(fs.call_count, 1)
            log.mark_unknown(rid)
            self.assertEqual(fs.call_count, 2)

    def test_reserve_is_locked(self):
        SpendLog.create(self.path)
        ctx = multiprocessing.get_context("fork")
        q = ctx.Queue()
        fd = os.open(str(self.path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            p = ctx.Process(target=_child_reserve, args=(str(self.path), 1.0, q))
            p.start()
            p.join(0.5)
            self.assertTrue(p.is_alive(), "child reserved while the lock was held")
            # while the child waits, this process reserves too (it already holds the lock,
            # so write directly through a second SpendLog after releasing)
            fcntl.flock(fd, fcntl.LOCK_UN)
            mine = None
            try:
                self.log().reserve("parent", "m", "dev", 0.6)
                mine = "ok"
            except MonthlyCapError:
                mine = "cap"
            p.join(10)
            self.assertFalse(p.is_alive())
        finally:
            os.close(fd)
        results = sorted([mine, q.get(timeout=5)])
        self.assertEqual(results, ["cap", "ok"])
        self.assertLessEqual(self.log().month_to_date(), 1.0)


class TestLoadMonthlyCap(Base):
    def test_load_monthly_cap(self):
        self.assertEqual(load_monthly_cap(FIXTURE_THRESHOLDS), 200.0)
        cases = {"missing key": {"guardrails": {}}, "zero": 0, "negative": -1, "nan": "NaN"}
        for name, value in cases.items():
            with self.subTest(name):
                if isinstance(value, dict):
                    text = json.dumps(value)
                else:
                    text = '{"guardrails": {"api_spend_usd_per_month": {"max": %s}}}' % value
                path = self.dir / "t.json"
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(SpendLogError):
                    load_monthly_cap(path)
        with self.assertRaises(SpendLogError):
            load_monthly_cap(self.dir / "missing.json")


if __name__ == "__main__":
    unittest.main()
