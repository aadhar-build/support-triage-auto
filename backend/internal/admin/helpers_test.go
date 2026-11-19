package admin

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
)

// fixturePath returns <repo>/tests/fixtures/admin/<name>.
func fixturePath(t *testing.T, name string) string {
	t.Helper()
	return filepath.Join(parity.RepoRoot(t), "tests", "fixtures", "admin", name)
}

func readFixture(t *testing.T, name string) string {
	t.Helper()
	b, err := os.ReadFile(fixturePath(t, name))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// manifest mirrors Python's _manifest: the synthetic fixture with kind,
// created_at, estimated_usd and prices_are_placeholders replaced.
func manifest(kind, createdAt string, usd any, placeholders bool) map[string]any {
	return map[string]any{
		"schema_version": "run-manifest/1",
		"kind":           kind,
		"created_at":     createdAt,
		"cost": map[string]any{
			"estimated_usd":           usd,
			"cost_per_1000_usd":       0.6,
			"prices_are_placeholders": placeholders,
		},
	}
}

func goodManifest() map[string]any {
	return manifest("synthetic", "2026-10-02T08:15:00Z", 0.12, true)
}

// makeRun writes one run folder. manifest and metrics are raw strings, values
// to marshal, or nil to omit.
func makeRun(t *testing.T, dir, name string, manifest, metrics any) string {
	t.Helper()
	d := filepath.Join(dir, name)
	if err := os.Mkdir(d, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, f := range []struct {
		name string
		v    any
	}{{"manifest.json", manifest}, {"metrics.json", metrics}} {
		if f.v == nil {
			continue
		}
		var b []byte
		if s, ok := f.v.(string); ok {
			b = []byte(s)
		} else {
			var err error
			if b, err = json.Marshal(f.v); err != nil {
				t.Fatal(err)
			}
		}
		if err := os.WriteFile(filepath.Join(d, f.name), b, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return d
}
