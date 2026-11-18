"""Tests for the read-only local admin console (triage.admin).

Every server here gets explicit fixture --rules and --thresholds paths; nothing reads the
owner's files. Run folders are built in temporary directories from tests/fixtures/admin/.
"""
import ast
import html
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from triage import admin

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIX = HERE / "fixtures" / "admin"
RULES = FIX / "rules.json"
THRESHOLDS = FIX / "thresholds.json"

PLANTED = ("PLANTED-THREAD-91827", "PLANTED-METRIC-ID-55", "PLANTED-NOTE-ID-33")


def _read(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _manifest(kind="synthetic", created_at="2026-10-02T08:15:00Z", usd=0.12, placeholders=True):
    m = _read("manifest_synthetic.json")
    m["kind"] = kind
    m["created_at"] = created_at
    m["cost"]["estimated_usd"] = usd
    m["cost"]["prices_are_placeholders"] = placeholders
    return m


def make_run(runs, name, manifest=None, metrics=None, extra=None):
    """Write one run folder. manifest/metrics are dicts (or raw strings) or None to omit."""
    d = Path(runs) / name
    d.mkdir()
    for fname, value in (("manifest.json", manifest), ("metrics.json", metrics)):
        if value is None:
            continue
        text = value if isinstance(value, str) else json.dumps(value)
        (d / fname).write_text(text, encoding="utf-8")
    for fname, text in (extra or {}).items():
        (d / fname).write_text(text, encoding="utf-8")
    return d


class ServerCase(unittest.TestCase):
    """Runs make_server on 127.0.0.1 port 0 in a test thread."""

    rules = RULES
    thresholds = THRESHOLDS

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self._tmp.name) / "runs"
        self.runs.mkdir()
        self.server = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(5)
        self._tmp.cleanup()

    def start(self, rules=None, thresholds=None):
        self.server = admin.make_server(str(self.runs), "127.0.0.1", 0,
                                        str(rules or self.rules),
                                        str(thresholds or self.thresholds))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, path, method="GET", host="default"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host == "default":
                conn.putheader("Host", "127.0.0.1:%d" % self.port)
            elif host is not None:
                conn.putheader("Host", host)
            conn.putheader("Content-Length", "0")
            conn.endheaders()
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8")
        finally:
            conn.close()

    def get(self, path):
        status, body = self.request(path)
        self.assertEqual(status, 200, path)
        return body


def _row(body, name):
    """The table row (or list item) of the page that names run `name`."""
    for chunk in body.split("<tr")[1:]:
        if html.escape(name) in chunk.split("</tr>")[0]:
            return chunk.split("</tr>")[0]
    raise AssertionError("no row for %s" % name)


class TestBind(unittest.TestCase):
    def test_bad_hosts_exit_2_before_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            for host in ("0.0.0.0", "localhost", "::", "::1"):
                with self.subTest(host=host):
                    rc = admin.main(["--runs", tmp, "--host", host, "--port", "0",
                                     "--rules", str(RULES), "--thresholds", str(THRESHOLDS)])
                    self.assertEqual(rc, 2)

    def test_missing_runs_dir_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = admin.main(["--runs", os.path.join(tmp, "absent"), "--port", "0",
                             "--rules", str(RULES), "--thresholds", str(THRESHOLDS)])
            self.assertEqual(rc, 2)
            f = Path(tmp) / "file"
            f.write_text("x")
            rc = admin.main(["--runs", str(f), "--port", "0",
                             "--rules", str(RULES), "--thresholds", str(THRESHOLDS)])
            self.assertEqual(rc, 2)

    def test_make_server_refuses_other_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            for host in ("localhost", "0.0.0.0", "::1"):
                with self.subTest(host=host):
                    with self.assertRaises(ValueError):
                        admin.make_server(tmp, host, 0, str(RULES), str(THRESHOLDS))

    def test_bound_address_is_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = admin.make_server(tmp, "127.0.0.1", 0, str(RULES), str(THRESHOLDS))
            try:
                self.assertEqual(server.server_address[0], "127.0.0.1")
            finally:
                server.server_close()

    def test_default_port_and_host(self):
        args = admin.parse_args(["--runs", "somewhere"])
        self.assertEqual(args.port, 8765)
        self.assertEqual(args.host, "127.0.0.1")


class TestHostHeader(ServerCase):
    def test_foreign_or_missing_host_is_403(self):
        self.start()
        self.assertEqual(self.request("/", host="evil.example")[0], 403)
        self.assertEqual(self.request("/", host="evil.example:8765")[0], 403)
        self.assertEqual(self.request("/", host=None)[0], 403)

    def test_loopback_hosts_are_200(self):
        self.start()
        self.assertEqual(self.request("/", host="127.0.0.1:%d" % self.port)[0], 200)
        self.assertEqual(self.request("/", host="localhost:9000")[0], 200)
        self.assertEqual(self.request("/", host="localhost")[0], 200)


class TestPages(ServerCase):
    HEADINGS = {"/": "Overview", "/runs": "Eval reports", "/drift": "Drift and incidents",
                "/policies": "Policies and thresholds"}

    def _check_all(self):
        for path, heading in self.HEADINGS.items():
            with self.subTest(path=path):
                self.assertIn("<h1>%s</h1>" % heading, self.get(path))

    def test_empty_runs_folder(self):
        self.start()
        self._check_all()
        self.assertIn("No runs yet.", self.get("/"))

    def test_one_run(self):
        make_run(self.runs, "run-a", _manifest(), _read("metrics_synthetic.json"))
        self.start()
        self._check_all()

    def test_three_runs(self):
        make_run(self.runs, "run-a", _manifest(created_at="2026-10-01T00:00:00Z"),
                 _read("metrics_synthetic.json"))
        make_run(self.runs, "run-b", _read("manifest_gold.json"), _read("metrics_gold.json"))
        make_run(self.runs, "run-c", _manifest(created_at="2026-10-05T00:00:00Z"))
        self.start()
        self._check_all()

    def test_drift_names_commands(self):
        self.start()
        body = self.get("/drift")
        self.assertIn("python3 -m triage.drift", body)
        self.assertIn("python3 -m triage.incidents", body)

    def test_overview_headline_rates(self):
        make_run(self.runs, "run-a", _manifest(), _read("metrics_synthetic.json"))
        self.start()
        body = self.get("/")
        # escalation.f2 from metrics_synthetic.json: 0.5555..., n 6, no ci, directional.
        self.assertIn("0.5556", body)
        self.assertIn("no interval", body)
        self.assertIn("directional", body)
        self.assertIn("run-a", body)


class TestRunOrder(ServerCase):
    def test_order_from_manifests_not_names_or_mtimes(self):
        make_run(self.runs, "a-run", _manifest(created_at="2026-10-02T00:00:00Z"))
        make_run(self.runs, "b-run", _manifest(created_at="2026-10-09T00:00:00Z"))
        make_run(self.runs, "c-run", _manifest(created_at="2026-10-05T00:00:00Z"))
        make_run(self.runs, "0-metrics-only", None, _read("metrics_synthetic.json"))
        make_run(self.runs, "1-broken", "{not json")
        # Folder times in the reverse of the created_at order.
        for i, name in enumerate(("b-run", "c-run", "a-run")):
            t = 1_600_000_000 + i * 86400
            os.utime(self.runs / name, (t, t))
        names = [r["name"] for r in admin.list_runs(str(self.runs))]
        self.assertEqual(names[:3], ["b-run", "c-run", "a-run"])
        self.assertEqual(set(names[3:]), {"0-metrics-only", "1-broken"})
        self.assertEqual(names[-1], "0-metrics-only")
        self.start()
        body = self.get("/runs")
        self.assertLess(body.index("b-run"), body.index("c-run"))
        self.assertLess(body.index("c-run"), body.index("a-run"))
        self.assertLess(body.index("a-run"), body.index("0-metrics-only"))
        self.assertIn("no manifest", _row(body, "0-metrics-only"))
        self.assertIn("unreadable manifest", _row(body, "1-broken"))
        for path in ("/", "/policies"):
            self.get(path)

    def test_bad_manifests_are_unreadable(self):
        good = _manifest()
        bad = {
            "badschema": dict(good, schema_version="run-manifest/9"),
            "badkind": dict(good, kind="gold"),
            "baddate": dict(good, created_at="2026-10-02 08:15:00"),
            "missingkey": {k: v for k, v in good.items() if k != "cost"},
            "notobject": [1, 2],
        }
        for name, m in bad.items():
            make_run(self.runs, name, m)
        runs = {r["name"]: r for r in admin.list_runs(str(self.runs))}
        for name in bad:
            self.assertEqual(runs[name]["status"], "unreadable manifest", name)


class TestNoGoldIds(ServerCase):
    def test_gold_report_and_planted_ids(self):
        make_run(self.runs, "gold-ok", _read("manifest_gold.json"), _read("metrics_gold.json"))
        planted_metrics = _read("metrics_gold.json")
        planted_metrics["action"]["ids"] = {"reply": ["PLANTED-METRIC-ID-55"]}
        make_run(self.runs, "gold-planted",
                 _manifest(kind="gold_random", created_at="2026-10-04T00:00:00Z"),
                 planted_metrics,
                 extra={"trace.jsonl": (FIX / "trace_gold.jsonl").read_text(encoding="utf-8"),
                        "notes.txt": "PLANTED-NOTE-ID-33\n"})
        self.start()
        report = self.get("/runs/gold-ok/report")
        self.assertIn("Gold runs are aggregate only", report)
        runs_body = self.get("/runs")
        self.assertIn("no usable metrics file", _row(runs_body, "gold-planted"))
        bodies = [self.get(p) for p in ("/", "/runs", "/runs/gold-ok/report",
                                        "/runs/gold-planted/report", "/policies")]
        self.assertIn("drill-down report not available", bodies[3])
        for body in bodies:
            for s in PLANTED:
                self.assertNotIn(s, body)


class TestShutdown(unittest.TestCase):
    def test_sigint_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONUNBUFFERED="1")
            proc = subprocess.Popen(
                [sys.executable, "-m", "triage.admin", "--runs", tmp, "--port", "0",
                 "--rules", str(RULES), "--thresholds", str(THRESHOLDS)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(ROOT),
                env=env)
            try:
                first = proc.stdout.readline()
                self.assertTrue(first.startswith("admin console on http://127.0.0.1:"), first)
                port = int(first.split("127.0.0.1:")[1].split("/")[0])
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", "/")
                self.assertEqual(conn.getresponse().status, 200)
                conn.close()
                proc.send_signal(signal.SIGINT)
                out, _ = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 0)
                self.assertIn("stopped", out)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
            with self.assertRaises(ConnectionRefusedError):
                socket.create_connection(("127.0.0.1", port), timeout=5).close()


class TestMethodsAndPaths(ServerCase):
    def test_other_methods_405(self):
        self.start()
        for method in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.request("/", method=method)[0], 405, method)

    def test_traversal_and_unknown_404(self):
        make_run(self.runs, "run-a", _manifest(), _read("metrics_synthetic.json"))
        self.start()
        for path in ("/runs/../x", "/runs/%2e%2e/x", "/runs/%2e%2e/report",
                     "/runs/..%2frun-a/report", "/runs/nope/report", "/nope",
                     "/runs/run-a", "/runs//etc/report"):
            self.assertEqual(self.request(path)[0], 404, path)
        self.assertEqual(self.request("/runs/run-a/report")[0], 200)


class TestPolicies(ServerCase):
    def test_fixture_rules_and_thresholds(self):
        self.start()
        body = self.get("/policies")
        for s in ("fixture-no-promise", "fixture-no-pii", "fixture-legal-topic",
                  "fixture-safety-topic", "fixture-rules-v3", "Fixture rule: never promise a gift.",
                  "guardrails.false_escalation_rate", "not set", "Currently on disk"):
            self.assertIn(s, body)
        for key, value in (("window_days", "9"), ("min_conversations", "4"),
                           ("effect", "escalate"), ("report_window_hours", "36")):
            self.assertIn("%s: %s" % (key, value), body)

    def test_broken_repeat_contact_block(self):
        data = _read("rules.json")
        del data["repeat_contact"]["window_days"]
        path = Path(self._tmp.name) / "rules_broken.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.start(rules=path)
        body = self.get("/policies")
        self.assertIn("fixture-no-promise", body)
        self.assertIn("fixture-legal-topic", body)
        self.assertIn(html.escape("repeat_contact rule in %s: 'window_days' is missing" % path),
                      body)
        self.assertNotIn("Traceback", body)

    def test_rules_not_json(self):
        path = Path(self._tmp.name) / "rules_bad.json"
        path.write_text("{not json", encoding="utf-8")
        self.start(rules=path)
        body = self.get("/policies")
        self.assertIn("not valid JSON", body)
        self.assertNotIn("Traceback", body)
        self.assertNotIn("fixture-no-promise", body)

    def test_missing_thresholds_file(self):
        self.start(thresholds=Path(self._tmp.name) / "absent.json")
        body = self.get("/policies")
        self.assertIn("thresholds file not found", body)
        self.assertIn("0.5", body)


class TestEffectiveConfig(ServerCase):
    def test_threshold_compared_with_disk(self):
        m40 = _read("metrics_synthetic.json")
        m40["config"]["threshold"] = 0.4
        m50 = _read("metrics_synthetic.json")
        m50["config"]["threshold"] = 0.5
        make_run(self.runs, "run-040", _manifest(created_at="2026-10-02T00:00:00Z"), m40)
        make_run(self.runs, "run-050", _manifest(created_at="2026-10-03T00:00:00Z"), m50)
        make_run(self.runs, "run-nometrics", _manifest(created_at="2026-10-04T00:00:00Z"))
        self.start()
        body = self.get("/policies")
        section = body.split("Each run&#x27;s effective config")[1]
        row40 = _row(section, "run-040")
        self.assertIn("0.4", row40)
        self.assertIn("0.5", row40)
        self.assertIn("differs from now", row40)
        self.assertIn("not recorded", row40)
        row50 = _row(section, "run-050")
        self.assertNotIn("differs from now", row50)
        self.assertIn("not recorded", row50)
        self.assertIn("config not recorded", _row(section, "run-nometrics"))


class TestSpend(ServerCase):
    def _month_times(self):
        now = datetime.now(timezone.utc)
        this1 = now.strftime("%Y-%m-01T00:00:01Z")
        this2 = now.strftime("%Y-%m-01T00:00:02Z")
        prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m-15T12:00:00Z")
        return this1, this2, prev

    def test_run_and_month_spend(self):
        this1, this2, prev = self._month_times()
        make_run(self.runs, "r1", _manifest(created_at=this1, usd=0.10))
        make_run(self.runs, "r2", _manifest(created_at=this2, usd=0.25))
        make_run(self.runs, "r0", _manifest(created_at=prev, usd=1.00))
        self.start()
        body = self.get("/")
        self.assertIn("This run: $0.25 estimated", body)
        self.assertIn("Per-run cap: $2.50", body)
        self.assertIn("This month: $0.35 estimated across 2 runs", body)
        self.assertIn("Monthly cap: $123", body)
        self.assertIn("placeholder prices, not real spend; API-equivalent dollars", body)
        self.assertIn("Estimated from run manifests", body)

    def test_null_cost(self):
        this1, _, _ = self._month_times()
        make_run(self.runs, "r1", _manifest(created_at=this1, usd=None, placeholders=False))
        self.start()
        body = self.get("/")
        self.assertIn("No cost recorded for this run.", body)
        self.assertNotIn("placeholder prices, not real spend", body)

    def test_monthly_cap_not_set(self):
        data = _read("thresholds.json")
        del data["guardrails"]["api_spend_usd_per_month"]
        path = Path(self._tmp.name) / "thresholds_nocap.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        make_run(self.runs, "r1", _manifest(created_at=self._month_times()[0]))
        self.start(thresholds=path)
        self.assertIn("Monthly cap: not set", self.get("/"))


class TestOffline(ServerCase):
    def test_no_external_urls(self):
        make_run(self.runs, "syn", _manifest(), _read("metrics_synthetic.json"))
        make_run(self.runs, "gold", _read("manifest_gold.json"), _read("metrics_gold.json"))
        self.start()
        for path in ("/", "/runs", "/drift", "/policies", "/runs/syn/report",
                     "/runs/gold/report"):
            body = self.get(path)
            self.assertNotIn("http://", body, path)
            self.assertNotIn("https://", body, path)
            self.assertNotIn("<script src", body, path)
            self.assertNotIn('rel="stylesheet"', body, path)


class TestSourceRules(unittest.TestCase):
    def test_tests_do_not_read_owner_files(self):
        text = Path(__file__).read_text(encoding="utf-8")
        for s in ("polic" + "ies/", "evals/" + "thresholds.json", "evals/" + "drift.json",
                  "evals/" + "gold", "brand" + "s/", "data/" + "raw"):
            self.assertNotIn(s, text)

    def test_stdlib_only(self):
        tree = ast.parse((ROOT / "src" / "triage" / "admin.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "relative import")
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                self.assertTrue(top == "triage" or top in sys.stdlib_module_names, name)


if __name__ == "__main__":
    unittest.main()
