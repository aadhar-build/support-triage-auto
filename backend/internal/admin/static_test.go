package admin

import (
	"os"
	"path/filepath"
	"regexp"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
)

func TestStatic(t *testing.T) {
	cfg := runsCfg(t, t.TempDir())
	index := "<!doctype html><main id=\"app\"></main>\n"
	writeFile(t, cfg.Web, "index.html", index)
	if err := os.Mkdir(filepath.Join(cfg.Web, "assets"), 0o755); err != nil {
		t.Fatal(err)
	}
	js := "console.log(1);\n"
	writeFile(t, cfg.Web, "assets/app-abc.js", js)
	writeFile(t, cfg.Web, "assets/app-abc.css", "body{}\n")
	srv := testServer(t, cfg)

	for _, p := range []string{"/", "/runs", "/runs/x%20y"} {
		r := get(t, srv, p)
		if r.status != 200 || r.body != index || r.header.Get("Content-Type") != "text/html; charset=utf-8" {
			t.Errorf("%s: %d %q %q", p, r.status, r.body, r.header.Get("Content-Type"))
		}
	}
	r := get(t, srv, "/assets/app-abc.js")
	if r.status != 200 || r.body != js || r.header.Get("Content-Type") != "text/javascript; charset=utf-8" {
		t.Errorf("js: %d %q %q", r.status, r.body, r.header.Get("Content-Type"))
	}
	if r := get(t, srv, "/assets/app-abc.css"); r.status != 200 || r.header.Get("Content-Type") != "text/css; charset=utf-8" {
		t.Errorf("css: %d %q", r.status, r.header.Get("Content-Type"))
	}
	if r := raw(t, srv, "POST", "/runs", hostOf(srv), "HTTP/1.1"); r.status != 405 {
		t.Errorf("POST /runs: %d", r.status)
	}
	if r := raw(t, srv, "GET", "/assets/app-abc.js", str("evil.example"), "HTTP/1.1"); r.status != 403 {
		t.Errorf("foreign host asset: %d", r.status)
	}

	empty := runsCfg(t, t.TempDir())
	srv = testServer(t, empty)
	if r := get(t, srv, "/"); r.status != 404 || r.body != "not found\n" {
		t.Errorf("no index: %d %q", r.status, r.body)
	}
	if r := get(t, srv, "/api/runs"); r.status != 200 {
		t.Errorf("api without index: %d", r.status)
	}

	b, err := os.ReadFile(filepath.Join(parity.RepoRoot(t), "web", "index.html"))
	if err != nil {
		t.Fatal(err)
	}
	ext := regexp.MustCompile(`(?i)(src|href)\s*=\s*["']?\s*(https?:|//)`)
	if ext.Match(b) {
		t.Errorf("web/index.html loads an external URL")
	}
}
