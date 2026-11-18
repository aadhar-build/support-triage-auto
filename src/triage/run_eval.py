"""Run the cascade on a seeded sample of one split and write an aggregate-only report.

Usage: python3 -m triage.run_eval --gold GOLD.csv --out REPORT [options]

No network calls and no keys. The only file written is --out, plus the audit trace when
--trace PATH is given (one JSON line per routed thread, outside the repository; it is
written before the report), plus gold-kind `triage.metrics` when --metrics-out PATH is given
(with a required --cohort; written last). Default prices are placeholders, not real prices.
The always-escalate topics come from --rules (default policies/rules.json), loaded before any
data; a missing or empty topic list exits 2. Stdout ends with topics_loaded=<n> (before
trace_records=<n> when a trace is written).

--larger real swaps the fake larger tier for ModelLargerTier (headless Claude Code on the
subscription login). It runs only on the dev split, or on the eval split behind
--post-merge-eval; --split full is always refused. Before any data is read it loads the
owner's price file (--prices), the monthly cap (--thresholds) and the spend log
(--spend-log, outside the repo), and refuses with exit 2 if any is missing or bad, or the
month is already at its cap. --max-spend-usd caps one run (at most MAX_RUN_SPEND_USD).
"""

from __future__ import annotations

import argparse
import hashlib
import math
import random
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from triage import metrics as metrics_mod
from triage.cheap_tier import CheapTier
from triage.evaluate import GoldFileError, load_gold, report_to_json, report_to_markdown, score
from triage.loader import HeaderError
from triage.model_prices import PricesError, UnknownBillError, load_prices
from triage.model_tier import MAX_RUN_SPEND_USD, PROMPT_VERSION, ClaudeCodeClient, \
    ModelLargerTier
from triage.policy_check import RulesError, load_escalate_topics, rules_version
from triage.router import DEFAULT_CONFIDENCE_THRESHOLD, route_threads
from triage.spend_log import SpendLog, SpendLogError, load_monthly_cap
from triage.split import DEFAULT_SPLIT_PATH, SPLIT_NAMES, SplitConfigError, load_split_config, \
    split_threads
from triage.threads import build_threads
from triage.tiers import PLACEHOLDER_PRICES, FakeAgentTier, FakeLargerTier
from triage.trace import REPO_ROOT, RunInfo, TraceError, build_record, check_trace_path, \
    code_version, write_trace

DEFAULT_DATA = "data/raw/twcs.csv"
DEFAULT_RULES_PATH = REPO_ROOT / "policies" / "rules.json"
DEFAULT_PRICES_PATH = REPO_ROOT / "policies" / "model_prices.json"
DEFAULT_THRESHOLDS_PATH = REPO_ROOT / "evals" / "thresholds.json"
DEFAULT_SPEND_LOG = "~/.local/state/support-triage/spend-log.jsonl"
DEFAULT_LARGER_MODEL = "claude-haiku-5-5"


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
    p.add_argument("--rules", default=str(DEFAULT_RULES_PATH),
                   help="rules file whose escalate_always topics force escalation")
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
    p.add_argument("--larger", choices=("fake", "real"), default="fake",
                   help="larger tier: fake stand-in (default) or a real model via Claude Code")
    p.add_argument("--max-spend-usd", type=float, default=MAX_RUN_SPEND_USD,
                   help="per-run cap in API-equivalent USD, at most %.2f" % MAX_RUN_SPEND_USD)
    p.add_argument("--larger-model", default=DEFAULT_LARGER_MODEL)
    p.add_argument("--prices", default=str(DEFAULT_PRICES_PATH),
                   help="owner price file (real mode only)")
    p.add_argument("--thresholds", default=str(DEFAULT_THRESHOLDS_PATH),
                   help="thresholds file holding the monthly cap (real mode only)")
    p.add_argument("--spend-log", default=DEFAULT_SPEND_LOG,
                   help="durable monthly spend log outside the repo (real mode only)")
    p.add_argument("--create-spend-log", action="store_true",
                   help="start a new spend log; refused when the file exists")
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--post-merge-eval", action="store_true",
                   help="opt in to one real eval-split scoring run after a prompt merge")
    return p


def _real_preflight(args, clock):
    """Refuse a real run before any data is read. Returns (prices, spend_log, cap)."""
    if args.split == "full":
        raise _Fail("--larger real never runs on the full split")
    if args.split == "eval" and not args.post_merge_eval:
        raise _Fail("--larger real on the eval split needs --post-merge-eval")
    if args.post_merge_eval and args.split != "eval":
        raise _Fail("--post-merge-eval only applies to --split eval")
    try:
        prices = load_prices(args.prices)
        prices.band(args.larger_model, 0)
    except PricesError as e:
        raise _Fail("bad prices file: %s" % " ".join(str(e).split()))
    except UnknownBillError:
        raise _Fail("prices file %s has no price for model %s" % (args.prices,
                                                                  args.larger_model))
    try:
        cap = load_monthly_cap(args.thresholds)
        path = os.path.expanduser(args.spend_log)
        if args.create_spend_log:
            SpendLog(path, cap)  # refuses a path inside the repo before anything is made
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, mode=0o700, exist_ok=True)
            SpendLog.create(path)
        log = SpendLog(path, cap, clock=clock)
        mtd = log.month_to_date()
    except SpendLogError as e:
        raise _Fail(" ".join(str(e).split()))
    except OSError as e:
        raise _Fail("cannot prepare spend log: %s" % (e.strerror or e))
    if mtd >= cap:
        raise _Fail("monthly cap reached: month to date %.4f of %.4f USD" % (mtd, cap))
    return prices, log, cap


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


def _run(args, model_client=None, clock=None) -> str:
    if args.sample_size < 1:
        raise _Fail("--sample-size must be at least 1, got %d" % args.sample_size)
    if not math.isfinite(args.threshold) or not (0.0 <= args.threshold <= 1.0):
        raise _Fail("--threshold must be in [0, 1], got %r" % args.threshold)
    prices = {"cheap": args.price_cheap, "larger": args.price_larger, "agent": args.price_agent}
    for name, price in prices.items():
        if not math.isfinite(price) or price < 0:
            raise _Fail("--price-%s must be a finite number >= 0, got %r" % (name, price))
    if not math.isfinite(args.max_spend_usd) or not 0 <= args.max_spend_usd <= MAX_RUN_SPEND_USD:
        raise _Fail("--max-spend-usd must be a finite number in [0, %.2f], got %r"
                    % (MAX_RUN_SPEND_USD, args.max_spend_usd))
    if args.post_merge_eval and args.larger != "real":
        raise _Fail("--post-merge-eval needs --larger real")
    if args.metrics_out is not None and args.cohort is None:
        raise _Fail("--cohort is required with --metrics-out")
    run_at = _resolve_run_at(args.run_at)
    if args.trace is not None:
        check_trace_path(args.trace)
    try:
        topics = load_escalate_topics(args.rules)
        rules_ver = rules_version(args.rules) if args.trace is not None else None
    except RulesError as e:
        raise _Fail("bad rules file: %s" % " ".join(str(e).split()))
    real = args.larger == "real"
    if real:
        model_prices, spend_log, monthly_cap = _real_preflight(args, clock)

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
    sample = random.Random(args.seed).sample(kept, min(args.sample_size, len(kept)))

    cheap = CheapTier(prices["cheap"])
    if real:
        client = model_client if model_client is not None else ClaudeCodeClient(
            args.larger_model, claude_bin=args.claude_bin)
        larger = ModelLargerTier(client, cheap, model_prices, spend_log, args.larger_model,
                                 run_id=uuid.uuid4().hex, split=args.split,
                                 max_spend_usd=args.max_spend_usd,
                                 price_per_call=prices["larger"])
    else:
        larger = FakeLargerTier(price_per_call=prices["larger"])
    routed = route_threads(sample, cheap, larger, FakeAgentTier(prices["agent"]),
                           threshold=args.threshold, topics=topics)
    usage = {"larger_mode": args.larger, "larger_model": larger.model_id}
    if real:
        try:
            mtd = spend_log.month_to_date()
        except SpendLogError:
            mtd = None  # the run already happened; the report says the figure is unknown
        usage.update(
            larger_prompt_version=PROMPT_VERSION, larger_post_merge_eval=args.post_merge_eval,
            larger_input_tokens=larger.input_tokens, larger_output_tokens=larger.output_tokens,
            larger_cache_write_tokens=larger.cache_write_tokens,
            larger_cache_read_tokens=larger.cache_read_tokens,
            larger_confirmed_usd=larger.confirmed_usd,
            larger_unconfirmed_usd=larger.unconfirmed_usd, max_spend_usd=args.max_spend_usd,
            monthly_cap_usd=monthly_cap, month_to_date_usd=mtd,
            larger_stopped_by=larger.stopped_by, larger_answered=larger.answered,
            larger_reservation_exceeded=larger.reservation_exceeded,
            larger_fallbacks=dict(larger.fallbacks))
    report = score(routed, gold, prices, sample_size=args.sample_size, seed=args.seed,
                   split=args.split, threshold=args.threshold, larger_usage=usage)
    text = report_to_json(report) if args.format == "json" else report_to_markdown(report)
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
    mtd = report.month_to_date_usd
    larger_part = (" larger=%s spend_usd=%.4f unconfirmed_usd=%.4f month_to_date_usd=%s "
                   "stopped_by=%s" % (args.larger, report.larger_confirmed_usd,
                                      report.larger_unconfirmed_usd,
                                      "n/a" if mtd is None else "%.4f" % mtd,
                                      report.larger_stopped_by or "none"))
    summary = summary.replace(" split=", larger_part + " split=", 1)
    summary += " topics_loaded=%d" % len(topics)
    if trace_records is not None:
        summary += " trace_records=%d" % trace_records
    return summary


def main(argv: Optional[List[str]] = None, *, model_client=None, clock=None) -> int:
    """`model_client` replaces ClaudeCodeClient and `clock` goes to SpendLog: test seams."""
    args = _parser().parse_args(argv)
    try:
        summary = _run(args, model_client=model_client, clock=clock)
    except (_Fail, TraceError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
