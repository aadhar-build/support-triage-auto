"""End-to-end tests for python3 -m triage.run_synthetic.

Every case here is made up by hand and written to a temp dir at run time. Every run passes
explicit --cases and --rules paths; the owner's own cases and rules files are never read.
Expected labels come from the spec's worked examples, not from re-running the cascade.
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
FIXTURE_CASES = REPO_ROOT / "tests" / "fixtures" / "synthetic_cases.json"

REFUND_DRAFT = "Sorry! We will sort it, full refund on the way"
CLEAN_DRAFT = "Sorry, please DM us."


def case(case_id, text, intent, action, escalate, must_pass=True, **extra):
    c = {
        "id": case_id,
        "title": "Title of %s" % case_id,
        "turns": [{"inbound": True, "text": text}],
        "expected": {"intent": intent, "action": action, "should_escalate": escalate},
        "must_pass": must_pass,
    }
    c.update(extra)
    return c


PARCEL = ("my parcel never arrived", "delivery", "ask", False)
HACKED = ("my account was hacked", "other", "escalate", True)
PRAISE = ("thanks, love it", "praise", "answer", False)
LOCKED = ("locked out, password reset fails", "account_access", "route", False)
URGENT = ("this is terrible and urgent", "complaint", "escalate", True)


class RunSyntheticTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.out = self.dir / "nested" / "out"
        self.rules = self.dir / "rules.json"
        self.rules.write_text(FIXTURE_RULES.read_text(encoding="utf-8"), encoding="utf-8")

    def write_cases(self, cases, name="cases.json"):
        path = self.dir / name
        path.write_text(json.dumps({"version": "test", "cases": cases}), encoding="utf-8")
        return path

    def run_cli(self, cases_path, rules=None, out=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        args = [sys.executable, "-m", "triage.run_synthetic",
                "--cases", str(cases_path),
                "--out", str(out or self.out),
                "--rules", str(rules or self.rules)]
        return subprocess.run(args, cwd=str(self.dir), env=env, capture_output=True,
                              text=True, timeout=60)

    def report_json(self):
        return json.loads((self.out / "synthetic_report.json").read_text(encoding="utf-8"))

    def report_md(self):
        return (self.out / "synthetic_report.md").read_text(encoding="utf-8")


class TestRunSynthetic(RunSyntheticTestBase):
    def test_all_pass_writes_both_reports(self):
        path = self.write_cases([case("parcel", *PARCEL), case("praise", *PRAISE),
                                 case("hacked", *HACKED)])
        proc = self.run_cli(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "cases=3 passed=3 failed=0 must_pass_failures=0 rules_loaded=3\n")
        self.assertTrue((self.out / "synthetic_report.md").is_file())
        raw = (self.out / "synthetic_report.json").read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("}\n"))
        report = self.report_json()
        self.assertEqual(sorted(report), ["cases", "summary"])
        self.assertEqual(report["summary"], {
            "cases_run": 3, "passed": 3, "failed": 0, "must_pass_cases": 3,
            "must_pass_failures": 0, "missed_escalations": 0, "false_escalations": 0,
            "policy_violations": 0, "policy_mismatches": 0, "rules_loaded": 3,
        })
        self.assertEqual([c["id"] for c in report["cases"]], ["parcel", "praise", "hacked"])
        self.assertEqual(report["cases"][2], {
            "id": "hacked", "title": "Title of hacked", "must_pass": True, "passed": True,
            "expected": {"intent": "other", "action": "escalate", "escalate": True},
            "actual": {"intent": "other", "action": "escalate", "escalate": True},
            "expected_violations": None, "actual_violations": None,
            "tiers": ["cheap", "larger"], "confidence": 1.0,
            "reason_codes": ["high_risk_to_larger"],
        })
        self.assertEqual(report["cases"][0]["actual"],
                         {"intent": "delivery", "action": "ask", "escalate": False})
        self.assertEqual(report["cases"][1]["actual"],
                         {"intent": "praise", "action": "answer", "escalate": False})
        md = self.report_md()
        self.assertTrue(md.startswith("# Synthetic regression report\n"))
        self.assertIn("| parcel | Title of parcel |", md)
        self.assertIn("| rules_loaded | 3 |", md)

    def test_rules_loaded_counts_the_rules_file(self):
        rules = json.loads(FIXTURE_RULES.read_text(encoding="utf-8"))
        rules["public_reply_rules"] = rules["public_reply_rules"][:1]
        one_rule = self.dir / "one_rule.json"
        one_rule.write_text(json.dumps(rules), encoding="utf-8")
        proc = self.run_cli(self.write_cases([case("parcel", *PARCEL)]), rules=one_rule)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout,
                         "cases=1 passed=1 failed=0 must_pass_failures=0 rules_loaded=1\n")
        self.assertEqual(self.report_json()["summary"]["rules_loaded"], 1)
        self.assertIn("| rules_loaded | 1 |", self.report_md())

    def test_report_records_tiers_and_confidence(self):
        path = self.write_cases([case("parcel", *PARCEL), case("praise", *PRAISE),
                                 case("hacked", *HACKED), case("locked", *LOCKED)])
        proc = self.run_cli(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "cases=4 passed=4 failed=0 must_pass_failures=0 rules_loaded=3\n")
        got = {c["id"]: (c["tiers"], c["confidence"]) for c in self.report_json()["cases"]}
        self.assertEqual(got, {
            "parcel": (["cheap", "agent"], 0.6),
            "praise": (["cheap", "agent"], 0.75),
            "hacked": (["cheap", "larger"], 1.0),
            "locked": (["cheap", "larger"], 1.0),
        })
        md = self.report_md()
        self.assertNotIn("tiers", md)
        self.assertNotIn("agent", md)

    def test_report_records_reason_codes(self):
        from triage.router import is_known_reason_code
        path = self.write_cases([case("parcel", *PARCEL), case("praise", *PRAISE),
                                 case("hacked", *HACKED), case("locked", *LOCKED)])
        proc = self.run_cli(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = {c["id"]: c["reason_codes"] for c in self.report_json()["cases"]}
        self.assertEqual(got, {
            "parcel": ["cheap_confident"],
            "praise": ["cheap_confident"],
            "hacked": ["high_risk_to_larger"],
            "locked": ["high_risk_to_larger"],
        })
        for codes in got.values():
            for code in codes:
                self.assertTrue(is_known_reason_code(code), code)
        self.assertNotIn("reason_codes", self.report_md())

    def test_label_mismatch_fails_case(self):
        path = self.write_cases([case("parcel", PARCEL[0], "billing", "ask", False,
                                      must_pass=False)])
        proc = self.run_cli(path)
        report = self.report_json()
        item = report["cases"][0]
        self.assertFalse(item["passed"])
        self.assertEqual(item["expected"]["intent"], "billing")
        self.assertEqual(item["actual"]["intent"], "delivery")
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(report["summary"]["passed"], 0)
        row = [l for l in self.report_md().splitlines() if l.startswith("| parcel |")][0]
        self.assertIn("billing", row)
        self.assertIn("delivery", row)
        self.assertTrue(row.endswith("| FAIL |"))
        self.assertEqual(proc.stdout, "cases=1 passed=0 failed=1 must_pass_failures=0 rules_loaded=3\n")

    def _failing_case(self, must_pass):
        return self.write_cases([
            case("good", *PRAISE),
            case("bad", HACKED[0], "other", "answer", False, must_pass=must_pass),
        ])

    def test_must_pass_failure_exits_1(self):
        proc = self.run_cli(self._failing_case(must_pass=True))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stdout, "cases=2 passed=1 failed=1 must_pass_failures=1 rules_loaded=3\n")
        self.assertEqual(self.report_json()["summary"]["must_pass_failures"], 1)
        self.assertTrue((self.out / "synthetic_report.md").is_file())

    def test_non_must_pass_failure_exits_0(self):
        proc = self.run_cli(self._failing_case(must_pass=False))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = self.report_json()["summary"]
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["must_pass_failures"], 0)
        self.assertEqual(summary["must_pass_cases"], 1)
        self.assertTrue((self.out / "synthetic_report.md").is_file())

    def test_policy_violation_match_and_mismatch(self):
        path = self.write_cases([
            case("refund-ok", *PARCEL, draft_reply=REFUND_DRAFT,
                 expected_violations=["t-promise"]),
            case("refund-bad", *PARCEL, must_pass=False, draft_reply=REFUND_DRAFT,
                 expected_violations=[]),
            case("clean", *PARCEL, draft_reply=CLEAN_DRAFT),
        ])
        proc = self.run_cli(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.report_json()
        by_id = {c["id"]: c for c in report["cases"]}
        self.assertTrue(by_id["refund-ok"]["passed"])
        self.assertEqual(by_id["refund-ok"]["actual_violations"], ["t-promise"])
        self.assertFalse(by_id["refund-bad"]["passed"])
        self.assertEqual(by_id["refund-bad"]["expected_violations"], [])
        self.assertEqual(by_id["refund-bad"]["actual_violations"], ["t-promise"])
        self.assertTrue(by_id["clean"]["passed"])
        self.assertEqual(by_id["clean"]["expected_violations"], [])
        self.assertEqual(by_id["clean"]["actual_violations"], [])
        self.assertEqual(report["summary"]["policy_violations"], 2)
        self.assertEqual(report["summary"]["policy_mismatches"], 1)
        self.assertEqual(report["summary"]["failed"], 1)
        md = self.report_md()
        self.assertIn("t-promise", md)

    def test_missed_and_false_escalation_counts(self):
        path = self.write_cases([
            case("missed", LOCKED[0], "account_access", "escalate", True, must_pass=False),
            case("false", URGENT[0], "complaint", "answer", False, must_pass=False),
            case("fine", *PARCEL),
        ])
        proc = self.run_cli(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = self.report_json()["summary"]
        self.assertEqual(summary["missed_escalations"], 1)
        self.assertEqual(summary["false_escalations"], 1)
        self.assertEqual(summary["failed"], 2)

    def test_reports_hold_no_conversation_text(self):
        path = self.dir / "cases.json"
        path.write_text(FIXTURE_CASES.read_text(encoding="utf-8"), encoding="utf-8")
        proc = self.run_cli(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        texts = {"json": (self.out / "synthetic_report.json").read_text(encoding="utf-8"),
                 "md": self.report_md(), "stdout": proc.stdout, "stderr": proc.stderr}
        for name, text in texts.items():
            self.assertNotIn("SENTINEL", text, name)
        for name in ("json", "md"):
            self.assertIn("fx-parcel", texts[name])
            self.assertIn("Made-up parcel that never came", texts[name])
        self.assertTrue(self.report_json()["cases"][0]["passed"])

    def test_empty_cases_list(self):
        proc = self.run_cli(self.write_cases([]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "cases=0 passed=0 failed=0 must_pass_failures=0 rules_loaded=3\n")
        report = self.report_json()
        self.assertEqual(report["cases"], [])
        self.assertEqual(report["summary"], {
            "cases_run": 0, "passed": 0, "failed": 0, "must_pass_cases": 0,
            "must_pass_failures": 0, "missed_escalations": 0, "false_escalations": 0,
            "policy_violations": 0, "policy_mismatches": 0, "rules_loaded": 3,
        })
        self.assertTrue((self.out / "synthetic_report.md").is_file())

    def test_bad_input_exits_2(self):
        good = case("parcel", *PARCEL)

        def without(key):
            c = json.loads(json.dumps(good))
            del c[key]
            return c

        no_escalate = json.loads(json.dumps(good))
        del no_escalate["expected"]["should_escalate"]
        bad_intent = json.loads(json.dumps(good))
        bad_intent["expected"]["intent"] = "not_an_intent"
        empty_turns = dict(good, turns=[])
        orphan_violations = dict(good, expected_violations=["t-promise"])

        def cases_file(name, text):
            p = self.dir / name
            p.write_text(text, encoding="utf-8")
            return p

        bad_rules = cases_file("bad_rules.json", "{ not json")
        good_cases = json.dumps({"cases": [good]})
        variants = {
            "missing cases file": (self.dir / "nope.json", None),
            "invalid JSON": (cases_file("c1.json", "{ not json"), None),
            "cases not a list": (cases_file("c2.json", json.dumps({"cases": {}})), None),
            "case missing id": (cases_file("c3.json", json.dumps({"cases": [without("id")]})),
                                None),
            "missing should_escalate": (cases_file("c4.json",
                                                   json.dumps({"cases": [no_escalate]})), None),
            "unknown intent": (cases_file("c5.json", json.dumps({"cases": [bad_intent]})),
                               None),
            "duplicate id": (cases_file("c6.json", json.dumps({"cases": [good, good]})), None),
            "turns empty": (cases_file("c7.json", json.dumps({"cases": [empty_turns]})), None),
            "violations without draft": (cases_file(
                "c8.json", json.dumps({"cases": [orphan_violations]})), None),
            "missing rules file": (cases_file("c9.json", good_cases), self.dir / "norules.json"),
            "malformed rules file": (cases_file("c10.json", good_cases), bad_rules),
        }
        for name, (cases_path, rules) in variants.items():
            with self.subTest(name):
                out = self.dir / ("out-" + name.replace(" ", "-"))
                proc = self.run_cli(cases_path, rules=rules, out=out)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                lines = proc.stderr.splitlines()
                self.assertEqual(len(lines), 1, proc.stderr)
                self.assertTrue(lines[0].strip())
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertFalse((out / "synthetic_report.json").exists())
                self.assertFalse((out / "synthetic_report.md").exists())

    def test_tests_do_not_read_owner_files(self):
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertNotIn("evals/" + "gold", source)
        self.assertNotIn("policies/" + "rules.json", source)
        self.assertIn('"--cases", str(cases_path)', source)
        self.assertIn('"--rules", str(rules or self.rules)', source)


if __name__ == "__main__":
    unittest.main()
