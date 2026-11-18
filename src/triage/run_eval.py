"""Run the cascade on a seeded sample of one split and write an aggregate-only report.

Usage: python3 -m triage.run_eval --gold GOLD.csv --out REPORT [options]

No network calls and no keys. The only file written is --out, plus the audit trace when
--trace PATH is given (one JSON line per routed thread, outside the repository; it is
written before the report), plus gold-kind `triage.metrics` when --metrics-out PATH is given
(with a required --cohort; written last). Default prices are placeholders, not real prices.
The always-escalate topics come from --rules (default policies/rules.json), loaded before any
data; a missing or empty topic list exits 2. Stdout gives topics_loaded=<n>, then
brand=<name|none> rules_loaded=<n> repeat_contact=<on|off> (before trace_records=<n> when a
trace is written).
With --brand NAME the rules come from brands/NAME/guidelines.md instead of --rules, and only
the split's threads attributed to NAME at their last inbound turn are sampled and routed
(context_signals.brand_attribution). Stdout then also gives brand_threads, excluded_threads,
excluded_other_brand and excluded_unattributed (over the whole split, before sampling), and
the report gains a "brand" object. --brand cannot be combined with --metrics-out.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from datetime import datetime, timezone
from typing import List, Optional

from triage import metrics as metrics_mod
from triage.brand_rules import add_rules_args, load_rules_source
from triage.cheap_tier import CheapTier
from triage.context_signals import brand_attribution
from triage.evaluate import GoldFileError, load_gold, report_to_json, report_to_markdown, score
from triage.loader import HeaderError
from triage.policy_check import RulesError
from triage.router import DEFAULT_CONFIDENCE_THRESHOLD, route_threads
from triage.split import DEFAULT_SPLIT_PATH, SPLIT_NAMES, SplitConfigError, load_split_config, \
    split_threads
from triage.threads import build_threads
from triage.tiers import PLACEHOLDER_PRICES, FakeAgentTier, FakeLargerTier
from triage.trace import REPO_ROOT, RunInfo, TraceError, build_record, check_trace_path, \
    code_version, write_trace

DEFAULT_DATA = "data/raw/twcs.csv"
DEFAULT_RULES_PATH = REPO_ROOT / "policies" / "rules.json"
ATTRIBUTION = "first-tweet mention or brand reply known at the last inbound turn"


class _Fail(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m triage.run_eval",
                                description=__doc__.splitlines()[0])
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--gold", required=True)
    p.add_argument("--sample-size", type=int, default=200)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", required=True)
    p.add_argument("--format", choices=("json", "md"), default="json")
    p.add_argument("--split", choices=SPLIT_NAMES, default="dev")
    p.add_argument("--split-config", default=str(DEFAULT_SPLIT_PATH))
    add_rules_args(p, DEFAULT_RULES_PATH)
    p.add_argument("--threshold", type=float, default=DEFAULT_CONFIDENCE_THRESHOLD)
    p.add_argument("--price-cheap", type=float, default=PLACEHOLDER_PRICES["cheap"])
    p.add_argument("--price-larger", type=float, default=PLACEHOLDER_PRICES["larger"])
    p.add_argument("--price-agent", type=float, default=PLACEHOLDER_PRICES["agent"])
    p.add_argument("--trace", default=None,
                   help="write an audit trace (JSON lines) to this path outside the repo")
    p.add_argument("--metrics-out", default=None,
                   help="write gold-kind metrics (metrics/1 JSON) to this path")
    p.add_argument("--cohort", choices=("gold_random", "hard_case"), default=None,
                   help="which gold set this is; required with --metrics-out")
    p.add_argument("--run-at", default=None,
                   help="ISO 8601 UTC time ending in Z for the metrics file (default: now)")
    return p


def _resolve_run_at(value: Optional[str]) -> str:
    if value is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        metrics_mod.check_run_at(value)
    except ValueError as e:
        raise _Fail("bad --run-at: %s" % e)
    return value


def _metrics_text(args, routed, gold, report, prices, run_at: str, gold_digest: str) -> str:
    n = report.threads_routed
    tier_shares = {t: metrics_mod.proportion(sum(1 for r in routed if t in r.tiers), n)
                   for t in report.tier_shares}
    operations = {"tier_shares": tier_shares,
                  "cost_per_1000_usd": report.cost_per_1000_threads,
                  "prices_are_placeholders": report.prices_are_placeholders}
    coverage = {"threads_routed": report.threads_routed,
                "threads_scored": report.threads_scored,
                "threads_without_gold": report.threads_without_gold,
                "gold_rows_unmatched": report.gold_rows_unmatched}
    cohort = {"name": args.cohort, "split": args.split, "gold_revision": "sha256:" + gold_digest,
              "brand_population": "all"}
    m = metrics_mod.compute(metrics_mod.cases_from_routed(routed, gold), "gold", run_at=run_at,
                            cohort=cohort, sample={"size": args.sample_size, "seed": args.seed},
                            config={"threshold": args.threshold, "prices": dict(prices)},
                            coverage=coverage, operations=operations)
    return metrics_mod.to_json(m)


BRAND_COUNT_KEYS = ("brand_threads", "excluded_threads", "excluded_other_brand",
                    "excluded_unattributed")


def _select_brand(kept, threads, brand):
    """The split's threads attributed to `brand`, and the counts over the whole split."""
    known = set()
    for t in threads:
        known.update(t.brand_ids)
    labels = [brand_attribution(t, brand, known) for t in kept]
    selected = [t for t, label in zip(kept, labels) if label == "brand"]
    other = labels.count("other_brand")
    unattributed = labels.count("unattributed")
    counts = {"name": brand, "brand_threads": len(selected),
              "excluded_threads": other + unattributed, "excluded_other_brand": other,
              "excluded_unattributed": unattributed, "attribution": ATTRIBUTION}
    return selected, counts


def _with_brand(text: str, fmt: str, counts: dict) -> str:
    """Add the brand population to a rendered report."""
    if fmt == "json":
        data = json.loads(text)
        data["brand"] = counts
        return json.dumps(data, sort_keys=True, indent=2) + "\n"
    lines = ["", "## Brand population", "", "| field | value |", "| --- | --- |",
             "| brand | %s |" % counts["name"]]
    lines += ["| %s | %d |" % (k, counts[k]) for k in BRAND_COUNT_KEYS]
    lines += ["| attribution | %s |" % counts["attribution"]]
    return text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"


def _run(args) -> str:
    if args.sample_size < 1:
        raise _Fail("--sample-size must be at least 1, got %d" % args.sample_size)
    if not math.isfinite(args.threshold) or not (0.0 <= args.threshold <= 1.0):
        raise _Fail("--threshold must be in [0, 1], got %r" % args.threshold)
    prices = {"cheap": args.price_cheap, "larger": args.price_larger, "agent": args.price_agent}
    for name, price in prices.items():
        if not math.isfinite(price) or price < 0:
            raise _Fail("--price-%s must be a finite number >= 0, got %r" % (name, price))
    if args.metrics_out is not None and args.cohort is None:
        raise _Fail("--cohort is required with --metrics-out")
    if args.metrics_out is not None and args.brand is not None:
        raise _Fail("--metrics-out is not supported with --brand (metrics cohorts cover "
                    "brand_population \"all\" only)")
    run_at = _resolve_run_at(args.run_at)
    if args.trace is not None:
        check_trace_path(args.trace)
    try:
        label, effective = load_rules_source(args)
        topics = effective.topics
        rules_ver = effective.version if args.trace is not None else None
    except RulesError as e:
        raise _Fail("bad rules file: %s" % " ".join(str(e).split()))

    try:
        gold = load_gold(args.gold)
        with open(args.gold, "rb") as f:
            gold_digest = hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        raise _Fail("gold file not found: %s" % args.gold)
    except OSError as e:
        raise _Fail("cannot read gold file %s: %s" % (args.gold, e.strerror or e))
    except GoldFileError as e:
        raise _Fail("bad gold file %s: %s" % (args.gold, e))
    except UnicodeDecodeError as e:
        raise _Fail("gold file %s is not valid UTF-8 (byte offset %d)" % (args.gold, e.start))

    try:
        config = load_split_config(args.split_config)
    except SplitConfigError as e:
        raise _Fail("bad split config: %s" % e)

    try:
        threads, _ = build_threads(args.data)
    except FileNotFoundError:
        raise _Fail("data file not found: %s (hint: run scripts/download_data.sh)" % args.data)
    except OSError as e:
        raise _Fail("cannot read data file %s: %s" % (args.data, e.strerror or e))
    except HeaderError as e:
        raise _Fail("bad header in %s: %s" % (args.data, e))
    except UnicodeDecodeError as e:
        raise _Fail("%s is not valid UTF-8 (byte offset %d)" % (args.data, e.start))

    mapping, _ = split_threads(threads, config)
    kept = sorted((t for t in threads if mapping[t.root_tweet_id] == args.split),
                  key=lambda t: t.root_tweet_id)
    brand_counts = None
    if args.brand is not None:
        kept, brand_counts = _select_brand(kept, threads, args.brand)
        if not kept:
            raise _Fail("no threads in split %s are attributed to brand %s"
                        % (args.split, args.brand))
    sample = random.Random(args.seed).sample(kept, min(args.sample_size, len(kept)))

    routed = route_threads(sample, CheapTier(prices["cheap"]), FakeLargerTier(
        price_per_call=prices["larger"]), FakeAgentTier(prices["agent"]),
        threshold=args.threshold, topics=topics)
    report = score(routed, gold, prices, sample_size=args.sample_size, seed=args.seed,
                   split=args.split, threshold=args.threshold)
    text = report_to_json(report) if args.format == "json" else report_to_markdown(report)
    if brand_counts is not None:
        text = _with_brand(text, args.format, brand_counts)
    trace_records = None
    if args.trace is not None:
        commit, dirty = code_version(REPO_ROOT)
        # No drift config is applied, so its version is null.
        run = RunInfo(code_version=commit, code_dirty=dirty, rules_version=rules_ver,
                      drift_config_version=None, threshold=args.threshold, split=args.split,
                      seed=args.seed)
        trace_records = write_trace(args.trace, (build_record(r, run) for r in routed))
    try:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        raise _Fail("cannot write %s: %s" % (args.out, e.strerror or e))
    if args.metrics_out is not None:
        text = _metrics_text(args, routed, gold, report, prices, run_at, gold_digest)
        try:
            with open(args.metrics_out, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            raise _Fail("cannot write %s: %s" % (args.metrics_out, e.strerror or e))
    summary = ("routed=%d scored=%d without_gold=%d gold_unmatched=%d split=%s"
               % (report.threads_routed, report.threads_scored, report.threads_without_gold,
                  report.gold_rows_unmatched, args.split))
    summary += " topics_loaded=%d" % len(topics)
    summary += " brand=%s rules_loaded=%d repeat_contact=%s" % (
        label, len(effective.rules), "on" if effective.repeat_contact is not None else "off")
    if brand_counts is not None:
        summary += "".join(" %s=%d" % (k, brand_counts[k]) for k in BRAND_COUNT_KEYS)
    if trace_records is not None:
        summary += " trace_records=%d" % trace_records
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = _run(args)
    except (_Fail, TraceError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
