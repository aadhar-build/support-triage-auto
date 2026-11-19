package admin

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/aadhar-build/support-triage-auto/backend/internal/report"
	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
)

var overviewNow = time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC)

func overview(t *testing.T, runs, thresholds string) map[string]any {
	t.Helper()
	o, err := Overview(runs, thresholds, overviewNow)
	if err != nil {
		t.Fatal(err)
	}
	return o
}

func jsonOf(t *testing.T, v any) string {
	t.Helper()
	b, err := route.PyJSONIndent(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func writeFile(t *testing.T, dir, name, body string) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestOverview(t *testing.T) {
	th := fixturePath(t, "thresholds.json")

	t.Run("empty states", func(t *testing.T) {
		got := jsonOf(t, overview(t, t.TempDir(), th))
		want := "{\n  \"latest\": null,\n  \"ok_count\": 0,\n  \"run_count\": 0,\n  \"spend\": null\n}"
		if got != want {
			t.Errorf("no folders: got %s", got)
		}
		dir := t.TempDir()
		makeRun(t, dir, "bad", "{", nil)
		makeRun(t, dir, "none", nil, nil)
		got = jsonOf(t, overview(t, dir, th))
		want = "{\n  \"latest\": null,\n  \"ok_count\": 0,\n  \"run_count\": 2,\n  \"spend\": null\n}"
		if got != want {
			t.Errorf("only bad manifests: got %s", got)
		}
	})

	spendDir := func(t *testing.T) string {
		dir := t.TempDir()
		makeRun(t, dir, "r1", manifest("synthetic", "2026-10-01T00:00:01Z", 0.10, true), nil)
		makeRun(t, dir, "r2", manifest("synthetic", "2026-10-01T00:00:02Z", 0.25, true), nil)
		makeRun(t, dir, "r0", manifest("synthetic", "2026-09-15T12:00:00Z", 1.00, true), nil)
		return dir
	}

	t.Run("TestSpend", func(t *testing.T) {
		o := overview(t, spendDir(t), th)
		latest := o["latest"].(map[string]any)
		if latest["name"] != "r2" || latest["kind"] != "synthetic" || latest["created_at"] != "2026-10-01T00:00:02Z" || latest["headlines"] != nil {
			t.Errorf("latest: %v", latest)
		}
		got := jsonOf(t, o["spend"])
		want := `{
  "month": "2026-10",
  "month_no_cost": 0,
  "month_runs": 2,
  "month_total_usd": 0.35,
  "monthly_cap_usd": 123.0,
  "placeholder_prices": true,
  "this_run_usd": 0.25
}`
		if got != want {
			t.Errorf("spend: got %s", got)
		}
		if o["run_count"] != 3 || o["ok_count"] != 3 {
			t.Errorf("counts: %v %v", o["run_count"], o["ok_count"])
		}
	})

	t.Run("null cost", func(t *testing.T) {
		dir := t.TempDir()
		makeRun(t, dir, "r", manifest("synthetic", "2026-10-05T00:00:00Z", nil, false), nil)
		spend := overview(t, dir, th)["spend"].(map[string]any)
		want := `{
  "month": "2026-10",
  "month_no_cost": 1,
  "month_runs": 1,
  "month_total_usd": 0.0,
  "monthly_cap_usd": 123.0,
  "placeholder_prices": false,
  "this_run_usd": null
}`
		if got := jsonOf(t, spend); got != want {
			t.Errorf("got %s", got)
		}
	})

	t.Run("monthly cap", func(t *testing.T) {
		dir := spendDir(t)
		tdir := t.TempDir()
		fx := readFixture(t, "thresholds.json")
		capOf := func(path string) any {
			return overview(t, dir, path)["spend"].(map[string]any)["monthly_cap_usd"]
		}
		swap := func(repl string) string {
			s := strings.Replace(fx, `"api_spend_usd_per_month": {"max": 123.0}`, repl, 1)
			if s == fx {
				t.Fatal("fixture has no cap")
			}
			return s
		}
		cases := map[string]string{
			"no cap":     swap(`"api_spend_usd_per_month": {"min": 1}`),
			"NaN":        swap(`"api_spend_usd_per_month": {"max": NaN}`),
			"Infinity":   swap(`"api_spend_usd_per_month": {"max": Infinity}`),
			"1e400":      swap(`"api_spend_usd_per_month": {"max": 1e400}`),
			"bool":       swap(`"api_spend_usd_per_month": {"max": true}`),
			"string":     swap(`"api_spend_usd_per_month": {"max": "5"}`),
			"not object": "[1]",
			"deep":       strings.Repeat("[", 600) + strings.Repeat("]", 600),
			"guard list": `{"guardrails": [1]}`,
			"int cap ok": `{"guardrails": {"api_spend_usd_per_month": {"max": 7}}}`,
		}
		for name, body := range cases {
			got := capOf(writeFile(t, tdir, "th.json", body))
			if name == "int cap ok" {
				if got != int64(7) {
					t.Errorf("%s: got %v", name, got)
				}
				continue
			}
			if got != nil {
				t.Errorf("%s: got %v, want null", name, got)
			}
		}
		if got := capOf(filepath.Join(tdir, "absent.json")); got != nil {
			t.Errorf("missing file: got %v", got)
		}
	})

	t.Run("headlines", func(t *testing.T) {
		dir := t.TempDir()
		makeRun(t, dir, "m", goodManifest(), readFixture(t, "metrics_synthetic.json"))
		rows := overview(t, dir, th)["latest"].(map[string]any)["headlines"].([]any)
		labels := []string{"Escalation F2", "Escalation TPR", "Escalation TNR", "Escalation precision",
			"Escalation F1", "Missed escalation rate", "False escalation rate", "Action accuracy", "Intent accuracy"}
		paths := [][2]string{{"escalation", "f2"}, {"escalation", "tpr"}, {"escalation", "tnr"}, {"escalation", "precision"},
			{"escalation", "f1"}, {"escalation", "fnr"}, {"escalation", "fpr"}, {"action", "accuracy"}, {"intent", "accuracy"}}
		raw, err := report.ReadJSON(fixturePath(t, "metrics_synthetic.json"))
		if err != nil {
			t.Fatal(err)
		}
		fx := raw.(map[string]any)
		if len(rows) != 9 {
			t.Fatalf("got %d rows", len(rows))
		}
		for i, r := range rows {
			row := r.(map[string]any)
			if row["label"] != labels[i] {
				t.Errorf("row %d label %v, want %s", i, row["label"], labels[i])
			}
			src := fx[paths[i][0]].(map[string]any)[paths[i][1]].(map[string]any)
			for _, k := range []string{"value", "n", "ci", "directional"} {
				if a, b := jsonOf(t, row[k]), jsonOf(t, src[k]); a != b {
					t.Errorf("row %d %s: got %s, want %s", i, k, a, b)
				}
			}
			if len(row) != 5 {
				t.Errorf("row %d has keys %v", i, row)
			}
		}
	})
}
