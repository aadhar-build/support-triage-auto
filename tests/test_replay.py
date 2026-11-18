"""Tests for python3 -m triage.replay, the admin conversation-and-state replay page.

Every input is a made-up fixture under tests/fixtures/replay/ or a copy edited in a temp
dir. Expected values are written by hand per case id from the fixture files.
"""

import io
import json
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from triage import replay, run_synthetic

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "replay"
FIXTURE_RULES = REPO_ROOT / "tests" / "fixtures" / "policy_rules.json"
CASE_IDS = ["rp-answer", "rp-ask", "rp-route3", "rp-escalate", "rp-held"]


def run_main(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = module.main(argv)
    return code, out.getvalue(), err.getvalue()


def read_records(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def write_records(path, records):
    Path(path).write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


class ReplayTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.cases = self.dir / "cases.json"
        shutil.copyfile(FIXTURES / "cases.json", self.cases)
        self.run_dir = self.dir / "run"
        self.run_dir.mkdir()
        for name in ("synthetic_report.json", "metrics.json"):
            shutil.copyfile(FIXTURES / name, self.run_dir / name)
        self.trace = self.dir / "trace.jsonl"
        shutil.copyfile(FIXTURES / "trace.jsonl", self.trace)
        self.out = self.dir / "page.html"

    def replay(self, cases=None, run=None, trace=None, out=None):
        return run_main(replay, ["--cases", str(cases or self.cases),
                                 "--run", str(run or self.run_dir),
                                 "--trace", str(trace or self.trace),
                                 "--out", str(out or self.out)])

    def assert_refused(self, result, message=None):
        code, stdout, stderr = result
        self.assertEqual(code, 2, stderr)
        self.assertEqual(len(stderr.splitlines()), 1, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(stdout, "")
        if message is not None:
            self.assertIn(message, stderr)
        self.assertFalse(self.out.exists())

    def edit_report(self, change):
        path = self.run_dir / "synthetic_report.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def edit_metrics(self, change):
        path = self.run_dir / "metrics.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def edit_trace(self, change):
        records = read_records(self.trace)
        records = change(records) or records
        write_records(self.trace, records)


class TestSuccess(ReplayTestBase):
    def test_writes_page_and_prints_path_and_count(self):
        code, stdout, stderr = self.replay()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertIn(str(self.out), stdout)
        self.assertIn("threads=5", stdout)
        page = self.out.read_text(encoding="utf-8")
        for case_id in CASE_IDS:
            self.assertIn(case_id, page)


class TestRefuse(ReplayTestBase):
    def test_eval_split_trace(self):
        self.assert_refused(self.replay(trace=FIXTURES / "trace_eval.jsonl"),
                            "not a synthetic trace")

    def test_eval_split_in_one_record(self):
        def change(records):
            records[2]["split"] = "eval"
        self.edit_trace(change)
        self.assert_refused(self.replay(), "not a synthetic trace")

    def test_invalid_cases_file(self):
        self.cases.write_text(json.dumps({"cases": [{"id": "x"}]}), encoding="utf-8")
        self.assert_refused(self.replay())

    def test_report_with_unknown_top_level_key(self):
        self.edit_report(lambda d: d.update(extra=1))
        self.assert_refused(self.replay())

    def test_unregistered_reason_code(self):
        def change(records):
            records[0]["reason_codes"] = ["made_up_code"]
        self.edit_trace(change)
        self.assert_refused(self.replay(), "unregistered")

    def test_trace_not_json(self):
        self.trace.write_text("not json\n", encoding="utf-8")
        self.assert_refused(self.replay())

    def test_missing_trace(self):
        self.assert_refused(self.replay(trace=self.dir / "missing.jsonl"))

    def test_unwritable_out(self):
        out = self.dir / "no_such_dir" / "page.html"
        code, stdout, stderr = self.replay(out=out)
        self.assertEqual(code, 2, stderr)
        self.assertEqual(len(stderr.splitlines()), 1, stderr)
        self.assertFalse(out.exists())


class TestFingerprint(ReplayTestBase):
    def test_no_metrics(self):
        (self.run_dir / "metrics.json").unlink()
        self.assert_refused(self.replay(), "run has no cases fingerprint; re-run run_synthetic")

    def test_metrics_not_json(self):
        (self.run_dir / "metrics.json").write_text("{", encoding="utf-8")
        self.assert_refused(self.replay(), "run has no cases fingerprint")

    def test_gold_kind(self):
        self.edit_metrics(lambda d: d.update(kind="gold"))
        self.assert_refused(self.replay(), "not a synthetic run")

    def test_cohort_name_not_synthetic(self):
        self.edit_metrics(lambda d: d["cohort"].update(name="gold-v1"))
        self.assert_refused(self.replay(), "not a synthetic run")

    def test_malformed_gold_revision(self):
        self.edit_metrics(lambda d: d["cohort"].update(gold_revision="sha256:abc"))
        self.assert_refused(self.replay(), "not a synthetic run")

    def test_different_cases_bytes_same_ids(self):
        data = json.loads(self.cases.read_text(encoding="utf-8"))
        data["cases"][1]["turns"][0]["text"] = "my parcel never arrived at all"
        self.cases.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.assertEqual([c["id"] for c in run_synthetic.load_cases(self.cases)], CASE_IDS)
        self.assert_refused(self.replay(), "run was made from a different cases file")


class TestBinding(ReplayTestBase):
    def refused_after_cases_edit(self, change):
        data = json.loads(self.cases.read_text(encoding="utf-8"))
        change(data["cases"])
        self.cases.write_text(json.dumps(data), encoding="utf-8")
        # re-stamp the fingerprint so only the id check can fail
        import hashlib
        digest = hashlib.sha256(self.cases.read_bytes()).hexdigest()
        self.edit_metrics(lambda d: d["cohort"].update(gold_revision="sha256:" + digest))
        self.assert_refused(self.replay())

    def test_case_missing(self):
        self.refused_after_cases_edit(lambda cases: cases.pop())

    def test_case_extra(self):
        def change(cases):
            extra = dict(cases[0])
            extra["id"] = "rp-extra"
            cases.append(extra)
        self.refused_after_cases_edit(change)

    def test_case_reordered(self):
        self.refused_after_cases_edit(lambda cases: cases.reverse())

    def test_thread_missing(self):
        self.edit_trace(lambda records: records[:-1])
        self.assert_refused(self.replay())

    def test_thread_extra(self):
        def change(records):
            extra = dict(records[-1])
            extra["thread_id"] = 6
            return records + [extra]
        self.edit_trace(change)
        self.assert_refused(self.replay())

    def test_thread_reordered(self):
        self.edit_trace(lambda records: [records[1], records[0]] + records[2:])
        self.assert_refused(self.replay())

    def test_final_action_differs(self):
        def change(records):
            records[1]["final"]["action"] = "route"
        self.edit_trace(change)
        self.assert_refused(self.replay(), "trace does not match run report")

    def test_final_intent_differs(self):
        def change(records):
            records[1]["final"]["intent"] = "billing"
        self.edit_trace(change)
        self.assert_refused(self.replay(), "trace does not match run report")

    def test_tier_list_differs(self):
        def change(records):
            records[2]["tiers"] = records[2]["tiers"][:1]
        self.edit_trace(change)
        self.assert_refused(self.replay(), "trace does not match run report")


class TestEndToEnd(ReplayTestBase):
    def test_producer_then_consumer(self):
        run_dir, trace = self.dir / "e2e-run", self.dir / "e2e-trace.jsonl"
        code, stdout, stderr = run_main(run_synthetic, [
            "--cases", str(self.cases), "--rules", str(FIXTURE_RULES),
            "--out", str(run_dir), "--run-at", "2026-11-18T10:00:00Z",
            "--trace", str(trace)])
        self.assertEqual(code, 0, stderr)
        code, stdout, stderr = self.replay(run=run_dir, trace=trace)
        self.assertEqual(code, 0, stderr)
        page = self.out.read_text(encoding="utf-8")
        for case_id in CASE_IDS:
            self.assertIn(case_id, page)


def test_tests_do_not_read_owner_files():
    source = Path(__file__).read_text(encoding="utf-8")
    assert ("evals/" + "gold") not in source
    assert ("policies/" + "rules.json") not in source


class TestOwnerFiles(unittest.TestCase):
    def test_tests_do_not_read_owner_files(self):
        test_tests_do_not_read_owner_files()


if __name__ == "__main__":
    unittest.main()
