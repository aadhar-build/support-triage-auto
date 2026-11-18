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


FIXTURE_PRICES = REPO_ROOT / "tests" / "fixtures" / "model_prices.json"
FIXTURE_THRESHOLDS = REPO_ROOT / "tests" / "fixtures" / "thresholds_spend.json"
SPLIT_ALL_EVAL = {"seed": 1, "unit": "thread", "fractions": {"dev": 0, "eval": 1.0, "full": 0}}
BANNED_KEYS = {"reason", "reasons", "reason_codes", "topic_id"}


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


class ScriptedClient:
    """Always answers with one valid reply priced at (1000 in, 50 out) on fixture-model."""

    def __init__(self):
        from triage.model_tier import ModelReply, TokenUsage
        self.calls = []
        self._reply = ModelReply(
            text='{"intent": "billing", "urgency": "normal", "action": "route", '
                 '"confidence": 0.7}',
            usage={"fixture-model": TokenUsage(1000, 50, 0, 0)})

    def complete(self, system, user):
        self.calls.append((system, user))
        return self._reply


class MustNotCall:
    def complete(self, system, user):
        raise AssertionError("complete() was called on a refused run")


class TestLarger(CliTestBase):
    def setUp(self):
        super().setUp()
        from datetime import datetime, timezone
        from triage.spend_log import SpendLog
        self.clock = lambda: datetime(2026, 11, 18, 10, 0, 0, tzinfo=timezone.utc)
        self.log = self.dir / "state" / "spend.jsonl"
        self.log.parent.mkdir()
        SpendLog.create(self.log)

    def main(self, *extra, client=None, split=None, out=None, log=None,
             prices=FIXTURE_PRICES, thresholds=FIXTURE_THRESHOLDS):
        import contextlib
        import io
        from triage import run_eval
        argv = ["--data", str(self.data), "--gold", str(self.gold),
                "--split-config", str(split or self.split), "--out", str(out or self.out),
                "--rules", str(FIXTURE_RULES), "--prices", str(prices),
                "--thresholds", str(thresholds), "--spend-log", str(log or self.log),
                "--larger-model", "fixture-model"]
        argv.extend(extra)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = run_eval.main(argv, model_client=client, clock=self.clock)
        return code, stdout.getvalue(), stderr.getvalue()

    def events(self, path=None):
        return [json.loads(x) for x in (path or self.log).read_text(
            encoding="utf-8").splitlines()]

    def test_fake_default_fields(self):
        missing = {name: self.dir / ("missing_%s" % name) for name in
                   ("prices.json", "thresholds.json", "spend.jsonl")}
        outs = {}
        for name, extra in (("default", ()), ("fake", ("--larger", "fake"))):
            out = self.dir / ("%s.json" % name)
            code, stdout, err = self.main(*extra, out=out, prices=missing["prices.json"],
                                          thresholds=missing["thresholds.json"],
                                          log=missing["spend.jsonl"])
            self.assertEqual(code, 0, err)
            self.assertIn(" larger=fake spend_usd=0.0000 unconfirmed_usd=0.0000 "
                          "month_to_date_usd=n/a stopped_by=none split=dev", stdout)
            outs[name] = json.loads(out.read_text(encoding="utf-8"))
        for path in missing.values():
            self.assertFalse(path.exists(), path)
        self.assertEqual(outs["default"], outs["fake"])
        data = outs["fake"]
        self.assertEqual(data["larger_mode"], "fake")
        self.assertEqual(data["larger_model"], "fake-larger")
        for key in ("larger_input_tokens", "larger_output_tokens", "larger_answered"):
            self.assertEqual(data[key], 0, key)
        self.assertEqual(data["larger_confirmed_usd"], 0.0)
        self.assertEqual(data["larger_unconfirmed_usd"], 0.0)
        self.assertIsNone(data["monthly_cap_usd"])
        self.assertIsNone(data["month_to_date_usd"])
        self.assertFalse(data["larger_post_merge_eval"])
        # same metric values as the subprocess run of the unchanged CLI
        proc = self.run_cli(out=self.dir / "cli.json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cli = json.loads((self.dir / "cli.json").read_text(encoding="utf-8"))
        for key in ("action_accuracy", "intent_accuracy", "missed_escalations",
                    "false_escalations", "needs_person_share", "tier_shares",
                    "cost_per_1000_threads"):
            self.assertEqual(data[key], cli[key], key)
        self.assertEqual(len(self.events()), 1)

    def test_real_refusals(self):
        bad_json = self.write("bad.json", "{nope")
        other_prices = self.write("other_prices.json", json.dumps({
            "version": "v", "models": {"some-other-model": {"bands": [
                {"max_input_tokens": None, "input_usd_per_mtok": 1, "output_usd_per_mtok": 1}]}}}))
        no_key = self.write("no_key.json", json.dumps({"guardrails": {}}))
        corrupt = self.dir / "corrupt.jsonl"
        corrupt.write_text(self.log.read_text(encoding="utf-8") + "{partial", encoding="utf-8")
        at_cap = self.dir / "at_cap.jsonl"
        at_cap.write_text(self.log.read_text(encoding="utf-8") + json.dumps(
            {"event": "reserve", "id": "a" * 32, "at": "2026-11-02T00:00:00+00:00",
             "run_id": "x", "model": "fixture-model", "split": "dev", "usd": 200.0}) + "\n",
            encoding="utf-8")
        in_repo = REPO_ROOT / "spend_should_not_exist.jsonl"
        eval_split = self.write("split_eval.json", json.dumps(SPLIT_ALL_EVAL))
        cases = {
            "split full": dict(extra=("--split", "full")),
            "split full post-merge": dict(extra=("--split", "full", "--post-merge-eval")),
            "split eval without opt-in": dict(extra=("--split", "eval"), split=eval_split),
            "post-merge on dev": dict(extra=("--post-merge-eval",)),
            "missing prices": dict(prices=self.dir / "nope.json"),
            "bad prices": dict(prices=bad_json),
            "model not priced": dict(prices=other_prices),
            "missing thresholds": dict(thresholds=self.dir / "nope_t.json"),
            "bad thresholds": dict(thresholds=no_key),
            "spend log in repo": dict(log=in_repo),
            "spend log in repo with create": dict(log=in_repo, extra=("--create-spend-log",)),
            "missing log": dict(log=self.dir / "nope.jsonl"),
            "corrupt log": dict(log=corrupt),
            "month at cap": dict(log=at_cap, thresholds=self.write(
                "t200.json", json.dumps({"guardrails": {"api_spend_usd_per_month": {"max": 200}}}))),
            "max spend -1": dict(extra=("--max-spend-usd", "-1")),
            "max spend nan": dict(extra=("--max-spend-usd", "nan")),
            "max spend inf": dict(extra=("--max-spend-usd", "inf")),
            "max spend 2.51": dict(extra=("--max-spend-usd", "2.51")),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                extra = kw.pop("extra", ())
                out = self.dir / ("out_%s.json" % name.replace(" ", "_"))
                before = {p: p.read_bytes() for p in (self.log, corrupt, at_cap)}
                code, stdout, err = self.main("--larger", "real", *extra, client=MustNotCall(),
                                              out=out, **kw)
                self.assertEqual(code, 2, err)
                self.assertTrue(err.startswith("error: "), err)
                self.assertEqual(len(err.strip().splitlines()), 1, err)
                self.assertEqual(stdout, "")
                self.assertFalse(out.exists())
                self.assertFalse(in_repo.exists())
                for p, b in before.items():
                    self.assertEqual(p.read_bytes(), b, p)

    def test_post_merge_eval_needs_real(self):
        code, _, err = self.main("--post-merge-eval", "--split", "eval", client=MustNotCall())
        self.assertEqual(code, 2)
        self.assertIn("--larger real", err)

    def test_create_spend_log(self):
        new = self.dir / "fresh" / "spend.jsonl"
        code, _, err = self.main("--larger", "real", client=ScriptedClient(), log=new)
        self.assertEqual(code, 2)
        self.assertFalse(new.exists())
        code, _, err = self.main("--larger", "real", "--create-spend-log",
                                 client=ScriptedClient(), log=new)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.events(new)[0]["event"], "open")
        code, _, err = self.main("--larger", "real", "--create-spend-log",
                                 client=ScriptedClient(), log=new)
        self.assertEqual(code, 2, err)

    def test_real_mode_report(self):
        client = ScriptedClient()
        md_out = self.dir / "report.md"
        code, stdout, err = self.main("--larger", "real", client=client)
        self.assertEqual(code, 0, err)
        code_md, _, err = self.main("--larger", "real", "--format", "md", client=ScriptedClient(),
                                    out=md_out)
        self.assertEqual(code_md, 0, err)
        n = len(client.calls)
        self.assertGreaterEqual(n, 1)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(data["larger_mode"], "real")
        self.assertEqual(data["larger_model"], "fixture-model")
        self.assertEqual(data["larger_prompt_version"], "larger-tier-v1")
        self.assertEqual(data["larger_input_tokens"], 1000 * n)
        self.assertEqual(data["larger_output_tokens"], 50 * n)
        self.assertAlmostEqual(data["larger_confirmed_usd"], 0.00125 * n)
        self.assertEqual(data["larger_unconfirmed_usd"], 0.0)
        self.assertEqual(data["max_spend_usd"], 2.5)
        self.assertEqual(data["monthly_cap_usd"], 200.0)
        self.assertAlmostEqual(data["month_to_date_usd"], 0.00125 * n)
        self.assertIsNone(data["larger_stopped_by"])
        self.assertEqual(data["larger_answered"], n)
        self.assertEqual(set(data["larger_fallbacks"].values()), {0})
        self.assertFalse(data["larger_post_merge_eval"])
        self.assertEqual(data["split"], "dev")
        self.assertIn(" larger=real spend_usd=", stdout)
        self.assertIn(" stopped_by=none split=dev topics_loaded=2", stdout)
        md = md_out.read_text(encoding="utf-8")
        self.assertIn("## Model spend (API-equivalent USD)", md)
        for row in ("confirmed_usd", "unconfirmed_usd", "monthly_cap_usd", "month_to_date_usd",
                    "stopped_by", "larger_prompt_version"):
            self.assertIn("| %s" % row, md)
        reserves = [e for e in self.events() if e["event"] == "reserve"]
        self.assertEqual(len(reserves), 2 * n)  # two runs, one reserve per call
        self.assertEqual({e["split"] for e in reserves}, {"dev"})

    def test_spend_log_has_no_thread_data(self):
        code, _, err = self.main("--larger", "real", client=ScriptedClient())
        self.assertEqual(code, 0, err)
        content = self.log.read_text(encoding="utf-8")
        import csv
        import io
        rows = list(csv.DictReader(io.StringIO(TWCS_HEADER + "\n".join(TWCS_ROWS) + "\n")))
        for row in rows:
            for value in (row["text"], row["author_id"]):
                self.assertNotIn(value, content)
        self.assertNotIn("SENTINEL", content)
        self.assertNotIn("thread", content)
        # fixture ids are single digits that also occur inside uuids and timestamps, so
        # check that no id appears as a JSON value
        for event in self.events():
            for key, value in event.items():
                for row in rows:
                    self.assertNotEqual(str(value), row["tweet_id"], key)

    def test_real_mode_trace(self):
        from triage.trace import validate_record
        row = '11,cust_SENTINELF,True,Tue Oct 31 22:16:47 +0000 2017,"thanks, quuxfrob SENTINEL_TEXT_11",,'
        self.data = self.write("twcs_topic.csv", TWCS_HEADER + "\n".join(TWCS_ROWS + [row]) + "\n")
        trace = self.dir / "t.jsonl"
        code, _, err = self.main("--larger", "real", "--trace", str(trace),
                                 client=ScriptedClient())
        self.assertEqual(code, 0, err)
        larger_steps = 0
        for line in trace.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            validate_record(rec)
            for step in rec["tiers"]:
                if step["tier"] == "larger":
                    larger_steps += 1
                    self.assertEqual(step["model_id"], "fixture-model")
            if "escalate_always" in rec["reason_codes"]:
                self.assertNotIn("larger", [t["tier"] for t in rec["tiers"]])
                self.assertEqual(rec["thread_id"], 11)
        self.assertGreaterEqual(larger_steps, 1)

    def test_post_merge_eval_recorded(self):
        eval_split = self.write("split_eval.json", json.dumps(SPLIT_ALL_EVAL))
        client = ScriptedClient()
        code, _, err = self.main("--larger", "real", "--split", "eval", "--post-merge-eval",
                                 client=client, split=eval_split)
        self.assertEqual(code, 0, err)
        self.assertGreaterEqual(len(client.calls), 1)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(data["split"], "eval")
        self.assertTrue(data["larger_post_merge_eval"])
        reserves = [e for e in self.events() if e["event"] == "reserve"]
        self.assertGreaterEqual(len(reserves), 1)
        self.assertEqual({e["split"] for e in reserves}, {"eval"})

    def test_real_mode_metrics_shape(self):
        eval_split = self.write("split_eval.json", json.dumps(SPLIT_ALL_EVAL))
        shapes = {}
        for mode in ("fake", "real"):
            mpath = self.dir / ("m_%s.json" % mode)
            extra = ("--split", "eval", "--post-merge-eval") if mode == "real" else \
                ("--split", "eval")
            code, _, err = self.main("--larger", mode, *extra, "--metrics-out", str(mpath),
                                     "--cohort", "gold_random", "--run-at",
                                     "2026-11-18T10:00:00Z", client=ScriptedClient(),
                                     split=eval_split)
            self.assertEqual(code, 0, err)
            m = json.loads(mpath.read_text(encoding="utf-8"))
            self.assertFalse(BANNED_KEYS & set(_keys(m)), mode)
            shapes[mode] = (set(m), set(m["operations"]), set(m["config"]))
        self.assertEqual(shapes["real"], shapes["fake"])


if __name__ == "__main__":
    unittest.main()
