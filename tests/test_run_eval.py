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
FIXTURE_RULES = REPO_ROOT / "tests" / "fixtures" / "policy_rules.json"

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

    def run_cli(self, *extra, data=None, gold=None, split=None, out=None, rules=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        args = [sys.executable, "-m", "triage.run_eval",
                "--data", str(data or self.data),
                "--gold", str(gold or self.gold),
                "--split-config", str(split or self.split),
                "--out", str(out or self.out),
                "--rules", str(rules or FIXTURE_RULES)]
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
        self.assertTrue(proc.stdout.endswith(" split=dev topics_loaded=2\n"), proc.stdout)
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

    def test_bad_topic_list_exits_2_before_data(self):
        rules = json.loads(FIXTURE_RULES.read_text(encoding="utf-8"))
        for name, topics in (("empty", []), ("missing", None)):
            with self.subTest(name):
                bad = dict(rules)
                if topics is None:
                    del bad["escalate_always"]
                else:
                    bad["escalate_always"] = topics
                path = self.write("rules_%s.json" % name, json.dumps(bad))
                out = self.dir / ("out_rules_%s.json" % name)
                for data in (None, self.dir / "missing.csv"):
                    proc = self.run_cli(out=out, rules=path, data=data)
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertTrue(proc.stderr.startswith("error: bad rules file: "),
                                    proc.stderr)
                    self.assertIn("escalate_always", proc.stderr)
                    self.assertEqual(len(proc.stderr.strip().splitlines()), 1, proc.stderr)
                    self.assertEqual(proc.stdout, "")
                    self.assertFalse(out.exists())

    def test_topics_loaded_follows_rules_file(self):
        alt = REPO_ROOT / "tests" / "fixtures" / "policy_rules_alt_topics.json"
        proc = self.run_cli(rules=alt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.endswith(" topics_loaded=1\n"), proc.stdout)


class TestTrace(CliTestBase):
    def test_trace_written(self):
        from triage.trace import validate_record
        trace = self.dir / "t.jsonl"
        proc = self.run_cli("--trace", str(trace), "--threshold", "0.6", "--seed", "3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = trace.read_text(encoding="utf-8").splitlines()
        report = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(len(lines), report["threads_routed"])
        self.assertEqual(len(lines), 5)
        records = [json.loads(line) for line in lines]
        for rec in records:
            validate_record(rec)
            self.assertEqual(rec["threshold"], 0.6)
            self.assertEqual((rec["split"], rec["seed"]), ("dev", 3))
            self.assertEqual(rec["rules_version"], "test-fixture")
            self.assertEqual(rec["trace_schema"], 2)
            self.assertEqual(rec["topic_id"], [])
            self.assertIsNone(rec["drift_config_version"])
            self.assertRegex(rec["code_version"], r"^[0-9a-f]{40}$")
        self.assertEqual(sorted(r["thread_id"] for r in records), [1, 3, 5, 7, 9])
        self.assertTrue(proc.stdout.strip().endswith(" topics_loaded=2 trace_records=5"),
                        proc.stdout)

    def test_trace_records_rules_version_and_topic_id(self):
        from triage.trace import validate_record
        row = '11,cust_SENTINELF,True,Tue Oct 31 22:16:47 +0000 2017,"thanks, quuxfrob SENTINEL_TEXT_11",,'
        data = self.write("twcs_topic.csv", TWCS_HEADER + "\n".join(TWCS_ROWS + [row]) + "\n")
        trace = self.dir / "t.jsonl"
        proc = self.run_cli("--trace", str(trace), data=data)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = {}
        for line in trace.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            validate_record(rec)
            self.assertEqual(rec["trace_schema"], 2)
            self.assertEqual(rec["rules_version"], "test-fixture")
            self.assertIsInstance(rec["topic_id"], list)
            records[rec["thread_id"]] = rec
        self.assertEqual(records[11]["topic_id"], ["t-zorblax"])
        self.assertEqual(records[11]["reason_codes"], ["escalate_always"])
        self.assertEqual(records[11]["final"]["action"], "escalate")
        self.assertEqual([t["tier"] for t in records[11]["tiers"]], ["cheap", "agent"])
        self.assertEqual({k: r["topic_id"] for k, r in records.items() if k != 11},
                         {1: [], 3: [], 5: [], 7: [], 9: []})
        self.assertNotIn("quuxfrob", trace.read_text(encoding="utf-8"))

    def test_trace_has_no_text_or_author_ids(self):
        trace = self.dir / "t.jsonl"
        proc = self.run_cli("--trace", str(trace))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        content = trace.read_text(encoding="utf-8")
        import csv
        import io
        rows = list(csv.DictReader(io.StringIO(TWCS_HEADER + "\n".join(TWCS_ROWS) + "\n")))
        for row in rows:
            for value in (row["text"], row["author_id"]):
                with self.subTest(value=value):
                    self.assertNotIn(value, content)
        self.assertNotIn("SENTINEL", content)

    def test_trace_inside_repo_refused(self):
        trace = REPO_ROOT / "trace_should_not_exist.jsonl"
        proc = self.run_cli("--trace", str(trace))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertTrue(proc.stderr.startswith("error: "), proc.stderr)
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1, proc.stderr)
        self.assertFalse(trace.exists())
        self.assertFalse(self.out.exists())

    def test_trace_refused_before_data_is_loaded(self):
        trace = REPO_ROOT / "trace_should_not_exist.jsonl"
        proc = self.run_cli("--trace", str(trace), data=self.dir / "missing.csv")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("inside the repository", proc.stderr)

    def test_trace_write_failure_writes_no_report(self):
        trace = self.dir / "no_such_dir" / "t.jsonl"
        proc = self.run_cli("--trace", str(trace))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertFalse(trace.exists())
        self.assertFalse(self.out.exists())

    def test_no_trace_summary_unchanged(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("trace_records", proc.stdout)

    def test_metrics_out(self):
        import hashlib
        base = ("--sample-size", "10", "--seed", "3")
        plain = self.run_cli(*base, out=self.dir / "plain.json")
        self.assertEqual(plain.returncode, 0, plain.stderr)
        mpath = self.dir / "m.json"
        proc = self.run_cli(*base, "--metrics-out", str(mpath), "--cohort", "hard_case",
                            "--run-at", "2026-11-18T10:00:00Z")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, plain.stdout)
        report = json.loads(self.out.read_text(encoding="utf-8"))
        raw = mpath.read_text(encoding="utf-8")
        m = json.loads(raw)
        self.assertEqual(m["kind"], "gold")
        self.assertEqual(m["run_at"], "2026-11-18T10:00:00Z")
        self.assertEqual(m["cohort"], {
            "name": "hard_case", "split": "dev", "brand_population": "all",
            "gold_revision": "sha256:" + hashlib.sha256(self.gold.read_bytes()).hexdigest()})
        self.assertEqual(m["n"], report["threads_scored"])
        self.assertEqual(m["coverage"]["threads_routed"], report["threads_routed"])
        self.assertEqual(m["coverage"], {k: report[k] for k in (
            "threads_routed", "threads_scored", "threads_without_gold",
            "gold_rows_unmatched")})
        self.assertEqual(m["sample"], {"size": 10, "seed": 3})
        self.assertEqual(m["config"]["threshold"], 0.5)
        self.assertEqual(m["config"]["prices"], report["prices"])
        ops = m["operations"]
        self.assertEqual(set(ops["tier_shares"]), set(report["tier_shares"]))
        for tier, share in ops["tier_shares"].items():
            self.assertEqual(share["value"], report["tier_shares"][tier], tier)
            self.assertEqual(share["n"], report["threads_routed"], tier)
            self.assertEqual(share["ci"], report["tier_shares_ci"][tier], tier)
        self.assertEqual(ops["cost_per_1000_usd"], report["cost_per_1000_threads"])
        self.assertTrue(ops["prices_are_placeholders"])
        self.assertEqual(m["escalation"]["counts"]["fn"], report["missed_escalations"])
        self.assertNotIn('"ids"', raw)
        self.assertNotIn("SENTINEL", raw)
        # the fixture's thread ids are small numbers that also occur as counts, so check
        # that no id-bearing key or string id appears, plus the distinctive gold-only id
        self.assertNotIn("thread_id", raw)
        for line in TWCS_ROWS:
            self.assertNotIn('"%s"' % line.split(",")[0], raw)
        self.assertNotIn("424242", raw)

        # together with --trace: trace, report and metrics are all written
        trace, out2, m2 = self.dir / "t.jsonl", self.dir / "r2.json", self.dir / "m2.json"
        proc = self.run_cli("--trace", str(trace), "--metrics-out", str(m2), "--cohort",
                            "gold_random", out=out2)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip().endswith(" topics_loaded=2 trace_records=5"),
                        proc.stdout)
        for path in (trace, out2, m2):
            self.assertTrue(path.is_file(), path)
        self.assertEqual(json.loads(m2.read_text(encoding="utf-8"))["cohort"]["name"],
                         "gold_random")

    def test_metrics_out_refused_before_anything_is_written(self):
        trace = self.dir / "t.jsonl"
        mpath = self.dir / "m.json"
        variants = {
            "no cohort": ("--metrics-out", str(mpath)),
            "bad run-at": ("--metrics-out", str(mpath), "--cohort", "hard_case",
                           "--run-at", "2026-11-18 10:00:00"),
            "bad run-at without metrics": ("--run-at", "tomorrow"),
        }
        for name, extra in variants.items():
            with self.subTest(name):
                proc = self.run_cli("--trace", str(trace), *extra,
                                    data=self.dir / "missing.csv")
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertTrue(proc.stderr.startswith("error: "), proc.stderr)
                self.assertEqual(len(proc.stderr.strip().splitlines()), 1, proc.stderr)
                self.assertNotIn("data file not found", proc.stderr)
                self.assertFalse(trace.exists())
                self.assertFalse(self.out.exists())
                self.assertFalse(mpath.exists())

    def test_metrics_write_failure_exits_2(self):
        mpath = self.dir / "no_such_dir" / "m.json"
        proc = self.run_cli("--metrics-out", str(mpath), "--cohort", "gold_random")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertFalse(mpath.exists())


if __name__ == "__main__":
    unittest.main()
