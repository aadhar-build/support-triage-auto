"""Admin replay: step through a synthetic run's threads turn by turn with the triage state.

Usage: python3 -m triage.replay --cases CASES.json --run DIR --trace TRACE.jsonl --out PAGE.html

Reads the made-up cases file, DIR/synthetic_report.json, DIR/metrics.json and the audit trace
that `run_synthetic --trace` wrote, checks that the three belong together, and writes one
self-contained offline HTML page. Only a synthetic run is accepted. Any refusal exits 2 with
one stderr line and writes nothing.

Until per-turn re-triage (#46) lands, each thread has one recorded decision, made once on the
full thread. It is shown only at the thread's last turn; earlier turns show no decision.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from pathlib import Path
from typing import List, Optional

from triage.review_page import ReportError, load_run
from triage.run_synthetic import METRICS_NAME, CasesError, load_cases
from triage.trace import TraceError, validate_record

STATE_FIELDS = ("intent", "urgency", "action", "confidence", "reason_codes", "tier",
                "model_id", "policy", "reply")
DECIDED_ONCE = "Decided once on the full thread; per-turn decisions arrive with #46"
NO_DECISION = "No decision recorded at this turn"
DRAFT_TAG = "draft from scenario file"

_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ReplayError(Exception):
    """The inputs can't be replayed. The message is one line."""


def _e(value) -> str:
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------- loading and checks

def _load_trace(path) -> list:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ReplayError("trace not found: %s" % path)
    except (OSError, UnicodeDecodeError) as e:
        raise ReplayError("cannot read trace %s: %s" % (path, getattr(e, "strerror", None) or e))
    records = []
    for n, line in enumerate(text.splitlines(), start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            raise ReplayError("trace %s line %d is not valid JSON" % (path, n))
        validate_record(record)
        if record["split"] != "synthetic":
            raise ReplayError("not a synthetic trace: %s line %d has split %r"
                              % (path, n, record["split"]))
        records.append(record)
    return records


def _check_metrics(run_dir, cases_path) -> None:
    try:
        metrics = json.loads((Path(run_dir) / METRICS_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ReplayError("run has no cases fingerprint; re-run run_synthetic")
    if not isinstance(metrics, dict):
        raise ReplayError("run has no cases fingerprint; re-run run_synthetic")
    cohort = metrics.get("cohort")
    if not isinstance(cohort, dict):
        cohort = {}
    revision = cohort.get("gold_revision")
    if metrics.get("kind") != "synthetic" or cohort.get("name") != "synthetic" \
            or not isinstance(revision, str) or not _REVISION_RE.match(revision):
        raise ReplayError("not a synthetic run: %s" % (Path(run_dir) / METRICS_NAME))
    try:
        digest = hashlib.sha256(Path(cases_path).read_bytes()).hexdigest()
    except OSError as e:
        raise ReplayError("cannot read cases file %s: %s" % (cases_path, e.strerror or e))
    if revision != "sha256:" + digest:
        raise ReplayError("run was made from a different cases file")


def _check_binding(cases: list, report_cases: list, records: list) -> None:
    if [c["id"] for c in report_cases] != [c["id"] for c in cases]:
        raise ReplayError("run report case ids do not match the cases file (same ids, "
                          "same order)")
    if [r["thread_id"] for r in records] != list(range(1, len(cases) + 1)):
        raise ReplayError("trace thread ids are not 1..%d in order" % len(cases))
    for rc, record in zip(report_cases, records):
        if record["final"]["intent"] != rc["actual"]["intent"] \
                or record["final"]["action"] != rc["actual"]["action"] \
                or [t["tier"] for t in record["tiers"]] != rc["tiers"]:
            raise ReplayError("trace does not match run report (case %s)" % rc["id"])


# ---------------------------------------------------------------- the state model

def recorded_state(case: dict, report_case: dict, record: dict) -> dict:
    """The triage state recorded for a thread: labels from the trace, policy from the report,
    reply from the cases file. Keys are STATE_FIELDS."""
    final, last = record["final"], record["tiers"][-1]
    violations = report_case["actual_violations"]
    if violations is None:
        policy = "not checked"
    elif not violations:
        policy = "passed"
    else:
        policy = ", ".join(violations)
    if violations:
        reply = "Reply held: failed policy check (%s)" % ", ".join(violations)
    elif violations == [] and "draft_reply" in case:
        reply = case["draft_reply"]
    else:
        reply = None
    return {"intent": final["intent"], "urgency": final["urgency"], "action": final["action"],
            "confidence": final["confidence"], "reason_codes": tuple(record["reason_codes"]),
            "tier": last["tier"], "model_id": last["model_id"], "policy": policy,
            "reply": reply}


def state_changes(prev: Optional[dict], cur: dict) -> tuple:
    """Names of the STATE_FIELDS that differ from prev to cur; every field when prev is None."""
    if prev is None:
        return STATE_FIELDS
    return tuple(f for f in STATE_FIELDS if prev.get(f) != cur.get(f))


def thread_steps(case: dict, state: dict) -> list:
    """Pre-#46 step model: one step per turn, the single recorded state on the last turn."""
    steps = [{"turn": t, "state": None, "note": None} for t in case["turns"]]
    steps[-1].update(state=state, note=DECIDED_ONCE)
    return steps


# ---------------------------------------------------------------- rendering

_CSS = """
body{font-family:system-ui,sans-serif;margin:16px;background:#fff;color:#111}
header,nav,section{max-width:960px}
.thread{border-top:2px solid #888;margin-top:16px;padding-top:8px}
.step{border:1px solid #ccc;border-radius:6px;padding:8px;margin:8px 0}
.speaker{font-weight:bold}
.state{background:#f4f4f4;padding:6px;margin-top:6px}
.field{margin:2px 0}
.changed{background:#fff2a8;font-weight:bold}
.first{color:#06c}
.held{color:#a00}
table{border-collapse:collapse}
td,th{border:1px solid #ccc;padding:2px 6px;text-align:left}
code{background:#eee;padding:0 3px}
@media (prefers-color-scheme: dark){
body{background:#111;color:#eee}.state{background:#222}.changed{background:#554a00}
code{background:#333}}
"""

_JS = """
(function(){
  var threads = document.querySelectorAll('section.thread');
  function showStep(sec, k){
    var steps = sec.querySelectorAll('li.step');
    if (k < 0) { k = 0; }
    if (k > steps.length - 1) { k = steps.length - 1; }
    for (var i = 0; i < steps.length; i++) { steps[i].hidden = (i !== k); }
    sec.setAttribute('data-current', String(k));
  }
  function select(id){
    for (var i = 0; i < threads.length; i++) {
      threads[i].hidden = (threads[i].id !== id);
    }
  }
  for (var i = 0; i < threads.length; i++) {
    (function(sec){
      showStep(sec, 0);
      sec.querySelector('button.next').hidden = false;
      sec.querySelector('button.back').hidden = false;
      sec.querySelector('button.next').onclick = function(){
        showStep(sec, Number(sec.getAttribute('data-current')) + 1); };
      sec.querySelector('button.back').onclick = function(){
        showStep(sec, Number(sec.getAttribute('data-current')) - 1); };
    })(threads[i]);
  }
  var links = document.querySelectorAll('nav a[data-target]');
  for (var j = 0; j < links.length; j++) {
    links[j].onclick = function(ev){
      ev.preventDefault(); select(this.getAttribute('data-target')); };
  }
  if (threads.length) { select(threads[0].id); }
})();
"""


def _field(name: str, label: str, value_html: str, changed: tuple, first: bool) -> str:
    cls = "field changed" if (name in changed and not first) else "field"
    return '<div class="%s" data-field="%s">%s: %s</div>' % (cls, name, _e(label), value_html)


def _state_html(state: dict, changed: tuple, first: bool, note: Optional[str]) -> str:
    codes = " ".join("<code>%s</code>" % _e(c) for c in state["reason_codes"])
    if state["reply"] is None:
        reply = "no draft"
    elif state["policy"] not in ("passed", "not checked"):
        reply = '<span class="held">%s</span>' % _e(state["reply"])
    else:
        reply = "%s <em>(%s)</em>" % (_e(state["reply"]), _e(DRAFT_TAG))
    parts = []
    if first:
        parts.append('<div class="first">first decision</div>')
    if note:
        parts.append('<div class="note">%s</div>' % _e(note))
    parts += [
        _field("intent", "intent", _e(state["intent"]), changed, first),
        _field("urgency", "urgency", _e(state["urgency"]), changed, first),
        _field("action", "action", _e(state["action"]), changed, first),
        _field("confidence", "confidence", _e(state["confidence"]), changed, first),
        _field("reason_codes", "reason codes", codes, changed, first),
        _field("tier", "deciding tier", _e(state["tier"]), changed, first),
        _field("model_id", "model id", "<code>%s</code>" % _e(state["model_id"]), changed,
               first),
        _field("policy", "policy check", _e(state["policy"]), changed, first),
        _field("reply", "reply", reply, changed, first),
    ]
    return "".join(parts)


def render_thread(index: int, case: dict, steps: list) -> str:
    """The replay steps for one thread. Each step is {"turn", "state" or None, "note"}."""
    brand = case.get("brand") or "Brand"
    items, prev = [], None
    for k, step in enumerate(steps, start=1):
        turn, state = step["turn"], step["state"]
        speaker = "Customer" if turn["inbound"] else brand
        if state is None:
            panel = '<div class="none">%s</div>' % _e(NO_DECISION)
        else:
            panel = _state_html(state, state_changes(prev, state), prev is None,
                                step.get("note"))
            prev = state
        items.append(
            '<li class="step" data-turn="%d"><div><span class="speaker">%s</span> '
            '(turn %d of %d)</div><div class="text">%s</div>'
            '<div class="state" data-turn="%d">%s</div></li>'
            % (k, _e(speaker), k, len(steps), _e(turn["text"]), k, panel))
    return ('<ol class="replay">%s</ol>'
            '<p><button class="back" type="button" hidden>Back</button> '
            '<button class="next" type="button" hidden>Next</button></p>' % "".join(items))


def _trace_html(record: dict) -> str:
    rows = "".join(
        "<tr><td>%s</td><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td>"
        "<td>%s</td></tr>" % tuple(_e(t[k]) for k in ("tier", "model_id", "intent", "urgency",
                                                       "action", "confidence"))
        for t in record["tiers"])
    final = record["final"]
    return ('<div class="trace"><h3>Trace</h3><table><tr><th>tier</th><th>model_id</th>'
            '<th>intent</th><th>urgency</th><th>action</th><th>confidence</th></tr>%s</table>'
            '<p class="final">final: intent %s, urgency %s, action %s, confidence %s</p>'
            '<p class="codes">reason codes: %s</p></div>'
            % (rows, _e(final["intent"]), _e(final["urgency"]), _e(final["action"]),
               _e(final["confidence"]),
               " ".join("<code>%s</code>" % _e(c) for c in record["reason_codes"])))


def render(cases: list, report_cases: list, records: list) -> str:
    """The whole replay page. Inputs must already be checked to belong together."""
    if records:
        r0 = records[0]
        header = ("code version <code>%s</code>, code_dirty %s, threshold %s, split %s"
                  % (_e(r0["code_version"]), _e(str(r0["code_dirty"]).lower()),
                     _e(r0["threshold"]), _e(r0["split"])))
    else:
        header = "no threads in this run"
    nav, sections = [], []
    for i, (case, rc, record) in enumerate(zip(cases, report_cases, records), start=1):
        anchor = "thread-%d" % i
        nav.append('<li><a href="#%s" data-target="%s">%s &#8212; %s</a> (%s, %s)</li>'
                   % (anchor, anchor, _e(case["id"]), _e(case["title"]),
                      _e(rc["actual"]["action"]), "pass" if rc["passed"] else "FAIL"))
        steps = thread_steps(case, recorded_state(case, rc, record))
        sections.append('<section class="thread" id="%s" data-case="%s"><h2>%s &#8212; %s</h2>'
                        '%s%s</section>'
                        % (anchor, _e(case["id"]), _e(case["id"]), _e(case["title"]),
                           render_thread(i, case, steps), _trace_html(record)))
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Triage replay</title><style>%s</style></head><body>'
            '<header><h1>Triage replay</h1><p>%s</p></header>'
            '<nav><h2>Threads</h2><ul>%s</ul></nav>%s<script>%s</script></body></html>\n'
            % (_CSS, header, "".join(nav), "".join(sections), _JS))


# ---------------------------------------------------------------- CLI

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m triage.replay",
                                description=__doc__.splitlines()[0])
    p.add_argument("--cases", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--trace", required=True)
    p.add_argument("--out", required=True)
    return p


def _run(args) -> int:
    cases = load_cases(args.cases)
    report_cases, _ = load_run(args.run)
    records = _load_trace(args.trace)
    _check_metrics(args.run, args.cases)
    _check_binding(cases, report_cases, records)
    page = render(cases, report_cases, records)
    try:
        Path(args.out).write_text(page, encoding="utf-8")
    except OSError as e:
        raise ReplayError("cannot write %s: %s" % (args.out, e.strerror or e))
    return len(cases)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        count = _run(args)
    except (ReplayError, CasesError, ReportError, TraceError) as e:
        print("error: %s" % " ".join(str(e).split()), file=sys.stderr)
        return 2
    print("wrote %s threads=%d" % (args.out, count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
