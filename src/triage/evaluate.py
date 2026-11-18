"""Score routed threads against a gold CSV and render an aggregate-only report.

The report never holds tweet text, author or brand ids, or any per-thread id or row.
Every rate whose denominator is 0 is None (null in JSON, n/a in Markdown).
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Dict, List, Mapping, Optional, Sequence

from triage.router import RoutedThread
from triage.tiers import ACTIONS, INTENTS, PLACEHOLDER_PRICES, check_price

GOLD_COLUMNS = ("thread_id", "intent", "action", "should_escalate")
TIER_NAMES = ("cheap", "larger", "agent")
_TRUE = ("true", "1")
_FALSE = ("false", "0")
_ID_RE = re.compile(r"\d+", re.ASCII)
_Z95 = 1.96
LARGER_FALLBACK_REASONS = ("spend_cap", "monthly_cap", "spend_log", "unknown_bill",
                           "reservation_exceeded", "api_error", "too_long", "parse_error",
                           "invalid_labels")


def _zero_fallbacks() -> Dict[str, int]:
    return {k: 0 for k in LARGER_FALLBACK_REASONS}


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
    # Larger-tier model spend, in API-equivalent USD (tokens times owner list prices).
    # Separate from the flat per-call cost_per_1000_threads above.
    larger_mode: str = "fake"
    larger_model: Optional[str] = None
    larger_prompt_version: Optional[str] = None
    larger_post_merge_eval: bool = False
    larger_input_tokens: int = 0
    larger_output_tokens: int = 0
    larger_cache_write_tokens: int = 0
    larger_cache_read_tokens: int = 0
    larger_confirmed_usd: float = 0.0
    larger_unconfirmed_usd: float = 0.0
    max_spend_usd: Optional[float] = None
    monthly_cap_usd: Optional[float] = None
    month_to_date_usd: Optional[float] = None
    larger_stopped_by: Optional[str] = None
    larger_answered: int = 0
    larger_reservation_exceeded: int = 0
    larger_fallbacks: Dict[str, int] = field(default_factory=_zero_fallbacks)


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
          threshold: Optional[float] = None,
          larger_usage: Optional[Mapping[str, object]] = None) -> Report:
    """Score routed threads. `larger_usage` fills the larger_* spend fields by name."""
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

    report = Report(
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
    if larger_usage:
        unknown = [k for k in larger_usage if k not in _USAGE_FIELDS]
        if unknown:
            raise ValueError("unknown larger_usage field(s): %s" % ", ".join(sorted(unknown)))
        usage = dict(larger_usage)
        if "larger_fallbacks" in usage:
            fb = _zero_fallbacks()
            fb.update(usage["larger_fallbacks"])
            usage["larger_fallbacks"] = fb
        report = replace(report, **usage)
    return report


_USAGE_FIELDS = tuple(f.name for f in fields(Report)
                      if f.name.startswith("larger_")
                      or f.name in ("max_spend_usd", "monthly_cap_usd", "month_to_date_usd"))


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


def _fmt_rate(rate: Optional[float], ci: Optional[List[float]]) -> str:
    if rate is None or ci is None:
        return "n/a"
    return "%.4f (%.4f to %.4f)" % (rate, ci[0], ci[1])


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
        ("action_accuracy", _fmt_rate(report.action_accuracy, report.action_accuracy_ci)),
        ("intent_accuracy", _fmt_rate(report.intent_accuracy, report.intent_accuracy_ci)),
        ("missed_escalations", report.missed_escalations),
        ("missed_escalation_rate", _fmt_rate(report.missed_escalation_rate,
                                             report.missed_escalation_rate_ci)),
        ("false_escalations", report.false_escalations),
        ("false_escalation_rate", _fmt_rate(report.false_escalation_rate,
                                            report.false_escalation_rate_ci)),
        ("needs_person_share", _fmt_rate(report.needs_person_share,
                                         report.needs_person_share_ci)),
    ]
    rows += [("tier_share.%s" % t, _fmt_rate(v, report.tier_shares_ci.get(t)))
             for t, v in report.tier_shares.items()]
    rows += [("cost_per_1000_threads", report.cost_per_1000_threads)]
    rows += [("price.%s" % t, v) for t, v in report.prices.items()]
    rows += [("prices_are_placeholders", report.prices_are_placeholders)]
    lines += ["| %s | %s |" % (k, _fmt(v)) for k, v in rows]
    if report.larger_mode == "real":
        lines += ["", "## Model spend (API-equivalent USD)", "",
                  "Tokens each call reported, times the owner's list prices. Not a bill.", "",
                  "| item | value |", "| --- | --- |"]
        spend = [
            ("larger_model", report.larger_model),
            ("larger_prompt_version", report.larger_prompt_version),
            ("larger_post_merge_eval", report.larger_post_merge_eval),
            ("confirmed_usd", report.larger_confirmed_usd),
            ("unconfirmed_usd", report.larger_unconfirmed_usd),
            ("max_spend_usd (per run cap)", report.max_spend_usd),
            ("monthly_cap_usd", report.monthly_cap_usd),
            ("month_to_date_usd", report.month_to_date_usd),
            ("stopped_by", report.larger_stopped_by or "none"),
            ("answered", report.larger_answered),
            ("reservation_exceeded", report.larger_reservation_exceeded),
            ("input_tokens", report.larger_input_tokens),
            ("output_tokens", report.larger_output_tokens),
            ("cache_write_tokens", report.larger_cache_write_tokens),
            ("cache_read_tokens", report.larger_cache_read_tokens),
        ]
        spend += [("fallback.%s" % k, v) for k, v in report.larger_fallbacks.items()]
        lines += ["| %s | %s |" % (k, _fmt(v)) for k, v in spend]
    return "\n".join(lines) + "\n"
