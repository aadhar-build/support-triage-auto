"""Turn a synthetic run into one self-contained offline HTML review page.

Usage: python3 -m triage.review_page --cases CASES.json --run DIR --out PAGE.html

Reads the made-up cases file and DIR/synthetic_report.json, and writes a page where a
reviewer can approve, edit, reject or change the action of each case, then download the
decisions as decisions.json for python3 -m triage.apply_review. Flagged cases (a policy
violation or action escalate) come first. A case with a policy violation shows only Edit and
Reject; a case flagged only by escalate needs a reason before Approve. The page makes no network requests. Only a
synthetic run is accepted: any other input exits 2 with a one-line error.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
from pathlib import Path
from typing import List, Optional

from triage.run_synthetic import JSON_NAME, SUMMARY_KEYS, CasesError, load_cases
from triage.tiers import ACTIONS

CASE_KEYS = ("id", "title", "must_pass", "passed", "expected", "actual",
             "expected_violations", "actual_violations", "tiers", "confidence")


class ReportError(Exception):
    """The run is not a synthetic run report. The message is one line."""


# ---------------------------------------------------------------- the run report

def _is_str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _check_report_case(index, c) -> None:
    where = "case at index %d" % index
    if not isinstance(c, dict) or set(c) != set(CASE_KEYS):
        raise ReportError("%s must be an object with exactly the keys %s"
                          % (where, ", ".join(CASE_KEYS)))
    if not isinstance(c["id"], str) or not c["id"]:
        raise ReportError("%s: 'id' is not a non-empty string" % where)
    if not isinstance(c["title"], str):
        raise ReportError("%s: 'title' is not a string" % where)
    actual = c["actual"]
    if not isinstance(actual, dict) or actual.get("action") not in ACTIONS \
            or not isinstance(actual.get("intent"), str):
        raise ReportError("%s: 'actual' needs a string 'intent' and an 'action' in %s"
                          % (where, ", ".join(ACTIONS)))
    if c["actual_violations"] is not None and not _is_str_list(c["actual_violations"]):
        raise ReportError("%s: 'actual_violations' is not null or a list of strings" % where)
    if not _is_str_list(c["tiers"]):
        raise ReportError("%s: 'tiers' is not a list of strings" % where)
    conf = c["confidence"]
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        raise ReportError("%s: 'confidence' is not a number" % where)


def load_run(run_dir) -> tuple:
    """Read and check DIR/synthetic_report.json. Returns (report cases, run id), where the
    run id is the hex SHA-256 of the file's bytes. Raises ReportError."""
    path = Path(run_dir) / JSON_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ReportError("run report not found: %s" % path)
    except OSError as e:
        raise ReportError("cannot read run report %s: %s" % (path, e.strerror or e))
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ReportError("run report %s is not valid UTF-8" % path)
    except json.JSONDecodeError as e:
        raise ReportError("run report %s is not valid JSON (line %d, column %d)"
                          % (path, e.lineno, e.colno))
    if not isinstance(data, dict) or set(data) != {"cases", "summary"}:
        raise ReportError("%s is not a synthetic run report: top level must have exactly "
                          "the keys cases and summary" % path)
    if not isinstance(data["summary"], dict) or set(data["summary"]) != set(SUMMARY_KEYS):
        raise ReportError("%s is not a synthetic run report: summary must have exactly the "
                          "keys %s" % (path, ", ".join(SUMMARY_KEYS)))
    cases = data["cases"]
    if not isinstance(cases, list):
        raise ReportError("%s is not a synthetic run report: cases is not a list" % path)
    seen = set()
    for index, c in enumerate(cases):
        try:
            _check_report_case(index, c)
        except ReportError as e:
            raise ReportError("%s is not a synthetic run report: %s" % (path, e))
        if c["id"] in seen:
            raise ReportError("%s is not a synthetic run report: duplicate case id" % path)
        seen.add(c["id"])
    return cases, hashlib.sha256(raw).hexdigest()


def has_violation(report_case: dict) -> bool:
    """The system's draft broke at least one policy rule."""
    return bool(report_case["actual_violations"])


def is_flagged(report_case: dict) -> bool:
    """A case is flagged when it has a policy violation or the system chose escalate."""
    return has_violation(report_case) or report_case["actual"]["action"] == "escalate"


# ---------------------------------------------------------------- the page

def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _violations(v) -> str:
    if v is None:
        return "n/a"
    return ", ".join(v) if v else "none"


def _case_html(c: dict, r: dict) -> str:
    flagged = is_flagged(r)
    if has_violation(r):
        verdict = '<button type="button" class="edit" disabled>Edit</button> '
    else:
        verdict = ('<button type="button" class="approve"%s>Approve</button> '
                   % (" disabled" if flagged else ""))
    turns = "".join(
        '<li class="turn %s"><span class="who">%s</span> %s</li>'
        % (who, who, _e(t["text"]))
        for t in c["turns"] for who in ["customer" if t["inbound"] else "brand"])
    labels = "".join('<dt>%s</dt><dd class="%s">%s</dd>' % (name, cls, _e(value)) for name, cls, value in (
        ("tier path", "tiers", " → ".join(r["tiers"])),
        ("intent", "intent", r["actual"]["intent"]),
        ("action", "action", r["actual"]["action"]),
        ("confidence", "confidence", r["confidence"]),
        ("policy violations", "violations", _violations(r["actual_violations"])),
    ))
    if "draft_reply" in c:
        # The newline after <textarea> is dropped by the parser, so a draft that starts
        # with a newline keeps it.
        draft = '<textarea class="draft" rows="4">\n%s</textarea>' % _e(c["draft_reply"])
    else:
        draft = '<p class="nodraft">no draft</p>'
    options = "".join('<option value="%s"%s>%s</option>'
                      % (a, " selected" if a == r["actual"]["action"] else "", a)
                      for a in ACTIONS)
    return (
        '<section class="case%s" data-case-id="%s" data-flagged="%s">\n'
        '<h2><code>%s</code> %s%s</h2>\n'
        '<ol class="turns">%s</ol>\n'
        '<dl class="labels">%s</dl>\n'
        '<div class="draftbox">%s</div>\n'
        '<div class="controls">'
        '<label>action <select class="action-select" data-original="%s">%s</select></label> '
        '<label>reason <input class="reason" type="text"></label> '
        '%s'
        '<button type="button" class="reject">Reject</button> '
        '<span class="status"></span></div>\n'
        '</section>\n'
    ) % (" flagged" if flagged else "", _e(c["id"]), "true" if flagged else "false",
         _e(c["id"]), _e(c["title"]), ' <span class="flag">flagged</span>' if flagged else "",
         turns, labels, draft, _e(r["actual"]["action"]), options, verdict)


STYLE = """
body { font-family: system-ui, sans-serif; margin: 0 auto; max-width: 60rem; padding: 1rem;
       background: #fff; color: #111; }
.case { border: 1px solid #ccc; border-radius: 6px; padding: 0.75rem 1rem; margin: 1rem 0; }
.case.flagged { border-color: #b00; }
.flag { color: #b00; font-size: 0.8em; text-transform: uppercase; }
.turns { padding-left: 1.2rem; }
.turn .who { font-weight: bold; }
.turn.brand { color: #335; }
.labels { display: grid; grid-template-columns: max-content 1fr; gap: 0.2rem 1rem; }
.labels dd { margin: 0; }
textarea { width: 100%; box-sizing: border-box; }
.nodraft { color: #666; font-style: italic; }
.controls { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
.status { font-weight: bold; }
"""

SCRIPT = """
(function () {
  "use strict";
  var runId = document.body.getAttribute("data-run-id");
  var decisions = {};
  var sections = document.querySelectorAll("section[data-case-id]");
  Array.prototype.forEach.call(sections, function (el) {
    var id = el.getAttribute("data-case-id");
    var flagged = el.getAttribute("data-flagged") === "true";
    var draft = el.querySelector("textarea.draft");
    var reason = el.querySelector("input.reason");
    var select = el.querySelector("select.action-select");
    var approve = el.querySelector("button.approve");
    var edit = el.querySelector("button.edit");
    var reject = el.querySelector("button.reject");
    var status = el.querySelector(".status");
    var original = draft ? draft.defaultValue : null;
    var systemAction = select.getAttribute("data-original");

    function edited() { return draft !== null && draft.value !== original; }
    function reasonText() { return reason.value.trim(); }
    function editReady() { return edited() && draft.value.trim() !== ""; }
    function refresh() {
      if (approve && flagged) { approve.disabled = !(reasonText() !== "" || edited()); }
      if (edit) { edit.disabled = !editReady(); }
    }
    function record(d) {
      if (reasonText() !== "") { d.reason = reasonText(); }
      if (select.value !== systemAction) { d.action = select.value; }
      decisions[id] = d;
      status.textContent = "recorded: " + d.decision;
    }
    if (draft) { draft.addEventListener("input", refresh); }
    reason.addEventListener("input", refresh);
    refresh();

    if (approve) {
      approve.addEventListener("click", function () {
        if (flagged && reasonText() === "" && !edited()) { return; }
        if (edited()) {
          if (draft.value.trim() === "") { status.textContent = "an edited draft cannot be empty"; return; }
          record({decision: "edit", draft: draft.value});
        } else {
          record({decision: "approve"});
        }
      });
    }
    if (edit) {
      // A policy-violation case: only an edited draft or a reject. apply_review checks the
      // edited draft against the policy rules; this page cannot.
      edit.addEventListener("click", function () {
        if (!editReady()) { status.textContent = "change the draft first"; return; }
        record({decision: "edit", draft: draft.value});
      });
    }
    reject.addEventListener("click", function () {
      if (reasonText() === "") { status.textContent = "reject needs a reason"; return; }
      record({decision: "reject"});
    });
  });

  document.getElementById("download").addEventListener("click", function () {
    var body = JSON.stringify({run_id: runId, decisions: decisions}, null, 2) + "\\n";
    var blob = new Blob([body], {type: "application/json"});
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "decisions.json";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(link.href);
  });
})();
"""


def render_page(cases: list, report_cases: list, run_id: str) -> str:
    pairs = list(zip(cases, report_cases))
    ordered = [p for p in pairs if is_flagged(p[1])] + [p for p in pairs if not is_flagged(p[1])]
    flagged = sum(is_flagged(r) for _, r in pairs)
    body = "".join(_case_html(c, r) for c, r in ordered)
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>Synthetic run review</title>\n<style>%s</style>\n</head>\n"
        "<body data-run-id=\"%s\">\n<h1>Synthetic run review</h1>\n"
        "<p>Run <code>%s</code>: %d cases, %d flagged. A case with a policy violation "
        "can only be edited or rejected, and apply_review checks the edited draft against "
        "the policy rules. A case flagged by escalate needs an edit or a reason before "
        "Approve. Decisions stay in this page until you download them.</p>\n"
        "<p><button type=\"button\" id=\"download\">Download decisions</button></p>\n"
        "%s<script>%s</script>\n</body>\n</html>\n"
    ) % (STYLE, _e(run_id), _e(run_id), len(pairs), flagged, body, SCRIPT)


# ---------------------------------------------------------------- command line

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m triage.review_page",
                                description=__doc__.splitlines()[0])
    p.add_argument("--cases", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--out", required=True)
    return p


def _run(args) -> tuple:
    cases = load_cases(args.cases)
    report_cases, run_id = load_run(args.run)
    if [c["id"] for c in cases] != [r["id"] for r in report_cases]:
        raise ReportError("the run's case ids do not match the cases file %s" % args.cases)
    page = render_page(cases, report_cases, run_id)
    out = Path(args.out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page, encoding="utf-8")
    except OSError as e:
        raise ReportError("cannot write page %s: %s" % (out, e.strerror or e))
    return len(cases), sum(is_flagged(r) for r in report_cases), out


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        n, flagged, out = _run(args)
    except (CasesError, ReportError) as e:
        print("error: %s" % " ".join(str(e).split()), file=sys.stderr)
        return 2
    print("cases=%d flagged=%d page=%s" % (n, flagged, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
