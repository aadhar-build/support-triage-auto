package admin

import (
	"bufio"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
	"github.com/aadhar-build/support-triage-auto/backend/internal/policy"
	"github.com/aadhar-build/support-triage-auto/backend/internal/report"
	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
)

const wantCSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"

type resp struct {
	status int
	header http.Header
	body   string
}

// testServer starts NewHandler(cfg) on an httptest server closed by t.Cleanup.
func testServer(t *testing.T, cfg Config) *httptest.Server {
	t.Helper()
	if cfg.Now == nil {
		cfg.Now = func() time.Time { return overviewNow }
	}
	srv := httptest.NewServer(NewHandler(cfg))
	t.Cleanup(srv.Close)
	return srv
}

// raw sends one request line and headers exactly as given, so no client
// cleans the path or fills in a Host.
func raw(t *testing.T, srv *httptest.Server, method, path string, host *string, proto string) resp {
	t.Helper()
	conn, err := net.Dial("tcp", srv.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	req := fmt.Sprintf("%s %s %s\r\n", method, path, proto)
	if host != nil {
		req += "Host: " + *host + "\r\n"
	}
	req += "Connection: close\r\n\r\n"
	if _, err := io.WriteString(conn, req); err != nil {
		t.Fatal(err)
	}
	r, err := http.ReadResponse(bufio.NewReader(conn), &http.Request{Method: method})
	if err != nil {
		t.Fatal(err)
	}
	defer r.Body.Close()
	b, err := io.ReadAll(r.Body)
	if err != nil {
		t.Fatal(err)
	}
	if r.Header.Get("X-Content-Type-Options") != "nosniff" {
		t.Errorf("%s %s: no nosniff header", method, path)
	}
	return resp{r.StatusCode, r.Header, string(b)}
}

func hostOf(srv *httptest.Server) *string {
	h := srv.Listener.Addr().String()
	return &h
}

func get(t *testing.T, srv *httptest.Server, path string) resp {
	t.Helper()
	return raw(t, srv, "GET", path, hostOf(srv), "HTTP/1.1")
}

func runsCfg(t *testing.T, runs string) Config {
	return Config{Runs: runs, Thresholds: fixturePath(t, "thresholds.json"), Web: t.TempDir()}
}

func str(s string) *string { return &s }

func TestHostHeader(t *testing.T) {
	srv := testServer(t, runsCfg(t, t.TempDir()))
	port := srv.Listener.Addr().(*net.TCPAddr).Port
	for _, h := range []*string{str("evil.example"), str("127.0.0.1.evil"), str("[::1]:8770"), str("localhost."), str("")} {
		r := raw(t, srv, "GET", "/api/runs", h, "HTTP/1.1")
		if r.status != 403 || r.body != "forbidden: Host must be 127.0.0.1 or localhost\n" {
			t.Errorf("Host %q: %d %q", *h, r.status, r.body)
		}
	}
	if r := raw(t, srv, "GET", "/api/runs", nil, "HTTP/1.0"); r.status != 403 || r.body != "forbidden: Host must be 127.0.0.1 or localhost\n" {
		t.Errorf("missing Host: %d %q", r.status, r.body)
	}
	for _, h := range []string{"127.0.0.1", fmt.Sprintf("127.0.0.1:%d", port), "localhost", "LOCALHOST:1", "127.0.0.1:abc"} {
		if r := raw(t, srv, "GET", "/api/runs", &h, "HTTP/1.1"); r.status != 200 {
			t.Errorf("Host %q: %d", h, r.status)
		}
	}
}

func TestMethods(t *testing.T) {
	srv := testServer(t, runsCfg(t, t.TempDir()))
	for _, m := range []string{"POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"} {
		for _, p := range []string{"/", "/api/runs"} {
			r := raw(t, srv, m, p, hostOf(srv), "HTTP/1.1")
			if r.status != 405 || (m != "HEAD" && r.body != "method not allowed: this console is read-only\n") {
				t.Errorf("%s %s: %d %q", m, p, r.status, r.body)
			}
			if r := raw(t, srv, m, p, str("evil.example"), "HTTP/1.1"); r.status != 403 {
				t.Errorf("%s %s foreign host: %d", m, p, r.status)
			}
		}
	}
}

func TestPathsAndTraversal(t *testing.T) {
	runs := t.TempDir()
	makeRun(t, runs, "run-a", goodManifest(), readFixture(t, "metrics_synthetic.json"))
	cfg := runsCfg(t, runs)
	writeFile(t, cfg.Web, "index.html", "<!doctype html>")
	if err := os.Mkdir(filepath.Join(cfg.Web, "assets"), 0o755); err != nil {
		t.Fatal(err)
	}
	writeFile(t, cfg.Web, "assets/real.js", "x")
	writeFile(t, cfg.Web, "assets/x.html", "x")
	writeFile(t, cfg.Web, "assets/.hidden.js", "x")
	if err := os.Symlink(filepath.Join(cfg.Web, "assets", "real.js"), filepath.Join(cfg.Web, "assets", "link.js")); err != nil {
		t.Fatal(err)
	}
	srv := testServer(t, cfg)
	if r := get(t, srv, "/api/runs/run-a/metrics"); r.status != 200 {
		t.Fatalf("control: %d", r.status)
	}
	if r := get(t, srv, "/assets/real.js"); r.status != 200 {
		t.Fatalf("asset control: %d", r.status)
	}
	for _, p := range []string{"/nope", "/api/runs/", "/api/runs/nope/metrics", "/api/runs/../x/metrics",
		"/api/runs/%2e%2e/metrics", "/api/runs/..%2frun-a/metrics", "/api/runs//etc/metrics",
		"/api/runs/run-a", "/api/runs/run-a/metrics/x", "/runs/a/b", "/assets/../index.html",
		"/assets/x.html", "/assets/.hidden.js", "/assets/link.js", "/runs/"} {
		r := get(t, srv, p)
		if r.status != 404 || r.body != "not found\n" || r.header.Get("Location") != "" {
			t.Errorf("%s: %d %q", p, r.status, r.body)
		}
	}
}

func TestInternalError(t *testing.T) {
	cfg := runsCfg(t, t.TempDir())
	cfg.panicPath = "/api/runs"
	srv := testServer(t, cfg)
	r := get(t, srv, "/api/runs")
	if r.status != 500 || r.body != "internal error\n" {
		t.Errorf("panic: %d %q", r.status, r.body)
	}
	if r := get(t, srv, "/api/overview"); r.status != 200 {
		t.Errorf("next request: %d", r.status)
	}
	// A runs folder that has gone away is an internal error too.
	gone := runsCfg(t, filepath.Join(t.TempDir(), "absent"))
	srv = testServer(t, gone)
	if r := get(t, srv, "/api/runs"); r.status != 500 || r.body != "internal error\n" {
		t.Errorf("unreadable runs: %d %q", r.status, r.body)
	}
}

func TestHeaders(t *testing.T) {
	cfg := runsCfg(t, t.TempDir())
	writeFile(t, cfg.Web, "index.html", "<!doctype html>")
	srv := testServer(t, cfg)
	for _, p := range []string{"/api/runs", "/api/overview"} {
		r := get(t, srv, p)
		if r.header.Get("Content-Type") != "application/json; charset=utf-8" {
			t.Errorf("%s: Content-Type %q", p, r.header.Get("Content-Type"))
		}
	}
	r := get(t, srv, "/")
	if r.header.Get("Content-Type") != "text/html; charset=utf-8" || r.header.Get("Content-Security-Policy") != wantCSP {
		t.Errorf("html headers: %v", r.header)
	}
	for _, r := range []resp{get(t, srv, "/nope"), raw(t, srv, "POST", "/", hostOf(srv), "HTTP/1.1"), raw(t, srv, "GET", "/", str("evil"), "HTTP/1.1")} {
		if r.header.Get("Content-Type") != "text/plain; charset=utf-8" {
			t.Errorf("text Content-Type %q", r.header.Get("Content-Type"))
		}
	}
	// nosniff is asserted on every response by raw.
}

func TestAPIRuns(t *testing.T) {
	runs := t.TempDir()
	makeRun(t, runs, "good", manifest("synthetic", "2026-10-02T08:15:00Z", 0.12, true), readFixture(t, "metrics_synthetic.json"))
	planted := strings.Replace(readFixture(t, "metrics_gold.json"), `"action": {`, `"action": {"ids": {"reply": ["PLANTED-METRIC-ID-55"]}, `, 1)
	makeRun(t, runs, "planted", manifest("gold_random", "2026-10-01T00:00:00Z", nil, false), planted)
	makeRun(t, runs, "bad", "{", nil)
	makeRun(t, runs, "none", nil, nil)
	srv := testServer(t, runsCfg(t, runs))
	r := get(t, srv, "/api/runs")
	want := `{
  "runs": [
    {
      "cost": {
        "cost_per_1000_usd": 0.6,
        "estimated_usd": 0.12,
        "prices_are_placeholders": true
      },
      "created_at": "2026-10-02T08:15:00Z",
      "has_metrics": true,
      "kind": "synthetic",
      "name": "good",
      "status": "ok"
    },
    {
      "cost": {
        "cost_per_1000_usd": 0.6,
        "estimated_usd": null,
        "prices_are_placeholders": false
      },
      "created_at": "2026-10-01T00:00:00Z",
      "has_metrics": false,
      "kind": "gold_random",
      "name": "planted",
      "status": "ok"
    },
    {
      "name": "bad",
      "status": "unreadable manifest"
    },
    {
      "name": "none",
      "status": "no manifest"
    }
  ]
}
`
	if r.status != 200 || r.body != want {
		t.Errorf("got %d\n%s", r.status, r.body)
	}
	for _, p := range []string{"/api/runs", "/api/overview", "/api/runs/good/metrics", "/api/runs/planted/metrics"} {
		b := get(t, srv, p).body
		if strings.Contains(b, runs) || strings.Contains(b, "manifest.json") {
			t.Errorf("%s leaks a path", p)
		}
	}
}

// decoded converts a response body the way report.ReadJSON does.
func decoded(t *testing.T, body string) (policy.Value, map[string]any) {
	t.Helper()
	p := writeFile(t, t.TempDir(), "body.json", body)
	v, err := report.ReadJSON(p)
	if err != nil {
		t.Fatal(err)
	}
	pv, err := policy.ParseJSON([]byte(body))
	if err != nil {
		t.Fatal(err)
	}
	return pv, v.(map[string]any)
}

// sameNumbers checks every number literal in got equals the literal at the same path in want.
func sameNumbers(t *testing.T, path string, got, want policy.Value) {
	t.Helper()
	if got.Kind != want.Kind {
		t.Errorf("%s: kind %v, want %v", path, got.Kind, want.Kind)
		return
	}
	switch got.Kind {
	case policy.Number:
		if got.Num != want.Num {
			t.Errorf("%s: %s, want %s", path, got.Num, want.Num)
		}
	case policy.Array:
		if len(got.Items) != len(want.Items) {
			t.Errorf("%s: length differs", path)
			return
		}
		for i := range got.Items {
			sameNumbers(t, fmt.Sprintf("%s[%d]", path, i), got.Items[i], want.Items[i])
		}
	case policy.Object:
		for _, k := range got.Keys {
			w, ok := want.Get(k)
			if !ok {
				t.Errorf("%s.%s: not in golden", path, k)
				continue
			}
			sameNumbers(t, path+"."+k, got.Vals[k], w)
		}
	}
}

func TestMetricsGoldenUnchanged(t *testing.T) {
	root := parity.RepoRoot(t)
	runs := t.TempDir()
	for _, g := range []struct{ run, golden, kind string }{
		{"syn", "run_synthetic/metrics.json", "synthetic"},
		{"eval", "run_eval/metrics.json", "gold_random"},
	} {
		golden, err := os.ReadFile(filepath.Join(root, "testdata", "golden", filepath.FromSlash(g.golden)))
		if err != nil {
			t.Fatal(err)
		}
		makeRun(t, runs, g.run, manifest(g.kind, "2026-10-02T08:15:00Z", 0.12, true), string(golden))
		srv := testServer(t, runsCfg(t, runs))
		r := get(t, srv, "/api/runs/"+g.run+"/metrics")
		if r.status != 200 {
			t.Fatalf("%s: %d", g.run, r.status)
		}
		pv, v := decoded(t, r.body)
		if v["status"] != "ok" || v["name"] != g.run {
			t.Fatalf("%s: %v %v", g.run, v["status"], v["name"])
		}
		out, err := route.PyJSONIndent(v["metrics"])
		if err != nil {
			t.Fatal(err)
		}
		parity.AssertGolden(t, g.golden, append(out, '\n'))
		gv, err := policy.ParseJSON(golden)
		if err != nil {
			t.Fatal(err)
		}
		mv, _ := pv.Get("metrics")
		sameNumbers(t, g.run, mv, gv)
	}
}

func TestDeeplyNested(t *testing.T) {
	for _, deep := range []string{strings.Repeat("[", 100000), strings.Repeat("[", 600) + strings.Repeat("]", 600)} {
		runs := t.TempDir()
		makeRun(t, runs, "deep-manifest", deep, nil)
		makeRun(t, runs, "deep-metrics", goodManifest(), deep)
		cfg := runsCfg(t, runs)
		cfg.Thresholds = writeFile(t, t.TempDir(), "th.json", deep)
		srv := testServer(t, cfg)
		r := get(t, srv, "/api/runs")
		if r.status != 200 || !strings.Contains(r.body, "\"name\": \"deep-manifest\",\n      \"status\": \"unreadable manifest\"") ||
			!strings.Contains(r.body, "\"has_metrics\": false") {
			t.Errorf("runs: %d %s", r.status, r.body)
		}
		r = get(t, srv, "/api/runs/deep-metrics/metrics")
		if r.status != 200 || !strings.Contains(r.body, "\"metrics\": null") {
			t.Errorf("metrics: %d %s", r.status, r.body)
		}
		r = get(t, srv, "/api/overview")
		if r.status != 200 || !strings.Contains(r.body, "\"monthly_cap_usd\": null") {
			t.Errorf("overview: %d %s", r.status, r.body)
		}
	}
}

// TestDeeplyNestedOtherwiseValid: a 600-deep value inside an otherwise good
// thresholds file is still refused, so the depth cap itself is what refuses it.
func TestDeeplyNestedOtherwiseValid(t *testing.T) {
	cfg := runsCfg(t, t.TempDir())
	makeRun(t, cfg.Runs, "r", goodManifest(), nil)
	body := `{"x": ` + strings.Repeat("[", 600) + strings.Repeat("]", 600) + `, "guardrails": {"api_spend_usd_per_month": {"max": 5}}}`
	cfg.Thresholds = writeFile(t, t.TempDir(), "th.json", body)
	srv := testServer(t, cfg)
	if r := get(t, srv, "/api/overview"); r.status != 200 || !strings.Contains(r.body, "\"monthly_cap_usd\": null") {
		t.Errorf("overview: %d %s", r.status, r.body)
	}
	shallow := `{"x": [[1]], "guardrails": {"api_spend_usd_per_month": {"max": 5}}}`
	cfg.Thresholds = writeFile(t, t.TempDir(), "th.json", shallow)
	srv = testServer(t, cfg)
	if r := get(t, srv, "/api/overview"); !strings.Contains(r.body, "\"monthly_cap_usd\": 5") {
		t.Errorf("control: %s", r.body)
	}
}

func TestNoGoldIds(t *testing.T) {
	runs := t.TempDir()
	makeRun(t, runs, "gold-ok", readFixture(t, "manifest_gold.json"), readFixture(t, "metrics_gold.json"))
	planted := strings.Replace(readFixture(t, "metrics_gold.json"), `"action": {`, `"action": {"ids": {"reply": ["PLANTED-METRIC-ID-55"]}, `, 1)
	d := makeRun(t, runs, "gold-planted", manifest("gold_random", "2026-10-04T00:00:00Z", 0.12, true), planted)
	writeFile(t, d, "trace.jsonl", readFixture(t, "trace_gold.jsonl"))
	writeFile(t, d, "notes.txt", "PLANTED-NOTE-ID-33\n")
	srv := testServer(t, runsCfg(t, runs))
	if r := get(t, srv, "/api/runs/gold-ok/metrics"); !strings.Contains(r.body, "\"status\": \"ok\"") {
		t.Fatalf("gold-ok not served: %s", r.body)
	}
	r := get(t, srv, "/api/runs/gold-planted/metrics")
	want := "{\n  \"metrics\": null,\n  \"name\": \"gold-planted\",\n  \"reason\": \"no usable metrics file\",\n  \"status\": \"ok\"\n}\n"
	if r.body != want {
		t.Errorf("planted: %s", r.body)
	}
	for _, p := range []string{"/api/runs", "/api/overview", "/api/runs/gold-ok/metrics", "/api/runs/gold-planted/metrics"} {
		b := get(t, srv, p).body
		for _, s := range []string{"PLANTED-THREAD-91827", "PLANTED-METRIC-ID-55", "PLANTED-NOTE-ID-33"} {
			if strings.Contains(b, s) {
				t.Errorf("%s leaks %s", p, s)
			}
		}
	}
}
