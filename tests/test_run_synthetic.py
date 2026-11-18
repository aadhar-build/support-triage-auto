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
        self.assertEqual(proc.stdout, "cases=3 passed=3 failed=0 must_pass_failures=0\n")
        self.assertTrue((self.out / "synthetic_report.md").is_file())
        raw = (self.out / "synthetic_report.json").read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("}\n"))
        report = self.report_json()
        self.assertEqual(sorted(report), ["cases", "summary"])
        self.assertEqual(report["summary"], {
            "cases_run": 3, "passed": 3, "failed": 0, "must_pass_cases": 3,
            "must_pass_failures": 0, "missed_escalations": 0, "false_escalations": 0,
            "policy_violations": 0, "policy_mismatches": 0,
        })
        self.assertEqual([c["id"] for c in report["cases"]], ["parcel", "praise", "hacked"])
        self.assertEqual(report["cases"][2], {
            "id": "hacked", "title": "Title of hacked", "must_pass": True, "passed": True,
            "expected": {"intent": "other", "action": "escalate", "escalate": True},
            "actual": {"intent": "other", "action": "escalate", "escalate": True},
            "expected_violations": None, "actual_violations": None,
        })
        self.assertEqual(report["cases"][0]["actual"],
                         {"intent": "delivery", "action": "ask", "escalate": False})
        self.assertEqual(report["cases"][1]["actual"],
                         {"intent": "praise", "action": "answer", "escalate": False})
        md = self.report_md()
        self.assertTrue(md.startswith("# Synthetic regression report\n"))
        self.assertIn("| parcel | Title of parcel |", md)


if __name__ == "__main__":
    unittest.main()
