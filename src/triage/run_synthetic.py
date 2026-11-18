"""Run made-up conversations through the cascade and check them against expected labels.

Usage: python3 -m triage.run_synthetic --cases CASES.json --out DIR [--rules RULES.json]
                                       [--run-at TIME] [--trace PATH]

Writes DIR/synthetic_report.json, DIR/synthetic_report.md and DIR/metrics.json (synthetic-kind
`triage.metrics`, stamped with --run-at, default now in UTC). Reports and stdout hold only
case ids, titles, labels, tier names, confidences, rule ids and counts, never turn, draft or
brand text. No network calls. Stdout and the reports' summary give rules_loaded, the number of
policy rules read from --rules, and topics_loaded, the number of always-escalate topics read
from the same file (a missing or empty topic list is bad input). Each case records the ids of
the topics that forced it to escalate in topic_id. Exit 0 when every must_pass case passes, 1 when one fails, 2 on
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
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from triage import metrics as metrics_mod
from triage.cheap_tier import CheapTier
from triage.policy_check import RulesError, check, load_escalate_topics, load_rules, \
    rules_version
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
                "policy_mismatches", "rules_loaded", "topics_loaded")


class _Fail(Exception):
    pass


class CasesError(_Fail):
    """A cases file failed validation. The message is one line."""


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m triage.run_synthetic",
                                description=__doc__.splitlines()[0])
    p.add_argument("--cases", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--rules", default=str(DEFAULT_RULES_PATH))
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


# ---------------------------------------------------------------- running

def _thread(position: int, c: dict) -> Thread:
    turns = tuple(
        Turn(tweet_id=position * 1000 + i, author_id=SYNTHETIC_AUTHOR,
             inbound=t["inbound"], created_at=SYNTHETIC_TIME, text=t["text"])
        for i, t in enumerate(c["turns"], start=1))
    return Thread(root_tweet_id=position, brand_ids=(), turns=turns,
                  brand_replied=any(not t.inbound for t in turns))


def _route(cases: list, topics) -> list:
    threads = [_thread(i, c) for i, c in enumerate(cases, start=1)]
    return route_threads(threads, CheapTier(), FakeLargerTier(), FakeAgentTier(),
                         threshold=DEFAULT_CONFIDENCE_THRESHOLD, topics=topics)


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


def _summary(results: list, rules_loaded: int, topics_loaded: int) -> dict:
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
    }


# ---------------------------------------------------------------- reports

def _md_cell(value) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _md_violations(v) -> str:
    if v is None:
        return "n/a"
    return ", ".join(v) if v else "none"


def report_to_markdown(results: list, summary: dict) -> str:
    lines = ["# Synthetic regression report", "",
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


def _write(out_dir: Path, results: list, summary: dict, metrics_text: str) -> None:
    json_text = json.dumps({"cases": results, "summary": summary},
                           sort_keys=True, indent=2) + "\n"
    md_text = report_to_markdown(results, summary)
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
        rules = load_rules(args.rules)
        topics = load_escalate_topics(args.rules)
        rules_ver = rules_version(args.rules) if args.trace is not None else None
    except RulesError as e:
        raise _Fail("bad rules file: %s" % " ".join(str(e).split()))
    routed = _route(cases, topics)
    results = _results(cases, rules, routed)
    summary = _summary(results, len(rules), len(topics))
    metrics_text = _metrics_text(args.cases, results, run_at)
    trace_records = None
    if args.trace is not None:
        trace_records = _trace(args.trace, routed, rules_ver)
    _write(Path(args.out), results, summary, metrics_text)
    return summary, trace_records


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary, trace_records = _run(args)
    except (_Fail, TraceError) as e:
        print("error: %s" % " ".join(str(e).split()), file=sys.stderr)
        return 2
    line = ("cases=%d passed=%d failed=%d must_pass_failures=%d rules_loaded=%d topics_loaded=%d"
            % (summary["cases_run"], summary["passed"], summary["failed"],
               summary["must_pass_failures"], summary["rules_loaded"], summary["topics_loaded"]))
    if trace_records is not None:
        line += " trace_records=%d" % trace_records
    print(line)
    return 1 if summary["must_pass_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
