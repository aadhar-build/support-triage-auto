"""Read-only local admin console: `python3 -m triage.admin --runs DIR [--port N]`.

A plain single-threaded `http.server.HTTPServer`, bound to the literal 127.0.0.1 only and
run in the foreground until Ctrl-C. Standard library only, no external URLs. It reads
each run folder's `manifest.json` and `metrics.json` (the latter only through
`triage.report.load_metrics`) and the `--rules` and `--thresholds` files, and writes
nothing.
"""
import argparse
import html
import json
import math
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote, unquote, urlsplit

from triage import context_signals, policy_check, report
from triage.metrics import COHORT_NAMES, check_run_at
from triage.router import DEFAULT_CONFIDENCE_THRESHOLD
from triage.trace import REPO_ROOT

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_RULES = REPO_ROOT / "policies" / "rules.json"
MANIFEST_SCHEMA = "run-manifest/1"
MANIFEST_KEYS = ("schema_version", "kind", "created_at", "cost")
COST_KEYS = ("estimated_usd", "cost_per_1000_usd", "prices_are_placeholders")
KINDS = tuple(name for names in COHORT_NAMES.values() for name in names)
ALLOWED_HOSTS = ("127.0.0.1", "localhost")

NO_MANIFEST = "no manifest"
BAD_MANIFEST = "unreadable manifest"
NO_METRICS = "no usable metrics file"
PER_RUN_CAP = "Per-run cap: $2.50 (#10)"
PLACEHOLDER_NOTE = "placeholder prices, not real spend; API-equivalent dollars"
SPEND_SOURCE = "Estimated from run manifests; #10's spend log is not read yet."
HEADLINES = (("escalation", "f2", "Escalation F2"), ("escalation", "tpr", "Escalation TPR"),
             ("escalation", "tnr", "Escalation TNR"),
             ("escalation", "precision", "Escalation precision"),
             ("escalation", "f1", "Escalation F1"),
             ("escalation", "fnr", "Missed escalation rate"),
             ("escalation", "fpr", "False escalation rate"),
             ("action", "accuracy", "Action accuracy"), ("intent", "accuracy", "Intent accuracy"))


def _e(value) -> str:
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------- runs

def _number_or_null(v) -> bool:
    return v is None or (not isinstance(v, bool) and isinstance(v, (int, float))
                         and math.isfinite(v) and v >= 0)


def _check_manifest(m) -> dict:
    """The manifest's fields, or ValueError for any shape problem."""
    if not isinstance(m, dict) or set(m) != set(MANIFEST_KEYS):
        raise ValueError("bad keys")
    if m["schema_version"] != MANIFEST_SCHEMA or m["kind"] not in KINDS:
        raise ValueError("bad schema or kind")
    check_run_at(m["created_at"])
    cost = m["cost"]
    if not isinstance(cost, dict) or set(cost) != set(COST_KEYS):
        raise ValueError("bad cost keys")
    if not (_number_or_null(cost["estimated_usd"]) and _number_or_null(cost["cost_per_1000_usd"])
            and isinstance(cost["prices_are_placeholders"], bool)):
        raise ValueError("bad cost values")
    return {"kind": m["kind"], "created_at": m["created_at"], "cost": dict(cost)}


def _time_key(created_at: str):
    base = datetime.strptime(created_at[:19], "%Y-%m-%dT%H:%M:%S")
    frac = created_at[19:-1]
    return base, float("0" + frac) if frac else 0.0


def list_runs(runs_dir) -> List[dict]:
    """One dict per direct subfolder of runs_dir, newest manifest first.

    Each has `name`, `status` ("ok", "no manifest" or "unreadable manifest") and, for ok
    runs, `kind`, `created_at` and `cost`. Only manifest.json is opened; folder times are
    never read. Unreadable manifests follow the valid ones, then folders with no manifest,
    each group by name."""
    good, other = [], []
    with os.scandir(runs_dir) as it:
        entries = [e for e in it if e.is_dir(follow_symlinks=False)]
    for entry in entries:
        run = {"name": entry.name, "path": entry.path}
        path = Path(entry.path) / "manifest.json"
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            run["status"] = NO_MANIFEST
            other.append(run)
            continue
        except (OSError, UnicodeDecodeError):
            run["status"] = BAD_MANIFEST
            other.append(run)
            continue
        try:
            run.update(_check_manifest(json.loads(text)))
        except (ValueError, TypeError, RecursionError):
            run["status"] = BAD_MANIFEST
            other.append(run)
            continue
        run["status"] = "ok"
        good.append(run)
    good.sort(key=lambda r: r["name"])
    good.sort(key=lambda r: _time_key(r["created_at"]), reverse=True)
    other.sort(key=lambda r: (r["status"] == NO_MANIFEST, r["name"]))
    return good + other


def _metrics(run) -> Optional[dict]:
    """The run's checked metrics, or None. Only runs with a valid manifest are read."""
    if run["status"] != "ok":
        return None
    try:
        return report.load_metrics(Path(run["path"]) / "metrics.json")
    except (report.ReportError, OSError, RecursionError):
        return None


# ---------------------------------------------------------------- formatting helpers

def _usd(v) -> str:
    text = "%.4f" % v
    while text.endswith("0") and len(text.split(".")[1]) > 2:
        text = text[:-1]
    return "$" + text


def _num4(v) -> str:
    return "n/a" if v is None else "%.4f" % v


def _rate(p) -> str:
    ci = p.get("ci")
    interval = "no interval" if ci is None else "%s to %s" % (_num4(ci[0]), _num4(ci[1]))
    badge = ' <span class="badge">directional</span>' if p.get("directional") is True else ""
    return "%s &#183; n=%s &#183; %s%s" % (_e(_num4(p.get("value"))), _e(p.get("n")),
                                           _e(interval), badge)


def _load_thresholds(path):
    try:
        data, _ = report.load_thresholds(path, explicit=True)
        return data
    except (report.ReportError, RecursionError):
        return None


def _monthly_cap(thresholds) -> Optional[float]:
    try:
        v = thresholds["guardrails"]["api_spend_usd_per_month"]["max"]
    except (KeyError, TypeError):
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def _disk_threshold(thresholds):
    """(value, is_set): the on-disk confidence threshold, or the code default."""
    try:
        v = thresholds["confidence_threshold"]["value"]
    except (KeyError, TypeError):
        return DEFAULT_CONFIDENCE_THRESHOLD, False
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return DEFAULT_CONFIDENCE_THRESHOLD, False
    return v, True


_CSS = """
body{font-family:system-ui,sans-serif;margin:1rem auto;max-width:60rem;padding:0 1rem;
color:#1b1b1b;background:#fff}
nav a{margin-right:1rem}table{border-collapse:collapse;margin:.5rem 0}
th,td{border:1px solid #ccc;padding:.25rem .5rem;text-align:left;vertical-align:top}
.badge{background:#fde7b0;border-radius:.25rem;padding:0 .3rem;font-size:.85em}
.note{color:#555}.error{color:#a00}
"""


def _page(title: str, body: str) -> str:
    nav = ('<nav><a href="/">Overview</a><a href="/runs">Eval reports</a>'
           '<a href="/drift">Drift and incidents</a>'
           '<a href="/policies">Policies and thresholds</a></nav>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>%s</title><style>%s</style></head><body>%s<h1>%s</h1>%s</body></html>\n'
            % (_e(title), _CSS, nav, _e(title), body))


def _table(headers, rows) -> str:
    head = "".join("<th>%s</th>" % _e(h) for h in headers)
    return "<table><tr>%s</tr>%s</table>" % (head, "".join(rows))


# ---------------------------------------------------------------- pages

def page_overview(runs_dir, thresholds_path) -> str:
    runs = list_runs(runs_dir)
    ok = [r for r in runs if r["status"] == "ok"]
    if not ok:
        body = "<p>No runs yet.</p>" if not runs else "<p>No run has a valid manifest.</p>"
        return _page("Overview", body)
    latest = ok[0]
    parts = ["<section><h2>Latest run</h2><ul><li>name: %s</li><li>kind: %s</li>"
             "<li>created_at: %s</li></ul>" % (_e(latest["name"]), _e(latest["kind"]),
                                                _e(latest["created_at"]))]
    m = _metrics(latest)
    if m is None:
        parts.append("<p>No metrics for this run.</p>")
    else:
        rows = ["<tr><td>%s</td><td>%s</td></tr>" % (_e(label), _rate(m[block][key]))
                for block, key, label in HEADLINES]
        parts.append(_table(("Rate", "Value"), rows))
    parts.append("</section>")

    now = datetime.now(timezone.utc)
    month = [r for r in ok if r["created_at"][:7] == now.strftime("%Y-%m")]
    usd = latest["cost"]["estimated_usd"]
    this_run = ("No cost recorded for this run." if usd is None
                else "This run: %s estimated" % _usd(usd))
    total = sum(r["cost"]["estimated_usd"] or 0 for r in month)
    nulls = sum(1 for r in month if r["cost"]["estimated_usd"] is None)
    month_line = "This month: %s estimated across %d run%s" % (
        _usd(total), len(month), "" if len(month) == 1 else "s")
    month_line += " (%d with no cost recorded)" % nulls
    cap = _monthly_cap(_load_thresholds(thresholds_path))
    cap_line = "Monthly cap: not set" if cap is None else "Monthly cap: %s" % _usd(cap)
    spend = ["<section><h2>Spend</h2><ul>",
             "<li>%s &#183; %s</li>" % (_e(this_run), _e(PER_RUN_CAP)),
             "<li>%s &#183; %s</li></ul>" % (_e(month_line), _e(cap_line))]
    if any(r["cost"]["prices_are_placeholders"] for r in [latest] + month):
        spend.append('<p class="note">%s</p>' % _e(PLACEHOLDER_NOTE))
    spend.append('<p class="note">%s</p></section>' % _e(SPEND_SOURCE))
    parts.extend(spend)
    return _page("Overview", "".join(parts))


def page_runs(runs_dir) -> str:
    runs = list_runs(runs_dir)
    if not runs:
        return _page("Eval reports", "<p>No runs yet.</p>")
    rows = []
    for r in runs:
        if r["status"] != "ok":
            rows.append("<tr><td>%s</td><td colspan=\"4\">%s</td></tr>"
                        % (_e(r["name"]), _e(r["status"])))
            continue
        usd = r["cost"]["estimated_usd"]
        cost = "no cost recorded" if usd is None else _usd(usd) + " estimated"
        if _metrics(r) is None:
            metrics = "no (%s)" % NO_METRICS
            name = _e(r["name"])
        else:
            metrics = "yes"
            name = '<a href="/runs/%s/report">%s</a>' % (_e(quote(r["name"], safe="")),
                                                         _e(r["name"]))
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                    % (name, _e(r["kind"]), _e(r["created_at"]), _e(cost), _e(metrics)))
    return _page("Eval reports", _table(("Run", "Kind", "created_at", "Estimated cost",
                                         "Has metrics"), rows))


def page_report(run, thresholds_path) -> str:
    m = _metrics(run)
    if m is None:
        return _page("Eval reports", "<p>%s: drill-down report not available (%s).</p>"
                     % (_e(run["name"]), _e(NO_METRICS)))
    try:
        targets, meta = report.load_thresholds(thresholds_path, explicit=True)
    except (report.ReportError, RecursionError):
        targets, meta = None, None
    return report.render(m, targets=targets, targets_meta=meta, no_thresholds=targets is None)


def page_drift() -> str:
    body = ("<p>The console doesn't show drift or incident outputs yet.</p><ul>"
            "<li>The weekly drift check (#20) runs as "
            "<code>python3 -m triage.drift --split dev --report-out PATH</code>.</li>"
            "<li>Incident grouping (#21) runs as "
            "<code>python3 -m triage.incidents --split dev --report-out PATH</code>.</li></ul>"
            "<p>Each writes an aggregate report to a path of the owner's choosing. "
            "Showing those reports here is a later slice.</p>")
    return _page("Drift and incidents", body)


def _descriptions(rules_path) -> dict:
    """{(section, id): description} from the rules file, for display only."""
    out = {}
    try:
        data = json.loads(Path(rules_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return out
    if not isinstance(data, dict):
        return out
    for section in ("public_reply_rules", "escalate_always"):
        items = data.get(section)
        for item in items if isinstance(items, list) else ():
            if isinstance(item, dict) and isinstance(item.get("description"), str):
                out[(section, item.get("id"))] = item["description"]
    return out


def _error(exc) -> str:
    if isinstance(exc, RecursionError):
        return '<p class="error">rules file is nested too deeply to read</p>'
    return '<p class="error">%s</p>' % _e(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)


def _rules_html(rules_path) -> str:
    desc = _descriptions(rules_path)
    parts = ["<h3>Rules file version</h3>"]
    try:
        version = policy_check.rules_version(rules_path)
        parts.append("<p>%s</p>" % _e("no version" if version is None else version))
    except (policy_check.RulesError, RecursionError) as exc:
        parts.append(_error(exc))
    parts.append("<h3>Public reply rules</h3>")
    try:
        rows = ["<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (_e(r.id), _e(r.type), _e(", ".join(r.phrases or r.detect)),
                   _e(desc.get(("public_reply_rules", r.id), "")))
                for r in policy_check.load_rules(rules_path)]
        parts.append(_table(("id", "type", "phrases or detectors", "description"), rows))
    except (policy_check.RulesError, RecursionError) as exc:
        parts.append(_error(exc))
    parts.append("<h3>Always-escalate topics</h3>")
    try:
        rows = ["<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (_e(t.id), _e(", ".join(t.phrases)),
                   _e(desc.get(("escalate_always", t.id), "")))
                for t in policy_check.load_escalate_topics(rules_path)]
        parts.append(_table(("id", "phrases", "description"), rows))
    except (policy_check.RulesError, RecursionError) as exc:
        parts.append(_error(exc))
    parts.append("<h3>Repeat-contact rule</h3>")
    try:
        rule = context_signals.load_repeat_contact_rule(rules_path)
        parts.append("<ul>%s</ul>" % "".join(
            "<li>%s: %s</li>" % (_e(k), _e(getattr(rule, k)))
            for k in ("window_days", "min_conversations", "effect", "report_window_hours")))
    except (context_signals.RepeatContactError, RecursionError) as exc:
        parts.append(_error(exc))
    return "".join(parts)


def _bounds(leaf) -> str:
    if not isinstance(leaf, dict):
        return "unreadable"
    found = ["%s %s" % (k, leaf[k]) for k in ("min", "max", "max_count") if k in leaf]
    return ", ".join(found) or "no bound"


def _thresholds_html(thresholds) -> str:
    default = ("not set; the code uses router.DEFAULT_CONFIDENCE_THRESHOLD (%s), a placeholder"
               % DEFAULT_CONFIDENCE_THRESHOLD)
    if thresholds is None:
        return "<h3>Thresholds</h3><p>thresholds file not found</p><p>Confidence threshold: %s</p>" \
            % _e(default)
    rows = []
    for group in report.TARGET_GROUPS:
        leaves = thresholds.get(group)
        for name, leaf in (leaves.items() if isinstance(leaves, dict) else ()):
            rows.append("<tr><td>%s</td><td>%s</td></tr>"
                        % (_e("%s.%s" % (group, name)), _e(_bounds(leaf))))
    parts = ["<h3>Thresholds</h3>", _table(("target", "bound"), rows)]
    reporting = thresholds.get("reporting")
    if isinstance(reporting, dict):
        parts.append("<h3>Reporting</h3><ul>%s</ul>" % "".join(
            "<li>%s: %s</li>" % (_e(k), _e(v)) for k, v in reporting.items()))
    value, is_set = _disk_threshold(thresholds)
    parts.append("<p>Confidence threshold: %s</p>" % _e(value if is_set else default))
    return "".join(parts)


def page_policies(runs_dir, rules_path, thresholds_path) -> str:
    thresholds = _load_thresholds(thresholds_path)
    disk, _ = _disk_threshold(thresholds)
    parts = ["<section><h2>Currently on disk</h2>", _rules_html(rules_path),
             _thresholds_html(thresholds), "</section>",
             "<section><h2>%s</h2>" % _e("Each run's effective config")]
    rows = []
    for r in list_runs(runs_dir):
        if r["status"] != "ok":
            rows.append("<tr><td>%s</td><td colspan=\"2\">%s</td><td colspan=\"3\">"
                        "config not recorded</td></tr>" % (_e(r["name"]), _e(r["status"])))
            continue
        m = _metrics(r)
        if m is None:
            rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td colspan=\"3\">"
                        "config not recorded</td></tr>"
                        % (_e(r["name"]), _e(r["kind"]), _e(r["created_at"])))
            continue
        threshold = m["config"]["threshold"]
        mark = " &#183; differs from now" if threshold != disk else ""
        prices = m["config"]["prices"]
        prices_text = "none" if prices is None else ", ".join(
            "%s %s" % (k, v) for k, v in sorted(prices.items()))
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s (now %s)%s</td>"
                    "<td>%s</td><td>not recorded</td></tr>"
                    % (_e(r["name"]), _e(r["kind"]), _e(r["created_at"]),
                       _e(_num_plain(threshold)), _e(disk), mark, _e(prices_text)))
    if rows:
        parts.append(_table(("Run", "Kind", "created_at", "Threshold", "Prices",
                             "Rules version"), rows))
    else:
        parts.append("<p>No runs yet.</p>")
    parts.append("</section>")
    return _page("Policies and thresholds", "".join(parts))


def _num_plain(v) -> str:
    return "not recorded" if v is None else str(v)


# ---------------------------------------------------------------- server

def _host_ok(header) -> bool:
    if not header:
        return False
    host = header.strip().lower()
    if host.startswith("["):
        return False
    host = host.rsplit(":", 1)[0] if ":" in host else host
    return host in ALLOWED_HOSTS


class _Handler(BaseHTTPRequestHandler):
    runs_dir = ""
    rules_path = ""
    thresholds_path = ""
    server_version = "triage-admin"
    sys_version = ""

    def log_message(self, format, *args):  # noqa: A002 - quiet console
        pass

    def _send(self, status: int, body: str, ctype: str = "text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _text(self, status: int, text: str):
        self._send(status, text + "\n", "text/plain; charset=utf-8")

    def _refuse(self):
        if not _host_ok(self.headers.get("Host")):
            self._text(403, "forbidden: Host must be 127.0.0.1 or localhost")
            return
        self._text(405, "method not allowed: this console is read-only")

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _refuse

    def do_GET(self):
        if not _host_ok(self.headers.get("Host")):
            self._text(403, "forbidden: Host must be 127.0.0.1 or localhost")
            return
        try:
            status, body = self._route(urlsplit(self.path).path)
        except Exception:  # never show a traceback or file content
            self._text(500, "internal error")
            return
        if status == 404:
            self._text(404, "not found")
        else:
            self._send(status, body)

    def _route(self, path: str):
        if path == "/":
            return 200, page_overview(self.runs_dir, self.thresholds_path)
        if path == "/runs":
            return 200, page_runs(self.runs_dir)
        if path == "/drift":
            return 200, page_drift()
        if path == "/policies":
            return 200, page_policies(self.runs_dir, self.rules_path, self.thresholds_path)
        parts = path.split("/")
        if len(parts) == 4 and parts[0] == "" and parts[1] == "runs" and parts[3] == "report":
            name = unquote(parts[2])
            for run in list_runs(self.runs_dir):
                if run["name"] == name:
                    return 200, page_report(run, self.thresholds_path)
        return 404, ""


def make_server(runs_dir, host, port, rules_path, thresholds_path) -> HTTPServer:
    """A plain HTTPServer on ("127.0.0.1", port). Any other host raises ValueError first."""
    if host != HOST:
        raise ValueError("host must be the literal %s, got %r" % (HOST, host))
    handler = type("AdminHandler", (_Handler,), {
        "runs_dir": str(runs_dir), "rules_path": str(rules_path),
        "thresholds_path": str(thresholds_path)})
    server = HTTPServer((HOST, port), handler)
    if server.server_address[0] != HOST:
        server.server_close()
        raise ValueError("server bound to %r, not %s" % (server.server_address[0], HOST))
    return server


def parse_args(argv):
    p = argparse.ArgumentParser(prog="python3 -m triage.admin",
                                description="Read-only local admin console (foreground).")
    p.add_argument("--runs", required=True, help="folder of run subfolders (read only)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="port (0 picks a free one)")
    p.add_argument("--host", default=HOST, help="only the literal 127.0.0.1 is accepted")
    p.add_argument("--rules", default=str(DEFAULT_RULES), help="rules file (read only)")
    p.add_argument("--thresholds", default=str(report.DEFAULT_THRESHOLDS),
                   help="thresholds file (read only)")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.host != HOST:
        print("error: --host must be the literal %s" % HOST, file=sys.stderr)
        return 2
    if not os.path.isdir(args.runs):
        print("error: --runs %s is not a directory" % args.runs, file=sys.stderr)
        return 2
    try:
        server = make_server(args.runs, args.host, args.port, args.rules, args.thresholds)
    except (OSError, ValueError, OverflowError) as exc:
        print("error: cannot start the console: %s" % exc, file=sys.stderr)
        return 1
    print("admin console on http://%s:%d/ (Ctrl-C to stop)" % (HOST, server.server_address[1]),
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
        print("stopped", flush=True)
        return 0
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
