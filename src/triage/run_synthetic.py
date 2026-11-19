"""Run made-up conversations through the cascade and check them against expected labels.

Usage: python3 -m triage.run_synthetic --cases CASES.json --out DIR
                                       [--rules RULES.json | --brand NAME [--brands-dir DIR]]
                                       [--run-at TIME] [--trace PATH]

Writes DIR/synthetic_report.json, DIR/synthetic_report.md and DIR/metrics.json (synthetic-kind
`triage.metrics`, stamped with --run-at, default now in UTC). Reports and stdout hold only
case ids, titles, labels, tier names, confidences, rule ids and counts, never turn, draft or
brand text. No network calls. Stdout and the reports' summary give rules_loaded, the number of
policy rules read from --rules, and topics_loaded, the number of always-escalate topics read
from the same file (a missing or empty topic list is bad input). Each case records the ids of
the topics that forced it to escalate in topic_id.
Cases with a made-up `customer` (plus `brand` and `started_at`) are counted per customer and
brand; when any case has one, the `repeat_contact` rule is read from --rules (bad input when
missing) and the reports' summary gives the trigger counts at its window and report window.
Customer values never leave memory.
--brand NAME takes the rules from the rules block of brands/NAME/guidelines.md instead of
--rules, applies them to every case (no case is filtered), and adds a top-level "brand" key
to the JSON report and a "Brand: NAME" line to the Markdown report. Stdout names the brand
(none for --rules) and whether the rules have a repeat_contact block. Exit 0 when every must_pass case passes, 1 when one fails, 2 on
bad input.
With --trace PATH it also writes one audit trace record per case (split "synthetic",
thread_id = 1-based case position) to PATH outside the repository, before any report; if the
trace can't be written, no report is written either.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from triage import metrics as metrics_mod
from triage.cheap_tier import CheapTier
from triage.brand_rules import add_rules_args, load_rules_source, repeat_contact_token, \
    rules_path_of
from triage.context_signals import ContactRecord, contact_counts
from triage.policy_check import RulesError, check
from triage.router import DEFAULT_CONFIDENCE_THRESHOLD, route_threads
from triage.threads import Thread, Turn
from triage.tiers import ACTIONS, INTENTS, FakeAgentTier, FakeLargerTier
from triage.trace import REPO_ROOT, RunInfo, TraceError, build_record, check_trace_path, \
    code_version, write_trace

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "policies" / "rules.json"
JSON_NAME = "synthetic_report.json"
MD_NAME = "synthetic_report.md"
METRICS_NAME = "metrics.json"

# Made-up values for the in-memory threads. The cheap tier reads only inbound turn text.
SYNTHETIC_AUTHOR = "synthetic_customer"
SYNTHETIC_TIME = datetime(2000, 1, 1, tzinfo=timezone.utc)

SUMMARY_KEYS = ("cases_run", "passed", "failed", "must_pass_cases", "must_pass_failures",
                "missed_escalations", "false_escalations", "policy_violations",
                "policy_mismatches", "rules_loaded", "topics_loaded",
                "repeat_contact_rule_loaded", "repeat_contact_triggers",
                "repeat_contact_triggers_report_window")


class _Fail(Exception):
    pass


class CasesError(_Fail):
    """A cases file failed validation. The message is one line."""


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m triage.run_synthetic",
                                description=__doc__.splitlines()[0])
    p.add_argument("--cases", required=True)
    p.add_argument("--out", required=True)
    add_rules_args(p, DEFAULT_RULES_PATH)
    p.add_argument("--run-at", default=None,
                   help="ISO 8601 UTC time ending in Z for metrics.json (default: now)")
    p.add_argument("--trace", default=None,
                   help="write an audit trace (JSON lines) to this path outside the repo")
    return p


def resolve_run_at(value: Optional[str]) -> str:
    """The given --run-at, checked, or the current UTC time to the second."""
    if value is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        metrics_mod.check_run_at(value)
    except ValueError as e:
        raise _Fail("bad --run-at: %s" % e)
    return value


# ---------------------------------------------------------------- loading and validation

def load_cases(path) -> list:
    """Load and validate a made-up cases file. Raises CasesError with a one-line message."""
    def fail(problem):
        return CasesError("bad cases file %s: %s" % (path, problem))

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raise CasesError("cases file not found: %s" % path)
    except OSError as e:
        raise CasesError("cannot read cases file %s: %s" % (path, e.strerror or e))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise CasesError("cases file %s is not valid UTF-8 (byte offset %d)" % (path, e.start))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise fail("not valid JSON (line %d, column %d)" % (e.lineno, e.colno))
    if not isinstance(data, dict):
        raise fail("top level must be a JSON object")
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise fail("'cases' is missing or is not a list")

    seen = set()
    for index, c in enumerate(cases):
        _check_case(index, c, seen, fail)
    return cases


def _check_case(index, c, seen, fail) -> None:
    if not isinstance(c, dict):
        raise fail("case at index %d is not an object" % index)
    case_id = c.get("id")
    where = "case at index %d" % index
    if not isinstance(case_id, str) or not case_id:
        raise fail("%s: 'id' is missing or is not a non-empty string" % where)
    where = "case at index %d (id %r)" % (index, case_id)
    if case_id in seen:
        raise fail("%s: duplicate 'id'" % where)
    seen.add(case_id)

    title = c.get("title")
    if not isinstance(title, str) or not title:
        raise fail("%s: 'title' is missing or is not a non-empty string" % where)

    turns = c.get("turns")
    if not isinstance(turns, list) or not turns:
        raise fail("%s: 'turns' is missing or is not a non-empty list" % where)
    for t_index, turn in enumerate(turns):
        if not isinstance(turn, dict) or not isinstance(turn.get("inbound"), bool) \
                or not isinstance(turn.get("text"), str):
            raise fail("%s: turns[%d] needs a bool 'inbound' and a string 'text'"
                       % (where, t_index))

    expected = c.get("expected")
    if not isinstance(expected, dict):
        raise fail("%s: 'expected' is missing or is not an object" % where)
    if expected.get("intent") not in INTENTS:
        raise fail("%s: 'expected.intent' must be one of %s" % (where, ", ".join(INTENTS)))
    if expected.get("action") not in ACTIONS:
        raise fail("%s: 'expected.action' must be one of %s" % (where, ", ".join(ACTIONS)))
    if not isinstance(expected.get("should_escalate"), bool):
        raise fail("%s: 'expected.should_escalate' is missing or is not a bool" % where)

    if not isinstance(c.get("must_pass"), bool):
        raise fail("%s: 'must_pass' is missing or is not a bool" % where)
    if "brand" in c and not isinstance(c["brand"], str):
        raise fail("%s: 'brand' must be a string" % where)
    if "customer" in c:
        if not isinstance(c["customer"], str) or not c["customer"]:
            raise fail("%s: 'customer' must be a non-empty string" % where)
        if not c.get("brand"):
            raise fail("%s: 'customer' is given without a non-empty 'brand'" % where)
        if "started_at" not in c:
            raise fail("%s: 'customer' is given without 'started_at'" % where)
    if "started_at" in c and parse_started_at(c["started_at"]) is None:
        raise fail("%s: 'started_at' must be an ISO 8601 timestamp with a timezone" % where)
    if "tags" in c and not (isinstance(c["tags"], list)
                            and all(isinstance(t, str) for t in c["tags"])):
        raise fail("%s: 'tags' must be a list of strings" % where)
    if "draft_reply" in c and not isinstance(c["draft_reply"], str):
        raise fail("%s: 'draft_reply' must be a string" % where)
    if "expected_violations" in c:
        ev = c["expected_violations"]
        if not (isinstance(ev, list) and all(isinstance(v, str) for v in ev)):
            raise fail("%s: 'expected_violations' must be a list of strings" % where)
        if "draft_reply" not in c:
            raise fail("%s: 'expected_violations' is given without 'draft_reply'" % where)


def parse_started_at(value) -> Optional[datetime]:
    """A case's started_at as an aware UTC datetime, or None when it is not a valid ISO 8601
    timestamp with a timezone. A trailing Z means UTC."""
    if not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------- running

def _thread(position: int, c: dict) -> Thread:
    # Inbound authors stay in memory: never written to a report, trace or stdout.
    customer = c.get("customer") or "%s_%d" % (SYNTHETIC_AUTHOR, position)
    brand = c.get("brand") or SYNTHETIC_AUTHOR
    start = parse_started_at(c["started_at"]) if "started_at" in c else None

    def at(i):  # one minute apart from started_at; SYNTHETIC_TIME for every turn otherwise
        return SYNTHETIC_TIME if start is None else start + timedelta(minutes=i - 1)

    turns = tuple(
        Turn(tweet_id=position * 1000 + i, author_id=customer if t["inbound"] else brand,
             inbound=t["inbound"], created_at=at(i), text=t["text"])
        for i, t in enumerate(c["turns"], start=1))
    return Thread(root_tweet_id=position, brand_ids=(), turns=turns,
                  brand_replied=any(not t.inbound for t in turns))


def _records(cases: list, threads: list) -> list:
    """A ContactRecord for each case with `customer`; its brand stands in for the @mention."""
    records = []
    for c, t in zip(cases, threads):
        if "customer" not in c:
            continue
        brand = c["brand"]
        records.append(ContactRecord(
            root_tweet_id=t.root_tweet_id, customer=c["customer"], start=t.turns[0].created_at,
            brands_at_start=frozenset({brand}),
            brand_replies=tuple((brand, x.created_at) for x in t.turns if not x.inbound)))
    return records


def _route(cases: list, topics, rule, *, threshold=DEFAULT_CONFIDENCE_THRESHOLD,
           tiers=None) -> tuple:
    """Route every case. Returns (routed, triggers at window_days, triggers at the report
    window); both counts are 0 when rule is None. `tiers` is (cheap, larger, agent),
    default CheapTier(), FakeLargerTier(), FakeAgentTier()."""
    threads = [_thread(i, c) for i, c in enumerate(cases, start=1)]
    counts = None
    triggers = triggers_report = 0
    if rule is not None:
        records = _records(cases, threads)
        counts = contact_counts(records, timedelta(days=rule.window_days))
        report = contact_counts(records, timedelta(hours=rule.report_window_hours))
        triggers = sum(n >= rule.min_conversations for n in counts.values())
        triggers_report = sum(n >= rule.min_conversations for n in report.values())
    cheap, larger, agent = tiers if tiers is not None else (
        CheapTier(), FakeLargerTier(), FakeAgentTier())
    routed = route_threads(threads, cheap, larger, agent, threshold=threshold, topics=topics,
                           repeat_rule=rule, repeat_counts=counts)
    return routed, triggers, triggers_report


def _results(cases: list, rules, routed: list) -> list:
    results = []
    for c, r in zip(cases, routed):
        exp = c["expected"]
        expected = {"intent": exp["intent"], "action": exp["action"],
                    "escalate": exp["should_escalate"]}
        actual = {"intent": r.labels.intent, "action": r.labels.action,
                  "escalate": r.labels.action == "escalate"}
        expected_violations = actual_violations = None
        if "draft_reply" in c:
            customer_turns = [t["text"] for t in c["turns"] if t["inbound"]]
            found = check(c["draft_reply"], customer_turns, rules)
            actual_violations = sorted({v.rule_id for v in found})
            expected_violations = sorted(set(c.get("expected_violations", [])))
        passed = expected == actual and expected_violations == actual_violations
        results.append({
            "id": c["id"], "title": c["title"], "must_pass": c["must_pass"],
            "passed": passed, "expected": expected, "actual": actual,
            "expected_violations": expected_violations,
            "actual_violations": actual_violations,
            "tiers": list(r.tiers), "confidence": r.labels.confidence,
            "reason_codes": list(r.reason_codes), "topic_id": list(r.topic_id),
        })
    return results


def _route_and_score(cases: list, effective, threshold, tiers) -> tuple:
    """(routed, results, triggers, triggers_report) under one rule set and threshold."""
    rule = effective.repeat_contact if any("customer" in c for c in cases) else None
    routed, triggers, triggers_report = _route(cases, effective.topics, rule,
                                               threshold=threshold, tiers=tiers)
    return routed, _results(cases, effective.rules, routed), triggers, triggers_report


def run_cases(cases: list, effective, *, threshold=DEFAULT_CONFIDENCE_THRESHOLD,
              tiers=None) -> tuple:
    """Route validated cases under `effective` (a brand_rules.EffectiveRules) and score them.
    Returns (routed, results). The repeat_contact rule is used when any case has
    `customer`; `tiers` is (cheap, larger, agent), default the offline fakes."""
    routed, results, _, _ = _route_and_score(cases, effective, threshold, tiers)
    return routed, results


def _summary(results: list, rules_loaded: int, topics_loaded: int, rule_loaded: int = 0,
             triggers: int = 0, triggers_report: int = 0) -> dict:
    drafts = [r for r in results if r["actual_violations"] is not None]
    return {
        "cases_run": len(results),
        "passed": sum(r["passed"] for r in results),
        "failed": sum(not r["passed"] for r in results),
        "must_pass_cases": sum(r["must_pass"] for r in results),
        "must_pass_failures": sum(r["must_pass"] and not r["passed"] for r in results),
        "missed_escalations": sum(r["expected"]["escalate"] and r["actual"]["action"] != "escalate"
                                  for r in results),
        "false_escalations": sum(not r["expected"]["escalate"]
                                 and r["actual"]["action"] == "escalate" for r in results),
        "policy_violations": sum(len(r["actual_violations"]) for r in drafts),
        "policy_mismatches": sum(r["expected_violations"] != r["actual_violations"]
                                 for r in drafts),
        "rules_loaded": rules_loaded,
        "topics_loaded": topics_loaded,
        "repeat_contact_rule_loaded": rule_loaded,
        "repeat_contact_triggers": triggers,
        "repeat_contact_triggers_report_window": triggers_report,
    }


# ---------------------------------------------------------------- reports

def _md_cell(value) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _md_violations(v) -> str:
    if v is None:
        return "n/a"
    return ", ".join(v) if v else "none"


def report_to_markdown(results: list, summary: dict, brand: Optional[str] = None) -> str:
    lines = ["# Synthetic regression report", ""]
    if brand is not None:
        lines += ["Brand: %s" % brand, ""]
    lines += [
             "| id | title | must_pass | expected intent | expected action | expected escalate"
             " | actual intent | actual action | actual escalate | violations expected"
             " | violations actual | result |",
             "| --- " * 12 + "|"]
    for r in results:
        e, a = r["expected"], r["actual"]
        cells = [r["id"], r["title"], str(r["must_pass"]).lower(), e["intent"], e["action"],
                 str(e["escalate"]).lower(), a["intent"], a["action"],
                 str(a["escalate"]).lower(), _md_violations(r["expected_violations"]),
                 _md_violations(r["actual_violations"]), "pass" if r["passed"] else "FAIL"]
        lines.append("| " + " | ".join(_md_cell(x) for x in cells) + " |")
    lines += ["", "## Summary", "", "| count | value |", "| --- | --- |"]
    lines += ["| %s | %d |" % (k, summary[k]) for k in SUMMARY_KEYS]
    return "\n".join(lines) + "\n"


def _write(out_dir: Path, results: list, summary: dict, metrics_text: str,
           brand: Optional[str] = None) -> None:
    report = {"cases": results, "summary": summary}
    if brand is not None:
        report["brand"] = brand
    json_text = json.dumps(report, sort_keys=True, indent=2) + "\n"
    md_text = report_to_markdown(results, summary, brand)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / JSON_NAME).write_text(json_text, encoding="utf-8")
        (out_dir / MD_NAME).write_text(md_text, encoding="utf-8")
        (out_dir / METRICS_NAME).write_text(metrics_text, encoding="utf-8")
    except OSError as e:
        raise _Fail("cannot write reports to %s: %s" % (out_dir, e.strerror or e))


def _metrics_text(cases_path, results: list, run_at: str) -> str:
    try:
        with open(cases_path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
    except OSError as e:
        raise _Fail("cannot read cases file %s: %s" % (cases_path, e.strerror or e))
    cohort = {"name": "synthetic", "split": None, "gold_revision": "sha256:" + digest,
              "brand_population": "all"}
    m = metrics_mod.compute(metrics_mod.cases_from_synthetic(results), "synthetic",
                            run_at=run_at, cohort=cohort, sample={"size": None, "seed": None},
                            config={"threshold": DEFAULT_CONFIDENCE_THRESHOLD, "prices": None})
    return metrics_mod.to_json(m)


def _trace(path, routed: list, rules_ver: Optional[str]) -> int:
    commit, dirty = code_version(REPO_ROOT)
    # Synthetic runs sample nothing (seed 0) and use no drift config.
    run = RunInfo(code_version=commit, code_dirty=dirty, rules_version=rules_ver,
                  drift_config_version=None, threshold=DEFAULT_CONFIDENCE_THRESHOLD,
                  split="synthetic", seed=0)
    return write_trace(path, (build_record(r, run) for r in routed))


def _run(args) -> tuple:
    if args.trace is not None:
        check_trace_path(args.trace)
    run_at = resolve_run_at(args.run_at)
    cases = load_cases(args.cases)
    try:
        label, effective = load_rules_source(args)
    except RulesError as e:
        raise _Fail("bad rules file: %s" % " ".join(str(e).split()))
    rules, topics = effective.rules, effective.topics
    rules_ver = effective.version if args.trace is not None else None
    rule = None
    if any("customer" in c for c in cases):
        rule = effective.repeat_contact
        if rule is None:
            raise _Fail("bad repeat_contact rule: repeat_contact rule in %s: "
                        "'repeat_contact' is missing or is not an object" % rules_path_of(args))
    routed, results, triggers, triggers_report = _route_and_score(
        cases, effective, DEFAULT_CONFIDENCE_THRESHOLD, None)
    summary = _summary(results, len(rules), len(topics), int(rule is not None), triggers,
                       triggers_report)
    metrics_text = _metrics_text(args.cases, results, run_at)
    trace_records = None
    if args.trace is not None:
        trace_records = _trace(args.trace, routed, rules_ver)
    _write(Path(args.out), results, summary, metrics_text, args.brand)
    return summary, trace_records, label, effective


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary, trace_records, label, effective = _run(args)
    except (_Fail, TraceError) as e:
        print("error: %s" % " ".join(str(e).split()), file=sys.stderr)
        return 2
    line = ("cases=%d passed=%d failed=%d must_pass_failures=%d rules_loaded=%d topics_loaded=%d"
            % (summary["cases_run"], summary["passed"], summary["failed"],
               summary["must_pass_failures"], summary["rules_loaded"], summary["topics_loaded"]))
    line += " brand=%s %s" % (label, repeat_contact_token(effective))
    if trace_records is not None:
        line += " trace_records=%d" % trace_records
    print(line)
    return 1 if summary["must_pass_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
