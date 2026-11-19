package eval

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"slices"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
)

// goldenRouted reads run_eval/trace.jsonl into routed threads, in file order.
// Only thread_id, final labels and tier names are kept.
func goldenRouted(t *testing.T) []route.RoutedThread {
	t.Helper()
	data, err := os.ReadFile(parity.GoldenPath(t, "run_eval/trace.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	var out []route.RoutedThread
	sc := bufio.NewScanner(bytes.NewReader(data))
	sc.Buffer(nil, 1<<20)
	for sc.Scan() {
		var rec struct {
			ThreadID uint64 `json:"thread_id"`
			Final    struct {
				Intent, Urgency, Action string
				Confidence              float64
			} `json:"final"`
			Tiers []struct {
				Tier string `json:"tier"`
			} `json:"tiers"`
		}
		if err := json.Unmarshal(sc.Bytes(), &rec); err != nil {
			t.Fatal(err)
		}
		r := route.RoutedThread{ThreadID: rec.ThreadID,
			Labels: route.Labels{Intent: rec.Final.Intent, Urgency: rec.Final.Urgency, Action: rec.Final.Action, Confidence: rec.Final.Confidence}}
		for _, tr := range rec.Tiers {
			r.Tiers = append(r.Tiers, tr.Tier)
		}
		out = append(out, r)
	}
	if err := sc.Err(); err != nil {
		t.Fatal(err)
	}
	return out
}

func placeholderPriceSlice() []TierPrice {
	return []TierPrice{{"cheap", route.PlaceholderPrices["cheap"]}, {"larger", route.PlaceholderPrices["larger"]}, {"agent", route.PlaceholderPrices["agent"]}}
}

// evalReport scores the golden run as run_eval does in fake mode.
func evalReport(t *testing.T) ([]route.RoutedThread, Gold, Report, string) {
	t.Helper()
	_, goldCSV, _ := parity.Inputs(t)
	g, err := LoadGold(goldCSV)
	if err != nil {
		t.Fatal(err)
	}
	routed := goldenRouted(t)
	// run_eval.py line 296: usage = {"larger_mode": args.larger, "larger_model": larger.model_id}
	u := DefaultLargerUsage()
	u.LargerMode, u.LargerModel = "fake", ptr("fake-larger")
	r, err := Score(routed, g, placeholderPriceSlice(), ScoreOptions{
		SampleSize: ptr[int64](200), Seed: ptr[int64](0), Split: ptr("dev"), Threshold: ptr(0.5), Usage: &u})
	if err != nil {
		t.Fatal(err)
	}
	return routed, g, r, goldCSV
}

func TestEvalReportGolden(t *testing.T) {
	_, _, r, _ := evalReport(t)
	t.Run("JSON", func(t *testing.T) {
		js, err := ReportToJSON(r)
		if err != nil {
			t.Fatal(err)
		}
		parity.AssertGolden(t, "run_eval/eval.json", js)
	})
	t.Run("MD", func(t *testing.T) {
		parity.AssertGolden(t, "run_eval/eval.md", []byte(ReportToMarkdown(r)))
	})
}

func fileRevision(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(data)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func TestSyntheticMetricsGolden(t *testing.T) {
	_, _, casesJSON := parity.Inputs(t)
	data, err := os.ReadFile(parity.GoldenPath(t, "run_synthetic/synthetic_report.json"))
	if err != nil {
		t.Fatal(err)
	}
	var report struct {
		Cases []SyntheticResult `json:"cases"`
	}
	if err := json.Unmarshal(data, &report); err != nil {
		t.Fatal(err)
	}
	cases, err := CasesFromSynthetic(report.Cases)
	if err != nil {
		t.Fatal(err)
	}
	// Copied from run_synthetic._metrics_text.
	m, err := Compute(cases, "synthetic", ComputeOptions{
		RunAt:  "2000-01-01T00:00:00Z",
		Cohort: map[string]any{"name": "synthetic", "split": nil, "gold_revision": fileRevision(t, casesJSON), "brand_population": "all"},
		Sample: map[string]any{"size": nil, "seed": nil},
		Config: map[string]any{"threshold": 0.5, "prices": nil},
	})
	if err != nil {
		t.Fatal(err)
	}
	js, err := ToJSON(m)
	if err != nil {
		t.Fatal(err)
	}
	parity.AssertGolden(t, "run_synthetic/metrics.json", js)
}

func TestEvalMetricsGolden(t *testing.T) {
	routed, g, r, goldCSV := evalReport(t)
	cases, err := CasesFromRouted(routed, g)
	if err != nil {
		t.Fatal(err)
	}
	// coverage and operations are built as in run_eval._metrics_text.
	shares := map[string]any{}
	for _, ts := range r.TierShares {
		num := 0
		for _, rt := range routed {
			if slices.Contains(rt.Tiers, ts.Tier) {
				num++
			}
		}
		shares[ts.Tier] = Proportion(num, r.ThreadsRouted)
	}
	prices := map[string]any{}
	for _, p := range placeholderPriceSlice() {
		prices[p.Tier] = p.Price
	}
	m, err := Compute(cases, "gold", ComputeOptions{
		RunAt:  "2000-01-01T00:00:00Z",
		Cohort: map[string]any{"name": "gold_random", "split": "dev", "gold_revision": fileRevision(t, goldCSV), "brand_population": "all"},
		Sample: map[string]any{"size": int64(200), "seed": int64(0)},
		Config: map[string]any{"threshold": 0.5, "prices": prices},
		Coverage: map[string]any{"threads_routed": r.ThreadsRouted, "threads_scored": r.ThreadsScored,
			"threads_without_gold": r.ThreadsWithoutGold, "gold_rows_unmatched": r.GoldRowsUnmatched},
		Operations: map[string]any{"tier_shares": shares, "cost_per_1000_usd": floatOrNil(r.CostPer1000Threads),
			"prices_are_placeholders": r.PricesArePlaceholders},
	})
	if err != nil {
		t.Fatal(err)
	}
	// Decoded cross-check, read from the golden at 1ebae68.
	action := m["action"].(map[string]any)["accuracy"].(map[string]any)
	counts := m["escalation"].(map[string]any)["counts"].(map[string]any)
	cov := m["coverage"].(map[string]any)
	if action["num"] != 191 || counts["fn"] != 2 || counts["fp"] != 3 || cov["gold_rows_unmatched"] != 1800 {
		t.Errorf("action.accuracy.num=%v escalation fn=%v fp=%v gold_rows_unmatched=%v, want 191, 2, 3, 1800",
			action["num"], counts["fn"], counts["fp"], cov["gold_rows_unmatched"])
	}
	js, err := ToJSON(m)
	if err != nil {
		t.Fatal(err)
	}
	parity.AssertGolden(t, "run_eval/metrics.json", js)
}
