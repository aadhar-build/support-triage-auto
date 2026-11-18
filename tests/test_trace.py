"""Tests for triage.trace. All threads are made up by hand; temp dirs only, no real data."""

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from triage.cheap_tier import CheapTier
from triage.policy_check import load_escalate_topics
from triage.router import route_thread
from triage.threads import Thread, Turn
from triage.tiers import FakeAgentTier, FakeLargerTier
from triage.trace import (
    TRACE_SCHEMA,
    RunInfo,
    TraceError,
    build_record,
    code_version,
    validate_record,
    write_trace,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2017, 10, 31, 22, 0, 0, tzinfo=timezone.utc)
RECORD_KEYS = {"trace_schema", "thread_id", "code_version", "code_dirty", "rules_version",
               "drift_config_version", "threshold", "split", "seed", "tiers", "final",
               "reason_codes", "topic_id"}
TIER_KEYS = {"tier", "model_id", "intent", "urgency", "action", "confidence"}
FINAL_KEYS = {"intent", "urgency", "action", "confidence"}
NULLABLE = {"rules_version", "drift_config_version"}
RUN = RunInfo(code_version="a" * 40, code_dirty=False, rules_version=None,
              drift_config_version=None, threshold=0.5, split="dev", seed=3)


def routed(root_id=21, text="hello SENTINEL_TEXT"):
    turn = Turn(root_id, "cust_SENTINEL", True, T0, text)
    t = Thread(root_tweet_id=root_id, brand_ids=(99,), turns=(turn,), brand_replied=False)
    return route_thread(t, CheapTier(), FakeLargerTier(), FakeAgentTier(), topics=())


TOPICS = load_escalate_topics(Path(__file__).resolve().parent / "fixtures" / "policy_rules.json")


def forced(root_id=22):
    turn = Turn(root_id, "cust_SENTINEL", True, T0, "thanks, quuxfrob")
    t = Thread(root_tweet_id=root_id, brand_ids=(), turns=(turn,), brand_replied=False)
    return route_thread(t, CheapTier(), FakeLargerTier(), FakeAgentTier(), topics=TOPICS)


def record(root_id=21):
    return build_record(routed(root_id), RUN)


class TestRecord(unittest.TestCase):
    def test_record_keys(self):
        rec = record()
        self.assertEqual(set(rec), RECORD_KEYS)
        self.assertEqual(rec["trace_schema"], TRACE_SCHEMA)
        self.assertEqual(rec["thread_id"], 21)
        self.assertEqual(rec["code_version"], "a" * 40)
        self.assertIs(rec["code_dirty"], False)
        self.assertEqual((rec["threshold"], rec["split"], rec["seed"]), (0.5, "dev", 3))
        self.assertEqual([t["tier"] for t in rec["tiers"]], ["cheap", "larger", "agent"])
        for tier in rec["tiers"]:
            self.assertEqual(set(tier), TIER_KEYS)
        self.assertEqual(rec["tiers"][0], {"tier": "cheap", "model_id": "keywords",
                                           "intent": "other", "urgency": "normal",
                                           "action": "ask", "confidence": 0.2})
        self.assertEqual(rec["final"], {"intent": "other", "urgency": "normal",
                                        "action": "ask", "confidence": 1.0})
        self.assertEqual(set(rec["final"]), FINAL_KEYS)
        self.assertEqual(rec["reason_codes"], ["low_confidence_to_larger"])
        self.assertEqual(rec["topic_id"], [])
        self.assertEqual(TRACE_SCHEMA, 2)
        text = json.dumps(rec)
        self.assertNotIn("SENTINEL", text)
        self.assertNotIn("99", text)
        validate_record(rec)

    def test_missing_or_null_field_fails(self):
        for key in sorted(RECORD_KEYS):
            with self.subTest(delete=key):
                rec = record()
                del rec[key]
                with self.assertRaisesRegex(TraceError, key):
                    validate_record(rec)
                if key != "thread_id":
                    with self.assertRaisesRegex(TraceError, "thread 21"):
                        validate_record(rec)
        for key in sorted(RECORD_KEYS - NULLABLE):
            with self.subTest(null=key):
                rec = record()
                rec[key] = None
                with self.assertRaisesRegex(TraceError, key):
                    validate_record(rec)
        mutations = {
            "tiers": lambda r: r.__setitem__("tiers", []),
            "model_id": lambda r: r["tiers"][0].__setitem__("model_id", None),
            "confidence": lambda r: r["tiers"][1].pop("confidence"),
            "reason_codes": lambda r: r.__setitem__("reason_codes", []),
            "reason_codes ": lambda r: r.__setitem__("reason_codes", ["made_up_code"]),
            "trace_schema": lambda r: r.__setitem__("trace_schema", 1),
        }
        for key, mutate in mutations.items():
            with self.subTest(mutate=key):
                rec = record()
                mutate(rec)
                with self.assertRaisesRegex(TraceError, key.strip()):
                    validate_record(rec)

    def test_forced_record_validates(self):
        rec = build_record(forced(), RUN)
        self.assertEqual(set(rec), RECORD_KEYS)
        self.assertEqual(rec["topic_id"], ["t-zorblax"])
        self.assertIn("escalate_always", rec["reason_codes"])
        self.assertEqual([t["tier"] for t in rec["tiers"]], ["cheap", "agent"])
        self.assertEqual(rec["final"]["action"], "escalate")
        self.assertNotIn("quuxfrob", json.dumps(rec))
        validate_record(rec)

    def test_topic_id_consistency(self):
        def schema_1(r):
            r["trace_schema"] = 1
        cases = {
            "missing topic_id": (record, lambda r: r.pop("topic_id")),
            "topic_id not a list": (record, lambda r: r.__setitem__("topic_id", "t-zorblax")),
            "topic_id not kebab": (lambda: build_record(forced(), RUN),
                                   lambda r: r.__setitem__("topic_id", ["Not_Kebab"])),
            "topic_id holds a non-string": (lambda: build_record(forced(), RUN),
                                            lambda r: r.__setitem__("topic_id", [3])),
            "escalate_always with empty topic_id": (lambda: build_record(forced(), RUN),
                                                    lambda r: r.__setitem__("topic_id", [])),
            "topic_id without escalate_always": (record,
                                                 lambda r: r.__setitem__("topic_id", ["t-x"])),
            "schema 1": (record, schema_1),
        }
        for name, (make, mutate) in cases.items():
            with self.subTest(name):
                rec = make()
                mutate(rec)
                with self.assertRaises(TraceError) as cm:
                    validate_record(rec)
                self.assertIn("thread 2", str(cm.exception))
        rec = record()
        rec["trace_schema"] = 1
        with self.assertRaisesRegex(TraceError, "'trace_schema' is not 2"):
            validate_record(rec)

    def test_error_message_is_one_line(self):
        rec = record()
        del rec["split"]
        with self.assertRaises(TraceError) as cm:
            validate_record(rec)
        self.assertEqual(len(str(cm.exception).splitlines()), 1)

    def test_nullable_versions(self):
        rec = record()
        self.assertIsNone(rec["rules_version"])
        self.assertIsNone(rec["drift_config_version"])
        validate_record(rec)
        run = RunInfo("b" * 40, True, "rules-1", "drift-1", 0.7, "eval", 0)
        rec = build_record(routed(), run)
        self.assertEqual((rec["rules_version"], rec["drift_config_version"]),
                         ("rules-1", "drift-1"))
        self.assertIs(rec["code_dirty"], True)
        validate_record(rec)


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                    "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True, timeout=30)


class TestCodeVersion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_code_version_clean_and_dirty(self):
        repo = self.dir / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        git(repo, "add", "a.txt")
        git(repo, "commit", "-q", "-m", "init")
        commit, dirty = code_version(repo)
        self.assertRegex(commit, r"^[0-9a-f]{40}$")
        self.assertIs(dirty, False)
        (repo / "untracked.txt").write_text("x\n", encoding="utf-8")
        self.assertEqual(code_version(repo), (commit, False))
        (repo / "a.txt").write_text("two\n", encoding="utf-8")
        self.assertEqual(code_version(repo), (commit, True))

    def test_code_version_not_a_repo(self):
        with self.assertRaises(TraceError):
            code_version(self.dir)


class TestWriteTrace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_write_trace_ok(self):
        dest = self.dir / "t.jsonl"
        n = write_trace(dest, [record(1), record(2)])
        self.assertEqual(n, 2)
        lines = dest.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["thread_id"] for line in lines], [1, 2])
        self.assertEqual(os.listdir(self.dir), ["t.jsonl"])

    def test_write_trace_refusals(self):
        inside = REPO_ROOT / "trace_should_not_exist.jsonl"
        with self.assertRaisesRegex(TraceError, "inside the repository"):
            write_trace(inside, [record()])
        self.assertFalse(inside.exists())
        with self.assertRaises(TraceError):
            write_trace(REPO_ROOT, [record()])

        dest = self.dir / "t.jsonl"
        bad = record(3)
        del bad["final"]
        with self.assertRaisesRegex(TraceError, "final"):
            write_trace(dest, iter([record(1), record(2), bad, record(4)]))
        self.assertFalse(dest.exists())
        self.assertEqual(os.listdir(self.dir), [])

    def test_write_trace_atomic(self):
        dest = self.dir / "t.jsonl"
        dest.write_bytes(b"old content\n")
        bad = record(2)
        bad["reason_codes"] = ["made_up_code"]
        with self.assertRaises(TraceError):
            write_trace(dest, [record(1), bad])
        self.assertEqual(dest.read_bytes(), b"old content\n")
        self.assertEqual(os.listdir(self.dir), ["t.jsonl"])

    def test_write_trace_missing_dir(self):
        dest = self.dir / "nope" / "t.jsonl"
        with self.assertRaises(TraceError):
            write_trace(dest, [record()])
        self.assertFalse(dest.parent.exists())


if __name__ == "__main__":
    unittest.main()
