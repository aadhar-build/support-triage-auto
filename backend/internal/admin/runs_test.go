package admin

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

type nameStatus struct{ name, status string }

func listed(t *testing.T, dir string) []nameStatus {
	t.Helper()
	runs, err := ListRuns(dir)
	if err != nil {
		t.Fatal(err)
	}
	out := make([]nameStatus, len(runs))
	for i, r := range runs {
		out[i] = nameStatus{r.Name, r.Status}
	}
	return out
}

func TestListRuns(t *testing.T) {
	t.Run("TestRunOrder", func(t *testing.T) {
		dir := t.TempDir()
		makeRun(t, dir, "a-run", manifest("synthetic", "2026-10-02T00:00:00Z", 0.12, true), nil)
		makeRun(t, dir, "b-run", manifest("synthetic", "2026-10-09T00:00:00Z", 0.12, true), nil)
		makeRun(t, dir, "c-run", manifest("synthetic", "2026-10-05T00:00:00Z", 0.12, true), nil)
		makeRun(t, dir, "0-metrics-only", nil, readFixture(t, "metrics_synthetic.json"))
		makeRun(t, dir, "1-broken", "{not json", nil)
		for i, name := range []string{"b-run", "c-run", "a-run"} {
			tm := time.Unix(1_600_000_000+int64(i)*86400, 0)
			if err := os.Chtimes(filepath.Join(dir, name), tm, tm); err != nil {
				t.Fatal(err)
			}
		}
		want := []nameStatus{{"b-run", "ok"}, {"c-run", "ok"}, {"a-run", "ok"},
			{"1-broken", "unreadable manifest"}, {"0-metrics-only", "no manifest"}}
		if got := listed(t, dir); !slices.Equal(got, want) {
			t.Errorf("got %v, want %v", got, want)
		}
	})

	t.Run("test_bad_manifests_are_unreadable", func(t *testing.T) {
		dir := t.TempDir()
		with := func(key string, v any) map[string]any {
			m := goodManifest()
			m[key] = v
			return m
		}
		missing := goodManifest()
		delete(missing, "cost")
		makeRun(t, dir, "badschema", with("schema_version", "run-manifest/9"), nil)
		makeRun(t, dir, "badkind", with("kind", "gold"), nil)
		makeRun(t, dir, "baddate", with("created_at", "2026-10-02 08:15:00"), nil)
		makeRun(t, dir, "missingkey", missing, nil)
		makeRun(t, dir, "notobject", "[1, 2]", nil)
		want := []nameStatus{{"baddate", "unreadable manifest"}, {"badkind", "unreadable manifest"},
			{"badschema", "unreadable manifest"}, {"missingkey", "unreadable manifest"},
			{"notobject", "unreadable manifest"}}
		if got := listed(t, dir); !slices.Equal(got, want) {
			t.Errorf("got %v, want %v", got, want)
		}
	})

	t.Run("edge cases", func(t *testing.T) {
		dir := t.TempDir()
		good := `{"schema_version": "run-manifest/1", "kind": "synthetic", "created_at": "2026-10-02T08:15:00Z",
 "cost": {"estimated_usd": 0.12, "cost_per_1000_usd": 0.6, "prices_are_placeholders": true}}`
		swap := func(old, repl string) string {
			s := strings.Replace(good, old, repl, 1)
			if s == good {
				t.Fatalf("no %q in manifest", old)
			}
			return s
		}
		makeRun(t, dir, "tie-b", manifest("synthetic", "2026-10-03T00:00:00.10Z", 0.1, true), nil)
		makeRun(t, dir, "tie-a", manifest("hard_case", "2026-10-03T00:00:00.1Z", 0.1, true), nil)
		makeRun(t, dir, "older", manifest("gold_random", "2026-10-03T00:00:00Z", nil, false), nil)
		makeRun(t, dir, "newest", swap("2026-10-02", "2026-10-04"), nil)
		if err := os.Symlink(filepath.Join(dir, "newest"), filepath.Join(dir, "zz-link")); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "plain-file"), []byte(good), 0o644); err != nil {
			t.Fatal(err)
		}
		d := makeRun(t, dir, "u-dir", nil, nil)
		if err := os.Mkdir(filepath.Join(d, "manifest.json"), 0o755); err != nil {
			t.Fatal(err)
		}
		makeRun(t, dir, "u-utf8", swap("synthetic", "synth\xffetic"), nil)
		makeRun(t, dir, "u-bom", "\ufeff"+good, nil)
		makeRun(t, dir, "u-trailing", good+" {}", nil)
		makeRun(t, dir, "u-nan", swap(`"estimated_usd": 0.12`, `"estimated_usd": NaN`), nil)
		makeRun(t, dir, "u-surrogate", swap(`"kind": "synthetic"`, `"kind": "\ud800"`), nil)
		makeRun(t, dir, "u-digits", swap("2026-10-02T08", "2026-10-\u06602T08"), nil)
		makeRun(t, dir, "u-true", swap(`"estimated_usd": 0.12`, `"estimated_usd": true`), nil)
		makeRun(t, dir, "u-neg", swap(`"estimated_usd": 0.12`, `"estimated_usd": -1`), nil)
		makeRun(t, dir, "u-bigint", swap(`"estimated_usd": 0.12`, `"estimated_usd": 99999999999999999999`), nil)
		makeRun(t, dir, "u-inf", swap(`"estimated_usd": 0.12`, `"estimated_usd": 1e400`), nil)
		makeRun(t, dir, "u-extra", swap(`"kind"`, `"note": "x", "kind"`), nil)
		makeRun(t, dir, "u-cost-type", swap(`"prices_are_placeholders": true`, `"prices_are_placeholders": 1`), nil)
		makeRun(t, dir, "empty", nil, nil)
		const u = "unreadable manifest"
		want := []nameStatus{{"newest", "ok"}, {"tie-a", "ok"}, {"tie-b", "ok"}, {"older", "ok"},
			{"u-bigint", u}, {"u-bom", u}, {"u-cost-type", u}, {"u-digits", u}, {"u-dir", u},
			{"u-extra", u}, {"u-inf", u}, {"u-nan", u}, {"u-neg", u}, {"u-surrogate", u},
			{"u-trailing", u}, {"u-true", u}, {"u-utf8", u}, {"empty", "no manifest"}}
		if got := listed(t, dir); !slices.Equal(got, want) {
			t.Errorf("got  %v\nwant %v", got, want)
		}
	})

	t.Run("ok run fields", func(t *testing.T) {
		dir := t.TempDir()
		makeRun(t, dir, "r", manifest("synthetic", "2026-10-02T08:15:00Z", nil, false), nil)
		runs, err := ListRuns(dir)
		if err != nil {
			t.Fatal(err)
		}
		r := runs[0]
		if r.Kind != "synthetic" || r.CreatedAt != "2026-10-02T08:15:00Z" || r.Cost["estimated_usd"] != nil ||
			r.Cost["cost_per_1000_usd"] != 0.6 || r.Cost["prices_are_placeholders"] != false || r.Dir != filepath.Join(dir, "r") {
			t.Errorf("got %+v", r)
		}
	})

	t.Run("missing runs dir", func(t *testing.T) {
		if _, err := ListRuns(filepath.Join(t.TempDir(), "absent")); err == nil {
			t.Error("no error for a missing runs dir")
		}
	})
}
