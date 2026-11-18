"""Score routed threads against a gold CSV and render an aggregate-only report.

The report never holds tweet text, author or brand ids, or any per-thread id or row.
Every rate whose denominator is 0 is None (null in JSON, n/a in Markdown).
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence

from triage.router import RoutedThread
from triage.tiers import ACTIONS, INTENTS, PLACEHOLDER_PRICES, check_price

GOLD_COLUMNS = ("thread_id", "intent", "action", "should_escalate")
TIER_NAMES = ("cheap", "larger", "agent")
_TRUE = ("true", "1")
_FALSE = ("false", "0")
_ID_RE = re.compile(r"\d+", re.ASCII)
_Z95 = 1.96


class GoldFileError(ValueError):
    """The gold CSV is empty, lacks a column, or has a bad value."""


@dataclass(frozen=True)
class GoldRow:
    intent: str
    action: str
    should_escalate: bool


@dataclass
class Report:
    threads_routed: int
    threads_scored: int
    threads_without_gold: int
    gold_rows_unmatched: int
    action_accuracy: Optional[float]
    intent_accuracy: Optional[float]
    missed_escalations: int
    missed_escalation_rate: Optional[float]
    false_escalations: int
    false_escalation_rate: Optional[float]
    needs_person_share: Optional[float]
    tier_shares: Dict[str, Optional[float]]
    cost_per_1000_threads: Optional[float]
    prices: Dict[str, float]
    prices_are_placeholders: bool
    sample_size: Optional[int] = None
    seed: Optional[int] = None
    split: Optional[str] = None
    threshold: Optional[float] = None
    action_accuracy_ci: Optional[List[float]] = None
    intent_accuracy_ci: Optional[List[float]] = None
    missed_escalation_rate_ci: Optional[List[float]] = None
    false_escalation_rate_ci: Optional[List[float]] = None
    needs_person_share_ci: Optional[List[float]] = None
    tier_shares_ci: Dict[str, Optional[List[float]]] = field(default_factory=dict)


def load_gold(path) -> Dict[int, GoldRow]:
    """Read and validate a gold CSV. FileNotFoundError passes through."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise GoldFileError("gold file is empty: expected columns " + ", ".join(GOLD_COLUMNS))
        header = [h.strip() if h else h for h in reader.fieldnames]
        reader.fieldnames = header
        missing = [c for c in GOLD_COLUMNS if c not in header]
        if missing:
            raise GoldFileError("gold file is missing column(s): " + ", ".join(missing))

        gold: Dict[int, GoldRow] = {}
        for row in reader:
            line = reader.line_num
            values = {c: row.get(c) for c in GOLD_COLUMNS}
            if any(v is None for v in values.values()):
                raise GoldFileError("line %d: row has too few fields" % line)
            values = {c: v.strip() for c, v in values.items()}

            if not _ID_RE.fullmatch(values["thread_id"]):
                raise GoldFileError("line %d: thread_id must be a non-negative integer, got %r"
                                    % (line, values["thread_id"]))
            thread_id = int(values["thread_id"])
            if thread_id in gold:
                raise GoldFileError("line %d: duplicate thread_id %d" % (line, thread_id))
            if values["intent"] not in INTENTS:
                raise GoldFileError("line %d: unknown intent %r" % (line, values["intent"]))
            if values["action"] not in ACTIONS:
                raise GoldFileError("line %d: unknown action %r" % (line, values["action"]))
            esc = values["should_escalate"].lower()
            if esc not in _TRUE + _FALSE:
                raise GoldFileError("line %d: should_escalate must be true/false/1/0, got %r"
                                    % (line, values["should_escalate"]))
            gold[thread_id] = GoldRow(values["intent"], values["action"], esc in _TRUE)
    return gold


def _rate(num: int, den: int) -> Optional[float]:
    return num / den if den else None


def wilson_interval(successes: int, total: int) -> Optional[List[float]]:
    """95% Wilson score interval for successes/total as [low, high]; None when total is 0."""
    if not total:
        return None
    p = successes / total
    z2 = _Z95 * _Z95
    denom = 1 + z2 / total
    centre = (p + z2 / (2 * total)) / denom
    half = _Z95 * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / denom
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == total else min(1.0, centre + half)
    return [low, high]


def score(routed: Sequence[RoutedThread], gold: Mapping[int, GoldRow],
          prices: Optional[Mapping[str, float]] = None, *, sample_size: Optional[int] = None,
          seed: Optional[int] = None, split: Optional[str] = None,
          threshold: Optional[float] = None) -> Report:
    prices = dict(PLACEHOLDER_PRICES if prices is None else prices)
    tier_names = list(TIER_NAMES)
    for r in routed:
        for t in r.tiers:
            if t not in tier_names:
                tier_names.append(t)
    for t in tier_names:
        if t not in prices:
            raise ValueError("no price given for tier %r" % (t,))
    for t, p in prices.items():
        check_price(t, p)

    n = len(routed)
    routed_ids = {r.thread_id for r in routed}
    scored = [(r, gold[r.thread_id]) for r in routed if r.thread_id in gold]

    action_ok = sum(1 for r, g in scored if r.labels.action == g.action)
    intent_ok = sum(1 for r, g in scored if r.labels.intent == g.intent)
    gold_esc = [(r, g) for r, g in scored if g.should_escalate]
    gold_no_esc = [(r, g) for r, g in scored if not g.should_escalate]
    missed = sum(1 for r, _ in gold_esc if r.labels.action != "escalate")
    false_esc = sum(1 for r, _ in gold_no_esc if r.labels.action == "escalate")

    escalated = sum(1 for r in routed if r.labels.action == "escalate")
    tier_counts = {t: sum(1 for r in routed if t in r.tiers) for t in tier_names}
    tier_shares = {t: _rate(c, n) for t, c in tier_counts.items()}
    total_cost = sum(prices[t] for r in routed for t in r.tiers)

    return Report(
        threads_routed=n,
        threads_scored=len(scored),
        threads_without_gold=n - len(scored),
        gold_rows_unmatched=sum(1 for tid in gold if tid not in routed_ids),
        action_accuracy=_rate(action_ok, len(scored)),
        intent_accuracy=_rate(intent_ok, len(scored)),
        missed_escalations=missed,
        missed_escalation_rate=_rate(missed, len(gold_esc)),
        false_escalations=false_esc,
        false_escalation_rate=_rate(false_esc, len(gold_no_esc)),
        needs_person_share=_rate(escalated, n),
        tier_shares=tier_shares,
        cost_per_1000_threads=1000 * total_cost / n if n else None,
        prices=prices,
        prices_are_placeholders=prices == PLACEHOLDER_PRICES,
        sample_size=sample_size,
        seed=seed,
        split=split,
        threshold=threshold,
        action_accuracy_ci=wilson_interval(action_ok, len(scored)),
        intent_accuracy_ci=wilson_interval(intent_ok, len(scored)),
        missed_escalation_rate_ci=wilson_interval(missed, len(gold_esc)),
        false_escalation_rate_ci=wilson_interval(false_esc, len(gold_no_esc)),
        needs_person_share_ci=wilson_interval(escalated, n),
        tier_shares_ci={t: wilson_interval(c, n) for t, c in tier_counts.items()},
    )


def report_to_json(report: Report) -> str:
    return json.dumps(asdict(report), sort_keys=True, indent=2) + "\n"


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return "%.4f" % value
    return str(value)


def report_to_markdown(report: Report) -> str:
    lines = ["# Triage eval report", ""]
    if report.prices_are_placeholders:
        lines += ["> Prices are placeholders, not real prices. Do not quote the cost figure "
                  "as a real cost.", ""]
    lines += ["| metric | value |", "| --- | --- |"]
    rows = [
        ("split", report.split), ("sample_size", report.sample_size), ("seed", report.seed),
        ("threshold", report.threshold),
        ("threads_routed", report.threads_routed), ("threads_scored", report.threads_scored),
        ("threads_without_gold", report.threads_without_gold),
        ("gold_rows_unmatched", report.gold_rows_unmatched),
        ("action_accuracy", report.action_accuracy),
        ("intent_accuracy", report.intent_accuracy),
        ("missed_escalations", report.missed_escalations),
        ("missed_escalation_rate", report.missed_escalation_rate),
        ("false_escalations", report.false_escalations),
        ("false_escalation_rate", report.false_escalation_rate),
        ("needs_person_share", report.needs_person_share),
    ]
    rows += [("tier_share.%s" % t, v) for t, v in report.tier_shares.items()]
    rows += [("cost_per_1000_threads", report.cost_per_1000_threads)]
    rows += [("price.%s" % t, v) for t, v in report.prices.items()]
    rows += [("prices_are_placeholders", report.prices_are_placeholders)]
    lines += ["| %s | %s |" % (k, _fmt(v)) for k, v in rows]
    return "\n".join(lines) + "\n"
