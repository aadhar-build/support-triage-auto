"""Tests for triage.report: the offline performance page built from a metrics/1 file."""

import ast
import copy
import dataclasses
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from triage import metrics as metrics_mod
from triage import report
from triage.metrics import ScoredCase, compute, proportion

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC = FIXTURES / "report_metrics_synthetic.json"
GOLD = FIXTURES / "report_metrics_gold.json"
OTHER = FIXTURES / "report_metrics_other.json"
THRESHOLDS = FIXTURES / "report_thresholds.json"
NONOVERLAP = "ranges don't overlap"
GOLD_NOTE = "Gold runs are aggregate only: conversation lists are not shown."


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = report.main(argv)
    return code, out.getvalue(), err.getvalue()


def data_block(page, block_id):
    m = re.search(r'<script type="application/json" id="%s">(.*?)</script>' % block_id, page,
                  re.S)
    assert m, "no %s block" % block_id
    return m.group(1)


def row(page, attr, name):
    """The HTML of the first <tr> (or <li>) carrying attr="name", up to its close tag."""
    m = re.search(r'<(tr|li)[^>]*\b%s="%s"[^>]*>.*?</\1>' % (attr, re.escape(name)), page, re.S)
    assert m, "no element with %s=%s" % (attr, name)
    return m.group(0)


def mark(page, attr, name):
    m = re.search(r'data-mark="([^"]*)"', row(page, attr, name))
    return m.group(1) if m else None


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, data):
        path = self.tmp / name
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def build(self, metrics_path, *extra, thresholds=THRESHOLDS):
        out = self.tmp / "page.html"
        code, stdout, stderr = run_main([str(metrics_path), "--thresholds", str(thresholds),
                                         "-o", str(out), *extra])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout.strip(), "page=%s" % out)
        return out.read_text(encoding="utf-8")

    def assert_refused(self, argv, out, needle=None):
        code, stdout, stderr = run_main(argv)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(len(stderr.splitlines()), 1, stderr)
        self.assertTrue(stderr.startswith("error: "), stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertFalse(Path(out).exists())
        if needle is not None:
            self.assertIn(needle, stderr)
        return stderr

    def targets(self):
        return load(THRESHOLDS)

    def render(self, metrics, compare=None, targets="fixture"):
        t = self.targets() if targets == "fixture" else targets
        meta = {"version": t.get("version"), "status": t.get("status")} if t else None
        return report.render(metrics, compare=compare, targets=t, targets_meta=meta)


class TestOffline(Base):
    def test_no_network_in_any_page(self):
        pages = [self.build(SYNTHETIC), self.build(GOLD), self.build(GOLD, "--compare", str(OTHER))]
        for page in pages:
            for needle in ("http://", "https://", "<link", "<img", "<iframe", "src=", "fetch(",
                           "XMLHttpRequest"):
                self.assertNotIn(needle, page)
            self.assertTrue(page.startswith("<!doctype html>"))
            self.assertIn('<meta charset="utf-8">', page)
            self.assertIn("prefers-color-scheme: dark", page)

    def test_stdlib_only_and_no_network_modules(self):
        banned = {"urllib", "http", "socket", "socketserver", "requests", "ssl"}
        stdlib = set(sys.stdlib_module_names)
        src = (REPO_ROOT / "src" / "triage" / "report.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                mods = [node.module or ""]
            else:
                continue
            for mod in mods:
                top = mod.split(".")[0]
                self.assertNotIn(top, banned, mod)
                self.assertTrue(top == "triage" or top in stdlib, "non-stdlib %s" % mod)
        for word in ("reason_codes", "topic_id", '"reason"', '"reasons"'):
            self.assertNotIn(word, src)


class TestNumbersMatch(Base):
    def test_data_block_round_trips(self):
        for path in (GOLD, SYNTHETIC):
            page = self.build(path)
            block = data_block(page, "metrics-data")
            self.assertNotIn("<", block)
            self.assertEqual(json.loads(block), load(path))

    def test_cells_hold_fixture_strings(self):
        page = self.build(GOLD)
        tpr = row(page, "data-metric", "escalation.tpr")
        self.assertIn("0.8000", tpr)
        self.assertIn("8 of 10", tpr)
        self.assertIn("0.4902 to 0.9433", tpr)
        f2 = row(page, "data-metric", "escalation.f2")
        self.assertIn("n=100", f2)
        self.assertIn("no range", f2)
        self.assertIn("0.8000", row(page, "data-metric", "escalation.tpr"))
        self.assertIn("86 of 100", row(page, "data-metric", "action.accuracy"))
        self.assertIn("30 of 100", row(page, "data-metric", "operations.tier_shares.larger"))

    def test_bare_cost_cell(self):
        page = self.build(GOLD)
        cost = row(page, "data-metric", "operations.cost_per_1000_usd")
        cell = re.search(r'<td class="value">(.*?)</td>', cost, re.S).group(1)
        self.assertIn("1.2345", cell)
        self.assertNotIn(" of ", cell)
        self.assertNotIn(" to ", cell)
        self.assertNotIn("no range", cell)

    def test_null_shows_na_and_header_fields(self):
        m = load(GOLD)
        m["escalation"]["precision"] = {"value": None, "num": 0, "n": 0, "ci": None,
                                        "directional": True}
        page = self.render(m)
        self.assertIn("n/a", row(page, "data-metric", "escalation.precision"))
        self.assertIn("no range", row(page, "data-metric", "escalation.precision"))
        for text in ("gold_random", "dev", "2026-11-09T10:00:00Z", "larger=0.5", "threads_routed"):
            self.assertIn(text, page)
        syn = self.render(load(SYNTHETIC))
        self.assertIn("none", syn)
        self.assertNotIn("threads_routed", syn)

    def test_escalation_detail_lists_every_metric(self):
        page = self.build(GOLD)
        for key in ("tpr", "tnr", "fpr", "fnr", "precision", "npv", "accuracy", "prevalence",
                    "f1", "f2", "f0_5", "balanced_accuracy", "mcc"):
            row(page, "data-detail", "escalation." + key)
        for key in ("tp", "fp", "tn", "fn", "positives", "predicted_positives"):
            self.assertIn('data-count="%s"' % key, page)


class TestFromProducer(Base):
    CASES = [ScoredCase("p1", "billing", "answer", False, "billing", "answer"),
             ScoredCase("p2", "delivery", "escalate", True, "delivery", "escalate"),
             ScoredCase("p3", "delivery", "ask", False, "billing", "ask"),
             ScoredCase("p4", "complaint", "escalate", True, "complaint", "route")]

    def produce(self, kind):
        if kind == "synthetic":
            return compute(self.CASES, "synthetic", run_at="2026-11-01T00:00:00Z",
                           cohort={"name": "synthetic", "split": None,
                                   "gold_revision": "sha256:" + "d" * 64,
                                   "brand_population": "all"},
                           sample={"size": None, "seed": None},
                           config={"threshold": 0.7, "prices": None})
        return compute([dataclasses.replace(c, case_id=None) for c in self.CASES], "gold",
                       run_at="2026-11-01T00:00:00Z",
                       cohort={"name": "gold_random", "split": "dev",
                               "gold_revision": "sha256:" + "a" * 64, "brand_population": "all"},
                       sample={"size": 4, "seed": 1},
                       config={"threshold": 0.7, "prices": {"cheap": 0.0}},
                       coverage={"threads_routed": 4, "threads_scored": 4,
                                 "threads_without_gold": 0, "gold_rows_unmatched": 0},
                       operations={"tier_shares": {"cheap": proportion(4, 4)},
                                   "cost_per_1000_usd": 0.0, "prices_are_placeholders": True})

    def test_pages_from_compute(self):
        for kind in ("synthetic", "gold"):
            m = self.produce(kind)
            path = self.write("%s.json" % kind, metrics_mod.to_json(m))
            page = self.build(path)
            f2 = m["escalation"]["f2"]["value"]
            self.assertIn("%.4f" % f2, row(page, "data-metric", "escalation.f2"))
            for block in ("action", "intent"):
                for label, entry in m[block]["per_class"].items():
                    cls = row(page, "data-class", "%s:%s" % (block, label))
                    self.assertIn('data-v="%d"' % entry["support"], cls)

    def test_compare_two_gold_runs_differing_in_run_at_and_sample(self):
        a = self.produce("gold")
        b = copy.deepcopy(a)
        b["run_at"] = "2026-11-08T00:00:00Z"
        b["sample"] = {"size": 9, "seed": 2}
        pa = self.write("a.json", metrics_mod.to_json(a))
        pb = self.write("b.json", metrics_mod.to_json(b))
        code, _, err = run_main([str(pa), "--compare", str(pb), "--thresholds", str(THRESHOLDS),
                                 "-o", str(self.tmp / "c.html")])
        self.assertEqual(code, 0, err)


class TestGoldAggregateOnly(Base):
    def test_gold_page_has_no_ids(self):
        page = self.build(GOLD)
        syn = load(SYNTHETIC)
        for grid in (syn["action"]["ids"], syn["intent"]["ids"]):
            for r in grid:
                for cell in r:
                    for case_id in cell:
                        self.assertNotIn(case_id, page)
        self.assertNotIn('"ids"', page)
        self.assertNotIn("data-cell", page)
        self.assertIn(GOLD_NOTE, page)

    def test_gold_with_ids_refused(self):
        m = load(GOLD)
        m["action"]["ids"] = [[[] for _ in range(3)] for _ in range(3)]
        path = self.write("gold_ids.json", m)
        out = self.tmp / "x.html"
        self.assert_refused([str(path), "--thresholds", str(THRESHOLDS), "-o", str(out)], out,
                            "ids")

    FAKE = ["T-FAKE-1", "T-FAKE-2"]

    def refuse_with(self, edit, source=GOLD):
        m = load(source)
        edit(m)
        path = self.write("edited.json", m)
        out = self.tmp / "x.html"
        self.assert_refused([str(path), "--thresholds", str(THRESHOLDS), "-o", str(out)], out)

    def test_extra_key_at_any_depth_refused(self):
        places = [
            lambda m: m["escalation"],
            lambda m: m["escalation"]["counts"],
            lambda m: m["escalation"]["fnr"],
            lambda m: m["escalation"]["mcc"],
            lambda m: m["action"],
            lambda m: m["action"]["confusion_matrix"],
            lambda m: m["action"]["per_class"]["answer"],
            lambda m: m["action"]["per_class"]["answer"]["tpr"],
            lambda m: m["intent"]["macro"],
            lambda m: m["intent"]["weighted"]["f1"],
            lambda m: m["operations"],
            lambda m: m["operations"]["tier_shares"]["cheap"],
            lambda m: m["coverage"],
            lambda m: m["sample"],
            lambda m: m["config"],
            lambda m: m["config"]["prices"],
        ]
        for i, place in enumerate(places):
            with self.subTest(place=i):
                self.refuse_with(lambda m: place(m).update(missed_cases=self.FAKE))

    def test_extra_key_in_synthetic_refused(self):
        self.refuse_with(lambda m: m["escalation"].update(missed_cases=self.FAKE), SYNTHETIC)
        self.refuse_with(lambda m: m["intent"]["per_class"][
            m["intent"]["confusion_matrix"]["labels"][0]].update(cases=self.FAKE), SYNTHETIC)

    def test_free_strings_in_unchecked_shapes_refused(self):
        edits = [
            lambda m: m["config"].update(threshold="T-FAKE-1"),
            lambda m: m["coverage"].update(threads_routed="T-FAKE-1"),
            lambda m: m["sample"].update(seed="T-FAKE-1"),
            lambda m: m["operations"]["tier_shares"].update({"T-FAKE-1": proportion(1, 2)}),
            lambda m: m["config"]["prices"].update({"T-FAKE-1": 1.0}),
            lambda m: m["action"]["macro"]["included_classes"].append("T-FAKE-1"),
            lambda m: m["escalation"]["counts"].update(tp=["T-FAKE-1"]),
            lambda m: m["action"]["accuracy"].update(value="x"),
            lambda m: m["action"]["per_class"]["answer"].update(support="x"),
            lambda m: m["escalation"]["fnr"].update(ci=[0.1, "x"]),
        ]
        for i, edit in enumerate(edits):
            with self.subTest(edit=i):
                self.refuse_with(edit)

    def test_gold_data_holds_only_allowlisted_strings(self):
        allowed = {
            # key names a metrics/1 gold file may carry
            "schema_version", "kind", "run_at", "cohort", "sample", "config", "coverage",
            "operations", "n", "directional_below", "averaging", "escalation", "action",
            "intent", "name", "split", "gold_revision", "brand_population", "size", "seed",
            "threshold", "prices", "threads_routed", "threads_scored", "threads_without_gold",
            "gold_rows_unmatched", "tier_shares", "cost_per_1000_usd", "prices_are_placeholders",
            "counts", "tp", "fp", "tn", "fn", "positives", "predicted_positives", "tpr", "tnr",
            "fpr", "fnr", "precision", "npv", "accuracy", "prevalence", "f1", "f2", "f0_5",
            "balanced_accuracy", "mcc", "value", "num", "ci", "directional", "confusion_matrix",
            "labels", "per_class", "macro", "weighted", "kappa", "support", "predicted",
            "zero_filled", "included_classes",
            # tier names, labels and the fixture's own allowlisted string values
            "cheap", "larger", "agent", "answer", "ask", "route", "escalate",
            "metrics/1", "gold", "gold_random", "dev", "all", "2026-11-09T10:00:00Z",
            "sha256:" + "b" * 64, metrics_mod.AVERAGING,
        } | set(metrics_mod.INTENTS)
        page = self.build(GOLD)
        found = set()

        def walk(v):
            if isinstance(v, dict):
                for k, x in v.items():
                    found.add(k)
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
            elif isinstance(v, str):
                found.add(v)
        walk(json.loads(data_block(page, "metrics-data")))
        self.assertEqual(found - allowed, set())


class TestSyntheticCells(Base):
    def test_cells_carry_indices_matching_ids(self):
        page = self.build(SYNTHETIC)
        data = json.loads(data_block(page, "metrics-data"))
        for block in ("action", "intent"):
            counts = data[block]["confusion_matrix"]["counts"]
            ids = data[block]["ids"]
            table = re.search(r'<table[^>]*data-matrix="%s".*?</table>' % block, page, re.S)
            self.assertTrue(table)
            cells = set(re.findall(r'data-cell="(\d+),(\d+)"', table.group(0)))
            expected = {(str(r), str(c)) for r in range(len(counts))
                        for c in range(len(counts)) if counts[r][c]}
            self.assertEqual(cells, expected)
            for r, c in expected:
                self.assertEqual(len(ids[int(r)][int(c)]), counts[int(r)][int(c)])
        self.assertNotIn(GOLD_NOTE, page)

    def test_sort_hooks(self):
        page = self.build(SYNTHETIC)
        self.assertIn("data-sort", page)
        self.assertIn("syn-001", data_block(page, "metrics-data"))


class TestCompare(Base):
    SUMMARY = ("escalation.f2", "escalation.tpr", "escalation.tnr", "escalation.precision",
               "escalation.f1", "escalation.fnr", "escalation.fpr", "action.accuracy",
               "intent.accuracy", "operations.tier_shares.cheap",
               "operations.tier_shares.larger", "operations.cost_per_1000_usd")

    def test_same_file(self):
        page = self.build(GOLD, "--compare", str(GOLD))
        for key in self.SUMMARY:
            self.assertIn("+0.0000", row(page, "data-compare", key))
        self.assertNotIn(NONOVERLAP, page)
        self.assertEqual(json.loads(data_block(page, "compare-data")), load(GOLD))

    def test_gold_vs_other_marks_one_row(self):
        page = self.build(GOLD, "--compare", str(OTHER))
        marked = [k for k in self.SUMMARY if NONOVERLAP in row(page, "data-compare", k)]
        self.assertEqual(marked, ["intent.accuracy"])
        self.assertIn("+0.5000", row(page, "data-compare", "intent.accuracy"))
        self.assertIn("-0.0500", row(page, "data-compare", "operations.tier_shares.larger"))

    def test_tier_only_in_one_run(self):
        other = load(OTHER)
        del other["operations"]["tier_shares"]["larger"]
        page = self.render(load(GOLD), compare=other)
        self.assertIn("not in other run", row(page, "data-compare",
                                              "operations.tier_shares.larger"))

    def test_never_significant(self):
        for page in (self.build(GOLD, "--compare", str(OTHER)), self.build(SYNTHETIC),
                     self.build(GOLD)):
            self.assertNotIn("significant", page.lower())


class TestCompareCompatibility(Base):
    def refuse(self, other_path, field):
        out = self.tmp / "x.html"
        err = self.assert_refused([str(GOLD), "--compare", str(other_path), "--thresholds",
                                   str(THRESHOLDS), "-o", str(out)], out)
        self.assertIn("cannot compare", err)
        self.assertIn(field, err)
        return err

    def test_kind(self):
        self.refuse(SYNTHETIC, "kind")

    def test_cohort_fields(self):
        for field, value in (("name", "hard_case"), ("split", "eval"),
                             ("gold_revision", "sha256:" + "e" * 64),
                             ("brand_population", "acme")):
            other = load(GOLD)
            other["cohort"][field] = value
            err = self.refuse(self.write("o_%s.json" % field, other), "cohort." + field)
            for f in report.KEY_FIELDS:
                if f != "cohort." + field:
                    self.assertNotIn(f + " differs", err)
        other = load(GOLD)
        other["cohort"]["split"] = "eval"
        err = self.refuse(self.write("o.json", other), "cohort.split")
        self.assertIn("cohort.split differs (dev vs eval)", err)

    def test_unlike_run_at_sample_config_compare_fine(self):
        page = self.build(GOLD, "--compare", str(OTHER))
        for text in ("2026-11-09T10:00:00Z", "2026-11-02T10:00:00Z", "size=100", "size=120",
                     "seed=7", "seed=3", "threshold=0.7", "threshold=0.65"):
            self.assertIn(text, page)


class TestDirectional(Base):
    def test_badge_follows_flag(self):
        page = self.build(GOLD)
        self.assertIn("directional", row(page, "data-metric", "escalation.tpr"))
        self.assertNotIn("directional", row(page, "data-metric", "escalation.tnr"))
        m = load(GOLD)
        m["escalation"]["tnr"]["directional"] = True
        m["escalation"]["tpr"]["directional"] = False
        page = self.render(m)
        self.assertIn("directional", row(page, "data-metric", "escalation.tnr"))
        self.assertNotIn("directional", row(page, "data-metric", "escalation.tpr"))


class TestTargets(Base):
    def test_pass_and_fail(self):
        page = self.render(load(GOLD))
        self.assertEqual(mark(page, "data-metric", "action.accuracy"), "pass")
        self.assertEqual(mark(page, "data-metric", "intent.accuracy"), "fail")
        self.assertEqual(mark(page, "data-metric", "escalation.fpr"), "pass")
        self.assertEqual(mark(page, "data-metric", "escalation.fnr"), "pass")
        self.assertEqual(mark(page, "data-metric", "operations.tier_shares.larger"), "fail")
        self.assertEqual(mark(page, "data-metric", "operations.cost_per_1000_usd"), "pass")
        self.assertEqual(mark(page, "data-metric", "escalation.f2"), "no-target")
        self.assertEqual(mark(page, "data-class", "action:answer"), "pass")
        self.assertEqual(mark(page, "data-class", "action:route"), "fail")
        self.assertEqual(mark(page, "data-class", "action:escalate"), "pass")

    def test_fnr_judged_on_upper_bound_strictly(self):
        m = load(GOLD)
        m["escalation"]["fnr"]["ci"][1] = 0.6
        self.assertEqual(mark(self.render(m), "data-metric", "escalation.fnr"), "fail")
        m["escalation"]["fnr"]["ci"] = None
        page = self.render(m)
        self.assertEqual(mark(page, "data-metric", "escalation.fnr"), "n/a")

    def test_placeholder_cost_not_compared(self):
        m = load(GOLD)
        m["operations"]["prices_are_placeholders"] = True
        page = self.render(m)
        cost = row(page, "data-metric", "operations.cost_per_1000_usd")
        self.assertIn("placeholder prices: not compared with target", cost)
        self.assertEqual(mark(page, "data-metric", "operations.cost_per_1000_usd"), "note")

    def test_hard_case_only(self):
        page = self.render(load(GOLD))
        hc = row(page, "data-target", "guardrails.missed_escalations_hard_case")
        self.assertIn("applies to the hard-case set only", hc)
        m = load(GOLD)
        m["cohort"]["name"] = "hard_case"
        self.assertEqual(mark(self.render(m), "data-target",
                              "guardrails.missed_escalations_hard_case"), "fail")
        m["escalation"]["counts"]["fn"] = 1
        self.assertEqual(mark(self.render(m), "data-target",
                              "guardrails.missed_escalations_hard_case"), "pass")

    def test_no_larger_tier(self):
        m = load(GOLD)
        del m["operations"]["tier_shares"]["larger"]
        page = self.render(m)
        self.assertNotIn('data-metric="operations.tier_shares.larger"', page)
        t = row(page, "data-target", "supporting.larger_tier_share")
        self.assertIn("not in this run", t)
        self.assertEqual(mark(page, "data-target", "supporting.larger_tier_share"), "note")

    def test_north_star(self):
        ns = row(self.render(load(GOLD)), "data-metric", "north_star")
        self.assertIn("not computed in this run", ns)
        self.assertIn("0.5000", ns)

    def test_missing_bad_and_unmeasured(self):
        t = self.targets()
        del t["supporting"]["action_accuracy"]
        t["supporting"]["intent_accuracy"] = {"min": 0.5, "max": 0.9}
        page = self.render(load(GOLD), targets=t)
        self.assertEqual(mark(page, "data-metric", "action.accuracy"), "no-target")
        self.assertIn("target not set", row(page, "data-metric", "action.accuracy"))
        self.assertEqual(mark(page, "data-metric", "intent.accuracy"), "bad-target")
        self.assertIn("target unreadable", row(page, "data-metric", "intent.accuracy"))
        t["guardrails"]["false_escalation_rate"] = {"max": True}
        page = self.render(load(GOLD), targets=t)
        self.assertEqual(mark(page, "data-metric", "escalation.fpr"), "bad-target")
        section = re.search(r"Targets not measured in this report.*?</ul>", page, re.S).group(0)
        for key in ("p95_latency_s", "judge_agreement", "reply_tie_or_win", "pii_in_public_draft",
                    "out_of_policy_promises", "eval_leakage", "api_spend_usd_per_month"):
            self.assertIn(key, section)
        for key in ("reporting", "confidence_threshold", "action_accuracy"):
            self.assertNotIn(key, section)

    def test_no_targets(self):
        page = report.render(load(GOLD), targets=None)
        for key in TestCompare.SUMMARY:
            self.assertEqual(mark(page, "data-metric", key), "no-target")
            self.assertIn("target not set", row(page, "data-metric", key))

    def test_version_and_status_shown(self):
        page = self.build(GOLD)
        self.assertIn("fixture-v9", page)
        self.assertIn("Made-up fixture thresholds for tests only", page)

    def test_default_thresholds_missing(self):
        out = self.tmp / "p.html"
        with mock.patch.object(report, "DEFAULT_THRESHOLDS", self.tmp / "nope.json"):
            code, _, err = run_main([str(GOLD), "-o", str(out)])
        self.assertEqual(code, 0, err)
        page = out.read_text(encoding="utf-8")
        self.assertIn("no thresholds file found", page)
        self.assertEqual(mark(page, "data-metric", "action.accuracy"), "no-target")


class TestBadInput(Base):
    def refuse(self, metrics_path, *extra, thresholds=THRESHOLDS, needle=None):
        out = self.tmp / "out.html"
        argv = [str(metrics_path), "-o", str(out), *extra]
        if thresholds is not None:
            argv += ["--thresholds", str(thresholds)]
        return self.assert_refused(argv, out, needle)

    def edited(self, edit):
        m = load(GOLD)
        edit(m)
        return self.write("edited.json", m)

    def test_missing_file(self):
        self.refuse(self.tmp / "missing.json")

    def test_invalid_json(self):
        self.refuse(self.write("bad.json", "{not json"))

    def test_array(self):
        self.refuse(self.write("arr.json", "[]"))

    def test_schema_version(self):
        self.refuse(self.edited(lambda m: m.update(schema_version="metrics/9")))

    def test_kind(self):
        self.refuse(self.edited(lambda m: m.update(kind="weird")))

    def test_missing_key(self):
        self.refuse(self.edited(lambda m: m.pop("averaging")), needle="averaging")

    def test_extra_brand(self):
        self.refuse(self.edited(lambda m: m.update(brand="acme")), needle="brand")

    def test_label_outside_actions(self):
        def edit(m):
            m["action"]["confusion_matrix"]["labels"][0] = "refund"
            m["action"]["per_class"]["refund"] = m["action"]["per_class"].pop("answer")
        self.refuse(self.edited(edit), needle="refund")

    def test_missing_compare(self):
        self.refuse(GOLD, "--compare", str(self.tmp / "missing.json"))

    def test_unwritable_out(self):
        out = self.tmp / "no_dir" / "page.html"
        self.assert_refused([str(GOLD), "--thresholds", str(THRESHOLDS), "-o", str(out)], out)

    def test_out_same_as_input_refused(self):
        src = self.write("in.json", GOLD.read_text(encoding="utf-8"))
        before = src.read_text(encoding="utf-8")
        code, stdout, stderr = run_main([str(src), "--thresholds", str(THRESHOLDS), "-o",
                                         str(src)])
        self.assertEqual((code, stdout), (2, ""))
        self.assertTrue(stderr.startswith("error: "), stderr)
        self.assertEqual(src.read_text(encoding="utf-8"), before)

    def test_explicit_thresholds_missing(self):
        self.refuse(GOLD, thresholds=self.tmp / "missing_thresholds.json")

    def test_thresholds_invalid_json(self):
        self.refuse(GOLD, thresholds=self.write("t.json", "{nope"))
        self.refuse(GOLD, thresholds=self.write("t2.json", "[1]"))


class TestEscaping(Base):
    NASTY = "x</script><!--<b>"

    def test_labels_via_render(self):
        m = load(GOLD)
        for block in ("action", "intent"):
            labels = m[block]["confusion_matrix"]["labels"]
            old = labels[0]
            labels[0] = self.NASTY
            m[block]["per_class"][self.NASTY] = m[block]["per_class"].pop(old)
        page = self.render(m)
        block = data_block(page, "metrics-data")
        self.assertNotIn("<", block)
        self.assertEqual(json.loads(block), m)
        self.assertNotIn("<b>", page)
        self.assertIn("x&lt;/script&gt;&lt;!--&lt;b&gt;", page)

    def test_ids_via_cli(self):
        m = load(SYNTHETIC)
        counts = m["action"]["confusion_matrix"]["counts"]
        r, c = next((r, c) for r in range(len(counts)) for c in range(len(counts))
                    if counts[r][c])
        m["action"]["ids"][r][c][0] = self.NASTY
        page = self.build(self.write("nasty.json", m))
        block = data_block(page, "metrics-data")
        self.assertNotIn("<", block)
        self.assertEqual(json.loads(block), m)
        self.assertNotIn("<b>", page)


class TestDoesNotReadOwnerFiles(unittest.TestCase):
    def test_source_has_no_owner_paths(self):
        source = Path(__file__).read_text(encoding="utf-8")
        for needle in ("evals/" + "thresholds.json", "evals/" + "gold", "docs/" + "prd.md",
                       "policies" + "/", "data/" + "raw"):
            self.assertNotIn(needle, source)

    def test_every_main_call_passes_thresholds(self):
        source = Path(__file__).read_text(encoding="utf-8")
        calls = [line for line in source.splitlines() if "run_main" + "([" in line]
        self.assertTrue(calls)
        without = [line for line in calls if "--thresholds" not in line]
        self.assertEqual(len(without), 1, without)
        self.assertIn('"-o", str(out)])', without[0])


if __name__ == "__main__":
    unittest.main()
