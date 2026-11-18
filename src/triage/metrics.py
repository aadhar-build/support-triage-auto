"""Full performance metrics for one scored run, as one JSON-ready dict (schema `metrics/1`).

`compute(...)` turns scored cases into escalation metrics (counts, proportions with Wilson 95%
intervals, F-scores, balanced accuracy, MCC) and, for action and intent, a confusion matrix,
one-vs-rest per-class metrics, macro and weighted averages, accuracy and Cohen's kappa.
`comparison_key(...)` says which runs may be compared. Gold-kind output holds no ids of any
kind; synthetic-kind output lists case ids per confusion cell. `compute` never reads the clock.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Mapping, Optional, Sequence

from triage.evaluate import wilson_interval
from triage.tiers import ACTIONS, INTENTS

SCHEMA_VERSION = "metrics/1"
DIRECTIONAL_BELOW = 30
KINDS = ("gold", "synthetic")
COHORT_NAMES = {"gold": ("gold_random", "hard_case"), "synthetic": ("synthetic",)}
COHORT_KEYS = ("name", "split", "gold_revision", "brand_population")
SAMPLE_KEYS = ("size", "seed")
AVERAGED = ("tpr", "tnr", "precision", "f1", "f2", "f0_5")
AVERAGING = ("Macro and weighted averages run over the included classes: the union of gold and "
             "predicted classes (support > 0 or predicted > 0), listed in included_classes. A "
             "per-class value that is null counts as zero in the average (zero_filled counts "
             "them) and still shows as null in per_class. Macro is the unweighted mean; "
             "weighted is sum(support * value) / sum(support).")

_RUN_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")
_REVISION_RE = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class ScoredCase:
    case_id: Optional[str]
    gold_intent: str
    gold_action: str
    gold_escalate: bool
    pred_intent: str
    pred_action: str

    def __post_init__(self):
        for name in ("gold_intent", "pred_intent"):
            if getattr(self, name) not in INTENTS:
                raise ValueError("unknown %s %r" % (name, getattr(self, name)))
        for name in ("gold_action", "pred_action"):
            if getattr(self, name) not in ACTIONS:
                raise ValueError("unknown %s %r" % (name, getattr(self, name)))
        if not isinstance(self.gold_escalate, bool):
            raise ValueError("gold_escalate must be a bool")


def cases_from_routed(routed, gold) -> List[ScoredCase]:
    """One case per routed thread with a gold row, in routed order, with no case id."""
    cases = []
    for r in routed:
        g = gold.get(r.thread_id)
        if g is None:
            continue
        cases.append(ScoredCase(None, g.intent, g.action, g.should_escalate,
                                r.labels.intent, r.labels.action))
    return cases


def cases_from_synthetic(results) -> List[ScoredCase]:
    """Cases from run_synthetic's per-case result dicts; only id, expected, actual are read."""
    return [ScoredCase(r["id"], r["expected"]["intent"], r["expected"]["action"],
                       r["expected"]["escalate"], r["actual"]["intent"], r["actual"]["action"])
            for r in results]


# ---------------------------------------------------------------- building blocks

def proportion(num: int, den: int) -> dict:
    """A proportion with its n, Wilson 95% interval and directional mark."""
    return {"value": num / den if den else None, "num": num, "n": den,
            "ci": wilson_interval(num, den), "directional": den < DIRECTIONAL_BELOW}


def _score(value: Optional[float], n: int, directional: bool) -> dict:
    return {"value": value, "n": n, "ci": None, "directional": directional}


def _fbeta(beta: float, tp: int, fp: int, fn: int) -> Optional[float]:
    b2 = beta * beta
    den = (1 + b2) * tp + b2 * fn + fp
    return (1 + b2) * tp / den if den else None


def _fscores(tp: int, fp: int, fn: int, n: int, directional: bool) -> dict:
    return {key: _score(_fbeta(beta, tp, fp, fn), n, directional)
            for key, beta in (("f1", 1), ("f2", 2), ("f0_5", 0.5))}


# ---------------------------------------------------------------- escalation

def _escalation(tp: int, fp: int, tn: int, fn: int) -> dict:
    n = tp + fp + tn + fn
    pos, neg = tp + fn, tn + fp
    out = {"counts": {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "positives": pos,
                      "predicted_positives": tp + fp},
           "tpr": proportion(tp, pos), "tnr": proportion(tn, neg),
           "fpr": proportion(fp, neg), "fnr": proportion(fn, pos),
           "precision": proportion(tp, tp + fp), "npv": proportion(tn, tn + fn),
           "accuracy": proportion(tp + tn, n), "prevalence": proportion(pos, n)}
    out.update(_fscores(tp, fp, fn, n, pos < DIRECTIONAL_BELOW))
    thin = pos < DIRECTIONAL_BELOW or neg < DIRECTIONAL_BELOW
    tpr, tnr = out["tpr"]["value"], out["tnr"]["value"]
    out["balanced_accuracy"] = _score(
        None if tpr is None or tnr is None else (tpr + tnr) / 2, n, thin)
    factors = (tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)
    out["mcc"] = _score((tp * tn - fp * fn) / math.sqrt(factors) if factors else None, n, thin)
    return out


# ---------------------------------------------------------------- action and intent

def _average(per_class: dict, included: List[str], key: str, weighted: bool, n: int) -> dict:
    total = weight = 0.0
    zero_filled = 0
    for label in included:
        value = per_class[label][key]["value"]
        if value is None:
            zero_filled += 1
            value = 0.0
        w = per_class[label]["support"] if weighted else 1
        total += w * value
        weight += w
    directional = any(per_class[c]["support"] < DIRECTIONAL_BELOW for c in included)
    out = _score(total / weight if included and weight else None, n, directional)
    out["zero_filled"] = zero_filled
    return out


def _classes(labels: Sequence[str], counts: List[List[int]], n: int) -> dict:
    k = len(labels)
    support = [sum(counts[i]) for i in range(k)]
    predicted = [sum(counts[r][i] for r in range(k)) for i in range(k)]
    per_class = {}
    for i, label in enumerate(labels):
        tp = counts[i][i]
        fn, fp = support[i] - tp, predicted[i] - tp
        tn = n - tp - fn - fp
        entry = {"support": support[i], "predicted": predicted[i],
                 "tp": tp, "fp": fp, "tn": tn, "fn": fn,
                 "tpr": proportion(tp, support[i]), "tnr": proportion(tn, tn + fp),
                 "precision": proportion(tp, predicted[i])}
        entry.update(_fscores(tp, fp, fn, n, support[i] < DIRECTIONAL_BELOW))
        per_class[label] = entry

    included = [c for i, c in enumerate(labels) if support[i] or predicted[i]]
    averages = {}
    for name, weighted in (("macro", False), ("weighted", True)):
        avg = {key: _average(per_class, included, key, weighted, n) for key in AVERAGED}
        avg["included_classes"] = list(included)
        averages[name] = avg

    correct = sum(counts[i][i] for i in range(k))
    expected_num = sum(support[i] * predicted[i] for i in range(k))  # p_e * n * n
    kappa = None
    if n and expected_num != n * n:
        p_o, p_e = correct / n, expected_num / (n * n)
        kappa = (p_o - p_e) / (1 - p_e)
    return {"confusion_matrix": {"labels": list(labels), "counts": counts},
            "per_class": per_class, "macro": averages["macro"],
            "weighted": averages["weighted"], "accuracy": proportion(correct, n),
            "kappa": _score(kappa, n, n < DIRECTIONAL_BELOW)}


# ---------------------------------------------------------------- validation

def _check_run_at(run_at) -> None:
    if not isinstance(run_at, str) or not _RUN_AT_RE.fullmatch(run_at):
        raise ValueError("run_at must be an ISO 8601 UTC time ending in Z, got %r" % (run_at,))
    try:
        datetime.strptime(run_at[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        raise ValueError("run_at is not a valid time: %r" % (run_at,))


def _check_exact_keys(name: str, value, keys: Sequence[str]) -> None:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("%s must be an object with exactly the keys %s"
                         % (name, ", ".join(keys)))


def _check_cohort(kind: str, cohort) -> None:
    _check_exact_keys("cohort", cohort, COHORT_KEYS)
    if cohort["name"] not in COHORT_NAMES[kind]:
        raise ValueError("cohort name %r does not fit kind %r" % (cohort["name"], kind))
    if cohort["split"] is not None and not isinstance(cohort["split"], str):
        raise ValueError("cohort split must be a string or null")
    if not isinstance(cohort["gold_revision"], str) \
            or not _REVISION_RE.fullmatch(cohort["gold_revision"]):
        raise ValueError("cohort gold_revision must be 'sha256:' + 64 hex digits")
    if cohort["brand_population"] != "all":
        raise ValueError("cohort brand_population must be 'all'")


def _check_sample(sample) -> None:
    _check_exact_keys("sample", sample, SAMPLE_KEYS)
    for key in SAMPLE_KEYS:
        v = sample[key]
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            raise ValueError("sample %s must be an int or null" % key)


# ---------------------------------------------------------------- public API

def compute(cases: Iterable[ScoredCase], kind: str, *, run_at: str, cohort: Mapping,
            sample: Mapping, config: Mapping, coverage: Optional[Mapping] = None,
            operations: Optional[Mapping] = None) -> dict:
    if kind not in KINDS:
        raise ValueError("kind must be 'gold' or 'synthetic', got %r" % (kind,))
    _check_run_at(run_at)
    _check_cohort(kind, cohort)
    _check_sample(sample)

    with_ids = kind == "synthetic"
    tp = fp = tn = fn = 0
    blocks = {}
    for name, labels in (("action", ACTIONS), ("intent", INTENTS)):
        k = len(labels)
        blocks[name] = {"index": {c: i for i, c in enumerate(labels)},
                        "counts": [[0] * k for _ in range(k)],
                        "ids": [[[] for _ in range(k)] for _ in range(k)] if with_ids else None}
    n = 0
    for c in cases:
        n += 1
        pred_esc = c.pred_action == "escalate"
        if c.gold_escalate:
            tp, fn = tp + pred_esc, fn + (not pred_esc)
        else:
            fp, tn = fp + pred_esc, tn + (not pred_esc)
        for name, gold, pred in (("action", c.gold_action, c.pred_action),
                                 ("intent", c.gold_intent, c.pred_intent)):
            b = blocks[name]
            r, col = b["index"][gold], b["index"][pred]
            b["counts"][r][col] += 1
            if with_ids:
                b["ids"][r][col].append(c.case_id)

    out = {"schema_version": SCHEMA_VERSION, "kind": kind, "run_at": run_at,
           "cohort": copy.deepcopy(dict(cohort)), "sample": copy.deepcopy(dict(sample)),
           "config": copy.deepcopy(config), "coverage": copy.deepcopy(coverage),
           "operations": copy.deepcopy(operations), "n": n,
           "directional_below": DIRECTIONAL_BELOW, "averaging": AVERAGING,
           "escalation": _escalation(int(tp), int(fp), int(tn), int(fn))}
    for name, labels in (("action", ACTIONS), ("intent", INTENTS)):
        block = _classes(labels, blocks[name]["counts"], n)
        if with_ids:
            block["ids"] = blocks[name]["ids"]
        out[name] = block
    return out


def comparison_key(metrics: Mapping) -> tuple:
    """The fields that must match for two metrics files to be compared."""
    try:
        cohort = metrics["cohort"]
        return (metrics["schema_version"], metrics["kind"], cohort["name"], cohort["split"],
                cohort["gold_revision"], cohort["brand_population"])
    except (KeyError, TypeError) as e:
        raise ValueError("metrics lacks a comparison field: %s" % e)


def to_json(metrics: Mapping) -> str:
    return json.dumps(metrics, sort_keys=True, indent=2) + "\n"
