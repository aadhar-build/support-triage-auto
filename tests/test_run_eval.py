"""End-to-end tests for python3 -m triage.run_eval.

All rows are made up by hand and written to a temp dir at run time (.gitignore ignores *.csv).
Nothing here reads data/raw/ or the real data/split.json.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

TWCS_HEADER = "tweet_id,author_id,inbound,created_at,text,response_tweet_id,in_response_to_tweet_id\n"
TWCS_ROWS = [
    # thread 1: delivery, brand replies
    '1,cust_SENTINELA,True,Tue Oct 31 22:10:47 +0000 2017,"where is my package SENTINEL_TEXT_1",2,',
    '2,SENTINELBRAND,False,Tue Oct 31 22:11:47 +0000 2017,"sorry, refund coming SENTINEL_TEXT_2",,1',
    # thread 3: billing
    '3,cust_SENTINELB,True,Tue Oct 31 22:12:47 +0000 2017,"charged twice for one invoice SENTINEL_TEXT_3",,',
    # thread 5: praise
    '5,cust_SENTINELC,True,Tue Oct 31 22:13:47 +0000 2017,"thanks, love it SENTINEL_TEXT_5",,',
    # thread 7: account access
    '7,cust_SENTINELD,True,Tue Oct 31 22:14:47 +0000 2017,"locked out, password reset fails SENTINEL_TEXT_7",,',
    # thread 9: nothing matches
    '9,cust_SENTINELE,True,Tue Oct 31 22:15:47 +0000 2017,"hello SENTINEL_TEXT_9",,',
]
GOLD = (
    "thread_id,intent,action,should_escalate\n"
    "1,delivery,ask,false\n"
    "3,billing,ask,false\n"
    "5,praise,answer,false\n"
    "7,account_access,escalate,true\n"
    "424242,other,ask,false\n"
)
SPLIT_ALL_DEV = {"seed": 1, "unit": "thread", "fractions": {"dev": 1.0, "eval": 0, "full": 0}}


class CliTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.data = self.write("twcs.csv", TWCS_HEADER + "\n".join(TWCS_ROWS) + "\n")
        self.gold = self.write("gold.csv", GOLD)
        self.split = self.write("split.json", json.dumps(SPLIT_ALL_DEV))
        self.out = self.dir / "report.json"

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_cli(self, *extra, data=None, gold=None, split=None, out=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        args = [sys.executable, "-m", "triage.run_eval",
                "--data", str(data or self.data),
                "--gold", str(gold or self.gold),
                "--split-config", str(split or self.split),
                "--out", str(out or self.out)]
        args.extend(extra)
        return subprocess.run(args, env=env, capture_output=True, text=True, timeout=60,
                              cwd=str(self.dir))


class TestRunEval(CliTestBase):
    def test_end_to_end(self):
        proc = self.run_cli("--sample-size", "10", "--seed", "3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(data["threads_routed"], 5)
        self.assertEqual(data["threads_scored"], 4)
        self.assertEqual(data["threads_without_gold"], 1)
        self.assertEqual(data["gold_rows_unmatched"], 1)
        self.assertEqual(data["split"], "dev")
        self.assertEqual(data["seed"], 3)
        self.assertEqual(data["sample_size"], 10)
        self.assertEqual(data["threshold"], 0.5)
        self.assertTrue(data["prices_are_placeholders"])
        self.assertEqual(set(data["tier_shares"]), {"cheap", "larger", "agent"})
        # thread 7 is routed (account access), gold says escalate: one missed escalation
        self.assertEqual(data["missed_escalations"], 1)
        self.assertEqual(len(proc.stdout.strip().splitlines()), 1)
        for text in (self.out.read_text(encoding="utf-8"), proc.stdout, proc.stderr):
            self.assertNotIn("SENTINEL", text)

    def test_markdown_format(self):
        out = self.dir / "report.md"
        proc = self.run_cli("--format", "md", out=out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        md = out.read_text(encoding="utf-8")
        self.assertIn("placeholder", md.lower())
        self.assertNotIn("SENTINEL", md)

    def test_deterministic_sample(self):
        a, b = self.dir / "a.json", self.dir / "b.json"
        for out in (a, b):
            proc = self.run_cli("--sample-size", "3", "--seed", "7", out=out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(json.loads(a.read_text(encoding="utf-8"))["threads_routed"], 3)

    def test_split_option(self):
        proc = self.run_cli("--split", "eval")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(data["threads_routed"], 0)
        self.assertEqual(data["split"], "eval")
        self.assertIsNone(data["cost_per_1000_threads"])

    def test_custom_prices_and_threshold(self):
        proc = self.run_cli("--price-cheap", "0.001", "--price-larger", "0.003",
                            "--price-agent", "0.02", "--threshold", "0.1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertFalse(data["prices_are_placeholders"])
        self.assertEqual(data["prices"], {"cheap": 0.001, "larger": 0.003, "agent": 0.02})
        self.assertEqual(data["threshold"], 0.1)

    def test_error_exits(self):
        bad_gold = self.write("bad_gold.csv", "thread_id,intent,action,should_escalate\n1,delivery,ask,maybe\n")
        bad_split = self.write("bad_split.json", json.dumps(
            {"seed": 1, "unit": "thread", "fractions": {"dev": 0.5, "eval": 0, "full": 0}}))
        bad_header = self.write("bad_header.csv", "a,b\n1,2\n")
        cases = {
            "missing data": dict(data=self.dir / "missing.csv"),
            "bad data header": dict(data=bad_header),
            "bad gold": dict(gold=bad_gold),
            "missing gold": dict(gold=self.dir / "missing_gold.csv"),
            "bad split config": dict(split=bad_split),
            "missing split config": dict(split=self.dir / "missing_split.json"),
            "sample size 0": dict(extra=("--sample-size", "0")),
            "negative price": dict(extra=("--price-larger", "-0.5")),
            "bad threshold": dict(extra=("--threshold", "1.5")),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                out = self.dir / ("out_%s.json" % name.replace(" ", "_"))
                extra = kw.pop("extra", ())
                proc = self.run_cli(*extra, out=out, **kw)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertTrue(proc.stderr.strip())
                self.assertEqual(len(proc.stderr.strip().splitlines()), 1, proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
