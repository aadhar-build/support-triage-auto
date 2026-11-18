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
REPEAT_RULES = REPO_ROOT / "tests" / "fixtures" / "repeat_contact_rules.json"
REPEAT_CASES = REPO_ROOT / "tests" / "fixtures" / "repeat_contact_cases.json"
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

    def test_forced_case_replays(self):
        data = json.loads(self.cases.read_text(encoding="utf-8"))
        data["cases"].append({
            "id": "rp-forced", "title": "Made-up praise naming a fixture topic phrase",
            "turns": [{"inbound": True, "text": "thanks, love it, quuxfrob"}],
            "expected": {"intent": "praise", "action": "escalate", "should_escalate": True},
            "must_pass": True})
        cases = self.dir / "forced-cases.json"
        cases.write_text(json.dumps(data), encoding="utf-8")
        run_dir, trace = self.dir / "forced-run", self.dir / "forced-trace.jsonl"
        code, stdout, stderr = run_main(run_synthetic, [
            "--cases", str(cases), "--rules", str(FIXTURE_RULES),
            "--out", str(run_dir), "--run-at", "2026-11-18T10:00:00Z",
            "--trace", str(trace)])
        self.assertEqual(code, 0, stderr)
        self.assertIn(" topics_loaded=2 ", stdout)
        code, stdout, stderr = self.replay(cases=cases, run=run_dir, trace=trace)
        self.assertEqual(code, 0, stderr)
        page = self.out.read_text(encoding="utf-8")
        forced = section(page, "rp-forced")
        self.assertIn("<code>escalate_always</code>", forced)
        self.assertEqual(field(panel(page, "rp-forced", 1), "action")[1], "action: escalate")
        self.assertNotIn("escalate_always", section(page, "rp-answer"))

    def test_repeat_contact_case_replays(self):
        cases = self.dir / "repeat-cases.json"
        shutil.copyfile(REPEAT_CASES, cases)
        customers = {c["customer"] for c in
                     json.loads(cases.read_text(encoding="utf-8"))["cases"]}
        run_dir, trace = self.dir / "repeat-run", self.dir / "repeat-trace.jsonl"
        code, stdout, stderr = run_main(run_synthetic, [
            "--cases", str(cases), "--rules", str(REPEAT_RULES),
            "--out", str(run_dir), "--run-at", "2026-11-18T10:00:00Z",
            "--trace", str(trace)])
        self.assertEqual(code, 0, stderr)
        code, stdout, stderr = self.replay(cases=cases, run=run_dir, trace=trace)
        self.assertEqual(code, 0, stderr)
        page = self.out.read_text(encoding="utf-8")
        day5 = section(page, "rc-03")
        self.assertIn("<code>repeat_contact</code>", day5)
        self.assertEqual(field(panel(page, "rc-03", 1), "action")[1], "action: escalate")
        self.assertNotIn("repeat_contact", section(page, "rc-02"))
        for customer in customers:
            self.assertNotIn(customer, page)
        self.assertNotIn("synthetic_customer", page)


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

    def test_forced_case_replays(self):
        data = json.loads(self.cases.read_text(encoding="utf-8"))
        data["cases"].append({
            "id": "rp-forced", "title": "Made-up praise naming a fixture topic phrase",
            "turns": [{"inbound": True, "text": "thanks, love it, quuxfrob"}],
            "expected": {"intent": "praise", "action": "escalate", "should_escalate": True},
            "must_pass": True})
        cases = self.dir / "forced-cases.json"
        cases.write_text(json.dumps(data), encoding="utf-8")
        run_dir, trace = self.dir / "forced-run", self.dir / "forced-trace.jsonl"
        code, stdout, stderr = run_main(run_synthetic, [
            "--cases", str(cases), "--rules", str(FIXTURE_RULES),
            "--out", str(run_dir), "--run-at", "2026-11-18T10:00:00Z",
            "--trace", str(trace)])
        self.assertEqual(code, 0, stderr)
        self.assertIn(" topics_loaded=2 ", stdout)
        code, stdout, stderr = self.replay(cases=cases, run=run_dir, trace=trace)
        self.assertEqual(code, 0, stderr)
        page = self.out.read_text(encoding="utf-8")
        forced = section(page, "rp-forced")
        self.assertIn("<code>escalate_always</code>", forced)
        self.assertEqual(field(panel(page, "rp-forced", 1), "action")[1], "action: escalate")
        self.assertNotIn("escalate_always", section(page, "rp-answer"))


def fixture_inputs():
    cases = run_synthetic.load_cases(FIXTURES / "cases.json")
    report = json.loads((FIXTURES / "synthetic_report.json").read_text(encoding="utf-8"))
    return cases, report["cases"], read_records(FIXTURES / "trace.jsonl")


def section(page, case_id):
    """The HTML of one thread's section."""
    start = page.index('data-case="%s"' % case_id)
    end = page.find("</section>", start)
    return page[start:end]


def panel(page, case_id, turn):
    """The state panel of one turn of one thread."""
    sec = section(page, case_id)
    start = sec.index('<div class="state" data-turn="%d">' % turn)
    end = sec.index("</li>", start)
    return sec[start:end]


def field(panel_html, name):
    m = re.search(r'<div class="([a-z ]+)" data-field="%s">(.*?)</div>' % name, panel_html)
    assert m, name
    return m.group(1), m.group(2)


def last_turn(case_id):
    return {"rp-answer": 1, "rp-ask": 2, "rp-route3": 3, "rp-escalate": 1, "rp-held": 1}[case_id]


class TestStateFromTrace(unittest.TestCase):
    EXPECTED = {
        # id: intent, urgency, action, confidence, reason code, tier, model id
        "rp-answer": ("praise", "low", "answer", "0.75", "cheap_confident", "agent",
                      "fake-agent"),
        "rp-ask": ("delivery", "normal", "ask", "0.6", "cheap_confident", "agent",
                   "fake-agent"),
        "rp-route3": ("account_access", "normal", "route", "1.0", "high_risk_to_larger",
                      "larger", "fake-larger"),
        "rp-escalate": ("other", "high", "escalate", "1.0", "high_risk_to_larger", "larger",
                        "fake-larger"),
        "rp-held": ("praise", "low", "answer", "0.75", "cheap_confident", "agent",
                    "fake-agent"),
    }

    def test_state_shows_trace_values(self):
        page = replay.render(*fixture_inputs())
        for case_id, exp in self.EXPECTED.items():
            with self.subTest(case_id):
                p = panel(page, case_id, last_turn(case_id))
                intent, urgency, action, conf, code, tier, model_id = exp
                self.assertEqual(field(p, "intent")[1], "intent: " + intent)
                self.assertEqual(field(p, "urgency")[1], "urgency: " + urgency)
                self.assertEqual(field(p, "action")[1], "action: " + action)
                self.assertEqual(field(p, "confidence")[1], "confidence: " + conf)
                self.assertIn("<code>%s</code>" % code, field(p, "reason_codes")[1])
                self.assertEqual(field(p, "tier")[1], "deciding tier: " + tier)
                self.assertIn(model_id, field(p, "model_id")[1])

    def test_urgency_comes_from_trace(self):
        cases, report_cases, records = fixture_inputs()
        records[1]["final"]["urgency"] = "high"
        page = replay.render(cases, report_cases, records)
        self.assertEqual(field(panel(page, "rp-ask", 2), "urgency")[1], "urgency: high")
        self.assertEqual(field(panel(page, "rp-answer", 1), "urgency")[1], "urgency: low")

    def test_brand_and_customer_labels(self):
        page = replay.render(*fixture_inputs())
        route = section(page, "rp-route3")
        self.assertEqual(re.findall(r'<span class="speaker">(.*?)</span>', route),
                         ["Customer", "ExampleCo", "Customer"])
        ask = section(page, "rp-ask")
        self.assertEqual(re.findall(r'<span class="speaker">(.*?)</span>', ask),
                         ["Customer", "Brand"])

    def test_header_and_thread_list(self):
        page = replay.render(*fixture_inputs())
        head = page[:page.index("</nav>")]
        self.assertIn("0123456789abcdef0123456789abcdef01234567", head)
        self.assertIn("code_dirty false", head)
        self.assertIn("threshold 0.5", head)
        self.assertIn("split synthetic", head)
        self.assertIn("rp-route3 &#8212; Made-up three-turn login trouble</a> (route, pass)",
                      head)


class TestNoLookAhead(unittest.TestCase):
    def test_three_turn_thread(self):
        page = replay.render(*fixture_inputs())
        for turn in (1, 2):
            p = panel(page, "rp-route3", turn)
            self.assertIn("No decision recorded at this turn", p)
            self.assertNotIn("route", p)
            self.assertNotIn("Decided once", p)
        p = panel(page, "rp-route3", 3)
        self.assertEqual(field(p, "action")[1], "action: route")
        self.assertIn("Decided once on the full thread; per-turn decisions arrive with #46", p)
        self.assertNotIn("No decision recorded", p)


class TestPolicyStates(unittest.TestCase):
    def setUp(self):
        self.page = replay.render(*fixture_inputs())

    def test_violation_holds_reply(self):
        p = panel(self.page, "rp-held", 1)
        self.assertEqual(field(p, "policy")[1], "policy check: t-promise")
        self.assertIn("Reply held: failed policy check (t-promise)", p)
        self.assertNotIn("HELDDRAFTMARK", self.page)

    def test_passed_shows_tagged_draft(self):
        p = panel(self.page, "rp-answer", 1)
        self.assertEqual(field(p, "policy")[1], "policy check: passed")
        self.assertIn("Glad you like it, CLEANDRAFTMARK. <em>(draft from scenario file)</em>",
                      field(p, "reply")[1])

    def test_null_is_not_checked(self):
        for case_id in ("rp-ask", "rp-route3", "rp-escalate"):
            with self.subTest(case_id):
                p = panel(self.page, case_id, last_turn(case_id))
                self.assertEqual(field(p, "policy")[1], "policy check: not checked")
                self.assertNotIn("passed", p)
                self.assertEqual(field(p, "reply")[1], "reply: no draft")


def hand_state(**changes):
    state = {"intent": "delivery", "urgency": "normal", "action": "ask", "confidence": 0.6,
             "reason_codes": ("cheap_confident",), "tier": "agent", "model_id": "fake-agent",
             "policy": "not checked", "reply": None}
    state.update(changes)
    return state


class TestStateChanges(unittest.TestCase):
    def test_equal_states(self):
        self.assertEqual(replay.state_changes(hand_state(), hand_state()), ())

    def test_two_fields_differ(self):
        cur = hand_state(action="route", reason_codes=("high_risk_to_larger",))
        self.assertEqual(replay.state_changes(hand_state(), cur), ("action", "reason_codes"))

    def test_first_state_is_all_fields(self):
        self.assertEqual(replay.state_changes(None, hand_state()),
                         ("intent", "urgency", "action", "confidence", "reason_codes", "tier",
                          "model_id", "policy", "reply"))

    def test_page_highlights_exactly_changed_fields(self):
        case = {"id": "hand", "title": "Hand-built", "turns": [
            {"inbound": True, "text": "one"}, {"inbound": False, "text": "two"},
            {"inbound": True, "text": "three"}]}
        steps = [
            {"turn": case["turns"][0], "state": hand_state(), "note": None},
            {"turn": case["turns"][1], "state": None, "note": None},
            {"turn": case["turns"][2], "note": None, "state": hand_state(
                action="route", reason_codes=("high_risk_to_larger",))},
        ]
        html_text = replay.render_thread(case, steps)
        first, third = html_text.split('data-turn="3"', 1)
        self.assertIn("first decision", first)
        self.assertNotIn("changed", first)
        self.assertNotIn("first decision", third)
        self.assertEqual(re.findall(r'class="field changed" data-field="([a-z_]+)"', third),
                         ["action", "reason_codes"])


class TestTracePanel(unittest.TestCase):
    def test_rows_in_trace_order(self):
        page = replay.render(*fixture_inputs())
        trace = section(page, "rp-escalate").split('<div class="trace">', 1)[1]
        rows = re.findall(r"<tr><td>([a-z]+)</td><td><code>([a-z-]+)</code></td>"
                          r"<td>([a-z_]+)</td><td>([a-z]+)</td><td>([a-z]+)</td>"
                          r"<td>([0-9.]+)</td></tr>", trace)
        self.assertEqual(rows, [("cheap", "keywords", "other", "high", "escalate", "0.2"),
                                ("larger", "fake-larger", "other", "high", "escalate", "1.0")])
        self.assertIn("final: intent other, urgency high, action escalate, confidence 1.0",
                      trace)
        self.assertIn("<code>high_risk_to_larger</code>", trace)


class TestOffline(unittest.TestCase):
    def test_no_network(self):
        page = replay.render(*fixture_inputs())
        for banned in ("http://", "https://", "fetch(", "XMLHttpRequest"):
            self.assertNotIn(banned, page)


class TestEscaping(unittest.TestCase):
    def test_script_is_escaped(self):
        payload = "<script>XSSMARK()</script>"
        cases, report_cases, records = fixture_inputs()
        case = cases[0]  # rp-answer: policy passed, so the draft is shown
        case["title"] = "t " + payload
        case["brand"] = "b " + payload
        case["draft_reply"] = "d " + payload
        case["turns"] = [{"inbound": True, "text": "c " + payload},
                         {"inbound": False, "text": "r " + payload}]
        page = replay.render(cases, report_cases, records)
        self.assertNotIn(payload, page)
        self.assertNotIn("<script>XSSMARK", page)
        for prefix in ("t ", "b ", "d ", "c ", "r "):
            self.assertIn(prefix + "&lt;script&gt;XSSMARK()&lt;/script&gt;", page)


def test_tests_do_not_read_owner_files():
    source = Path(__file__).read_text(encoding="utf-8")
    assert ("evals/" + "gold") not in source
    assert ("policies/" + "rules.json") not in source


class TestOwnerFiles(unittest.TestCase):
    def test_tests_do_not_read_owner_files(self):
        test_tests_do_not_read_owner_files()


if __name__ == "__main__":
    unittest.main()
