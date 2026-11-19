package route

import (
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestExportedPyJSON pins the exported compact writer the spend log uses to
// literal bytes Python 3.12's json.dumps(rec, sort_keys=True) writes.
func TestExportedPyJSON(t *testing.T) {
	cases := []struct {
		name string
		in   map[string]any
		want string
	}{
		{"reserve", map[string]any{
			"event": "reserve", "id": "00000000000000000000000000000002", "at": "2026-10-31T23:59:00+00:00",
			"run_id": "run-1", "model": "claude-haiku-5-5", "split": "dev", "usd": 0.1,
		}, `{"at": "2026-10-31T23:59:00+00:00", "event": "reserve", "id": "00000000000000000000000000000002", "model": "claude-haiku-5-5", "run_id": "run-1", "split": "dev", "usd": 0.1}`},
		{"settle", map[string]any{
			"event": "settle", "id": "00000000000000000000000000000002", "at": "2026-10-31T23:59:00+00:00",
			"usd": 0.000125, "input_tokens": int64(1000), "output_tokens": int64(50),
			"cache_write_tokens": int64(0), "cache_read_tokens": int64(0),
		}, `{"at": "2026-10-31T23:59:00+00:00", "cache_read_tokens": 0, "cache_write_tokens": 0, "event": "settle", "id": "00000000000000000000000000000002", "input_tokens": 1000, "output_tokens": 50, "usd": 0.000125}`},
		{"small", map[string]any{"usd": 1e-05}, `{"usd": 1e-05}`},
		{"big", map[string]any{"usd": 1e+16}, `{"usd": 1e+16}`},
		{"zero", map[string]any{"usd": 0.0}, `{"usd": 0.0}`},
		{"non-ascii", map[string]any{"run_id": "é"}, "{\"run_id\": \"\\u00e9\"}"},
		{"astral", map[string]any{"run_id": "😀"}, "{\"run_id\": \"\\ud83d\\ude00\"}"},
	}
	for _, c := range cases {
		got, err := PyJSON(c.in)
		if err != nil || got != c.want {
			t.Errorf("%s: got %q, %v; want %q", c.name, got, err, c.want)
		}
	}
	if _, err := PyJSON(map[string]any{"usd": math.NaN()}); err == nil {
		t.Error("NaN: want an error")
	}
}

// TestExportedResolvePath: the exported resolver resolves a symlink before a
// following `..`, the same path CheckTracePath refuses.
func TestExportedResolvePath(t *testing.T) {
	repo, out := realDir(t), realDir(t)
	if err := os.MkdirAll(filepath.Join(repo, "a", "b"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(repo, "a", "b"), filepath.Join(out, "link")); err != nil {
		t.Fatal(err)
	}
	got, err := ResolvePath(out + "/link/../x")
	if err != nil || got != repo+"/a/x" {
		t.Errorf("ResolvePath: %q, %v; want %q", got, err, repo+"/a/x")
	}
	if err := CheckTracePath(repo, out+"/link/../x"); err == nil || !strings.Contains(err.Error(), got) {
		t.Errorf("CheckTracePath does not refuse the resolved path %q: %v", got, err)
	}
	if got, err := ResolvePath(repo); err != nil || got != repo {
		t.Errorf("ResolvePath(root): %q, %v", got, err)
	}
}
