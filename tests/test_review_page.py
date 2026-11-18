"""End-to-end tests for python3 -m triage.review_page.

Every case is made up by hand and written to a temp dir. A run is produced by the
run_synthetic CLI with the fixture rules, then the review page CLI turns it into HTML.
Expected values come from the spec's worked examples, not from re-running the cascade.
"""

import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_RULES = REPO_ROOT / "tests" / "fixtures" / "policy_rules.json"

REFUND_DRAFT = "Sorry! We will sort it, full refund on the way"
CLEAN_DRAFT = "Sorry, please DM us."
HOSTILE = '</textarea></script><script>alert(1)</script>"><img src=x onerror=alert(1)>'


def case(case_id, text, intent, action, escalate, **extra):
    c = {
        "id": case_id,
        "title": "Title of %s" % case_id,
        "turns": [{"inbound": True, "text": text}],
        "expected": {"intent": intent, "action": action, "should_escalate": escalate},
        "must_pass": False,
    }
    c.update(extra)
    return c


PARCEL = ("my parcel never arrived", "delivery", "ask", False)
HACKED = ("my account was hacked", "other", "escalate", True)
PRAISE = ("thanks, love it", "praise", "answer", False)


def standard_cases():
    """Cases-file order: parcel, hacked, praise, refund. hacked (escalate) and refund
    (t-promise violation) are flagged."""
    return [
        case("parcel", *PARCEL, draft_reply=CLEAN_DRAFT,
             turns=[{"inbound": True, "text": "my parcel never arrived"},
                    {"inbound": False, "text": "Made-up brand reply about tracking"},
                    {"inbound": True, "text": "still nothing today"}]),
        case("hacked", *HACKED),
        case("praise", *PRAISE),
        case("refund", *PARCEL, draft_reply=REFUND_DRAFT, expected_violations=["t-promise"]),
    ]


def case_blocks(page):
    """Map each case id (as escaped in the page) to its markup, in page order."""
    starts = [m.start() for m in re.finditer(r'<section [^>]*data-case-id=', page)]
    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(page)
        chunk = page[start:end]
        case_id = re.match(r'<section [^>]*data-case-id="([^"]*)"', chunk).group(1)
        blocks.append((case_id, chunk))
    return blocks


class ReviewTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.rules = self.dir / "rules.json"
        self.rules.write_text(FIXTURE_RULES.read_text(encoding="utf-8"), encoding="utf-8")
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = str(REPO_ROOT / "src")

    def write_cases(self, cases, name="cases.json"):
        path = self.dir / name
        path.write_text(json.dumps({"version": "test", "cases": cases}), encoding="utf-8")
        return path

    def make_run(self, cases, name="run"):
        cases_path = self.write_cases(cases, name=name + "-cases.json")
        run_dir = self.dir / name
        proc = subprocess.run(
            [sys.executable, "-m", "triage.run_synthetic", "--cases", str(cases_path),
             "--out", str(run_dir), "--rules", str(self.rules)],
            cwd=str(self.dir), env=self.env, capture_output=True, text=True, timeout=60)
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        return cases_path, run_dir

    def run_page(self, cases_path, run_dir, out):
        return subprocess.run(
            [sys.executable, "-m", "triage.review_page", "--cases", str(cases_path),
             "--run", str(run_dir), "--out", str(out)],
            cwd=str(self.dir), env=self.env, capture_output=True, text=True, timeout=60)

    def page_for(self, cases):
        cases_path, run_dir = self.make_run(cases)
        out = self.dir / "pages" / "review.html"
        proc = self.run_page(cases_path, run_dir, out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc, out.read_text(encoding="utf-8"), run_dir


class TestReviewPage(ReviewTestBase):
    def test_page_lists_every_case_with_labels(self):
        proc, page, _ = self.page_for(standard_cases())
        out = self.dir / "pages" / "review.html"
        self.assertEqual(proc.stdout, "cases=4 flagged=2 page=%s\n" % out)
        self.assertEqual(proc.stderr, "")
        blocks = dict(case_blocks(page))
        self.assertEqual(sorted(blocks), ["hacked", "parcel", "praise", "refund"])

        def field(block, name):
            m = re.search(r'<dd class="%s">(.*?)</dd>' % name, block, re.S)
            self.assertIsNotNone(m, name)
            return m.group(1)

        def turns(block):
            return re.findall(r'<li class="turn (customer|brand)">(.*?)</li>', block, re.S)

        parcel = blocks["parcel"]
        self.assertIn("Title of parcel", parcel)
        self.assertEqual([who for who, _ in turns(parcel)], ["customer", "brand", "customer"])
        texts = [t for _, t in turns(parcel)]
        self.assertIn("my parcel never arrived", texts[0])
        self.assertIn("Made-up brand reply about tracking", texts[1])
        self.assertIn("still nothing today", texts[2])
        self.assertEqual(field(parcel, "tiers"), "cheap \u2192 agent")
        self.assertEqual(field(parcel, "intent"), "delivery")
        self.assertEqual(field(parcel, "action"), "ask")
        self.assertEqual(field(parcel, "confidence"), "0.6")
        self.assertEqual(field(parcel, "violations"), "none")
        self.assertRegex(parcel, r"<textarea[^>]*>\n?%s</textarea>" % re.escape(
            html.escape(CLEAN_DRAFT, quote=True)))

        hacked = blocks["hacked"]
        self.assertIn("Title of hacked", hacked)
        self.assertEqual([who for who, _ in turns(hacked)], ["customer"])
        self.assertIn("my account was hacked", turns(hacked)[0][1])
        self.assertEqual(field(hacked, "tiers"), "cheap \u2192 larger")
        self.assertEqual(field(hacked, "intent"), "other")
        self.assertEqual(field(hacked, "action"), "escalate")
        self.assertEqual(field(hacked, "confidence"), "1.0")
        self.assertEqual(field(hacked, "violations"), "n/a")
        self.assertIn("no draft", hacked)
        self.assertNotIn("<textarea", hacked)

        praise = blocks["praise"]
        self.assertEqual(field(praise, "tiers"), "cheap \u2192 agent")
        self.assertEqual(field(praise, "intent"), "praise")
        self.assertEqual(field(praise, "action"), "answer")
        self.assertEqual(field(praise, "confidence"), "0.75")
        self.assertEqual(field(praise, "violations"), "n/a")

        refund = blocks["refund"]
        self.assertEqual(field(refund, "violations"), "t-promise")
        self.assertIn(html.escape(REFUND_DRAFT, quote=True), refund)
    def test_case_text_is_escaped(self):
        hostile = case(HOSTILE, *PARCEL, draft_reply=HOSTILE)
        hostile["title"] = HOSTILE
        hostile["turns"] = [{"inbound": True, "text": HOSTILE}]
        proc, page, _ = self.page_for([hostile, case("praise", *PRAISE)])
        self.assertNotIn(HOSTILE, page)
        self.assertNotIn(HOSTILE, proc.stdout)
        escaped = html.escape(HOSTILE, quote=True)
        # id (attribute and heading), title, turn and draft.
        self.assertGreaterEqual(page.count(escaped), 5)
        self.assertIn('data-case-id="%s"' % escaped, page)
        self.assertEqual(page.count("<script"), 1)
        self.assertEqual(page.count("<img"), 0)
        self.assertEqual(page.count("</textarea>"), 1)
        self.assertEqual(page.count("</script>"), 1)

    def test_page_is_self_contained(self):
        _, page, _ = self.page_for(standard_cases())
        for needle in ("http://", "https://", "<link", "<iframe", "<img", "fetch(",
                       "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "import(",
                       "@import", "url("):
            with self.subTest(needle):
                self.assertNotIn(needle, page)
        self.assertIsNone(re.search(
            r"""\b(src|href|action)\s*=\s*["']?\s*(https?:|//)""", page, re.I))
        self.assertEqual(len(re.findall(r"<script\b", page)), 1)
        self.assertEqual(len(re.findall(r"<style\b", page)), 1)
        self.assertEqual(page.count("<script>"), 1)
        self.assertEqual(page.count("<style>"), 1)

    def test_flagged_first_and_approve_disabled(self):
        _, page, _ = self.page_for(standard_cases())
        blocks = case_blocks(page)
        self.assertEqual([i for i, _ in blocks], ["hacked", "refund", "parcel", "praise"])
        for case_id, block in blocks:
            with self.subTest(case_id):
                flagged = case_id in ("hacked", "refund")
                head = re.match(r"<section [^>]*>", block).group(0)
                self.assertIn('data-flagged="%s"' % ("true" if flagged else "false"), head)
                approve = re.search(r'<button [^>]*class="approve"[^>]*>', block)
                edit = re.search(r'<button [^>]*class="edit"[^>]*>', block)
                if case_id == "refund":
                    # feedback-1: a policy-violation case shows only Edit and Reject.
                    self.assertIsNone(approve, block)
                    self.assertIsNotNone(edit, block)
                    self.assertIn("disabled", edit.group(0))
                else:
                    self.assertIsNone(edit, block)
                    self.assertEqual("disabled" in approve.group(0), flagged, approve.group(0))
                reject = re.search(r'<button [^>]*class="reject"[^>]*>', block).group(0)
                self.assertNotIn("disabled", reject)
                self.assertIn('<input class="reason"', block)
                options = re.findall(r'<option value="([^"]+)"', block)
                self.assertEqual(options, ["answer", "ask", "route", "escalate"])
        system = {"hacked": "escalate", "refund": "ask", "parcel": "ask", "praise": "answer"}
        for case_id, block in blocks:
            selected = re.findall(r'<option value="([^"]+)" selected>', block)
            self.assertEqual(selected, [system[case_id]], case_id)

    def test_page_embeds_run_id(self):
        _, page, run_dir = self.page_for(standard_cases())
        run_id = hashlib.sha256(
            (run_dir / "synthetic_report.json").read_bytes()).hexdigest()
        self.assertIn('data-run-id="%s"' % run_id, page)

    def test_load_run_accepts_reason_codes(self):
        from triage.review_page import load_run
        from triage.router import is_known_reason_code
        _, run_dir = self.make_run(standard_cases())
        cases, run_id = load_run(run_dir)
        self.assertEqual([c["id"] for c in cases], ["parcel", "hacked", "praise", "refund"])
        for c in cases:
            self.assertIsInstance(c["reason_codes"], list)
            self.assertTrue(c["reason_codes"])
            for code in c["reason_codes"]:
                self.assertTrue(is_known_reason_code(code), code)

    def test_load_run_refuses_missing_reason_codes(self):
        from triage.review_page import ReportError, load_run
        _, run_dir = self.make_run(standard_cases())
        report = json.loads((run_dir / "synthetic_report.json").read_text(encoding="utf-8"))
        for name, mutate in (("missing", lambda c: c.pop("reason_codes")),
                             ("not a list", lambda c: c.__setitem__("reason_codes", "x")),
                             ("non-string", lambda c: c.__setitem__("reason_codes", [1]))):
            with self.subTest(name):
                bad = json.loads(json.dumps(report))
                mutate(bad["cases"][0])
                d = self.dir / ("rc-" + name.replace(" ", "-"))
                d.mkdir()
                (d / "synthetic_report.json").write_text(json.dumps(bad), encoding="utf-8")
                with self.assertRaises(ReportError):
                    load_run(d)

    def test_refuses_non_synthetic_input(self):
        cases_path, run_dir = self.make_run(standard_cases())
        good = json.loads((run_dir / "synthetic_report.json").read_text(encoding="utf-8"))

        def run_with(name, mutate):
            d = self.dir / ("bad-" + name)
            d.mkdir()
            report = json.loads(json.dumps(good))
            text = mutate(report)
            (d / "synthetic_report.json").write_text(
                text if isinstance(text, str) else json.dumps(report), encoding="utf-8")
            return d

        def extra_key(r):
            r["note"] = "x"

        def missing_summary_key(r):
            del r["summary"]["policy_mismatches"]

        def case_missing_tiers(r):
            del r["cases"][1]["tiers"]

        empty = self.dir / "empty-run"
        empty.mkdir()
        bad_cases = self.dir / "bad-cases.json"
        bad_cases.write_text("{ not json", encoding="utf-8")
        fewer = self.write_cases(standard_cases()[:3], name="fewer.json")
        variants = {
            "missing report": (cases_path, empty),
            "malformed report JSON": (cases_path, run_with("json", lambda r: "{ not json")),
            "extra top-level key": (cases_path, run_with("extra", extra_key)),
            "missing summary key": (cases_path, run_with("summary", missing_summary_key)),
            "case missing tiers": (cases_path, run_with("tiers", case_missing_tiers)),
            "ids do not match": (fewer, run_dir),
            "invalid cases file": (bad_cases, run_dir),
            "missing cases file": (self.dir / "nope.json", run_dir),
        }
        for name, (cases_file, run) in variants.items():
            with self.subTest(name):
                out = self.dir / ("page-" + name.replace(" ", "-")) / "review.html"
                proc = self.run_page(cases_file, run, out)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                lines = proc.stderr.splitlines()
                self.assertEqual(len(lines), 1, proc.stderr)
                self.assertTrue(lines[0].startswith("error:"), lines[0])
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
