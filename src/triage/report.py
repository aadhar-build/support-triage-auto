"""Performance report: one offline HTML page from a `metrics/1` file.

Usage: python3 -m triage.report METRICS.json [--compare OTHER.json] [--thresholds PATH] -o REPORT.html

Shows headline rates with counts, 95% ranges and "directional" badges, escalation detail,
confusion matrices and sortable per-class tables for action and intent, and an optional
week-on-week compare. Green and red come only from the targets in the thresholds file. Any
refusal exits 2 with one stderr line and writes nothing.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import List, Optional

from triage.metrics import AVERAGED, COHORT_KEYS, KINDS, SCHEMA_VERSION, comparison_key
from triage.tiers import ACTIONS, INTENTS
from triage.trace import REPO_ROOT

DEFAULT_THRESHOLDS = REPO_ROOT / "evals" / "thresholds.json"
KEY_FIELDS = ("schema_version", "kind", "cohort.name", "cohort.split", "cohort.gold_revision",
              "cohort.brand_population")
TOP_KEYS = ("schema_version", "kind", "run_at", "cohort", "sample", "config", "coverage",
            "operations", "n", "directional_below", "averaging", "escalation", "action", "intent")
COUNT_KEYS = ("tp", "fp", "tn", "fn", "positives", "predicted_positives")
ESC_PROPORTIONS = ("tpr", "tnr", "fpr", "fnr", "precision", "npv", "accuracy", "prevalence")
ESC_SCORES = ("f1", "f2", "f0_5", "balanced_accuracy", "mcc")
BLOCK_KEYS = ("confusion_matrix", "per_class", "macro", "weighted", "accuracy", "kappa")
TARGET_GROUPS = ("north_star", "supporting", "guardrails")
MEASURED = {"north_star.correctly_handled_without_person", "supporting.action_accuracy",
            "supporting.intent_accuracy", "supporting.action_accuracy_per_action",
            "supporting.larger_tier_share", "supporting.cost_per_1000_usd",
            "guardrails.false_escalation_rate", "guardrails.missed_escalation_rate_ci_upper",
            "guardrails.missed_escalations_hard_case"}

NONOVERLAP = "ranges don't overlap"
GOLD_NOTE = "Gold runs are aggregate only: conversation lists are not shown."
NOT_COMPUTED = "not computed in this run"
NOT_SET = "target not set"
UNREADABLE = "target unreadable"
NOT_IN_RUN = "not in this run"
PLACEHOLDER_NOTE = "placeholder prices: not compared with target"
HARD_CASE_NOTE = "applies to the hard-case set only"


class ReportError(Exception):
    """The inputs can't be reported. The message is one line."""


def _e(value) -> str:
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------- loading and checks

def _read_json(path, what: str):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ReportError("%s not found: %s" % (what, path))
    except (OSError, UnicodeDecodeError) as e:
        raise ReportError("cannot read %s %s: %s" % (what, path, getattr(e, "strerror", None) or e))
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ReportError("%s %s is not valid JSON: %s" % (what, path, e))


def _has_ids(value) -> bool:
    if isinstance(value, dict):
        return "ids" in value or any(_has_ids(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_ids(v) for v in value)
    return False


def _need(obj, keys, where: str) -> None:
    if not isinstance(obj, dict):
        raise ReportError("%s must be an object" % where)
    missing = [k for k in keys if k not in obj]
    if missing:
        raise ReportError("%s lacks %s" % (where, ", ".join(missing)))


def _check_proportion(p, where: str) -> None:
    _need(p, ("value", "num", "n", "ci", "directional"), where)
    ci = p["ci"]
    if ci is not None and not (isinstance(ci, list) and len(ci) == 2):
        raise ReportError("%s.ci must be null or [low, high]" % where)


def _check_score(p, where: str) -> None:
    _need(p, ("value", "n", "ci", "directional"), where)


def _is_grid(grid, k: int, cell_ok) -> bool:
    return (isinstance(grid, list) and len(grid) == k
            and all(isinstance(r, list) and len(r) == k and all(cell_ok(c) for c in r)
                    for r in grid))


def _check_block(name: str, block, vocab, synthetic: bool) -> None:
    _need(block, BLOCK_KEYS, name)
    cm = block["confusion_matrix"]
    _need(cm, ("labels", "counts"), name + ".confusion_matrix")
    labels = cm["labels"]
    if not isinstance(labels, list) or len(set(map(str, labels))) != len(labels):
        raise ReportError("%s labels must be a list of distinct labels" % name)
    for label in labels:
        if label not in vocab:
            raise ReportError("%s label %r is not a known %s" % (name, label, name))
    k = len(labels)
    if not _is_grid(cm["counts"], k, lambda c: isinstance(c, int) and not isinstance(c, bool)
                    and c >= 0):
        raise ReportError("%s counts must be a %dx%d list of non-negative ints" % (name, k, k))
    per_class = block["per_class"]
    if not isinstance(per_class, dict) or set(per_class) != set(labels):
        raise ReportError("%s per_class must have exactly the labels as keys" % name)
    for label in labels:
        _need(per_class[label], ("support", "predicted", "tpr", "tnr", "precision", "f1", "f2",
                                 "f0_5"), "%s.per_class.%s" % (name, label))
    for avg in ("macro", "weighted"):
        _need(block[avg], AVERAGED + ("included_classes",), "%s.%s" % (name, avg))
        for key in AVERAGED:
            _need(block[avg][key], ("value", "n", "ci", "directional", "zero_filled"),
                  "%s.%s.%s" % (name, avg, key))
    _check_proportion(block["accuracy"], name + ".accuracy")
    _check_score(block["kappa"], name + ".kappa")
    if synthetic:
        if not _is_grid(block.get("ids"), k, lambda c: isinstance(c, list)
                        and all(isinstance(i, str) for i in c)):
            raise ReportError("%s.ids must be a %dx%d list of lists of strings" % (name, k, k))


def check_metrics(m) -> dict:
    if not isinstance(m, dict):
        raise ReportError("metrics file must be a JSON object")
    unknown = sorted(set(m) - set(TOP_KEYS))
    if unknown:
        raise ReportError("not a metrics/1 file: unknown key %s" % ", ".join(unknown))
    missing = [k for k in TOP_KEYS if k not in m]
    if missing:
        raise ReportError("not a metrics/1 file: missing key %s" % ", ".join(missing))
    if m["schema_version"] != SCHEMA_VERSION:
        raise ReportError("schema_version must be %s, got %r" % (SCHEMA_VERSION,
                                                                 m["schema_version"]))
    if m["kind"] not in KINDS:
        raise ReportError("kind must be one of %s, got %r" % (", ".join(KINDS), m["kind"]))
    if not isinstance(m["cohort"], dict) or set(m["cohort"]) != set(COHORT_KEYS):
        raise ReportError("cohort must have exactly the keys %s" % ", ".join(COHORT_KEYS))
    synthetic = m["kind"] == "synthetic"
    if not synthetic and _has_ids(m):
        raise ReportError("a gold metrics file must not carry ids")
    esc = m["escalation"]
    _need(esc, ("counts",) + ESC_PROPORTIONS + ESC_SCORES, "escalation")
    _need(esc["counts"], COUNT_KEYS, "escalation.counts")
    for key in ESC_PROPORTIONS:
        _check_proportion(esc[key], "escalation." + key)
    for key in ESC_SCORES:
        _check_score(esc[key], "escalation." + key)
    _check_block("action", m["action"], ACTIONS, synthetic)
    _check_block("intent", m["intent"], INTENTS, synthetic)
    ops = m["operations"]
    if ops is not None:
        _need(ops, ("tier_shares", "cost_per_1000_usd", "prices_are_placeholders"), "operations")
        if not isinstance(ops["tier_shares"], dict):
            raise ReportError("operations.tier_shares must be an object")
        for tier, p in ops["tier_shares"].items():
            _check_proportion(p, "operations.tier_shares." + str(tier))
    return m


def load_metrics(path) -> dict:
    return check_metrics(_read_json(path, "metrics file"))


def load_thresholds(path, explicit: bool):
    """(targets, meta) or (None, None) when the default file is absent."""
    if not explicit and not Path(path).exists():
        return None, None
    data = _read_json(path, "thresholds file")
    if not isinstance(data, dict):
        raise ReportError("thresholds file %s must be a JSON object" % path)
    return data, {"version": data.get("version"), "status": data.get("status")}


def compare_refusal(metrics: dict, other: dict) -> Optional[str]:
    a, b = comparison_key(metrics), comparison_key(other)
    if a == b:
        return None
    diffs = ["%s differs (%s vs %s)" % (f, x, y) for f, x, y in zip(KEY_FIELDS, a, b) if x != y]
    return "cannot compare: " + "; ".join(diffs)


# ---------------------------------------------------------------- formatting

def _num(v) -> str:
    return "n/a" if v is None else "%.4f" % v


def _badge(p) -> str:
    return ' <span class="badge">directional</span>' if p.get("directional") is True else ""


def _range(p) -> str:
    ci = p.get("ci")
    return "no range" if ci is None else "%s to %s" % (_num(ci[0]), _num(ci[1]))


def fmt_proportion(p) -> str:
    return "%s &#183; %s of %s &#183; %s%s" % (_e(_num(p["value"])), _e(p["num"]), _e(p["n"]),
                                                _e(_range(p)), _badge(p))


def fmt_score(p) -> str:
    return "%s &#183; n=%s &#183; no range%s" % (_e(_num(p["value"])), _e(p["n"]), _badge(p))


def fmt_cell(p) -> str:
    if not isinstance(p, dict):
        return _e(_num(p))
    return fmt_proportion(p) if "num" in p else fmt_score(p)


def _pairs(obj) -> str:
    if obj is None:
        return "none"
    if not isinstance(obj, dict):
        return str(obj)
    parts = []
    for k in sorted(obj):
        v = obj[k]
        if isinstance(v, dict):
            v = ", ".join("%s=%s" % (kk, "none" if vv is None else vv)
                          for kk, vv in sorted(v.items()))
            parts.append("%s=(%s)" % (k, v))
        else:
            parts.append("%s=%s" % (k, "none" if v is None else v))
    return "; ".join(parts)


# ---------------------------------------------------------------- targets

def _leaf(targets, path: str):
    """('missing',) | ('bad',) | (bound, number)."""
    if targets is None:
        return ("missing",)
    group, key = path.split(".")
    g = targets.get(group)
    if g is None:
        return ("missing",)
    if not isinstance(g, dict):
        return ("bad",)
    if key not in g:
        return ("missing",)
    leaf = g[key]
    if not isinstance(leaf, dict) or len(leaf) != 1:
        return ("bad",)
    (bound, value), = leaf.items()
    if bound not in ("min", "max", "max_count") or isinstance(value, bool) \
            or not isinstance(value, (int, float)):
        return ("bad",)
    return (bound, value)


def judge(targets, path: str, value, strict: bool = False):
    """(mark, text) for one target applied to one value."""
    leaf = _leaf(targets, path)
    if leaf[0] == "missing":
        return "no-target", NOT_SET
    if leaf[0] == "bad":
        return "bad-target", UNREADABLE
    bound, limit = leaf
    text = "target %s %s" % ({"min": "at least", "max": "at most",
                              "max_count": "at most"}[bound], limit)
    if strict:
        text = "target below %s" % limit
    if value is None:
        return "n/a", text + ": n/a"
    if bound == "min":
        ok = value >= limit
    elif strict:
        ok = value < limit
    else:
        ok = value <= limit
    return ("pass" if ok else "fail"), "%s: %s" % (text, "pass" if ok else "fail")


def _summary_rows(m, targets):
    """[(key, label, cell_html, mark, target_text)] in page order."""
    esc = m["escalation"]
    rows = []
    leaf = _leaf(targets, "north_star.correctly_handled_without_person")
    ns_text = NOT_SET if leaf[0] == "missing" else UNREADABLE if leaf[0] == "bad" \
        else "target at least %s" % _num(leaf[1])
    rows.append(("north_star", "Correctly handled without a person (north star)",
                 _e(NOT_COMPUTED), "note" if leaf[0] not in ("missing", "bad")
                 else {"missing": "no-target", "bad": "bad-target"}[leaf[0]], ns_text))

    def add(key, label, p, target=None, value=None, strict=False):
        if target is None:
            mk, text = "no-target", NOT_SET
        else:
            mk, text = judge(targets, target, value, strict)
        rows.append((key, label, fmt_cell(p), mk, text))

    add("escalation.f2", "Escalation F2 (headline)", esc["f2"])
    add("escalation.tpr", "Escalation TPR (recall)", esc["tpr"])
    add("escalation.tnr", "Escalation TNR", esc["tnr"])
    add("escalation.precision", "Escalation precision", esc["precision"])
    add("escalation.f1", "Escalation F1", esc["f1"])
    ci = esc["fnr"]["ci"]
    add("escalation.fnr", "Missed escalation rate (FNR)", esc["fnr"],
        "guardrails.missed_escalation_rate_ci_upper", None if ci is None else ci[1], strict=True)
    add("escalation.fpr", "False escalation rate (FPR)", esc["fpr"],
        "guardrails.false_escalation_rate", esc["fpr"]["value"])
    add("action.accuracy", "Action accuracy", m["action"]["accuracy"],
        "supporting.action_accuracy", m["action"]["accuracy"]["value"])
    add("intent.accuracy", "Intent accuracy", m["intent"]["accuracy"],
        "supporting.intent_accuracy", m["intent"]["accuracy"]["value"])
    ops = m["operations"]
    if ops is not None:
        for tier in sorted(ops["tier_shares"]):
            p = ops["tier_shares"][tier]
            if tier == "larger":
                add("operations.tier_shares.larger", "Tier share: larger", p,
                    "supporting.larger_tier_share", p["value"])
            else:
                add("operations.tier_shares." + tier, "Tier share: %s" % tier, p)
        cost = ops["cost_per_1000_usd"]
        if ops["prices_are_placeholders"] is True:
            rows.append(("operations.cost_per_1000_usd", "Cost per 1000 threads (USD)",
                         _e(_num(cost)) + " &#183; placeholder prices", "note",
                         PLACEHOLDER_NOTE))
        else:
            add("operations.cost_per_1000_usd", "Cost per 1000 threads (USD)", cost,
                "supporting.cost_per_1000_usd", cost)
    return rows


def _target_rows(m, targets):
    """[(path, mark, text)] for every measured target."""
    out = []
    summary = {k: (mk, t) for k, _, _, mk, t in _summary_rows(m, targets)}
    mapping = (("north_star.correctly_handled_without_person", "north_star"),
               ("supporting.action_accuracy", "action.accuracy"),
               ("supporting.intent_accuracy", "intent.accuracy"),
               ("guardrails.false_escalation_rate", "escalation.fpr"),
               ("guardrails.missed_escalation_rate_ci_upper", "escalation.fnr"))
    for path, key in mapping:
        mk, text = summary[key]
        out.append((path, mk, "%s (%s)" % (text, key)))
    leaf = _leaf(targets, "supporting.action_accuracy_per_action")
    out.append(("supporting.action_accuracy_per_action",
                *(("no-target", NOT_SET) if leaf[0] == "missing" else
                  ("bad-target", UNREADABLE) if leaf[0] == "bad" else
                  ("note", "per-class action TPR, marked in the action per-class table"))))
    if "operations.tier_shares.larger" in summary:
        mk, text = summary["operations.tier_shares.larger"]
        out.append(("supporting.larger_tier_share", mk, text))
    else:
        leaf = _leaf(targets, "supporting.larger_tier_share")
        out.append(("supporting.larger_tier_share",
                    *(("no-target", NOT_SET) if leaf[0] == "missing" else
                      ("bad-target", UNREADABLE) if leaf[0] == "bad" else ("note", NOT_IN_RUN))))
    if "operations.cost_per_1000_usd" in summary:
        out.append(("supporting.cost_per_1000_usd", *summary["operations.cost_per_1000_usd"]))
    else:
        out.append(("supporting.cost_per_1000_usd", "note", NOT_IN_RUN))
    if m["cohort"]["name"] == "hard_case":
        mk, text = judge(targets, "guardrails.missed_escalations_hard_case",
                         m["escalation"]["counts"]["fn"])
        out.append(("guardrails.missed_escalations_hard_case", mk,
                    "%s (missed escalations: %s)" % (text, m["escalation"]["counts"]["fn"])))
    else:
        out.append(("guardrails.missed_escalations_hard_case", "note", HARD_CASE_NOTE))
    return out


def _unmeasured(targets) -> List[str]:
    if not targets:
        return []
    keys = []
    for group in TARGET_GROUPS:
        g = targets.get(group)
        if isinstance(g, dict):
            keys += ["%s.%s" % (group, k) for k in g if "%s.%s" % (group, k) not in MEASURED]
    return keys


# ---------------------------------------------------------------- rendering

_CSS = """
body{font-family:system-ui,sans-serif;margin:16px;background:#fff;color:#111}
header,section{max-width:1100px}
table{border-collapse:collapse;margin:8px 0}
td,th{border:1px solid #ccc;padding:2px 6px;text-align:left;vertical-align:top}
th[data-sort]{cursor:pointer;text-decoration:underline dotted}
.badge{background:#ffe08a;color:#111;border-radius:4px;padding:0 4px;font-size:85%}
tr[data-mark="pass"] td.mark{background:#c8f0c8}
tr[data-mark="fail"] td.mark{background:#f6c4c4}
li[data-mark="pass"]{color:#060}li[data-mark="fail"]{color:#a00}
.nonoverlap{font-weight:bold}
td.cell{cursor:pointer;text-decoration:underline}
.note{font-style:italic}
code{background:#eee;padding:0 3px}
@media (prefers-color-scheme: dark){
body{background:#111;color:#eee}code{background:#333}
tr[data-mark="pass"] td.mark{background:#1d4a1d}
tr[data-mark="fail"] td.mark{background:#5a1d1d}
li[data-mark="pass"]{color:#8d8}li[data-mark="fail"]{color:#f88}}
"""

_JS = """
(function(){
  var heads = document.querySelectorAll('th[data-sort]');
  function val(td){
    var v = td.getAttribute('data-v');
    if (v === null || v === '') { return null; }
    var f = parseFloat(v);
    return isNaN(f) ? v : f;
  }
  for (var i = 0; i < heads.length; i++) {
    heads[i].addEventListener('click', function(){
      var th = this, table = th.closest('table');
      var body = table.querySelector('tbody.sortable');
      var col = Array.prototype.indexOf.call(th.parentNode.children, th);
      var dir = th.getAttribute('data-dir') === 'asc' ? 'desc' : 'asc';
      th.setAttribute('data-dir', dir);
      var rows = Array.prototype.slice.call(body.querySelectorAll('tr'));
      rows.sort(function(a, b){
        var x = val(a.children[col]), y = val(b.children[col]);
        if (x === null && y === null) { return 0; }
        if (x === null) { return 1; }
        if (y === null) { return -1; }
        var c = x < y ? -1 : (x > y ? 1 : 0);
        return dir === 'asc' ? c : -c;
      });
      for (var j = 0; j < rows.length; j++) { body.appendChild(rows[j]); }
    });
  }
  var cells = document.querySelectorAll('td.cell');
  if (!cells.length) { return; }
  var data = JSON.parse(document.getElementById('metrics-data').textContent);
  for (var k = 0; k < cells.length; k++) {
    cells[k].addEventListener('click', function(){
      var rc = this.dataset.cell.split(',');
      var block = this.dataset.block;
      var ids = data[block].ids[parseInt(rc[0], 10)][parseInt(rc[1], 10)];
      var labels = data[block].confusion_matrix.labels;
      var title = document.getElementById('cell-title');
      title.textContent = block + ': gold ' + labels[parseInt(rc[0], 10)] + ', predicted ' +
        labels[parseInt(rc[1], 10)] + ' (' + ids.length + ' cases)';
      var list = document.getElementById('cell-ids');
      while (list.firstChild) { list.removeChild(list.firstChild); }
      for (var n = 0; n < ids.length; n++) {
        var li = document.createElement('li');
        li.textContent = ids[n];
        list.appendChild(li);
      }
    });
  }
})();
"""


def _data_block(block_id: str, data) -> str:
    return '<script type="application/json" id="%s">%s</script>' % (
        block_id, json.dumps(data, sort_keys=True).replace("<", "\\u003c"))


def _header(m, targets_meta, no_thresholds: bool) -> str:
    items = ["kind: %s" % m["kind"], "run_at: %s" % m["run_at"],
             "cohort: %s" % _pairs(m["cohort"]), "sample: %s" % _pairs(m["sample"]),
             "config: %s" % _pairs(m["config"]), "n: %s" % m["n"],
             "directional below n=%s" % m["directional_below"]]
    if m["kind"] == "gold" and m["coverage"] is not None:
        items.append("coverage: %s" % _pairs(m["coverage"]))
    if no_thresholds:
        items.append("thresholds: no thresholds file found")
    elif targets_meta:
        items.append("thresholds version: %s" % targets_meta.get("version"))
        items.append("thresholds status: %s" % targets_meta.get("status"))
    return "<ul>%s</ul>" % "".join("<li>%s</li>" % _e(i) for i in items)


def _summary_html(m, targets) -> str:
    rows = ['<tr data-metric="%s" data-mark="%s"><th>%s</th><td class="value">%s</td>'
            '<td class="mark">%s</td></tr>' % (_e(k), _e(mk), _e(label), cell, _e(text))
            for k, label, cell, mk, text in _summary_rows(m, targets)]
    return ('<section><h2>Summary</h2><table><thead><tr><th>Rate</th><th>Value</th>'
            '<th>Target</th></tr></thead><tbody>%s</tbody></table></section>' % "".join(rows))


def _targets_html(m, targets) -> str:
    items = ['<li data-target="%s" data-mark="%s">%s: %s</li>' % (_e(p), _e(mk), _e(p), _e(t))
             for p, mk, t in _target_rows(m, targets)]
    unmeasured = "".join("<li>%s</li>" % _e(k) for k in _unmeasured(targets))
    return ('<section><h2>Targets</h2><ul>%s</ul><h3>Targets not measured in this report</h3>'
            '<ul>%s</ul></section>' % ("".join(items), unmeasured))


def _escalation_html(m) -> str:
    esc = m["escalation"]
    counts = "".join('<tr><th>%s</th><td data-count="%s">%s</td></tr>'
                     % (_e(k), _e(k), _e(esc["counts"][k])) for k in COUNT_KEYS)
    rows = "".join('<tr data-detail="escalation.%s"><th>%s</th><td>%s</td></tr>'
                   % (_e(k), _e(k), fmt_cell(esc[k])) for k in ESC_PROPORTIONS + ESC_SCORES)
    return ('<section><h2>Escalation detail</h2><table><tbody>%s</tbody></table>'
            '<table><tbody>%s</tbody></table></section>' % (counts, rows))


def _matrix_html(m, name: str) -> str:
    block = m[name]
    labels = block["confusion_matrix"]["labels"]
    counts = block["confusion_matrix"]["counts"]
    synthetic = m["kind"] == "synthetic"
    k = len(labels)
    head = "".join("<th>%s</th>" % _e(lab) for lab in labels)
    body = []
    for r in range(k):
        cells = []
        for c in range(k):
            if synthetic and counts[r][c]:
                cells.append('<td class="cell" data-block="%s" data-cell="%d,%d">%s</td>'
                             % (_e(name), r, c, _e(counts[r][c])))
            else:
                cells.append("<td>%s</td>" % _e(counts[r][c]))
        body.append("<tr><th>%s</th>%s<td>%s</td></tr>"
                    % (_e(labels[r]), "".join(cells), _e(sum(counts[r]))))
    col_totals = "".join("<td>%s</td>" % _e(sum(counts[r][c] for r in range(k)))
                         for c in range(k))
    body.append("<tr><th>total</th>%s<td>%s</td></tr>"
                % (col_totals, _e(sum(map(sum, counts)))))
    return ('<h3>%s confusion matrix (rows gold, columns predicted)</h3>'
            '<table data-matrix="%s"><thead><tr><th>gold \\ predicted</th>%s<th>total</th></tr>'
            '</thead><tbody>%s</tbody></table>' % (_e(name), _e(name), head, "".join(body)))


def _v(p) -> str:
    value = p.get("value") if isinstance(p, dict) else p
    return "" if value is None else repr(value)


def _per_class_html(m, name: str, targets) -> str:
    block = m[name]
    cols = ("tpr", "tnr", "precision", "f1", "f2", "f0_5")
    head = "".join('<th data-sort="%s">%s</th>' % (c, c)
                   for c in ("class", "support", "predicted") + cols) + "<th>target</th>"
    rows = []
    for label in block["confusion_matrix"]["labels"]:
        e = block["per_class"][label]
        if name == "action" and e["support"]:
            mk, text = judge(targets, "supporting.action_accuracy_per_action", e["tpr"]["value"])
        elif name == "action":
            mk, text = "note", "no support: not judged"
        else:
            mk, text = "no-target", NOT_SET
        cells = "".join('<td data-v="%s">%s</td>' % (_e(_v(e[c])), fmt_cell(e[c])) for c in cols)
        rows.append('<tr data-class="%s:%s" data-mark="%s"><td data-v="%s">%s</td>'
                    '<td data-v="%d">%s</td><td data-v="%d">%s</td>%s<td class="mark">%s</td>'
                    '</tr>' % (_e(name), _e(label), _e(mk), _e(label), _e(label), e["support"],
                               _e(e["support"]), e["predicted"], _e(e["predicted"]), cells,
                               _e(text)))
    avg_rows = []
    for avg in ("macro", "weighted"):
        a = block[avg]
        cells = "".join("<td>%s &#183; zero_filled=%s</td>" % (fmt_cell(a[c]), _e(a[c]["zero_filled"]))
                        if c in a else "<td></td>" for c in cols)
        avg_rows.append("<tr><th>%s</th><td colspan=\"2\">included: %s</td>%s<td></td></tr>"
                        % (_e(avg), _e(", ".join(map(str, a["included_classes"]))), cells))
    return ('<h3>%s per class</h3><table data-perclass="%s"><thead><tr>%s</tr></thead>'
            '<tbody class="sortable">%s</tbody><tbody>%s</tbody></table>'
            '<p>accuracy: %s</p><p>kappa: %s</p>'
            % (_e(name), _e(name), head, "".join(rows), "".join(avg_rows),
               fmt_cell(block["accuracy"]), fmt_cell(block["kappa"])))


def _value_of(p):
    return p.get("value") if isinstance(p, dict) else p


def _disjoint(a, b) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    x, y = a.get("ci"), b.get("ci")
    if x is None or y is None or "num" not in a or "num" not in b:
        return False
    return x[1] < y[0] or y[1] < x[0]


def _metric_values(m) -> dict:
    esc = m["escalation"]
    out = {"escalation.f2": esc["f2"], "escalation.tpr": esc["tpr"], "escalation.tnr": esc["tnr"],
           "escalation.precision": esc["precision"], "escalation.f1": esc["f1"],
           "escalation.fnr": esc["fnr"], "escalation.fpr": esc["fpr"],
           "action.accuracy": m["action"]["accuracy"], "intent.accuracy": m["intent"]["accuracy"]}
    ops = m["operations"]
    if ops is not None:
        for tier, p in ops["tier_shares"].items():
            out["operations.tier_shares." + tier] = p
        out["operations.cost_per_1000_usd"] = ops["cost_per_1000_usd"]
    return out


def _compare_html(m, other) -> str:
    a, b = _metric_values(m), _metric_values(other)
    order = [k for k in a] + [k for k in b if k not in a]
    tiers = sorted(k for k in order if k.startswith("operations.tier_shares."))
    order = [k for k in order if not k.startswith("operations.tier_shares.")
             and k != "operations.cost_per_1000_usd"] + tiers + \
        [k for k in order if k == "operations.cost_per_1000_usd"]
    rows = []
    for key in order:
        if key not in a or key not in b:
            here = fmt_cell(a[key]) if key in a else "not in this run"
            there = fmt_cell(b[key]) if key in b else "not in other run"
            rows.append('<tr data-compare="%s"><th>%s</th><td>%s</td><td>%s</td><td>%s</td>'
                        '<td></td></tr>' % (_e(key), _e(key), here if key in a else _e(here),
                                            there if key in b else _e(there),
                                            _e("not in other run" if key in a
                                               else "not in this run")))
            continue
        x, y = _value_of(a[key]), _value_of(b[key])
        delta = "n/a" if x is None or y is None else "%+.4f" % (x - y)
        flag = '<span class="nonoverlap">%s</span>' % NONOVERLAP if _disjoint(a[key], b[key]) else ""
        rows.append('<tr data-compare="%s"><th>%s</th><td>%s</td><td>%s</td><td>%s</td>'
                    '<td>%s</td></tr>' % (_e(key), _e(key), fmt_cell(a[key]), fmt_cell(b[key]),
                                          _e(delta), flag))
    runs = "".join("<li>%s: run_at %s; sample %s; config %s</li>"
                   % (_e(who), _e(r["run_at"]), _e(_pairs(r["sample"])), _e(_pairs(r["config"])))
                   for who, r in (("this run", m), ("other run", other)))
    return ('<section><h2>Compare</h2><ul>%s</ul><p class="note">Delta is this run minus the '
            'other run. A mark appears only when both 95%% ranges exist and do not touch.</p>'
            '<table><thead><tr><th>Rate</th><th>This run</th><th>Other run</th><th>Delta</th>'
            '<th>Ranges</th></tr></thead><tbody>%s</tbody></table></section>'
            % (runs, "".join(rows)))


def render(metrics, compare=None, targets=None, targets_meta=None, no_thresholds=False) -> str:
    m = metrics
    parts = [_header(m, targets_meta, no_thresholds), _summary_html(m, targets),
             _targets_html(m, targets), _escalation_html(m)]
    if compare is not None:
        parts.append(_compare_html(m, compare))
    matrices = '<section><h2>Confusion matrices</h2>'
    if m["kind"] == "gold":
        matrices += '<p class="note">%s</p>' % _e(GOLD_NOTE)
    matrices += _matrix_html(m, "action") + _matrix_html(m, "intent")
    if m["kind"] == "synthetic":
        matrices += '<h3 id="cell-title">Click a cell to list its case ids</h3><ul id="cell-ids"></ul>'
    matrices += "</section>"
    parts.append(matrices)
    parts.append("<section><h2>Per-class tables</h2>%s%s<p>%s</p></section>"
                 % (_per_class_html(m, "action", targets), _per_class_html(m, "intent", targets),
                    _e(m["averaging"])))
    blocks = _data_block("metrics-data", m)
    if compare is not None:
        blocks += _data_block("compare-data", compare)
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Triage performance report</title><style>%s</style></head><body>'
            '<header><h1>Triage performance report</h1>%s</header>%s%s<script>%s</script>'
            '</body></html>\n' % (_CSS, parts[0], "".join(parts[1:]), blocks, _JS))


# ---------------------------------------------------------------- CLI

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m triage.report",
                                description=__doc__.splitlines()[0])
    p.add_argument("metrics")
    p.add_argument("--compare")
    p.add_argument("--thresholds")
    p.add_argument("-o", "--out", required=True)
    return p


def _run(args) -> None:
    m = load_metrics(args.metrics)
    other = None
    if args.compare is not None:
        other = load_metrics(args.compare)
        refusal = compare_refusal(m, other)
        if refusal:
            raise ReportError(refusal)
    explicit = args.thresholds is not None
    targets, meta = load_thresholds(args.thresholds if explicit else DEFAULT_THRESHOLDS, explicit)
    page = render(m, compare=other, targets=targets, targets_meta=meta,
                  no_thresholds=targets is None)
    try:
        Path(args.out).write_text(page, encoding="utf-8")
    except OSError as e:
        raise ReportError("cannot write %s: %s" % (args.out, e.strerror or e))


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _run(args)
    except ReportError as e:
        print("error: %s" % " ".join(str(e).split()), file=sys.stderr)
        return 2
    print("page=%s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
