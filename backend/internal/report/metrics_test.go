package report

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
)

func fixture(t *testing.T, rel string) string {
	t.Helper()
	return filepath.Join(parity.RepoRoot(t), filepath.FromSlash(rel))
}

func loadMap(t *testing.T, rel string) map[string]any {
	t.Helper()
	v, err := ReadJSON(fixture(t, rel))
	if err != nil {
		t.Fatalf("read %s: %v", rel, err)
	}
	return v.(map[string]any)
}

func block(m map[string]any, keys ...string) map[string]any {
	for _, k := range keys {
		m = m[k].(map[string]any)
	}
	return m
}

func TestCheckMetrics(t *testing.T) {
	for _, rel := range []string{
		"tests/fixtures/admin/metrics_synthetic.json",
		"tests/fixtures/admin/metrics_gold.json",
		"testdata/golden/run_synthetic/metrics.json",
		"testdata/golden/run_eval/metrics.json",
	} {
		if _, err := LoadMetrics(fixture(t, rel)); err != nil {
			t.Errorf("%s refused: %v", rel, err)
		}
	}

	const syn = "tests/fixtures/admin/metrics_synthetic.json"
	const gold = "tests/fixtures/admin/metrics_gold.json"
	cases := []struct {
		name, rel string
		edit      func(m map[string]any)
		want      string
	}{
		{"unknown key", syn, func(m map[string]any) { m["extra"] = 1 }, "not a metrics/1 file: unknown key extra"},
		{"missing key", syn, func(m map[string]any) { delete(m, "averaging"); delete(m, "n") }, "not a metrics/1 file: missing key n, averaging"},
		{"bad schema", syn, func(m map[string]any) { m["schema_version"] = "metrics/2" }, "schema_version must be metrics/1, got 'metrics/2'"},
		{"gold ids", gold, func(m map[string]any) { block(m, "action")["ids"] = []any{} }, "a gold metrics file must not carry ids"},
		{"float count", syn, func(m map[string]any) { m["n"] = 3.0 }, "n must be a non-negative int"},
		{"negative count", syn, func(m map[string]any) { m["directional_below"] = int64(-1) }, "directional_below must be a non-negative int"},
		{"bool value", syn, func(m map[string]any) { block(m, "escalation", "tpr")["value"] = true }, "escalation.tpr.value must be a number or null"},
		{"non-square grid", syn, func(m map[string]any) {
			cm := block(m, "action", "confusion_matrix")
			cm["counts"] = cm["counts"].([]any)[:3]
		}, "action counts must be a 4x4 list of non-negative ints"},
		{"unknown label", syn, func(m map[string]any) {
			cm := block(m, "intent", "confusion_matrix")
			cm["labels"].([]any)[0] = "weather"
		}, "intent label 'weather' is not a known intent"},
		{"per_class keys", syn, func(m map[string]any) { delete(block(m, "action", "per_class"), "ask") }, "action per_class must have exactly the labels as keys"},
		{"included outside", syn, func(m map[string]any) { block(m, "action", "macro")["included_classes"] = []any{"route", "nope"} }, "action.macro.included_classes must be a list of the block's labels"},
		{"synthetic ids", syn, func(m map[string]any) {
			ids := block(m, "action")["ids"].([]any)
			ids[0].([]any)[0] = []any{int64(1)}
		}, "action.ids must be a 4x4 list of lists of strings"},
		{"unknown tier", syn, func(m map[string]any) { block(m, "config")["prices"] = map[string]any{"huge": 1.0} }, "config.prices has unknown tier huge"},
	}
	for _, c := range cases {
		m := loadMap(t, c.rel)
		c.edit(m)
		_, err := CheckMetrics(m)
		if err == nil || err.Error() != c.want {
			t.Errorf("%s: got %v, want %q", c.name, err, c.want)
		}
	}

	// %r of a value read from a file keeps the file's key order (Fable tiebreak (b)).
	b, err := os.ReadFile(fixture(t, syn))
	if err != nil {
		t.Fatal(err)
	}
	text := strings.Replace(string(b), `"schema_version": "metrics/1"`, `"schema_version": {"b": 1, "a": [2.0, null, "x"]}`, 1)
	if text == string(b) {
		t.Fatal("fixture has no schema_version line to replace")
	}
	dir := t.TempDir()
	odd := filepath.Join(dir, "metrics.json")
	if err := os.WriteFile(odd, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err = LoadMetrics(odd)
	want := "schema_version must be metrics/1, got {'b': 1, 'a': [2.0, None, 'x']}"
	if err == nil || err.Error() != want {
		t.Errorf("ordered repr: got %v, want %q", err, want)
	}

	// Owner decision Q3: NaN, Infinity and unpaired surrogates are refused.
	for _, bad := range []string{"NaN", "Infinity", `"\ud800"`} {
		text := strings.Replace(string(b), `"schema_version": "metrics/1"`, `"schema_version": `+bad, 1)
		p := filepath.Join(dir, "q3.json")
		if err := os.WriteFile(p, []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
		if _, err := LoadMetrics(p); err == nil {
			t.Errorf("%s accepted", bad)
		}
	}
}
