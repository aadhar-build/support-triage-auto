"""End-to-end tests for python3 -m triage.apply_review.

A run is produced from made-up cases by the run_synthetic CLI with the fixture rules.
Decisions files are hand-written JSON. The run id is computed here from the report bytes.
"""

import hashlib
import json
import subprocess
import sys
import unittest

from tests.test_review_page import REFUND_DRAFT, PARCEL, ReviewTestBase, case, standard_cases

NEW_DRAFT = "Sorry, please DM us your order number."


class ApplyTestBase(ReviewTestBase):
    def setUp(self):
        super().setUp()
        self.cases_path, self.run_dir = self.make_run(standard_cases())
        self.report = self.run_dir / "synthetic_report.json"
        self.applied = self.run_dir / "review_applied.json"
        self.run_id = hashlib.sha256(self.report.read_bytes()).hexdigest()
        self._n = 0

    def decisions_file(self, decisions, run_id=None, raw=None):
        self._n += 1
        path = self.dir / ("decisions-%d.json" % self._n)
        if raw is None:
            raw = json.dumps({"run_id": self.run_id if run_id is None else run_id,
                              "decisions": decisions})
        path.write_text(raw, encoding="utf-8")
        return path

    def apply(self, decisions_path, run_dir=None, cases_path=None, rules=None):
        return subprocess.run(
            [sys.executable, "-m", "triage.apply_review", "--run",
             str(run_dir or self.run_dir), "--decisions", str(decisions_path),
             "--cases", str(cases_path or self.cases_path),
             "--rules", str(rules or self.rules)],
            cwd=str(self.dir), env=self.env, capture_output=True, text=True, timeout=60)

    def assert_refused(self, proc, target, expected):
        self.assertEqual(proc.returncode, 2, proc.stderr)
        lines = proc.stderr.splitlines()
        self.assertEqual(len(lines), 1, proc.stderr)
        self.assertTrue(lines[0].startswith("error:"), lines[0])
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(target.read_bytes(), expected)

    def applied_json(self):
        return json.loads(self.applied.read_text(encoding="utf-8"))


WORKED = {
    "praise": {"decision": "approve"},
    "refund": {"decision": "edit", "draft": NEW_DRAFT},
    "parcel": {"decision": "reject", "reason": "Needs a person", "action": "escalate"},
}


class TestApplyReview(ApplyTestBase):
    def test_apply_counts_and_changed_actions(self):
        proc = self.apply(self.decisions_file(WORKED))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "approved=1 edited=1 rejected=1 undecided=1\n"
                                      "action changed: parcel ask -> escalate\n")
        self.assertEqual(proc.stderr, "")
        self.assertNotIn(NEW_DRAFT, proc.stdout)
        self.assertNotIn("Needs a person", proc.stdout)
        self.assertEqual(self.applied_json(), {
            "run_id": self.run_id,
            "cases": {
                "praise": {"decision": "approve", "original_action": "answer",
                           "action": "answer"},
                "refund": {"decision": "edit", "original_action": "ask", "action": "ask",
                           "draft": NEW_DRAFT},
                "parcel": {"decision": "reject", "original_action": "ask",
                           "action": "escalate", "reason": "Needs a person"},
            },
            "summary": {"approved": 1, "edited": 1, "rejected": 1, "undecided": 1},
            "actions_changed": [{"id": "parcel", "from": "ask", "to": "escalate"}],
        })
        raw = self.applied.read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("}\n"))
        self.assertEqual(raw, json.dumps(self.applied_json(), sort_keys=True, indent=2) + "\n")

    def test_apply_is_idempotent(self):
        path = self.decisions_file(WORKED)
        first = self.apply(path)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.applied.read_bytes()
        second = self.apply(path)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.applied.read_bytes(), before)
        self.assertEqual(second.stdout, first.stdout)
        self.assertEqual(second.stdout.splitlines()[0],
                         "approved=1 edited=1 rejected=1 undecided=1")

    def test_apply_resumes_by_case_id(self):
        a = self.decisions_file({
            "parcel": {"decision": "approve"},
            "refund": {"decision": "edit", "draft": NEW_DRAFT, "action": "route"},
        })
        proc = self.apply(a)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "approved=1 edited=1 rejected=0 undecided=2\n"
                                      "action changed: refund ask -> route\n")
        b = self.decisions_file({
            "praise": {"decision": "approve", "action": "ask"},
            "parcel": {"decision": "reject", "reason": "Wrong tone"},
        })
        proc = self.apply(b)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "approved=1 edited=1 rejected=1 undecided=1\n"
                                      "action changed: praise answer -> ask\n"
                                      "action changed: refund ask -> route\n")
        self.assertEqual(self.applied_json()["cases"], {
            "praise": {"decision": "approve", "original_action": "answer", "action": "ask"},
            "parcel": {"decision": "reject", "original_action": "ask", "action": "ask",
                       "reason": "Wrong tone"},
            "refund": {"decision": "edit", "original_action": "ask", "action": "route",
                       "draft": NEW_DRAFT},
        })
        self.assertEqual(self.applied_json()["actions_changed"],
                         [{"id": "praise", "from": "answer", "to": "ask"},
                          {"id": "refund", "from": "ask", "to": "route"}])

    def test_refusals(self):
        bad_run = self.dir / "bad-run"
        bad_run.mkdir()
        report = json.loads(self.report.read_text(encoding="utf-8"))
        report["extra"] = 1
        (bad_run / "synthetic_report.json").write_text(json.dumps(report), encoding="utf-8")
        (bad_run / "review_applied.json").write_text("left alone\n", encoding="utf-8")

        variants = {
            "wrong run_id": (self.decisions_file(WORKED, run_id="0" * 64), None),
            "unknown case id": (self.decisions_file({"nope": {"decision": "approve"}}), None),
            "approve flagged without reason": (
                self.decisions_file({"hacked": {"decision": "approve"}}), None),
            "approve flagged with blank reason": (
                self.decisions_file({"refund": {"decision": "approve", "reason": "  "}}), None),
            "edit without draft": (self.decisions_file({"parcel": {"decision": "edit"}}), None),
            "edit with empty draft": (
                self.decisions_file({"parcel": {"decision": "edit", "draft": ""}}), None),
            "reject without reason": (
                self.decisions_file({"parcel": {"decision": "reject"}}), None),
            "unknown decision": (self.decisions_file({"parcel": {"decision": "maybe"}}), None),
            "unknown action": (self.decisions_file(
                {"parcel": {"decision": "approve", "action": "refund"}}), None),
            "non-string reason": (self.decisions_file(
                {"parcel": {"decision": "reject", "reason": 5}}), None),
            "non-synthetic report": (self.decisions_file(WORKED), bad_run),
            "malformed decisions JSON": (self.decisions_file(None, raw="{ not json"), None),
        }
        # An earlier good apply must survive every refusal byte for byte.
        seed = self.apply(self.decisions_file({"praise": {"decision": "approve"}}))
        self.assertEqual(seed.returncode, 0, seed.stderr)
        before = self.applied.read_bytes()
        for name, (path, run_dir) in variants.items():
            with self.subTest(name):
                target = (run_dir or self.run_dir) / "review_applied.json"
                expected = target.read_bytes()
                proc = self.apply(path, run_dir=run_dir)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                lines = proc.stderr.splitlines()
                self.assertEqual(len(lines), 1, proc.stderr)
                self.assertTrue(lines[0].startswith("error:"), lines[0])
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(target.read_bytes(), expected)
        self.assertEqual(self.applied.read_bytes(), before)

    def test_refuses_review_file_from_another_run(self):
        self.applied.write_text(json.dumps({"run_id": "f" * 64, "cases": {}}),
                                encoding="utf-8")
        before = self.applied.read_bytes()
        proc = self.apply(self.decisions_file(WORKED))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertTrue(proc.stderr.startswith("error:"))
        self.assertEqual(self.applied.read_bytes(), before)

    def test_refuses_malformed_existing_review_file(self):
        good = {"decision": "approve", "original_action": "ask", "action": "ask"}
        bad_entries = {
            "non-object entry": 5,
            "missing decision": {"original_action": "ask", "action": "ask"},
            "unknown decision": dict(good, decision="maybe"),
            "unknown action": dict(good, action="shout"),
            "missing original_action": {"decision": "approve", "action": "ask"},
            "original_action not the run's": dict(good, original_action="escalate"),
            "edit without draft": dict(good, decision="edit"),
            "non-string reason": dict(good, reason=7),
            "unknown key": dict(good, note="x"),
        }
        for name, entry in bad_entries.items():
            with self.subTest(name):
                self.applied.write_text(
                    json.dumps({"run_id": self.run_id, "cases": {"parcel": entry}}),
                    encoding="utf-8")
                before = self.applied.read_bytes()
                proc = self.apply(self.decisions_file(WORKED))
                self.assertEqual(proc.returncode, 2, proc.stderr)
                lines = proc.stderr.splitlines()
                self.assertEqual(len(lines), 1, proc.stderr)
                self.assertTrue(lines[0].startswith("error:"), lines[0])
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(self.applied.read_bytes(), before)

    def test_flagged_approve_needs_reason(self):
        # feedback-1: an escalate-only flagged case (hacked) can be approved with a
        # reason; a violation case (refund) cannot be approved at all (see
        # TestViolationCases).
        proc = self.apply(self.decisions_file({
            "hacked": {"decision": "approve", "reason": "Escalation is right here"},
            "praise": {"decision": "approve"},
        }))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "approved=2 edited=0 rejected=0 undecided=2\n")
        cases = self.applied_json()["cases"]
        self.assertEqual(cases["hacked"]["reason"], "Escalation is right here")
        self.assertNotIn("reason", cases["praise"])


    def test_refuses_bad_cases_or_rules(self):
        seed = self.apply(self.decisions_file({"praise": {"decision": "approve"}}))
        self.assertEqual(seed.returncode, 0, seed.stderr)
        before = self.applied.read_bytes()
        other_cases = self.write_cases([case("other", *PARCEL)], name="other-cases.json")
        bad_cases = self.write_cases([{"id": "x"}], name="bad-cases.json")
        empty_rules = self.dir / "empty-rules.json"
        empty_rules.write_text(json.dumps({"public_reply_rules": []}), encoding="utf-8")
        variants = {
            "missing cases file": {"cases_path": self.dir / "nope.json"},
            "invalid cases file": {"cases_path": bad_cases},
            "cases ids do not match the run": {"cases_path": other_cases},
            "missing rules file": {"rules": self.dir / "no-rules.json"},
            "empty rules list": {"rules": empty_rules},
        }
        for name, kwargs in variants.items():
            with self.subTest(name):
                proc = self.apply(self.decisions_file(WORKED), **kwargs)
                self.assert_refused(proc, self.applied, before)


EMAIL_TEXT = "my parcel never arrived, write to sam@example.test"


class TestViolationCases(ApplyTestBase):
    """feedback-1: a case with any policy violation can't be approved, even with a reason.
    It needs an edit whose draft passes policy_check with zero violations, or a reject."""

    def test_violation_case_cannot_be_approved_or_kept_violating(self):
        seed = self.apply(self.decisions_file({"praise": {"decision": "approve"}}))
        self.assertEqual(seed.returncode, 0, seed.stderr)
        before = self.applied.read_bytes()
        variants = {
            "approve with reason": {"refund": {"decision": "approve",
                                               "reason": "Refund was pre-agreed"}},
            "approve without reason": {"refund": {"decision": "approve"}},
            "edit keeps a banned phrase": {"refund": {
                "decision": "edit", "draft": "Sorry, a full refund is coming"}},
            "edit on an unflagged case adds a banned phrase": {"parcel": {
                "decision": "edit", "draft": "We'll refund you today"}},
        }
        for name, decisions in variants.items():
            with self.subTest(name):
                proc = self.apply(self.decisions_file(decisions))
                self.assert_refused(proc, self.applied, before)
                for d in decisions.values():
                    if "draft" in d:
                        self.assertNotIn(d["draft"], proc.stderr)
                        self.assertIn("t-promise", proc.stderr)

    def test_edit_echoing_customer_email_is_refused(self):
        cases_path, run_dir = self.make_run([
            case("mail", *PARCEL, turns=[{"inbound": True, "text": EMAIL_TEXT}],
                 draft_reply=REFUND_DRAFT, expected_violations=["t-promise"]),
        ], name="mailrun")
        run_id = hashlib.sha256((run_dir / "synthetic_report.json").read_bytes()).hexdigest()
        draft = "Sorry, we have emailed sam@example.test"
        path = self.decisions_file({"mail": {"decision": "edit", "draft": draft}},
                                   run_id=run_id)
        proc = self.apply(path, run_dir=run_dir, cases_path=cases_path)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertTrue(proc.stderr.startswith("error:"), proc.stderr)
        self.assertIn("t-pii", proc.stderr)
        self.assertNotIn("sam@example.test", proc.stderr)
        self.assertFalse((run_dir / "review_applied.json").exists())

    def test_clean_edit_or_reject_is_accepted(self):
        proc = self.apply(self.decisions_file({
            "refund": {"decision": "edit", "draft": NEW_DRAFT,
                       "reason": "Removed the refund promise"}}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "approved=0 edited=1 rejected=0 undecided=3\n")
        proc = self.apply(self.decisions_file({
            "refund": {"decision": "reject", "reason": "Promise not allowed"}}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "approved=0 edited=0 rejected=1 undecided=3\n")

    def test_refuses_existing_entry_that_breaks_the_rule(self):
        legacy = {
            "approved violation case": {"decision": "approve", "original_action": "ask",
                                        "action": "ask", "reason": "Pre-agreed"},
            "edit that still violates": {"decision": "edit", "original_action": "ask",
                                         "action": "ask", "draft": REFUND_DRAFT},
        }
        for name, entry in legacy.items():
            with self.subTest(name):
                self.applied.write_text(
                    json.dumps({"run_id": self.run_id, "cases": {"refund": entry}}),
                    encoding="utf-8")
                before = self.applied.read_bytes()
                proc = self.apply(self.decisions_file({"praise": {"decision": "approve"}}))
                self.assert_refused(proc, self.applied, before)
                self.assertNotIn(REFUND_DRAFT, proc.stderr)


if __name__ == "__main__":
    unittest.main()
