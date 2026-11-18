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
TWCS_ROWS = [
    # 11: legal + fraud_or_hacked + strong_negative (3), brand reply 12
    '11,cust_SENTINEL11,True,Tue Oct 31 22:10:11 +0000 2017,"my lawyer says this fraud is the worst SENTINEL_TEXT_11",12,',
    '12,SENTINELBRAND,False,Tue Oct 31 22:10:12 +0000 2017,"sorry to hear that SENTINEL_TEXT_12",,11',
    # 13: fraud_or_hacked + strong_negative (2)
    '13,cust_SENTINEL13,True,Tue Oct 31 22:10:13 +0000 2017,"my account was hacked, terrible SENTINEL_TEXT_13",,',
    # 15, 17, 19, 21, 23, 27, 33: one signal each
    '15,cust_SENTINEL15,True,Tue Oct 31 22:10:15 +0000 2017,"worst service SENTINEL_TEXT_15",,',
    '17,cust_SENTINEL17,True,Tue Oct 31 22:10:17 +0000 2017,"still waiting on my order SENTINEL_TEXT_17",,',
    '19,cust_SENTINEL19,True,Tue Oct 31 22:10:19 +0000 2017,"talking to my attorney SENTINEL_TEXT_19",,',
    '21,cust_SENTINEL21,True,Tue Oct 31 22:10:21 +0000 2017,"this app is useless SENTINEL_TEXT_21",,',
    # 23: many strong_negative words, still one signal
    '23,cust_SENTINEL23,True,Tue Oct 31 22:10:23 +0000 2017,"terrible terrible awful horrible SENTINEL_TEXT_23",,',
    # 25: no signal
    '25,cust_SENTINEL25,True,Tue Oct 31 22:10:25 +0000 2017,"hello there SENTINEL_TEXT_25",,',
    # 27: only "injured", which the cheap tier does not know
    '27,cust_SENTINEL27,True,Tue Oct 31 22:10:27 +0000 2017,"my child was injured by the charger SENTINEL_TEXT_27",,',
    # 29: signal words only in the brand turn
    '29,cust_SENTINEL29,True,Tue Oct 31 22:10:29 +0000 2017,"can you help SENTINEL_TEXT_29",30,',
    '30,SENTINELBRAND,False,Tue Oct 31 22:10:30 +0000 2017,"this is terrible, ask our lawyer SENTINEL_TEXT_30",,29',
    # 31: fireworks / pursue only
    '31,cust_SENTINEL31,True,Tue Oct 31 22:10:31 +0000 2017,"fireworks tonight, I pursue deals SENTINEL_TEXT_31",,',
    # 33: three turns, repeated_contact in the second inbound turn
    '33,cust_SENTINEL33,True,Tue Oct 31 22:10:33 +0000 2017,"where is my refund SENTINEL_TEXT_33",34,',
    '34,SENTINELBRAND,False,Tue Oct 31 22:10:34 +0000 2017,"please DM us SENTINEL_TEXT_34",35,33',
    '35,cust_SENTINEL33,True,Tue Oct 31 22:10:35 +0000 2017,"still no reply SENTINEL_TEXT_35",,34',
]
ONE_SIGNAL = {15, 17, 19, 21, 23, 27, 33}
QUALIFYING = {11, 13} | ONE_SIGNAL
ALL_IDS = [str(i) for i in range(11, 36)] + ["SENTINEL"]


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
        self.assertEqual([p["thread_id"] for p in picks[:2]], [11, 13])
        self.assertEqual(picks[0]["signals"], ["legal", "fraud_or_hacked", "strong_negative"])
        self.assertEqual(picks[1]["signals"], ["fraud_or_hacked", "strong_negative"])
        self.assertEqual({p["thread_id"] for p in picks[2:]}, ONE_SIGNAL)
        by_id = {p["thread_id"]: p for p in picks}
        self.assertEqual(by_id[23]["signals"], ["strong_negative"])
        self.assertEqual([p["rank"] for p in picks], list(range(1, 10)))


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
