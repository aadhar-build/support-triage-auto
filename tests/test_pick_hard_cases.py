"""Tests for python3 -m triage.pick_hard_cases.

All rows are made up by hand and written to a temp dir at run time (.gitignore ignores *.csv).
Nothing here reads data/raw/ or the real data/split.json.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from triage.pick_hard_cases import escalation_signals
from triage.threads import Thread, Turn

_T0 = datetime(2017, 10, 31, 22, 10, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parents[1]

TWCS_HEADER = "tweet_id,author_id,inbound,created_at,text,response_tweet_id,in_response_to_tweet_id\n"
BASE = 9100000  # fixture ids are BASE + small number, so no id can collide with a count


def _id(n):
    return BASE + n


def _row(n, author, inbound, text, response="", parent=""):
    response = "" if response == "" else str(_id(response))
    parent = "" if parent == "" else str(_id(parent))
    return '%d,%s,%s,Tue Oct 31 22:10:%02d +0000 2017,"%s",%s,%s' % (
        _id(n), author, inbound, n, text, response, parent)


TWCS_ROWS = [
    # 11: legal + fraud_or_hacked + strong_negative (3), brand reply 12
    _row(11, "cust_SENTINEL11", True, "my lawyer says this fraud is the worst SENTINEL_TEXT_11", 12),
    _row(12, "SENTINELBRAND", False, "sorry to hear that SENTINEL_TEXT_12", parent=11),
    # 13: fraud_or_hacked + strong_negative (2)
    _row(13, "cust_SENTINEL13", True, "my account was hacked, terrible SENTINEL_TEXT_13"),
    # 15, 17, 19, 21, 23, 27, 33: one signal each
    _row(15, "cust_SENTINEL15", True, "worst service SENTINEL_TEXT_15"),
    _row(17, "cust_SENTINEL17", True, "still waiting on my order SENTINEL_TEXT_17"),
    _row(19, "cust_SENTINEL19", True, "talking to my attorney SENTINEL_TEXT_19"),
    _row(21, "cust_SENTINEL21", True, "this app is useless SENTINEL_TEXT_21"),
    # 23: many strong_negative words, still one signal
    _row(23, "cust_SENTINEL23", True, "terrible terrible awful horrible SENTINEL_TEXT_23"),
    # 25: no signal
    _row(25, "cust_SENTINEL25", True, "hello there SENTINEL_TEXT_25"),
    # 27: only "injured", which the cheap tier does not know
    _row(27, "cust_SENTINEL27", True, "my child was injured by the charger SENTINEL_TEXT_27"),
    # 29: signal words only in the brand turn
    _row(29, "cust_SENTINEL29", True, "can you help SENTINEL_TEXT_29", 30),
    _row(30, "SENTINELBRAND", False, "this is terrible, ask our lawyer SENTINEL_TEXT_30", parent=29),
    # 31: fireworks / pursue only
    _row(31, "cust_SENTINEL31", True, "fireworks tonight, I pursue deals SENTINEL_TEXT_31"),
    # 33: three turns, repeated_contact in the second inbound turn
    _row(33, "cust_SENTINEL33", True, "where is my refund SENTINEL_TEXT_33", 34),
    _row(34, "SENTINELBRAND", False, "please DM us SENTINEL_TEXT_34", 35, 33),
    _row(35, "cust_SENTINEL33", True, "still no reply SENTINEL_TEXT_35", parent=34),
]
ONE_SIGNAL = {_id(n) for n in (15, 17, 19, 21, 23, 27, 33)}
QUALIFYING = {_id(11), _id(13)} | ONE_SIGNAL
FIXTURE_IDS = [str(_id(n)) for n in range(11, 36)]


def _split(dev, eval_, full, seed=1):
    return {"seed": seed, "unit": "thread", "fractions": {"dev": dev, "eval": eval_, "full": full}}


SPLIT_ALL_EVAL = _split(0, 1.0, 0)
SPLIT_ALL_DEV = _split(1.0, 0, 0)
SPLIT_ALL_FULL = _split(0, 0, 1.0)
SPLIT_MIXED = _split(0.3, 0.4, 0.3)


class CliTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.data = self.write("twcs.csv", TWCS_HEADER + "\n".join(TWCS_ROWS) + "\n")
        self.split = self.write("split.json", json.dumps(SPLIT_ALL_EVAL))
        self.exclude = self.write("random300.csv", "thread_id\n")
        self.out = self.dir / "labelling.jsonl"

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_cli(self, *extra, data=None, split=None, exclude=None, out=None, cwd=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        args = [sys.executable, "-m", "triage.pick_hard_cases",
                "--data", str(data or self.data),
                "--split-config", str(split or self.split),
                "--exclude-ids", str(exclude or self.exclude),
                "--out", str(out or self.out)]
        args.extend(extra)
        return subprocess.run(args, env=env, capture_output=True, text=True, timeout=60,
                              cwd=str(cwd or self.dir))

    def picks(self, path=None):
        lines = (path or self.out).read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def picked_ids(self, path=None):
        return [p["thread_id"] for p in self.picks(path)]


class TestPickHardCases(CliTestBase):
    def test_rank_by_distinct_signals(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        picks = self.picks()
        self.assertEqual([p["thread_id"] for p in picks[:2]], [_id(11), _id(13)])
        self.assertEqual(picks[0]["signals"], ["legal", "fraud_or_hacked", "strong_negative"])
        self.assertEqual(picks[1]["signals"], ["fraud_or_hacked", "strong_negative"])
        self.assertEqual({p["thread_id"] for p in picks[2:]}, ONE_SIGNAL)
        by_id = {p["thread_id"]: p for p in picks}
        self.assertEqual(by_id[_id(23)]["signals"], ["strong_negative"])
        self.assertEqual([p["rank"] for p in picks], list(range(1, 10)))

    def test_dev_and_full_splits_never_picked(self):
        for name, config in (("dev", SPLIT_ALL_DEV), ("full", SPLIT_ALL_FULL)):
            with self.subTest(split=name):
                split = self.write("split-%s.json" % name, json.dumps(config))
                proc = self.run_cli(split=split)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.picked_ids(), [])
                summary = json.loads(proc.stdout)
                self.assertEqual(summary["threads_considered"], 0)
                self.assertEqual(summary["threads_picked"], 0)

    def test_mixed_split_picks_only_eval(self):
        from triage.split import assign_split, config_from_dict
        config = config_from_dict(SPLIT_MIXED)
        eval_hits = {i for i in QUALIFYING if assign_split(i, config) == "eval"}
        other_hits = QUALIFYING - eval_hits
        self.assertTrue(eval_hits, "fixture needs signal-hitting eval threads")
        self.assertTrue(other_hits, "fixture needs signal-hitting dev/full threads")
        split = self.write("split-mixed.json", json.dumps(SPLIT_MIXED))
        proc = self.run_cli(split=split)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        picked = set(self.picked_ids())
        self.assertEqual(picked, eval_hits)
        self.assertFalse(picked & other_hits)

    def test_random_sample_ids_excluded(self):
        exclude = self.write("exclude.csv", "thread_id,note\n%d,x\n999999,y\n%d,dup\n" % (_id(19), _id(19)))
        proc = self.run_cli(exclude=exclude)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(_id(19), self.picked_ids())
        self.assertEqual(set(self.picked_ids()), QUALIFYING - {_id(19)})
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["threads_considered"], 12)
        self.assertEqual(summary["threads_excluded_random_sample"], 1)
        self.assertEqual(summary["exclude_ids_unmatched"], 1)
        self.assertEqual(summary["threads_qualifying"], 8)

    def test_no_signal_never_picked_and_fewer_than_n(self):
        proc = self.run_cli("--n", "50")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        picked = self.picked_ids()
        self.assertEqual(sorted(picked), sorted(QUALIFYING))
        for no_signal in (_id(25), _id(29), _id(31)):
            self.assertNotIn(no_signal, picked)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["threads_qualifying"], 9)
        self.assertEqual(summary["threads_picked"], 9)

    def test_same_inputs_and_seed_same_ids(self):
        a, b = self.dir / "a.jsonl", self.dir / "b.jsonl"
        for out in (a, b):
            proc = self.run_cli("--n", "5", "--seed", "7", out=out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(len(self.picked_ids(a)), 5)

    def test_seed_reorders_ties_only(self):
        orders_small = set()
        for seed in range(20):
            out = self.dir / ("all-%d.jsonl" % seed)
            proc = self.run_cli("--seed", str(seed), out=out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            picks = self.picks(out)
            self.assertEqual(picks[0]["thread_id"], _id(11))
            scores = [len(p["signals"]) for p in picks]
            self.assertEqual(scores, sorted(scores, reverse=True))
            self.assertEqual({p["thread_id"] for p in picks}, QUALIFYING)

            out = self.dir / ("five-%d.jsonl" % seed)
            proc = self.run_cli("--n", "5", "--seed", str(seed), out=out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            ids = self.picked_ids(out)
            self.assertEqual(ids[:2], [_id(11), _id(13)])
            self.assertTrue(set(ids[2:]) <= ONE_SIGNAL)
            orders_small.add(tuple(ids))
        self.assertGreaterEqual(len(orders_small), 2)

    def test_out_inside_repo_refused(self):
        name = "hardcase-guard-test-%d.jsonl" % os.getpid()
        target = REPO_ROOT / name
        link_dir = self.dir / "repo-link"
        link_dir.symlink_to(REPO_ROOT, target_is_directory=True)
        link_file = self.dir / "out-link.jsonl"
        link_file.symlink_to(target)
        cases = [
            ("plain", str(target), self.dir),
            ("relative from repo root", name, REPO_ROOT),
            ("dotdot", str(REPO_ROOT / "src" / ".." / name), self.dir),
            ("symlinked dir", str(link_dir / name), self.dir),
            ("symlink file", str(link_file), self.dir),
        ]
        self.addCleanup(lambda: target.unlink() if target.exists() else None)
        self.assertFalse(target.exists())
        for label, out, cwd in cases:
            with self.subTest(case=label):
                summary_out = self.dir / "guard-summary.json"
                proc = self.run_cli("--summary-out", str(summary_out), out=out, cwd=cwd)
                self.assertEqual(proc.returncode, 2)
                self.assertTrue(proc.stderr.startswith("error: "), proc.stderr)
                self.assertIn("inside the repository", proc.stderr)
                self.assertFalse(target.exists())
                self.assertFalse(summary_out.exists())
                self.assertEqual(proc.stdout, "")

    def test_out_dotdot_outside_repo_accepted(self):
        sub = self.dir / "sub"
        sub.mkdir()
        out = sub / ".." / "dotdot.jsonl"
        proc = self.run_cli(out=out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.dir / "dotdot.jsonl").exists())

    def test_summary_has_counts_only(self):
        summary_out = self.dir / "summary.json"
        proc = self.run_cli("--summary-out", str(summary_out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        stdout_proc = self.run_cli()
        self.assertEqual(stdout_proc.returncode, 0, stdout_proc.stderr)
        file_text = summary_out.read_text(encoding="utf-8")
        for text in (file_text, stdout_proc.stdout):
            summary = json.loads(text)
            self.assertEqual(set(summary), {
                "threads_considered", "threads_excluded_random_sample", "exclude_ids_unmatched",
                "threads_qualifying", "threads_picked", "picked_per_signal", "n", "seed"})
            self.assertEqual(summary["picked_per_signal"], {
                "legal": 2, "fraud_or_hacked": 2, "safety": 1, "repeated_contact": 2,
                "strong_negative": 5})
        for text in (file_text, proc.stdout, proc.stderr, stdout_proc.stdout,
                     stdout_proc.stderr):
            self.assertNotIn("SENTINEL", text)
            self.assertNotIn("cust_", text)
            for fixture_id in FIXTURE_IDS:
                self.assertNotIn(fixture_id, text)

    def test_labelling_file_contents(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        by_id = {p["thread_id"]: p for p in self.picks()}
        self.assertEqual(set(by_id[_id(11)]), {"rank", "thread_id", "signals", "turns"})
        self.assertEqual(by_id[_id(11)]["rank"], 1)
        self.assertEqual(by_id[_id(11)]["turns"], [
            {"tweet_id": _id(11), "inbound": True, "created_at": "2017-10-31T22:10:11+00:00",
             "text": "my lawyer says this fraud is the worst SENTINEL_TEXT_11"},
            {"tweet_id": _id(12), "inbound": False, "created_at": "2017-10-31T22:10:12+00:00",
             "text": "sorry to hear that SENTINEL_TEXT_12"},
        ])
        self.assertEqual([t["text"] for t in by_id[_id(33)]["turns"]], [
            "where is my refund SENTINEL_TEXT_33", "please DM us SENTINEL_TEXT_34",
            "still no reply SENTINEL_TEXT_35"])
        self.assertEqual(by_id[_id(33)]["signals"], ["repeated_contact"])
        raw = self.out.read_text(encoding="utf-8")
        self.assertNotIn("cust_SENTINEL", raw)
        self.assertNotIn("SENTINELBRAND", raw)

    def test_signal_outside_cheap_tier_keywords_picked(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        by_id = {p["thread_id"]: p for p in self.picks()}
        self.assertIn(_id(27), by_id)
        self.assertEqual(by_id[_id(27)]["signals"], ["safety"])


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
