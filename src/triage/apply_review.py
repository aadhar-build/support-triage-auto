"""Apply a reviewer's decisions file to a synthetic run.

Usage: python3 -m triage.apply_review --run DIR --decisions DECISIONS.json --cases CASES.json
       [--rules RULES.json | --brand NAME [--brands-dir DIR]]

Checks the decisions against DIR/synthetic_report.json and the made-up cases file the run
came from, and merges them by case id into
DIR/review_applied.json, so applying the same file twice gives the same bytes and a later
file resumes the review. Prints approved, edited, rejected and undecided counts and each
changed action; never prints draft or reason text. This command enforces the review rules:
a reject needs a reason and an edit needs a draft. A case with a policy violation can't be
approved at all: it needs an edit or a reject. Every edited draft must pass policy_check
(the --rules file, default the same as run_synthetic) with zero violations. Approving a case
flagged only because its action is escalate needs a reason. The rules are checked over the
merged result, so an older review_applied.json entry that breaks them is refused too. Exit 0
on success, 2 on bad input, leaving any existing output untouched.
--brand NAME checks drafts against the rules block of brands/NAME/guidelines.md instead of
--rules. The last stdout line names the brand (none for --rules), the rules and topics loaded
and whether the rules have a repeat_contact block.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from triage.brand_rules import add_rules_args, load_rules_source, loaded_fields
from triage.policy_check import RulesError, check
from triage.review_page import ReportError, has_violation, is_flagged, load_run
from triage.run_synthetic import DEFAULT_RULES_PATH, CasesError, load_cases
from triage.tiers import ACTIONS

APPLIED_NAME = "review_applied.json"
DECISIONS = ("approve", "edit", "reject")
DECISION_KEYS = ("decision", "draft", "reason", "action")
APPLIED_KEYS = ("decision", "original_action", "action", "draft", "reason")


class ApplyError(Exception):
    """The decisions can't be applied. The message is one line."""


def _read_json(path, what):
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        raise ApplyError("%s not found: %s" % (what, path))
    except OSError as e:
        raise ApplyError("cannot read %s %s: %s" % (what, path, e.strerror or e))
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ApplyError("%s %s is not valid UTF-8" % (what, path))
    except json.JSONDecodeError as e:
        raise ApplyError("%s %s is not valid JSON (line %d, column %d)"
                         % (what, path, e.lineno, e.colno))


def _filled(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _entry(case_id, d, report_case) -> dict:
    """Check one decision against its report case and return the review_applied entry."""
    where = "decision for case %r" % case_id
    if not isinstance(d, dict):
        raise ApplyError("%s is not an object" % where)
    unknown = sorted(set(d) - set(DECISION_KEYS))
    if unknown:
        raise ApplyError("%s has unknown keys: %s" % (where, ", ".join(unknown)))
    decision = d.get("decision")
    if decision not in DECISIONS:
        raise ApplyError("%s: 'decision' must be one of %s" % (where, ", ".join(DECISIONS)))
    for key in ("draft", "reason"):
        if key in d and not isinstance(d[key], str):
            raise ApplyError("%s: '%s' must be a string" % (where, key))
    if "action" in d and d["action"] not in ACTIONS:
        raise ApplyError("%s: 'action' must be one of %s" % (where, ", ".join(ACTIONS)))
    if decision == "edit" and not _filled(d.get("draft")):
        raise ApplyError("%s: an edit needs a non-empty 'draft'" % where)
    if decision != "edit" and "draft" in d:
        raise ApplyError("%s: 'draft' is only allowed with an edit" % where)
    if decision == "reject" and not _filled(d.get("reason")):
        raise ApplyError("%s: a reject needs a non-empty 'reason'" % where)

    original = report_case["actual"]["action"]
    entry = {"decision": decision, "original_action": original,
             "action": d.get("action", original)}
    if decision == "edit":
        entry["draft"] = d["draft"]
    if _filled(d.get("reason")):
        entry["reason"] = d["reason"]
    return entry


def _new_entries(path, report_cases, run_id) -> dict:
    data = _read_json(path, "decisions file")
    if not isinstance(data, dict) or set(data) != {"run_id", "decisions"}:
        raise ApplyError("decisions file %s must be an object with exactly the keys run_id "
                         "and decisions" % path)
    if data["run_id"] != run_id:
        raise ApplyError("decisions file %s is for another run (run_id does not match)" % path)
    if not isinstance(data["decisions"], dict):
        raise ApplyError("decisions file %s: 'decisions' is not an object" % path)
    by_id = {r["id"]: r for r in report_cases}
    entries = {}
    for case_id, d in data["decisions"].items():
        if case_id not in by_id:
            raise ApplyError("decisions file %s names a case id that is not in the run"
                             % path)
        entries[case_id] = _entry(case_id, d, by_id[case_id])
    return entries


def _previous_entries(path: Path, report_cases, run_id) -> dict:
    if not path.exists():
        return {}
    data = _read_json(path, "existing review file")
    if not isinstance(data, dict) or not isinstance(data.get("cases"), dict):
        raise ApplyError("existing review file %s is malformed" % path)
    if data.get("run_id") != run_id:
        raise ApplyError("existing review file %s is for another run; move it aside first"
                         % path)
    by_id = {r["id"]: r for r in report_cases}
    if not set(data["cases"]) <= set(by_id):
        raise ApplyError("existing review file %s names a case id that is not in the run"
                         % path)
    for case_id, entry in data["cases"].items():
        if not _valid_previous(entry, by_id[case_id]["actual"]["action"]):
            raise ApplyError("existing review file %s has a malformed entry; move it aside "
                             "first" % path)
    return data["cases"]


def _valid_previous(e, original_action) -> bool:
    """True when e has the shape _entry writes for a case whose system action is given."""
    if not isinstance(e, dict) or not set(e) <= set(APPLIED_KEYS):
        return False
    decision = e.get("decision")
    return (decision in DECISIONS
            and e.get("original_action") == original_action
            and e.get("action") in ACTIONS
            and (_filled(e.get("draft")) if decision == "edit" else "draft" not in e)
            and ("reason" not in e or _filled(e["reason"]))
            and (decision != "reject" or "reason" in e))


def _check_rules(case_id, entry, report_case, customer_turns, rules) -> None:
    """Enforce the review rules on one merged entry. Errors name rule ids, never text."""
    where = "decision for case %r" % case_id
    if entry["decision"] == "approve":
        if has_violation(report_case):
            raise ApplyError("%s: a case with a policy violation cannot be approved; edit "
                             "the draft or reject it" % where)
        if is_flagged(report_case) and not _filled(entry.get("reason")):
            raise ApplyError("%s: approving a flagged case needs a non-empty 'reason'"
                             % where)
    if entry["decision"] == "edit":
        found = sorted({v.rule_id for v in check(entry["draft"], customer_turns, rules)})
        if found:
            raise ApplyError("%s: the edited draft breaks policy rules: %s"
                             % (where, ", ".join(found)))


def _review(report_cases, run_id, cases: dict) -> dict:
    decided = [c["decision"] for c in cases.values()]
    changed = [{"id": i, "from": e["original_action"], "to": e["action"]}
               for i, e in sorted(cases.items()) if e["action"] != e["original_action"]]
    return {
        "run_id": run_id,
        "cases": cases,
        "summary": {"approved": decided.count("approve"), "edited": decided.count("edit"),
                    "rejected": decided.count("reject"),
                    "undecided": sum(r["id"] not in cases for r in report_cases)},
        "actions_changed": changed,
    }


def _write_atomic(path: Path, text: str) -> None:
    try:
        fd, tmp = tempfile.mkstemp(prefix=".review_applied.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, str(path))
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except OSError as e:
        raise ApplyError("cannot write %s: %s" % (path, e.strerror or e))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m triage.apply_review",
                                description=__doc__.splitlines()[0])
    p.add_argument("--run", required=True)
    p.add_argument("--decisions", required=True)
    p.add_argument("--cases", required=True)
    add_rules_args(p, DEFAULT_RULES_PATH)
    return p


def _run(args) -> dict:
    source_cases = load_cases(args.cases)
    report_cases, run_id = load_run(args.run)
    if [c["id"] for c in source_cases] != [r["id"] for r in report_cases]:
        raise ApplyError("the run's case ids do not match the cases file %s" % args.cases)
    try:
        label, effective = load_rules_source(args)
    except RulesError as e:
        raise ApplyError("bad rules file: %s" % e)
    rules = effective.rules
    new = _new_entries(args.decisions, report_cases, run_id)
    out = Path(args.run) / APPLIED_NAME
    cases = dict(_previous_entries(out, report_cases, run_id))
    cases.update(new)
    by_id = {r["id"]: r for r in report_cases}
    turns = {c["id"]: [t["text"] for t in c["turns"] if t["inbound"]] for c in source_cases}
    for case_id in sorted(cases):
        _check_rules(case_id, cases[case_id], by_id[case_id], turns[case_id], rules)
    review = _review(report_cases, run_id, cases)
    _write_atomic(out, json.dumps(review, sort_keys=True, indent=2) + "\n")
    return review, loaded_fields(label, effective)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        review, loaded = _run(args)
    except (CasesError, ReportError, ApplyError) as e:
        print("error: %s" % " ".join(str(e).split()), file=sys.stderr)
        return 2
    s = review["summary"]
    print("approved=%d edited=%d rejected=%d undecided=%d"
          % (s["approved"], s["edited"], s["rejected"], s["undecided"]))
    for c in review["actions_changed"]:
        print("action changed: %s %s -> %s" % (c["id"], c["from"], c["to"]))
    print(loaded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
