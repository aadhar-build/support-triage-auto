"""Unit tests for triage.metrics.

Every case is built by hand in this file. Expected values are hand-computed literals from the
spec's acceptance criteria, never recomputed the way the module does.
"""

import ast
import json
import sys
import unittest
from pathlib import Path

from triage import metrics
from triage.evaluate import GoldRow, wilson_interval
from triage.metrics import ScoredCase, comparison_key, compute, to_json
from triage.router import RoutedThread
from triage.tiers import ACTIONS, INTENTS, Labels

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_AT = "2026-11-18T10:00:00Z"
REV = "sha256:" + "a" * 64
GOLD_COHORT = {"name": "gold_random", "split": "dev", "gold_revision": REV,
               "brand_population": "all"}
SYN_COHORT = {"name": "synthetic", "split": None, "gold_revision": REV,
              "brand_population": "all"}
SAMPLE = {"size": 200, "seed": 0}
CONFIG = {"threshold": 0.5, "prices": None}


def run(cases, kind="gold", **kw):
    cohort = kw.pop("cohort", GOLD_COHORT if kind == "gold" else SYN_COHORT)
    return compute(cases, kind, run_at=kw.pop("run_at", RUN_AT), cohort=cohort,
                   sample=kw.pop("sample", SAMPLE), config=kw.pop("config", CONFIG), **kw)


def sc(gold_action, pred_action, gold_escalate=None, gold_intent="other", pred_intent="other",
       case_id=None):
    if gold_escalate is None:
        gold_escalate = gold_action == "escalate"
    return ScoredCase(case_id=case_id, gold_intent=gold_intent, gold_action=gold_action,
                      gold_escalate=gold_escalate, pred_intent=pred_intent,
                      pred_action=pred_action)


def escalation_fixture():
    # TP=8, FP=2, TN=85, FN=5
    return ([sc("escalate", "escalate")] * 8 + [sc("answer", "escalate")] * 2
            + [sc("answer", "answer")] * 85 + [sc("escalate", "answer")] * 5)


def action_fixture():
    pairs = ([("answer", "answer")] * 3 + [("answer", "ask")] + [("ask", "ask")] * 2
             + [("ask", "route")] + [("route", "route")] * 2 + [("escalate", "escalate")]
             + [("escalate", "answer")])
    return [sc(g, p) for g, p in pairs]


def never_predicted_fixture():
    return [sc("answer", "answer")] * 2 + [sc("ask", "answer")] * 2


def unsupported_fixture():
    return [sc("answer", "answer"), sc("answer", "route")]


def all_one_class_fixture():
    return [sc("ask", "ask")] * 7


def walk(obj):
    """Yield every (key, value) pair in nested dicts and lists."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


class TestEscalation(unittest.TestCase):
    def test_escalation_fixture(self):
        e = run(escalation_fixture())["escalation"]
        self.assertEqual(e["counts"], {"tp": 8, "fp": 2, "tn": 85, "fn": 5, "positives": 13,
                                       "predicted_positives": 10})
        expected = {"tpr": 0.61538, "tnr": 0.97701, "precision": 0.8, "npv": 0.94444,
                    "accuracy": 0.93, "prevalence": 0.13, "fpr": 0.02299, "fnr": 0.38462,
                    "balanced_accuracy": 0.79620, "f1": 0.69565, "f2": 0.64516,
                    "f0_5": 0.75472, "mcc": 0.66408}
        for key, value in expected.items():
            self.assertAlmostEqual(e[key]["value"], value, delta=1e-4, msg=key)
        self.assertEqual((e["tpr"]["num"], e["tpr"]["n"], e["tpr"]["directional"]),
                         (8, 13, True))
        self.assertEqual((e["tnr"]["n"], e["tnr"]["directional"]), (87, False))
        self.assertTrue(e["f2"]["directional"])
        self.assertEqual(e["f2"]["n"], 100)

    def test_intervals_are_wilson(self):
        m = run(escalation_fixture())
        e = m["escalation"]
        props = ("tpr", "tnr", "fpr", "fnr", "precision", "npv", "accuracy", "prevalence")
        for key in props:
            self.assertEqual(e[key]["ci"], wilson_interval(e[key]["num"], e[key]["n"]), key)
        for key in ("f1", "f2", "f0_5", "balanced_accuracy", "mcc"):
            self.assertIsNone(e[key]["ci"], key)
        self.assertEqual(e["tpr"]["ci"], wilson_interval(8, 13))
        for block in ("action", "intent"):
            b = m[block]
            self.assertEqual(b["accuracy"]["ci"],
                             wilson_interval(b["accuracy"]["num"], b["accuracy"]["n"]))
            self.assertIsNone(b["kappa"]["ci"])
            for entry in b["per_class"].values():
                for key in ("tpr", "tnr", "precision"):
                    self.assertEqual(entry[key]["ci"],
                                     wilson_interval(entry[key]["num"], entry[key]["n"]))
                for key in ("f1", "f2", "f0_5"):
                    self.assertIsNone(entry[key]["ci"])
            for avg in ("macro", "weighted"):
                for key in ("tpr", "tnr", "precision", "f1", "f2", "f0_5"):
                    self.assertIsNone(b[avg][key]["ci"])

    def test_zero_denominators(self):
        e = run([sc("answer", "answer")] * 12)["escalation"]
        for key in ("tpr", "fnr", "precision", "f1", "f2", "f0_5", "mcc",
                    "balanced_accuracy"):
            self.assertIsNone(e[key]["value"], key)
        for key in ("tpr", "fnr", "precision"):
            self.assertIsNone(e[key]["ci"], key)
        for key in ("tnr", "npv", "accuracy"):
            self.assertEqual(e[key]["value"], 1.0, key)

    def test_fbeta_formula(self):
        e = run(escalation_fixture())["escalation"]
        p, r = 0.8, 8 / 13
        for beta, key in ((0.5, "f0_5"), (1, "f1"), (2, "f2")):
            b2 = beta * beta
            self.assertAlmostEqual(e[key]["value"], (1 + b2) * p * r / (b2 * p + r),
                                   places=9, msg=key)

    def test_empty_input(self):
        m = run([])
        self.assertEqual(m["n"], 0)
        self.assertEqual(m["escalation"]["counts"], {"tp": 0, "fp": 0, "tn": 0, "fn": 0,
                                                     "positives": 0,
                                                     "predicted_positives": 0})
        for key, value in walk(m["escalation"]):
            if key == "value":
                self.assertIsNone(value)
        for block in ("action", "intent"):
            self.assertIsNone(m[block]["accuracy"]["value"])
            self.assertIsNone(m[block]["kappa"]["value"])
            self.assertEqual(m[block]["macro"]["included_classes"], [])
            self.assertIsNone(m[block]["macro"]["f1"]["value"])


class TestClasses(unittest.TestCase):
    def test_all_one_class(self):
        a = run(all_one_class_fixture())["action"]
        self.assertEqual(a["per_class"]["ask"]["tpr"]["value"], 1.0)
        for label in ("answer", "route", "escalate"):
            self.assertIsNone(a["per_class"][label]["tpr"]["value"], label)
            self.assertEqual(a["per_class"][label]["support"], 0, label)
        self.assertEqual(a["macro"]["included_classes"], ["ask"])
        self.assertIsNone(a["kappa"]["value"])
        self.assertEqual(a["accuracy"]["value"], 1.0)

    def test_action_fixture(self):
        a = run(action_fixture())["action"]
        f1 = {"answer": 0.75, "ask": 0.66667, "route": 0.8, "escalate": 0.66667}
        for label, value in f1.items():
            self.assertAlmostEqual(a["per_class"][label]["f1"]["value"], value, delta=1e-4)
        self.assertAlmostEqual(a["accuracy"]["value"], 0.72727, delta=1e-4)
        self.assertAlmostEqual(a["kappa"]["value"], 0.625, delta=1e-4)
        self.assertEqual(a["confusion_matrix"]["labels"], list(ACTIONS))
        self.assertEqual(a["confusion_matrix"]["counts"],
                         [[3, 1, 0, 0], [0, 2, 1, 0], [0, 0, 2, 0], [1, 0, 0, 1]])

    def test_counts_sum_to_n(self):
        fixtures = [escalation_fixture(), action_fixture(), never_predicted_fixture(),
                    unsupported_fixture(), all_one_class_fixture(), [],
                    [sc("answer", "ask", gold_intent="billing", pred_intent="delivery"),
                     sc("route", "route", gold_intent="praise", pred_intent="praise")]]
        for cases in fixtures:
            m = run(cases)
            n = m["n"]
            self.assertEqual(n, len(cases))
            for block, labels in (("action", ACTIONS), ("intent", INTENTS)):
                b = m[block]
                counts = b["confusion_matrix"]["counts"]
                self.assertEqual(b["confusion_matrix"]["labels"], list(labels))
                self.assertEqual(sum(map(sum, counts)), n)
                for i, label in enumerate(labels):
                    c = b["per_class"][label]
                    self.assertEqual(c["tp"] + c["fp"] + c["tn"] + c["fn"], n)
                    self.assertEqual(c["support"], sum(counts[i]))
                    self.assertEqual(c["predicted"], sum(row[i] for row in counts))

    def test_macro_and_weighted(self):
        a = run(action_fixture())["action"]
        self.assertAlmostEqual(a["macro"]["f1"]["value"], 0.72083, delta=1e-4)
        self.assertAlmostEqual(a["weighted"]["f1"]["value"], 0.72121, delta=1e-4)
        self.assertAlmostEqual(a["macro"]["precision"]["value"], 0.77083, delta=1e-4)
        self.assertAlmostEqual(a["weighted"]["precision"]["value"], 0.75758, delta=1e-4)
        self.assertEqual(a["macro"]["precision"]["zero_filled"], 0)
        self.assertEqual(a["macro"]["f1"]["zero_filled"], 0)
        self.assertEqual(a["macro"]["included_classes"],
                         ["answer", "ask", "route", "escalate"])

    def test_never_predicted_class(self):
        a = run(never_predicted_fixture())["action"]
        self.assertIsNone(a["per_class"]["ask"]["precision"]["value"])
        self.assertAlmostEqual(a["macro"]["precision"]["value"], 0.25, delta=1e-4)
        self.assertEqual(a["macro"]["precision"]["zero_filled"], 1)
        self.assertAlmostEqual(a["weighted"]["precision"]["value"], 0.25, delta=1e-4)
        self.assertAlmostEqual(a["macro"]["tpr"]["value"], 0.5, delta=1e-4)
        self.assertAlmostEqual(a["macro"]["f1"]["value"], 0.33333, delta=1e-4)
        self.assertEqual(a["macro"]["included_classes"], ["answer", "ask"])
        self.assertEqual(a["weighted"]["included_classes"], ["answer", "ask"])

    def test_unsupported_class(self):
        a = run(unsupported_fixture())["action"]
        self.assertIsNone(a["per_class"]["route"]["tpr"]["value"])
        self.assertAlmostEqual(a["macro"]["precision"]["value"], 0.5, delta=1e-4)
        self.assertAlmostEqual(a["weighted"]["precision"]["value"], 1.0, delta=1e-4)
        self.assertAlmostEqual(a["macro"]["tpr"]["value"], 0.25, delta=1e-4)
        self.assertEqual(a["macro"]["tpr"]["zero_filled"], 1)
        self.assertAlmostEqual(a["macro"]["f1"]["value"], 0.33333, delta=1e-4)
        self.assertAlmostEqual(a["weighted"]["f1"]["value"], 0.66667, delta=1e-4)
        self.assertEqual(a["macro"]["included_classes"], ["answer", "route"])

    def test_directional_flag(self):
        e29 = run([sc("answer", "answer")] * 29)["escalation"]
        e30 = run([sc("answer", "answer")] * 30)["escalation"]
        self.assertTrue(e29["accuracy"]["directional"])
        self.assertFalse(e30["accuracy"]["directional"])
        self.assertTrue(e29["tnr"]["directional"])
        self.assertFalse(e30["tnr"]["directional"])
        # a class with support 2 in n=100
        cases = [sc("ask", "ask")] * 2 + [sc("answer", "answer")] * 98
        m = run(cases)
        a = m["action"]
        self.assertEqual(a["per_class"]["ask"]["support"], 2)
        for key in ("f1", "f2", "f0_5"):
            self.assertTrue(a["per_class"]["ask"][key]["directional"], key)
            self.assertFalse(a["per_class"]["answer"][key]["directional"], key)
            self.assertEqual(a["per_class"]["ask"][key]["n"], 100)
            self.assertTrue(a["macro"][key]["directional"], key)
            self.assertTrue(a["weighted"][key]["directional"], key)
        self.assertFalse(a["accuracy"]["directional"])
        self.assertFalse(a["kappa"]["directional"])
        self.assertEqual(m["directional_below"], 30)


class TestMetadataAndPrivacy(unittest.TestCase):
    def test_gold_report_has_no_ids(self):
        routed = [
            RoutedThread(thread_id=987654321, labels=Labels("billing", "normal", "ask", 0.9),
                         cheap_labels=Labels("billing", "normal", "ask", 0.9),
                         tiers=("cheap",), reason_codes=("high_risk_to_larger",)),
            RoutedThread(thread_id=987654322,
                         labels=Labels("other", "high", "escalate", 1.0),
                         cheap_labels=Labels("other", "high", "escalate", 1.0),
                         tiers=("cheap", "larger")),
            RoutedThread(thread_id=555, labels=Labels("other", "low", "answer", 1.0),
                         cheap_labels=Labels("other", "low", "answer", 1.0), tiers=("cheap",)),
        ]
        gold = {987654321: GoldRow("billing", "ask", False),
                987654322: GoldRow("other", "escalate", True)}
        cases = metrics.cases_from_routed(routed, gold)
        self.assertEqual(len(cases), 2)
        self.assertEqual([c.case_id for c in cases], [None, None])
        self.assertEqual(cases[1], ScoredCase(None, "other", "escalate", True, "other",
                                              "escalate"))
        text = to_json(run(cases))
        for needle in ("987654321", "987654322"):
            self.assertNotIn(needle, text)
        direct = [sc("answer", "answer", case_id="SECRETCASE_ALPHA"),
                  sc("ask", "route", case_id="SECRETCASE_BETA")]
        m = run(direct)
        text = to_json(m)
        self.assertNotIn("SECRETCASE", text)
        self.assertNotIn('"ids"', text)
        self.assertNotIn("ids", [k for k, _ in walk(m)])

    def test_synthetic_ids_in_cells(self):
        results = [
            {"id": "c1", "expected": {"intent": "delivery", "action": "ask", "escalate": False},
             "actual": {"intent": "delivery", "action": "ask", "escalate": False},
             "reason_codes": ["high_risk_to_larger"], "tiers": ["cheap"], "confidence": 0.9,
             "expected_violations": None, "actual_violations": None},
            {"id": "c2", "expected": {"intent": "other", "action": "escalate", "escalate": True},
             "actual": {"intent": "delivery", "action": "ask", "escalate": False},
             "reason_codes": []},
            {"id": "c3", "expected": {"intent": "delivery", "action": "ask", "escalate": False},
             "actual": {"intent": "delivery", "action": "ask", "escalate": False},
             "reason_codes": []},
        ]
        cases = metrics.cases_from_synthetic(results)
        self.assertEqual(cases[1], ScoredCase("c2", "other", "escalate", True, "delivery",
                                              "ask"))
        m = run(cases, kind="synthetic")
        a = m["action"]
        ask, esc = ACTIONS.index("ask"), ACTIONS.index("escalate")
        self.assertEqual(a["ids"][ask][ask], ["c1", "c3"])
        self.assertEqual(a["ids"][esc][ask], ["c2"])
        self.assertEqual(sum(len(cell) for row in a["ids"] for cell in row), 3)
        i = m["intent"]
        other, delivery = INTENTS.index("other"), INTENTS.index("delivery")
        self.assertEqual(i["ids"][other][delivery], ["c2"])
        self.assertEqual(len(i["ids"]), len(INTENTS))
        self.assertEqual(m["escalation"]["counts"]["fn"], 1)

    def test_run_metadata(self):
        coverage = {"threads_routed": 5, "threads_scored": 4, "threads_without_gold": 1,
                    "gold_rows_unmatched": 1}
        operations = {"tier_shares": {"cheap": {"value": 1.0}}, "cost_per_1000_usd": 2.0,
                      "prices_are_placeholders": True}
        config = {"threshold": 0.7, "prices": {"cheap": 0.0, "larger": 0.1}}
        m = run(action_fixture(), config=config, coverage=coverage, operations=operations)
        self.assertEqual(m["schema_version"], "metrics/1")
        self.assertEqual(m["kind"], "gold")
        self.assertEqual(m["run_at"], RUN_AT)
        self.assertEqual(m["cohort"], GOLD_COHORT)
        self.assertEqual(m["sample"], SAMPLE)
        self.assertEqual(m["config"], config)
        self.assertEqual(m["coverage"], coverage)
        self.assertEqual(m["operations"], operations)
        self.assertIsInstance(m["averaging"], str)
        self.assertIn("zero", m["averaging"])
        syn = run([], kind="synthetic")
        self.assertIsNone(syn["coverage"])
        self.assertIsNone(syn["operations"])

        bad = [
            dict(run_at="2026-11-18T10:00:00"),
            dict(run_at="2026-11-18T10:00:00+00:00"),
            dict(run_at="not a date Z"),
            dict(cohort={k: v for k, v in GOLD_COHORT.items() if k != "split"}),
            dict(cohort=dict(GOLD_COHORT, extra=1)),
            dict(cohort=dict(GOLD_COHORT, name="synthetic")),
            dict(cohort=dict(GOLD_COHORT, gold_revision="sha256:xyz")),
            dict(cohort=dict(GOLD_COHORT, brand_population="SomeBrand")),
            dict(sample={"size": 1}),
            dict(sample={"size": 1, "seed": 2, "extra": 3}),
        ]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                run([], **kw)
        with self.assertRaises(ValueError):
            run([], kind="synthetic", cohort=dict(GOLD_COHORT, name="hard_case"))
        run([], cohort=dict(GOLD_COHORT, name="hard_case"))

    def test_comparison_key(self):
        a = run(action_fixture())
        b = run(escalation_fixture(), run_at="2026-11-25T09:30:00Z",
                sample={"size": 50, "seed": 9}, config={"threshold": 0.9, "prices": None})
        self.assertEqual(comparison_key(a), comparison_key(b))
        self.assertEqual(comparison_key(a), ("metrics/1", "gold", "gold_random", "dev", REV,
                                             "all"))
        variants = [
            run([], kind="synthetic", cohort=dict(SYN_COHORT, split="dev")),
            run([], cohort=dict(GOLD_COHORT, name="hard_case")),
            run([], cohort=dict(GOLD_COHORT, split="eval")),
            run([], cohort=dict(GOLD_COHORT, gold_revision="sha256:" + "b" * 64)),
        ]
        for v in variants:
            self.assertNotEqual(comparison_key(a), comparison_key(v))
        other_brand = json.loads(to_json(a))
        other_brand["cohort"]["brand_population"] = "subset"
        self.assertNotEqual(comparison_key(a), comparison_key(other_brand))
        broken = json.loads(to_json(a))
        del broken["cohort"]["split"]
        with self.assertRaises(ValueError):
            comparison_key(broken)
        with self.assertRaises(ValueError):
            comparison_key({})

    def test_no_reason_fields(self):
        results = [{"id": "c1", "expected": {"intent": "other", "action": "escalate",
                                             "escalate": True},
                    "actual": {"intent": "other", "action": "escalate", "escalate": True},
                    "reason_codes": ["high_risk_to_larger"]}]
        for m in (run(action_fixture()),
                  run(metrics.cases_from_synthetic(results), kind="synthetic")):
            keys = {k for k, _ in walk(m)}
            for banned in ("reason", "reasons", "reason_codes", "topic_id"):
                self.assertNotIn(banned, keys)

    def test_to_json(self):
        m = run(action_fixture())
        text = to_json(m)
        self.assertEqual(text, json.dumps(m, sort_keys=True, indent=2) + "\n")
        self.assertEqual(json.loads(text), m)

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            run([], kind="weekly")
        with self.assertRaises(ValueError):
            sc("answer", "shout")
        with self.assertRaises(ValueError):
            sc("dance", "answer")
        with self.assertRaises(ValueError):
            sc("answer", "answer", gold_intent="weather")
        with self.assertRaises(ValueError):
            sc("answer", "answer", pred_intent="weather")
        with self.assertRaises(ValueError):
            metrics.cases_from_synthetic([{"id": "x", "expected": {
                "intent": "other", "action": "nope", "escalate": False},
                "actual": {"intent": "other", "action": "answer"}}])


class TestHygiene(unittest.TestCase):
    def test_tests_do_not_read_owner_files(self):
        source = Path(__file__).read_text(encoding="utf-8")
        for needle in ("evals/" + "thresholds.json", "evals/" + "drift.json",
                       "evals/" + "gold", "docs/" + "prd.md", "policies" + "/",
                       "scenarios" + "/", "data/" + "raw"):
            self.assertNotIn(needle, source)

    def test_stdlib_only(self):
        banned = {"urllib", "http", "socket", "requests", "ssl", "ftplib", "smtplib"}
        stdlib = set(sys.stdlib_module_names)
        src = (REPO_ROOT / "src" / "triage" / "metrics.py").read_text(encoding="utf-8")
        self.assertNotIn("os.environ", src)
        self.assertNotIn("datetime.now", src)
        self.assertNotIn("time.time", src)
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


if __name__ == "__main__":
    unittest.main()
