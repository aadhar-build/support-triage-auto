package eval

import (
	"bufio"
	"bytes"
	"encoding/json"
	"os"
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
